"""GitHub's answers about the account stop every further write: a 401 or a suspension-, abuse- or
secondary-rate-limit-shaped 403 halts the worker and every worker of its host (`halt.json`); a spent
primary rate limit pauses until it resets. A 403 about the request (a locked issue) stays a GHError."""

import datetime as dt
import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import gh as ghmod
from tauceti_roadmap_worker import worker
from tauceti_roadmap_worker.gh import GH, GHError, GHHalt, GHRateLimited
from tauceti_roadmap_worker.lanes import LaneResult

from .fakes import FakeBoard
from .test_work_loop import Stop, parsed


def answering(monkeypatch, stderr: str):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", stderr)
    monkeypatch.setattr(ghmod.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("stderr", [
    "HTTP 401: Bad credentials (https://api.github.com/user)",
    "HTTP 403: Sorry. Your account was suspended.",
])
def test_an_answer_about_the_account_halts_and_nothing_more_is_sent(monkeypatch, stderr):
    calls, gh = answering(monkeypatch, stderr), GH()
    with pytest.raises(GHHalt):
        gh.write("POST", "repos/o/r/issues/1/comments", {"body": "x"})
    with pytest.raises(GHHalt):
        gh.api("repos/o/r")
    assert len(calls) == 1 and gh.halted


def test_a_403_about_the_request_is_an_ordinary_error(monkeypatch):
    answering(monkeypatch, "HTTP 403: Unable to create comment because issue is locked.")
    gh = GH()
    with pytest.raises(GHError):
        gh.write("POST", "repos/o/r/issues/1/comments", {"body": "x"})
    assert not gh.halted


def test_a_spent_rate_limit_pauses_until_the_pause_ends(monkeypatch):
    calls = answering(monkeypatch, "HTTP 403: API rate limit exceeded for user ID 1.")
    t = [1000.0]
    gh = GH(clock=lambda: t[0])
    with pytest.raises(GHRateLimited) as exc:
        gh.api("repos/o/r")
    assert exc.value.until == 1000.0 + ghmod.RATE_PAUSE
    with pytest.raises(GHRateLimited):
        gh.api("repos/o/r")
    assert len(calls) == 1
    t[0] += ghmod.RATE_PAUSE + 1
    with pytest.raises(GHRateLimited):          # sent again after the pause (and refused again here)
        gh.api("repos/o/r")
    assert len(calls) == 2


def ctx(tmp_path):
    return SimpleNamespace(cfg=parsed(), board=FakeBoard(), state=tmp_path, worker_id="w1")


def test_a_halt_ends_the_loop_and_stops_the_hosts_other_workers(monkeypatch, tmp_path):
    def work_once(*a, **kw):
        raise GHHalt("gh api -X POST: HTTP 403: Sorry. Your account was suspended.")
    monkeypatch.setattr(worker, "work_once", work_once)
    lines = []
    assert worker.work_loop(ctx(tmp_path), None, list, log=lines.append, sleep=lambda s: None) == "halted"
    assert "suspended" in worker.halt_record(ctx(tmp_path))["detail"]
    monkeypatch.setattr(worker, "work_once", lambda *a, **kw: pytest.fail("a halted host started a round"))
    assert worker.work_loop(ctx(tmp_path), None, list, log=lines.append, sleep=lambda s: None) == "halted"


def test_a_rate_limited_round_sleeps_until_the_reset(monkeypatch, tmp_path):
    now = dt.datetime(2026, 10, 25, 5, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(worker, "now", lambda: now)
    rounds = iter([LaneResult("", "github-limited", "rate limit", retry_at=now + dt.timedelta(minutes=15))])

    def work_once(*a, **kw):
        try:
            return next(rounds)
        except StopIteration:
            raise Stop from None
    monkeypatch.setattr(worker, "work_once", work_once)
    sleeps = []
    with pytest.raises(Stop):
        worker.work_loop(ctx(tmp_path), None, list, log=lambda line: None, sleep=sleeps.append)
    assert sleeps == [900.0] and worker.halt_record(ctx(tmp_path)) is None


def test_a_halted_job_sends_nothing_more(monkeypatch, tmp_path):
    board = FakeBoard()
    job = SimpleNamespace(id="PKG-Foo", number=7, labels=[], inputs={})

    class Lane:
        agent_started = True

        def run(self, job, ctx):
            raise GHHalt("HTTP 401: Bad credentials")
    c = SimpleNamespace(cfg=parsed(), board=board, claims=None, dry_run=True, worker_id="w1", operator="alice",
                        agent="claude", model="m")
    monkeypatch.setattr(worker, "release", lambda *a: pytest.fail("a halted round tried to release the job"))
    monkeypatch.setattr(worker, "request_board_run", lambda *a: pytest.fail("a halted round asked for a board run"))
    assert worker.run_job(c, job, Lane(), "key").status == "halted"


def test_the_secondary_limit_pauses_like_the_primary_one(monkeypatch):
    """A burst limit is transient (GitHub says wait a few minutes); a host running several workers as one
    account meets it, so it pauses the worker instead of halting every worker until a human intervenes."""
    from tauceti_roadmap_worker.gh import GHRateLimited
    calls, gh = answering(monkeypatch, "HTTP 403: You have exceeded a secondary rate limit. Please wait a few minutes before you try again."), GH()
    with pytest.raises(GHRateLimited):
        gh.api("repos/o/r")
    assert not gh.halted


def test_an_unanswered_push_probe_is_unknown_not_a_refusal(monkeypatch):
    from types import SimpleNamespace
    from tauceti_roadmap_worker import claims
    from tauceti_roadmap_worker.gh import GHError
    cfg = SimpleNamespace(repositories=SimpleNamespace(claims="TauCetiProject/tauceti-roadmap-claims",
                                                       packages_repo=lambda: "TauCetiProject/TauCetiRoadmapJobs"))
    monkeypatch.delenv("CLAIM_REPO", raising=False)
    assert claims.namespace_for(cfg, SimpleNamespace(can_push=lambda repo: True), "me") == "TauCetiProject/tauceti-roadmap-claims"
    assert claims.namespace_for(cfg, SimpleNamespace(can_push=lambda repo: False), "me") == "me/TauCetiRoadmapJobs"
    with pytest.raises(GHError):
        claims.namespace_for(cfg, SimpleNamespace(can_push=lambda repo: None), "me")
