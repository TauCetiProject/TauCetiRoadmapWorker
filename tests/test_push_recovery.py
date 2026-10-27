"""A push never throws away a run (regression: FIX-RT-ArithmeticDynamics~1 committed 31 repairs with
every gate passing, then `git push --force-with-lease` failed on stale remote-tracking information,
the job was released, and the next attempt ran the whole agent again).

- the lease names the head the work was built on and is checked against the remote itself;
- a branch that moved blocks the job for a human instead of being overwritten;
- a push that keeps failing keeps the run, and the next attempt pushes it without the agent; so
  does a round whose lease was lost;
- every checkout carries the worker's git identity, so an agent's own `git commit` works."""

import dataclasses
import os
import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import agents, claims, gitops
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import LaneResult, lane_for
from tauceti_roadmap_worker.lanes import base as lane_base

from .test_checkout import needs_git
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """A 'fork' (bare) holding branch roadmap/foo at a base commit, and a work clone with a fix on top,
    cloned in a way that leaves no remote-tracking ref for the fork's branch."""
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "init", "-q", "--bare", str(fork)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("base\n")
    sh(seed, "add", "-A")
    sh(seed, "commit", "-q", "-m", "base")
    sh(seed, "push", "-q", str(fork), "HEAD:refs/heads/roadmap/foo")
    base = sh(seed, "rev-parse", "HEAD")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(seed), str(work)], check=True)
    (work / "README.md").write_text("repaired\n")
    sh(work, "commit", "-q", "-am", "repairs")
    monkeypatch.setattr(gitops, "url", lambda repo: str(fork) if repo == "alice/TauCetiRoadmap" else str(tmp_path / "nowhere"))
    return fork, seed, work, base


@needs_git
def test_the_lease_is_the_head_the_work_was_built_on(repos):
    fork, _seed, work, base = repos
    gitops.push(work, "alice/TauCetiRoadmap", "roadmap/foo", expected=base)     # no tracking ref needed
    assert sh(fork, "rev-parse", "roadmap/foo") == sh(work, "rev-parse", "HEAD")


@needs_git
def test_a_branch_that_moved_is_never_overwritten(repos):
    fork, seed, work, base = repos
    (seed / "README.md").write_text("someone else\n")
    sh(seed, "commit", "-q", "-am", "elsewhere")
    sh(seed, "push", "-q", str(fork), "HEAD:refs/heads/roadmap/foo")
    moved = sh(seed, "rev-parse", "HEAD")
    with pytest.raises(gitops.BranchMoved) as info:
        gitops.push(work, "alice/TauCetiRoadmap", "roadmap/foo", expected=base)
    assert f"moved from {base[:7]} to {moved[:7]} while the fix ran" in str(info.value)
    assert sh(fork, "rev-parse", "roadmap/foo") == moved


@needs_git
def test_a_failed_push_is_a_push_error(repos):
    _fork, _seed, work, _base = repos
    with pytest.raises(gitops.PushError):
        gitops.push(work, "nobody/TauCetiRoadmap", "roadmap/foo")


@needs_git
def test_every_checkout_has_the_workers_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))                 # no global identity
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    gitops.set_identity(repo, "alice")
    (repo / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "an agent's commit"], check=True)   # works
    assert gitops.git(repo, "log", "-1", "--format=%an <%ae>").strip() == "alice <alice@users.noreply.github.com>"


def lane_with(ctx, monkeypatch, finish):  # noqa: F811
    """A red-team fix on a roadmap pull request whose finish is scripted (the agent must not run again)."""
    job = new_job("FIX-RT-ArithmeticDynamics~1", inputs={"pr": 1336, "operator": "alice"})
    job.number = 27
    lane = lane_for(job)
    calls = []
    real_prepare = lane_base.Lane.prepare
    monkeypatch.setattr(lane, "prepare", lambda job_, ctx_: real_prepare(lane, job_, ctx_))
    monkeypatch.setattr(lane, "validate", lambda job_, ctx_, ws: [])
    monkeypatch.setattr(lane, "finish", lambda job_, ctx_, ws: calls.append("finish") or finish(ws))
    monkeypatch.setattr(lane, "submit", lambda job_, ctx_, ws, note: calls.append("submit") or "https://x/1")
    monkeypatch.setattr(lane, "sleep", lambda s: calls.append(("sleep", s)))
    return job, lane, calls


def test_a_moved_branch_blocks_the_job(ctx, monkeypatch):  # noqa: F811
    def finish(ws):
        raise gitops.BranchMoved("roadmap/arithmetic-dynamics", "1921611" + "0" * 33, "abcdef0" + "0" * 33)
    job, lane, _calls = lane_with(ctx, monkeypatch, finish)
    ws = lane.prepare(job, ctx)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "blocked" and "the pull request moved from 1921611 to abcdef0 while the fix ran" in result.detail


def test_a_failed_push_keeps_the_run_and_the_next_attempt_only_pushes(ctx, monkeypatch):  # noqa: F811
    tries = []

    def finish(ws):
        lane.push(lambda: tries.append(1) or (_ for _ in ()).throw(gitops.PushError("git push: rejected")))
    job, lane, calls = lane_with(ctx, monkeypatch, finish)
    ws = lane.prepare(job, ctx)
    report = ws.report
    report.write_text("## R01 (P1) — applied\nfixed\n")
    result = lane.deliver(job, ctx, ws)
    assert len(tries) == 3 and calls.count(("sleep", 60)) == 2               # three pushes, a minute apart
    assert result.status == "failed" and "next attempt pushes it without running the agent (try 1 of 3)" in result.detail
    pending = lane_base.read_pending(ctx, job)
    assert pending["tries"] == 1 and "rejected" in pending["error"]
    kept = ws.job_dir / lane_base.PENDING_DIR / "board" / "packages" / "ArithmeticDynamics" / "reports"
    assert (kept / f"{job.id}.md").read_text().startswith("## R01")

    # The next attempt: the report comes back, the agent does not run, the push goes through.
    monkeypatch.setattr(agents, "run_agent", lambda *a, **k: pytest.fail("the agent ran again"))
    report.unlink()
    job2, lane2, calls2 = lane_with(ctx, monkeypatch, lambda ws: "repaired")
    result = lane2.run(job2, ctx)
    assert result.status == "submitted" and calls2 == ["finish", "submit"]
    assert "pushing the run kept after a failed push (try 2)" in result.detail
    assert lane_base.read_pending(ctx, job2) is None and report.read_text().startswith("## R01")


def test_a_push_that_keeps_failing_gives_the_run_up(ctx, monkeypatch):  # noqa: F811
    def finish(ws):
        raise gitops.PushError("git push: network")
    job, lane, _calls = lane_with(ctx, monkeypatch, finish)
    ws = lane.prepare(job, ctx)
    for n in (1, 2):
        assert "try " + str(n) + " of 3" in lane.deliver(job, ctx, ws).detail
    last = lane.deliver(job, ctx, ws)
    assert last.status == "failed" and "failed 3 times; the run is given up" in last.detail
    assert lane_base.read_pending(ctx, job) is None
    assert isinstance(last, LaneResult)


@needs_git
def test_the_kept_commit_comes_back_onto_its_branch(repos, tmp_path):
    from types import SimpleNamespace

    from .fakes import example_config
    _fork, _seed, work, base = repos
    sha = sh(work, "rev-parse", "HEAD")
    run_ctx = SimpleNamespace(cfg=example_config(), wdir=tmp_path / "w1")
    job = new_job("FIX-RT-Foo~1", inputs={"pr": 905})
    board_dir = tmp_path / "board"
    (board_dir / "packages" / "Foo" / "reports").mkdir(parents=True)
    (board_dir / "packages" / "Foo" / "reports" / f"{job.id}.md").write_text("## R01 (P1) — applied\n")
    ws = lane_base.Workspace(job_dir=tmp_path / "w1" / "jobs" / "FIX-RT-Foo_r1", scratch=tmp_path / "s", board=board_dir,
                             content=work, workdir=work, report=board_dir / "r.md", branch="w1/x",
                             extra={"head": base, "branch": "roadmap/foo"})
    ws.job_dir.mkdir(parents=True)
    assert lane_base.save_pending(run_ctx, job, ws, "push rejected") == 1
    entry = lane_base.read_pending(run_ctx, job)
    assert (entry["sha"], entry["base"], entry["branch"]) == (sha, base, "roadmap/foo")
    assert (ws.job_dir / lane_base.PENDING_DIR / "commit.bundle").is_file()
    # A fresh worktree at the pull request's head, then the restore.
    sh(work, "checkout", "-q", "--detach", base)
    fresh = lane_base.Workspace(**{**ws.__dict__, "extra": {"head": base, "branch": "roadmap/foo"}})
    (board_dir / "packages" / "Foo" / "reports" / f"{job.id}.md").unlink()
    lane_base.restore_pending(run_ctx, job, fresh, entry)
    assert sh(work, "rev-parse", "HEAD") == sha and sh(work, "rev-parse", "--abbrev-ref", "HEAD") == "roadmap/foo"
    assert fresh.extra["push_base"] == base
    assert (board_dir / "packages" / "Foo" / "reports" / f"{job.id}.md").read_text().startswith("## R01")


def test_a_lost_lease_finishes_nothing_and_keeps_the_run(ctx, monkeypatch):  # noqa: F811
    """A round whose lease was lost keeps its run as a failed push's is, and the next attempt on
    this worker pushes it without the agent."""
    job, lane, calls = lane_with(ctx, monkeypatch, lambda ws: "repaired")
    ws = lane.prepare(job, ctx)
    ws.report.write_text("## R01 (P1) — applied\nfixed\n")
    lost = "the lease roadmap/P/job/FIX-RT-ArithmeticDynamics_r1 was lost: another owner holds it"

    def fence():
        raise claims.LeaseLost(lost)
    result = lane.deliver(job, dataclasses.replace(ctx, gh=SimpleNamespace(fence=fence)), ws)
    assert result.status == "lease-lost" and calls == []
    assert "nothing more is submitted, and the run is kept in" in result.detail
    assert lane_base.read_pending(ctx, job)["error"] == lost
    job2, lane2, calls2 = lane_with(ctx, monkeypatch, lambda ws: "repaired")
    assert lane2.run(job2, ctx).status == "submitted" and calls2 == ["finish", "submit"]
