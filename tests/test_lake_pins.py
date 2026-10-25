"""A checkout builds with the Lake packages of the pin it has now, never another pin's (design review
R09, 2026-10-24: a reused content checkout kept the link and the `.ready` of its earlier pin after the
manifest moved, and a pull request worktree with another manifest shared main's packages)."""

import os
import subprocess
from types import SimpleNamespace

from tauceti_roadmap_worker import openroadmaps
from tauceti_roadmap_worker.lanes import base, lake, pr

from .fakes import example_config
from .test_checkout import commit, needs_git, origin, run

LAKE = "/usr/bin/lake"
PIN_A = {"lake-manifest.json": '{"packages": [{"name": "mathlib", "rev": "aaaa"}]}\n',
         "lean-toolchain": "leanprover/lean4:v4.30.0\n"}
PIN_B = {"lake-manifest.json": '{"packages": [{"name": "mathlib", "rev": "bbbb"}]}\n',
         "lean-toolchain": "leanprover/lean4:v4.31.0\n"}


def pinned(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (path / name).write_text(text)
    return path


def fetched(state, checkout):
    """The shared packages of the checkout's pin, filled and marked ready for that pin."""
    shared = lake.shared_packages(state, checkout)
    (shared / "mathlib").mkdir(parents=True, exist_ok=True)
    lake.mark_ready(shared, lake.lake_pin(checkout))
    return shared


def counting_fetch():
    calls = []

    def run_(cmd, **kw):
        calls.append(kw["cwd"])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return calls, run_


def target(link):
    return os.path.realpath(link)


def test_a_reused_checkout_follows_its_pin_from_a_to_b_and_back(tmp_path):
    state, content = tmp_path / "state", pinned(tmp_path / "content", PIN_A)
    shared_a = fetched(state, content)
    link = content / ".lake" / "packages"
    setup = lake.prepare_lake(content, state, fetch=False, which=lambda n: LAKE)
    assert setup.ready and setup.packages == shared_a and target(link) == str(shared_a.resolve())

    pinned(content, PIN_B)
    calls, fetch = counting_fetch()
    moved = lake.prepare_lake(content, state, fetch=False, which=lambda n: LAKE)
    assert moved.packages == lake.shared_packages(state, content) != shared_a
    assert not moved.ready and "not fetched yet" in moved.reason
    assert link.is_symlink() and target(link) == str(moved.packages.resolve())
    # Replacing the link unlinked it: nothing was removed through it from A's packages.
    assert (shared_a / "mathlib").is_dir() and lake.ready_pin(shared_a) == shared_a.parent.name
    fetched_b = lake.prepare_lake(content, state, run=fetch, free=lambda p: 500.0, which=lambda n: LAKE)
    assert fetched_b.ready and fetched_b.note == "shared Lake packages fetched" and calls == [content]

    pinned(content, PIN_A)
    back = lake.prepare_lake(content, state, run=fetch, free=lambda p: 500.0, which=lambda n: LAKE)
    assert back.ready and back.packages == shared_a and len(calls) == 1          # A is still ready: no fetch
    assert target(link) == str(shared_a.resolve())


def test_the_readiness_of_one_pin_is_refused_for_another(tmp_path):
    state, content = tmp_path / "state", pinned(tmp_path / "content", PIN_A)
    own = content / ".lake" / "packages"           # the checkout's own build, marked ready for A
    (own / "mathlib").mkdir(parents=True)
    lake.mark_ready(own, lake.lake_pin(content))
    assert lake.prepare_lake(content, state, fetch=False, which=lambda n: LAKE).ready

    pinned(content, PIN_B)
    refused = lake.prepare_lake(content, state, fetch=False, which=lambda n: LAKE)
    assert refused.packages == own.resolve() and not refused.ready and "not fetched yet" in refused.reason
    calls, fetch = counting_fetch()
    assert lake.ensure_lake_cache(content, own, fetch) == "shared Lake packages fetched" and calls == [content]
    assert lake.ready_pin(own) == lake.lake_pin(content) and (own / "mathlib").is_dir() and not own.is_symlink()
    # A `.ready` that names no pin (written before readiness held one) is fetched again once.
    (own.parent / ".ready").write_text("2026-10-21T09:52:00Z\n")
    assert lake.ready_pin(own) != lake.lake_pin(content)
    assert lake.ensure_lake_cache(content, own, fetch) == "shared Lake packages fetched" and len(calls) == 2


def test_worktrees_share_the_packages_of_their_own_pin_only(tmp_path):
    state, content = tmp_path / "state", pinned(tmp_path / "content", PIN_A)
    same, other = pinned(tmp_path / "pr" / "Alpha", PIN_A), pinned(tmp_path / "pr" / "Beta", PIN_B)
    shared_a = fetched(state, content)
    lake.link_checkout_packages(content, state)
    ok = lake.prepare_lake(same, state, content=content, fetch=False, which=lambda n: LAKE)
    assert ok.ready and ok.packages == shared_a                                 # matching pins share
    apart = lake.prepare_lake(other, state, content=content, fetch=False, which=lambda n: LAKE)
    assert apart.packages == lake.shared_packages(state, other) != shared_a and not apart.ready
    assert target(other / ".lake" / "packages") == str(apart.packages.resolve())
    # The content checkout's own build is shared by a worktree of its pin, never by another pin's.
    (content / ".lake" / "packages").unlink()
    (content / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    assert lake.link_lake_packages(content, same, state) == (content / ".lake" / "packages").resolve()
    assert lake.link_lake_packages(content, other, state) == lake.shared_packages(state, other)


def test_the_operators_packages_are_used_for_their_pin_and_refused_for_another(tmp_path, monkeypatch):
    state, content = tmp_path / "state", pinned(tmp_path / "content", PIN_A)
    mine = pinned(tmp_path / "TauCetiRoadmap", PIN_A)
    tree = mine / ".lake" / "packages"
    (tree / "mathlib").mkdir(parents=True)
    monkeypatch.setenv(lake.EXTERNAL, str(tree))
    link = content / ".lake" / "packages"
    assert lake.wire_packages(content, state) == (tree.resolve(), "")
    assert target(link) == str(tree.resolve())

    pinned(mine, PIN_B)                             # the operator's checkout moved on: its packages follow it
    used, why = lake.wire_packages(content, state)
    assert used == lake.shared_packages(state, content) and target(link) == str(used.resolve())
    assert why.startswith(f"{lake.EXTERNAL}={tree} not used: it was built for {mine}, whose manifest, toolchain or "
                          "lakefile differ")
    assert (tree / "mathlib").is_dir()                                         # unlinked, never emptied
    assert lake.prepare_lake(content, state, fetch=False, which=lambda n: LAKE).warning == why
    # Lean names are resolved against the checkout's own pin, never the refused packages.
    assert openroadmaps.packages_dir(content) is None
    fetched(state, content)
    assert openroadmaps.packages_dir(content) == link

    loose = tmp_path / "packages"
    (loose / "mathlib").mkdir(parents=True)
    monkeypatch.setenv(lake.EXTERNAL, str(loose))
    assert "cannot be verified" in lake.external_packages(content)[1]
    monkeypatch.setenv(lake.EXTERNAL, str(tmp_path / "missing"))
    assert "is not a directory" in lake.external_packages(content)[1]
    monkeypatch.delenv(lake.EXTERNAL)
    assert lake.external_packages(content) == (None, "")


def worker(tmp_path):
    return SimpleNamespace(cfg=example_config(), wdir=tmp_path / "w1", state=tmp_path / "state", dry_run=False)


def workspace(tmp_path, content):
    return base.Workspace(job_dir=tmp_path, scratch=tmp_path, board=tmp_path / "board", content=content,
                          workdir=tmp_path, report=tmp_path / "r.md", branch="b")


@needs_git
def test_a_pull_request_branch_with_another_pin_gets_its_own_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(lake, "lake_on_path", lambda: LAKE)
    remote = origin(tmp_path / "origin", PIN_A)
    run(remote, "checkout", "-q", "-b", "bump")
    commit(remote, PIN_B, "bump the pins")
    run(remote, "update-ref", "refs/pull/7/head", "bump")
    run(remote, "checkout", "-q", "main")
    content = tmp_path / "w1" / "content"
    run(tmp_path, "clone", "-q", str(remote), str(content))
    ctx = worker(tmp_path)
    shared_a = fetched(ctx.state, content)

    ws = workspace(tmp_path, content)
    wt = pr.fresh_worktree(ctx, content, "Alpha", "port", ws)
    assert ws.extra["lake"].ready and ws.extra["lake"].packages == shared_a     # main's pin, shared

    wt, _sha = pr.pr_branch_worktree(ctx, content, "Alpha", 7, "roadmap/alpha", ws)
    assert (wt / "lean-toolchain").read_text() == PIN_B["lean-toolchain"]
    assert ws.extra["lake"].packages == lake.shared_packages(ctx.state, wt) != shared_a
    assert ws.extra["lake"].reason == "lake exe cache get exited 1"             # conftest: the fetch is `false`
    assert target(wt / ".lake" / "packages") == str(ws.extra["lake"].packages.resolve())
    assert lake.ready_pin(shared_a) == lake.lake_pin(content)                  # main's readiness untouched


@needs_git
def test_a_kept_commit_restored_onto_a_worktree_is_rewired_for_its_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(lake, "lake_on_path", lambda: LAKE)
    content = origin(tmp_path / "w1" / "content", PIN_A)
    commit(content, PIN_B, "the kept run's commit, on the pins of its day")
    kept = subprocess.run(["git", "-C", str(content), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    run(content, "checkout", "-q", "--detach", "HEAD~1")
    ctx = worker(tmp_path)
    shared_a = fetched(ctx.state, content)
    ws = workspace(tmp_path, content)
    wt = pr.fresh_worktree(ctx, content, "Alpha", "port", ws)
    assert ws.extra["lake"].packages == shared_a
    ws.workdir = wt
    base.restore_pending(ctx, SimpleNamespace(id="PORT-Alpha"), ws, {"sha": kept})
    assert ws.extra["lake"].packages == lake.shared_packages(ctx.state, wt) != shared_a
    assert target(wt / ".lake" / "packages") == str(ws.extra["lake"].packages.resolve())
