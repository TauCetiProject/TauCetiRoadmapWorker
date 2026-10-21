"""A worker never fills the disk with Lake packages (regression: the root disk filled at 08:55Z, every
worker crashed in record_failure with ENOSPC; each pr/<Name> and port/<Name> worktree had its own
16 GB `.lake`, six of them for two finished roadmaps)."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import worker
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import base, lane_for, pr

from .fakes import FakeBoard, example_config
from .test_checkout import needs_git


def checkout(path, manifest="{}"):
    path.mkdir(parents=True)
    (path / "lake-manifest.json").write_text(manifest)
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.30.0\n")
    return path


def test_worktrees_of_one_pin_share_one_package_directory(tmp_path):
    state, content = tmp_path / "state", tmp_path / "content"
    content.mkdir()
    a, b = checkout(tmp_path / "pr" / "Alpha"), checkout(tmp_path / "port" / "Beta")
    other = checkout(tmp_path / "pr" / "Gamma", manifest='{"packages": [{"rev": "new"}]}')
    sa, sb, sg = (pr.link_lake_packages(content, w, state) for w in (a, b, other))
    assert sa == sb == state / "lake" / pr.lake_pin(a) / "packages" and sg != sa      # a new pin: its own copy
    assert (a / ".lake" / "packages").is_symlink() and os.path.realpath(a / ".lake" / "packages") == str(sa.resolve())
    # A copy left by an earlier build is replaced by the link.
    (b / ".lake" / "packages").unlink()
    (b / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    pr.link_lake_packages(content, b, state)
    assert (b / ".lake" / "packages").is_symlink()
    # The content checkout's own packages (TAUCETI_ROADMAP_LAKE_PACKAGES) still win.
    (content / ".lake" / "packages").mkdir(parents=True)
    assert pr.link_lake_packages(content, a, state) == content / ".lake" / "packages"


def test_the_cache_is_fetched_once_per_pin(tmp_path):
    state, content = tmp_path / "state", tmp_path / "content"
    content.mkdir()
    wt = checkout(tmp_path / "pr" / "Alpha")
    shared = pr.link_lake_packages(content, wt, state)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    assert pr.ensure_lake_cache(wt, shared, run) == "shared Lake packages fetched"
    assert pr.ensure_lake_cache(wt, shared, run) == "shared Lake packages ready" and len(calls) == 1
    failing = pr.link_lake_packages(content, checkout(tmp_path / "pr" / "New", manifest='{"x": 1}'), state)
    assert "exited 1" in pr.ensure_lake_cache(wt, failing, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert not (failing.parent / ".ready").exists()                            # tried again next time


@needs_git
def test_finished_roadmaps_lose_their_worktrees(tmp_path):
    content = tmp_path / "w1" / "content"
    subprocess.run(["git", "init", "-q", str(content)], check=True)
    subprocess.run(["git", "-C", str(content), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q",
                    "--allow-empty", "-m", "base"], check=True)
    state = tmp_path / "state"
    for kind, name in (("pr", "Done"), ("port", "Done"), ("pr", "Open")):
        wt = tmp_path / "w1" / kind / name
        subprocess.run(["git", "-C", str(content), "worktree", "add", "-q", "--detach", str(wt), "HEAD"], check=True)
        pr.link_lake_packages(content, wt, state)
    shared = state / "lake"
    (next(shared.iterdir()) / "packages" / "mathlib.olean").write_text("x")
    removed = pr.sweep_worktrees(tmp_path / "w1", content, {"Open"})
    assert removed == ["pr/Done", "port/Done"] and (tmp_path / "w1" / "pr" / "Open").is_dir()
    assert (next(shared.iterdir()) / "packages" / "mathlib.olean").is_file()   # the shared packages stay


def test_build_lanes_wait_for_disk_space(monkeypatch):
    monkeypatch.setattr(base, "free_gb", lambda path: 12.0)
    ctx = SimpleNamespace(cfg=example_config(), claims=None, agent="codex", model="m", operator="alice",
                          state=__import__("pathlib").Path("/tmp"))
    for job_id in ("PORT-Foo", "RT-Foo~1a", "FIX-RT-Foo~1", "GATE-Foo~1"):
        job = new_job(job_id)
        assert "only 12 GB free" in lane_for(job).eligible(job, ctx), job_id
    review = new_job("REV-PKG-Foo")
    assert lane_for(review).eligible(review, ctx) == ""                          # no build: no threshold


def test_a_full_disk_never_crashes_the_worker(tmp_path, monkeypatch):
    ctx = SimpleNamespace(wdir=tmp_path / "w1")

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(type(tmp_path), "write_text", full)
    assert worker.record_failure(ctx, "RT-Foo~1a") == 1                          # no exception
    monkeypatch.undo()

    def work_once(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(worker, "work_once", work_once)
    sleeps, lines = [], []

    def sleep(s):
        sleeps.append(s)
        raise KeyboardInterrupt
    cfg = example_config()
    with pytest.raises(KeyboardInterrupt):
        worker.work_loop(SimpleNamespace(cfg=cfg, board=FakeBoard()), None, list, log=lines.append, sleep=sleep)
    assert "No space left on device" in lines[0] and sleeps == [cfg.worker.idle_seconds]


def test_doctor_warns_below_forty_gigabytes(monkeypatch, tmp_path):
    from tauceti_roadmap_worker import cli
    monkeypatch.setattr(base, "free_gb", lambda path: 31.0)
    level, what, text = cli.free_space_row(tmp_path)
    assert (level, what) == (cli.WARN, "free space") and text.startswith("31 GB free on the state directory's disk (below 40")
    monkeypatch.setattr(base, "free_gb", lambda path: 128.0)
    assert cli.free_space_row(tmp_path)[0] == cli.OK
