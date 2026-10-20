"""The `--loop` cadence: `[worker] idle_seconds` when nothing is eligible, and a poll within a minute
after a failure or once the board answers a job the worker parked (regression: in the first pilot
the waivers came two minutes after the park, and the worker picked the job up eight minutes later)."""

from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import config, worker
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import LaneResult
from tauceti_roadmap_worker.paths import HERE

from .fakes import FakeBoard

JOB = "TARGET-QSeries"


class Stop(Exception):
    pass


def parsed(idle: str = "") -> config.Config:
    text = (HERE / "tauceti-roadmap.toml.example").read_text()
    return config.parse(text.replace("idle_seconds = 600\n", idle) if idle else text, "example")


def loop(monkeypatch, results: list[LaneResult], board=None, on_sleep=None, idle_seconds=900):
    """Run work_loop over scripted rounds; return the sleeps and the log."""
    cfg = parsed()
    cfg.worker.idle_seconds = idle_seconds
    ctx = SimpleNamespace(cfg=cfg, board=board or FakeBoard())
    rounds = iter(results)

    def work_once(base, only, job_id="", order=None, log=print):
        try:
            return next(rounds)
        except StopIteration:
            raise Stop from None
    monkeypatch.setattr(worker, "work_once", work_once)
    sleeps, lines = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        if on_sleep:
            on_sleep(len(sleeps))
    with pytest.raises(Stop):
        worker.work_loop(ctx, None, list, log=lines.append, sleep=sleep)
    return sleeps, lines


IDLE = LaneResult("", "idle", "no eligible job")


def test_idle_sleep_comes_from_the_configuration(monkeypatch):
    sleeps, _ = loop(monkeypatch, [IDLE, IDLE], idle_seconds=900)
    assert sleeps == [900, 900]
    assert parsed().worker.idle_seconds == 600
    assert parsed("idle_seconds = 120\n").worker.idle_seconds == 120
    assert config.parse('[project]\nname = "P"\n').worker.idle_seconds == 600     # the default
    with pytest.raises(config.ConfigError, match="worker.idle_seconds"):
        parsed("idle_seconds = 5\n")


def test_a_parked_job_is_polled_within_a_minute_of_its_answer(monkeypatch):
    job = new_job(JOB)
    job.state = "needs-references"
    board = FakeBoard(jobs=[job])

    def answer(n):
        if n == 3:                                # `board references --yes` during the second minute
            job.state = "available"
    parked = LaneResult(JOB, "needs-references", "2 source(s) missing")
    sleeps, lines = loop(monkeypatch, [parked, IDLE, LaneResult(JOB, "submitted", "")], board, answer)
    assert sleeps[:3] == [worker.AFTER_ROUND, 60, 60]         # then the poll: the third round ran
    assert any(f"{JOB} answered on the board: polling now" in line for line in lines)


def test_a_parked_job_answered_elsewhere_is_forgotten(monkeypatch):
    job = new_job(JOB)
    job.state = "needs-references"
    parked = LaneResult(JOB, "needs-references", "")
    # The worker takes the answered job on its own before an idle wait notices: no extra wake-up.
    sleeps, _ = loop(monkeypatch, [parked, LaneResult(JOB, "submitted", ""), IDLE], FakeBoard(jobs=[job]))
    assert sleeps == [worker.AFTER_ROUND, worker.AFTER_ROUND, 900]


def test_after_a_failure_the_next_polls_come_within_a_minute(monkeypatch):
    failed = LaneResult(JOB, "failed", "GitError: checkout")
    sleeps, _ = loop(monkeypatch, [failed, IDLE, IDLE])
    assert sleeps == [worker.AFTER_ROUND, 60, 900]
    assert worker.AFTER_ROUND <= worker.QUICK_POLL <= 60
