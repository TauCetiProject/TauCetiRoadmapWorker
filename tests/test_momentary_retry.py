"""A momentary service error is tried again in place before the round is given up (regression,
2026-11-13: the model's API answered a few requests with "API Error: 503 credential validation
failed. This is a server-side issue, usually temporary — try again in a moment." and "API Error: 503
status code (no body)"; six lanes ended their rounds `limited` and lost 3 to 45 minutes of agent
work each). A usage limit of the account is still the round's end at once."""

import dataclasses
import json

import pytest

from tauceti_roadmap_worker import agents
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import base, lane_for

from .fakes import FakeGH, FakeTransport
from .test_checkout import NAME, live, needs_git  # noqa: F401 - the shared fixture
from .test_usage_limit_messages import CLAUDE_CREDITS, CODEX_LIMIT

VALIDATION_503 = ("API Error: 503 credential validation failed. This is a server-side issue, usually temporary — "
                  "try again in a moment.")
NO_BODY_503 = "API Error: 503 status code (no body)"
RETRY_AFTER_429 = "Error: 429 Too Many Requests, retry after 30 seconds"
BRIEF = "Write the report.\n"


@pytest.mark.parametrize("message", [VALIDATION_503, NO_BODY_503])
def test_a_service_error_with_no_reset_time_is_momentary(message):
    result = agents.classify(1, f"START\npartial work\n{message}\nEXIT 1\n", "", seconds=200.0)
    assert result.limited and result.momentary and result.message == message and result.reset_at is not None


@pytest.mark.parametrize("message", [CLAUDE_CREDITS, CODEX_LIMIT, RETRY_AFTER_429])
def test_a_limit_of_the_account_or_one_with_a_reset_time_is_not_momentary(message):
    result = agents.classify(1, "", message + "\n", seconds=200.0)
    assert result.limited and not result.momentary
    assert not agents.classify(1, "START\nall done\nEXIT 1\n", "").momentary     # a plain failure


class Ctx:
    agent, model, worker_id = "claude", "fable", "w1"
    cfg = type("C", (), {"agents": type("A", (), {"timeout_minutes": 1})(),
                         "sandbox": type("S", (), {"profile": "off", "proxy_url": "", "netjail": "",
                                                   "claude_hook": False})()})()

    def __init__(self, wdir):
        self.wdir = wdir


def scripted(tmp_path, monkeypatch, outputs, timed_out=False):
    """A lane, its context and workspace, with an agent whose runs print `outputs` in turn ((exit
    code, text) each, 100 seconds each) into their logs; the runs and the lane's sleeps are returned
    as they happen."""
    job = new_job("REV-PKG-Foo~2")
    lane = lane_for(job)
    wdir = tmp_path / "w1"
    ws = base.Workspace(job_dir=wdir / "jobs" / "REV-PKG-Foo_r2", scratch=tmp_path / "s", board=tmp_path,
                        content=tmp_path, workdir=tmp_path, report=tmp_path / "r.md", branch="b", job_id=job.id)
    ws.job_dir.mkdir(parents=True)
    runs, sleeps, left = [], [], iter(outputs)

    def agent(task):
        code, text = next(left)
        task.log_out.write_text(f"START\n{text}\nEXIT {code}\n", encoding="utf-8")
        runs.append(task)
        return agents.classify(code, task.log_out.read_text(encoding="utf-8"), "", seconds=100.0, timed_out=timed_out)
    monkeypatch.setattr(agents, "run_agent", agent)
    monkeypatch.setattr(lane, "sleep", sleeps.append)
    return lane, Ctx(wdir), ws, runs, sleeps


def test_a_service_error_then_a_success_is_the_success(tmp_path, monkeypatch):
    lane, ctx, ws, runs, sleeps = scripted(tmp_path, monkeypatch, [(1, VALIDATION_503), (0, "the report is written")])
    result = lane.run_agent(ctx, ws, BRIEF)
    assert result.ok and not result.limited and not result.momentary
    assert sleeps == [60] and result.seconds == 200.0                           # both runs count as work
    assert [task.brief for task in runs] == [BRIEF, base.CUT_OFF_NOTE + BRIEF]
    assert all(task.workdir == ws.workdir for task in runs)                    # the same workspace
    # Each run has its own logs and its own usage record.
    assert [task.log_out.name for task in runs] == ["run-1.out", "run-2.out"]
    assert VALIDATION_503 in (ws.job_dir / "run-1.out").read_text() and "the report is written" in (
        ws.job_dir / "run-2.out").read_text()
    rows = [json.loads(line) for line in (ws.job_dir / "usage.jsonl").read_text().splitlines()]
    assert [(r["run"], r["limited"], r["seconds"]) for r in rows] == [("run-1", True, 100.0), ("run-2", False, 100.0)]
    assert (ctx.wdir / "usage.jsonl").read_text() == (ws.job_dir / "usage.jsonl").read_text()
    note = f"a service error cut the agent off ({VALIDATION_503}): its brief runs again in 60 s (further run 1 of 3)"
    assert ws.extra["notes"] == [note]


def test_the_note_asks_the_agent_to_continue_from_what_the_earlier_run_left():
    note = base.CUT_OFF_NOTE
    assert "cut off by a service error" in note and "files in the working directory" in note
    assert "continue from them instead of starting over" in note and note.endswith("\n\n")


def test_a_labelled_run_keeps_its_label_and_model_when_it_runs_again(tmp_path, monkeypatch):
    lane, ctx, ws, runs, sleeps = scripted(tmp_path, monkeypatch, [(1, NO_BODY_503), (1, NO_BODY_503), (0, "done")])
    result = lane.run_agent(ctx, ws, BRIEF, "repair", model="claude-opus-5-5")
    assert result.ok and sleeps == [60, 120] and result.seconds == 300.0
    assert [task.log_out.name for task in runs] == ["run-1-repair.out", "run-2-repair.out", "run-3-repair.out"]
    assert {task.model for task in runs} == {"claude-opus-5-5"}


def test_a_service_error_on_every_run_ends_limited(tmp_path, monkeypatch):
    lane, ctx, ws, runs, sleeps = scripted(tmp_path, monkeypatch, [(1, NO_BODY_503)] * 9)
    result = lane.run_agent(ctx, ws, BRIEF)
    assert result.limited and result.momentary and result.message == NO_BODY_503 and result.reset_at is not None
    assert sleeps == [60, 120, 240] == list(base.MOMENTARY_WAITS)
    assert len(runs) == 4 and result.seconds == 400.0                           # the first run and three further ones
    assert [r["run"] for r in map(json.loads, (ws.job_dir / "usage.jsonl").read_text().splitlines())] == [
        "run-1", "run-2", "run-3", "run-4"]


@pytest.mark.parametrize("message", [CODEX_LIMIT, CLAUDE_CREDITS, RETRY_AFTER_429])
def test_a_usage_limit_is_not_run_again(tmp_path, monkeypatch, message):
    lane, ctx, ws, runs, sleeps = scripted(tmp_path, monkeypatch, [(1, message)] * 2)
    result = lane.run_agent(ctx, ws, BRIEF)
    assert result.limited and len(runs) == 1 and sleeps == [] and result.seconds == 100.0
    assert (result.reset_at is None) == (message == CLAUDE_CREDITS)            # the reset time the message gave
    assert "notes" not in ws.extra


def test_a_run_that_reached_its_timeout_is_not_run_again(tmp_path, monkeypatch):
    lane, ctx, ws, runs, sleeps = scripted(tmp_path, monkeypatch, [(124, NO_BODY_503)] * 2, timed_out=True)
    result = lane.run_agent(ctx, ws, BRIEF)
    assert result.limited and result.timed_out and len(runs) == 1 and sleeps == []


@needs_git
def test_a_round_whose_every_run_is_cut_off_ends_limited_with_all_its_work(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(f"PKG-{NAME}")
    job.number = 1
    lane = lane_for(job)
    seen, sleeps = [], []

    def agent(task, base_env=None):
        seen.append(task)
        task.log_out.write_text(f"START\npartial report\n{VALIDATION_503}\nEXIT 1\n", encoding="utf-8")
        return agents.classify(1, task.log_out.read_text(encoding="utf-8"), "", seconds=15 * 60.0)
    monkeypatch.setattr(agents, "run_agent", agent)
    monkeypatch.setattr(lane, "sleep", sleeps.append)
    monkeypatch.setattr(lane, "validate", lambda job, ctx, ws: pytest.fail("the checks ran on a limited run"))
    result = lane.run(job, ctx)
    assert result.status == "limited" and result.detail == VALIDATION_503 and result.retry_at is not None
    assert len(seen) == 4 and sleeps == [60, 120, 240] and result.worked == 60 * 60.0
    assert result.describe() == f"limited (after 60 min of work): {VALIDATION_503}"

