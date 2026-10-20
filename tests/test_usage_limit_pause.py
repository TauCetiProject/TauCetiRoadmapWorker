"""A usage limit pauses only the agent family that hit it (regression: with every Claude account out
of Fable quota, two unpinned workers each took a Fable red-team pass, hit the limit and slept until
the reset with Astra and Sol work waiting). A worker whose every usable family is paused sleeps
until the first reset, as before."""

import dataclasses
import datetime as dt
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import worker
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import LaneResult

from .fakes import FakeBoard
from .test_models import base
from .test_work_loop import IDLE, Stop, parsed

NOW = dt.datetime(2026, 10, 20, 22, 0, tzinfo=dt.UTC)
RESET = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def run_loop(monkeypatch, results, agent="codex", agent_fixed=False, clock=None):
    """work_loop over scripted rounds; returns the sleeps, the log and the `paused` each round saw."""
    monkeypatch.setattr(worker.shutil, "which", lambda a: f"/usr/bin/{a}")
    clock = clock or Clock()
    monkeypatch.setattr(worker, "now", clock)
    rounds, seen = iter(results), []

    def work_once(base_, only, job_id="", order=None, log=print, avoid=None, paused=None, noted=None):
        seen.append(dict(paused or {}))
        try:
            step = next(rounds)
        except StopIteration:
            raise Stop from None
        return step(clock) if callable(step) else step
    monkeypatch.setattr(worker, "work_once", work_once)
    ctx = SimpleNamespace(cfg=parsed(), board=FakeBoard(), agent=agent, agent_fixed=agent_fixed, model_fixed=False)
    sleeps, lines = [], []
    with pytest.raises(Stop):
        worker.work_loop(ctx, None, list, log=lines.append, sleep=sleeps.append)
    return sleeps, lines, seen


LIMITED = LaneResult("RT-Foo~1b", "limited", "usage limit", retry_at=RESET, agent="claude")


def test_a_limit_pauses_only_its_family(monkeypatch):
    sleeps, lines, seen = run_loop(monkeypatch, [LIMITED, LaneResult("RT-Foo~1a", "submitted", "", agent="codex")])
    assert sleeps[0] == worker.AFTER_ROUND                         # no sleep until 01:05Z
    assert seen[1] == {"claude": RESET + dt.timedelta(seconds=300)}  # the next round leaves Claude jobs alone
    assert any("claude paused by a usage limit until 2026-10-21T01:05Z; going on with codex" in line for line in lines)


def test_the_pause_ends_at_the_reset(monkeypatch):
    clock = Clock()

    def later(c):
        c.t = RESET + dt.timedelta(minutes=6)
        return IDLE
    _, lines, seen = run_loop(monkeypatch, [LIMITED, later, IDLE], clock=clock)
    assert seen[1] and seen[2] == {}
    assert any("claude's usage limit is over: its jobs are eligible again" in line for line in lines)


def test_a_worker_whose_only_family_is_paused_sleeps_until_the_reset(monkeypatch):
    sleeps, _, _ = run_loop(monkeypatch, [LIMITED], agent="claude", agent_fixed=True)
    assert sleeps == [(RESET - NOW).total_seconds() + 300]
    no_reset = LaneResult("RT-Foo~1b", "limited", "usage limit", agent="claude")
    sleeps, _, _ = run_loop(monkeypatch, [no_reset], agent="claude", agent_fixed=True)
    assert sleeps == [1800]


def test_jobs_of_a_paused_family_are_skipped_and_logged_once(monkeypatch):
    monkeypatch.setattr(worker.shutil, "which", lambda a: f"/usr/bin/{a}")
    monkeypatch.setattr(worker, "failures", lambda ctx: {})
    fable_a, fable_b = (new_job(f"RT-Foo~1{p}", requires_agent="claude", requires_model="fable") for p in "bc")
    fable_a.state = fable_b.state = "available"
    board = FakeBoard(jobs=[fable_a, fable_b])
    ctx = dataclasses.replace(base(), board=board, dry_run=True)
    paused, noted, lines = {"claude": RESET}, {}, []
    ctx_, why = worker.for_job(ctx, fable_a, paused)
    assert ctx_ is None and why == "claude paused by a usage limit until 2026-10-21T01:00Z"
    for _ in range(2):
        result = worker.work_once(ctx, None, log=lines.append, paused=paused, noted=noted)
        assert result.status == "idle"
    assert lines == ["skip claude jobs (first: RT-Foo~1b): claude paused by a usage limit until 2026-10-21T01:00Z"]
    assert worker.for_job(ctx, new_job("REV-PKG-Foo"), paused)[0].agent == "codex"   # Astra work goes on
