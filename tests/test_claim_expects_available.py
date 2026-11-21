"""A worker picks its job from a listing that lags the board by seconds. The claim is written only
while the issue is still `state:available`: a job the board closed in the meantime (`done`), or one
another worker claimed, is left exactly as it is, the lane never runs, and nothing is released."""

from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import worker
from tauceti_roadmap_worker.lanes import LaneResult

from .fakes import FakeBoard
from .test_work_loop import parsed


class Lane:
    agent_started = False

    def __init__(self):
        self.ran = False

    def run(self, job, ctx):
        self.ran = True
        return LaneResult(job.id, "submitted", "")


def context(board):
    return SimpleNamespace(cfg=parsed(), board=board, claims=None, gh=None, dry_run=False, worker_id="w1",
                           operator="alice", agent="claude", model="m")


def job_in(state: str):
    return SimpleNamespace(id="PRREVIEW-Foo~4", number=7, labels=[], inputs={}, state=state)


@pytest.mark.parametrize("state", ["done", "claimed", "submitted", "blocked"])
def test_a_job_that_left_available_is_not_claimed(monkeypatch, state):
    board, lane, job, lines = FakeBoard(), Lane(), job_in(state), []
    monkeypatch.setattr(worker, "prune_job_dirs", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "release", lambda *a: pytest.fail("a job that was never claimed was released"))
    monkeypatch.setattr(worker, "request_board_run", lambda *a: pytest.fail("a board run was asked for nothing"))
    result = worker.run_job(context(board), job, lane, "key", log=lines.append)
    assert result.status == "taken" and not lane.ran
    assert job.state == state and board.states == [] and board.posted == []
    assert any("no longer available" in line for line in lines)


def test_an_available_job_is_claimed_and_run(monkeypatch):
    board, lane, job = FakeBoard(), Lane(), job_in("available")
    monkeypatch.setattr(worker, "prune_job_dirs", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "request_board_run", lambda *a: False)
    result = worker.run_job(context(board), job, lane, "key", log=lambda line: None)
    assert result.status == "submitted" and lane.ran
    assert board.states == [(job.id, "claimed")]


def test_a_taken_round_exits_like_an_idle_one():
    from tauceti_roadmap_worker import cli
    assert cli.ROUND_EXIT["taken"] == cli.ROUND_EXIT["idle"] == 75
