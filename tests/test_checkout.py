"""Every round starts from a clean checkout of the board's main (regression: the first pilot).

A target lane parked on missing references with a `sources.toml` it had written, untracked, in the
worker's board checkout; `board references` then committed the board's own `sources.toml` to main,
and every later round failed at `git checkout --detach` ("untracked working tree files would be
overwritten"). The real-git tests below replay that against local repositories standing in for
GitHub."""

import shutil
import subprocess

import pytest

from tauceti_roadmap_worker import config, gitops
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import Context, lane_for
from tauceti_roadmap_worker.lanes.target import TargetLane
from tauceti_roadmap_worker.paths import HERE

from .test_lane_dry_run import FakeBoard

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
NAME = "QSeries"
PKG = f"packages/{NAME}"


def test_sync_checkout_forces_the_remote_copy(tmp_path, monkeypatch):
    calls = []

    def fake_git(cwd, *args, check=True, timeout=900):
        calls.append(args)
        return "abc\n"
    monkeypatch.setattr(gitops, "git", fake_git)
    (tmp_path / "board" / ".git").mkdir(parents=True)
    assert gitops.sync_checkout("o/board", tmp_path / "board", "main", scrub=(PKG,)) == "abc"
    assert calls == [("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"),
                     ("checkout", "-q", "--force", "--detach", "origin/main"),
                     ("reset", "-q", "--hard", "origin/main"),
                     ("clean", "-qfd"),
                     ("clean", "-qfdx", "--", PKG),
                     ("rev-parse", "HEAD")]


# --- against real repositories ---------------------------------------------------------------------

def run(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=cwd, check=True, capture_output=True, text=True)


def origin(path, files: dict[str, str]):
    path.mkdir(parents=True)
    run(path, "init", "-q", "-b", "main")
    commit(path, files, "initial")
    return path


def commit(repo, files: dict[str, str], message: str):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", message)


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A worker context whose board and content repositories are local repositories."""
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("TAUCETI_ROADMAP_LAKE_PACKAGES", raising=False)
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")
    board = origin(tmp_path / "origin-board", {f"{PKG}/target.md": "Verdict: design\n", ".gitignore": "*.log\n"})
    content = origin(tmp_path / "origin-content", {"README.md": "roadmaps\n"})
    where = {cfg.repositories.packages_repo(): board, cfg.repositories.content: content}
    monkeypatch.setattr(gitops, "url", lambda repo: where[repo].as_uri())
    ctx = Context(cfg=cfg, gh=None, board=FakeBoard(), claims=None, worker_id="w1", operator="alice",
                  agent="codex", model="m")
    return ctx, board


def left_by_a_parked_round(ws):
    """What the agent of the parked round wrote and nobody submitted."""
    pkg = ws.board / PKG
    (pkg / "sources.toml").write_text("# the agent's copy\n")
    (pkg / "target.md").write_text("Verdict: design\nedited by the agent\n")
    (pkg / "notes.log").write_text("ignored, but inside the package folder\n")
    (ws.board / "elsewhere.log").write_text("ignored, outside the package folder\n")


@needs_git
def test_the_board_copy_wins_over_what_a_parked_round_left(live):
    ctx, board = live
    job = new_job(f"TARGET-{NAME}")
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    left_by_a_parked_round(ws)
    # `board references --yes` records the maintainer's waivers on the board's main.
    commit(board, {f"{PKG}/sources.toml": "# the board's copy: waivers\n"}, "Sources: answers")

    ws = lane.prepare(job, ctx)

    pkg = ws.board / PKG
    assert (pkg / "sources.toml").read_text() == "# the board's copy: waivers\n"
    assert (pkg / "target.md").read_text() == "Verdict: design\n"
    assert not (pkg / "notes.log").exists() and (ws.board / "elsewhere.log").exists()
    assert gitops.current_branch(ws.board) == "w1/TARGET-QSeries" and gitops.changed_files(ws.board) == []
    kept = ws.job_dir / "unsubmitted" / PKG
    assert (kept / "sources.toml").read_text() == "# the agent's copy\n"
    assert (kept / "target.md").read_text().endswith("edited by the agent\n")


@needs_git
def test_a_lane_that_resumes_a_checkpoint_keeps_its_own_unsubmitted_work(live):
    class Resuming(TargetLane):
        resumes_checkpoint = True
    ctx, board = live
    job, other = new_job(f"TARGET-{NAME}"), new_job(f"DESIGN-{NAME}")
    ws = Resuming().prepare(job, ctx)
    left_by_a_parked_round(ws)
    commit(board, {f"{PKG}/sources.toml": "# the board's copy: waivers\n"}, "Sources: answers")

    ws = Resuming().prepare(job, ctx)
    assert (ws.board / PKG / "sources.toml").read_text() == "# the agent's copy\n"
    assert (ws.board / PKG / "target.md").read_text().endswith("edited by the agent\n")

    # Another job on the same roadmap never inherits it: the board's copy wins there.
    ws = Resuming().prepare(other, ctx)
    assert (ws.board / PKG / "sources.toml").read_text() == "# the board's copy: waivers\n"
    assert (ws.board / PKG / "target.md").read_text() == "Verdict: design\n"
