"""A usage limit is recognised from any of the CLIs' messages, wherever it appears in stdout or
stderr and whatever the exit code, and never counts as an attempt (regression, 2026-10-21: the
Claude CLI printed "You're out of usage credits. Switch to another model, or manage usage credits
at https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue." after 18 minutes of
work and exited 1; the worker logged `failed: agent exited 1`, counted the attempt and started the
job again at once)."""

import dataclasses
import datetime as dt
import json

import pytest

from tauceti_roadmap_worker import agents, worker
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import LaneResult, lane_for

from .fakes import FakeGH, FakeTransport
from .test_checkout import NAME, live, needs_git, run  # noqa: F401 - the shared fixture
from .test_states import Lane, cfg, round_ctx  # noqa: F401 - the shared fixtures

CLAUDE_CREDITS = ("You're out of usage credits. Switch to another model, or manage usage credits at "
                  "https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue.")
CLAUDE_MODEL = ("You've reached your Fable limit. Switch to another model, or manage usage credits at "
                "https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue.")
CODEX_LIMIT = ("ERROR: You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more "
               "credits or try again at Oct 27th, 2026 6:34 AM.")
CODEX_CAPACITY = "ERROR: Selected model is at capacity. Please try a different model."
NOW = dt.datetime(2026, 10, 21, 18, 30, tzinfo=dt.UTC)

# One live or representative message per row of agents.LIMIT_PATTERNS, in the table's order.
MESSAGES = {
    "claude: credits exhausted": CLAUDE_CREDITS,
    "claude: manage usage credits link": "Switch to another model, or manage usage credits at https://claude.ai/x",
    "claude: limit link parameter": "see https://claude.ai/settings/usage?from=cc_cli_limit_message",
    "usage limit": CODEX_LIMIT,
    "claude: reached your <model|window> limit": CLAUDE_MODEL,
    "claude: limit will reset": "Your limit will reset at 3pm (Europe/Paris).",
    "claude api: credit balance": "API Error: 400 Your credit balance is too low to access the Anthropic API.",
    "codex: model at capacity": CODEX_CAPACITY,
    "claude api: overloaded": '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    "http: too many requests": "Error: 429 Too Many Requests",
    "http: status 429": "request failed with status 429",
    "claude cli: api error 429/5xx": "API Error: 529 {\"type\":\"error\"}",
    "rate limit": "openai: rate limit exceeded, retrying",
}


def test_every_pattern_has_a_message_and_vice_versa():
    assert [name for name, _, _ in agents.LIMIT_PATTERNS] == list(MESSAGES)


@pytest.mark.parametrize("name,pattern,failed_only", agents.LIMIT_PATTERNS, ids=[n for n, _, _ in agents.LIMIT_PATTERNS])
def test_each_pattern_recognises_its_message(name, pattern, failed_only):
    message = MESSAGES[name]
    out = f"START 2026-10-21T18:12:02Z claude fable /w\n{message}\nEXIT 1 2026-10-21T18:30:34Z\n"
    result = agents.classify(1, out, "", seconds=1100.0)
    assert result.limited and not result.ok and result.message == message
    assert agents.classify(1, "", out).limited                                   # on stderr as well
    assert agents.classify(1, out.upper(), "").limited                          # case-insensitively
    assert agents.classify(0, out, "").limited is not failed_only               # exit 0: the specific ones only
    assert not agents.classify(1, "START\nall done\nEXIT 1\n", "").limited


def test_the_message_is_found_anywhere_in_the_output_not_only_in_the_tail():
    err = "brief echo\n" * 2000 + CODEX_LIMIT + "\n" + "tokens used\n127,174\n" + "x" * 20000
    assert agents.classify(1, "START\nEXIT 1\n", err).message == CODEX_LIMIT
    out = "START\n" + CLAUDE_CREDITS + "\n" + "partial report\n" * 5000 + "EXIT 1\n"
    assert agents.classify(1, out, "").message == CLAUDE_CREDITS


def test_a_bare_429_or_a_quoted_rate_limit_in_a_successful_run_is_not_a_limit():
    sources = "   429  part of the lemma we have that NS(X) = Pic(X)\n(Theorem 5), 1421-1429 (Selberg via [7])\n"
    assert not agents.classify(1, "", sources).limited
    assert not agents.classify(0, "the API rate limit was raised in 2024; Too Many Requests was retried\n", "").limited
    assert agents.classify(1, "", "Error: 429 Too Many Requests\n").limited


def test_a_limited_run_is_not_transient_and_keeps_its_exit_code():
    result = agents.classify(1, "", CODEX_LIMIT + "\nConnection error\n", seconds=12.0)
    assert result.limited and not result.transient and result.exit_code == 1 and result.seconds == 12.0
    assert agents.classify(1, "", "Connection error\n").transient


def test_a_long_message_line_is_cut_for_the_log():
    assert len(agents.classify(1, "usage limit " + "x" * 1000, "").message) == agents.MESSAGE_WIDTH


@pytest.mark.parametrize("text,expected", [
    (CODEX_LIMIT, dt.datetime(2026, 10, 27, 6, 34)),                            # Codex: local clock time
    ("try again at October 27, 2026 at 6:34 PM", dt.datetime(2026, 10, 27, 18, 34)),
    ("limit reached; resets 3pm (Europe/Paris)", dt.datetime(2026, 10, 22, 13, 0, tzinfo=dt.UTC)),  # 18:30Z: tomorrow
    ("resets Oct 25, 9am (UTC)", dt.datetime(2026, 10, 25, 9, 0, tzinfo=dt.UTC)),
    ("Your limit will reset at 7:45pm (UTC)", dt.datetime(2026, 10, 21, 19, 45, tzinfo=dt.UTC)),
    ("until 19:05 UTC", dt.datetime(2026, 10, 21, 19, 5, tzinfo=dt.UTC)),
    ("try again in 2 hours 30 minutes", NOW + dt.timedelta(hours=2, minutes=30)),
    ("resets in 45 minutes", NOW + dt.timedelta(minutes=45)),
    ("retry after 30 seconds", NOW + dt.timedelta(seconds=30)),
    ("resets at 2026-10-22T03:00:00Z", dt.datetime(2026, 10, 22, 3, 0, tzinfo=dt.UTC)),
])
def test_reset_time_hints(text, expected):
    got = agents.reset_time(text, NOW)
    assert got is not None
    if expected.tzinfo is None:                                                 # a clock time in the host's zone
        assert got.tzinfo is not None and got.replace(tzinfo=None) == expected
    else:
        assert got == expected


def test_no_reset_hint_means_no_reset_time():
    assert agents.reset_time(CLAUDE_CREDITS, NOW) is None
    assert agents.reset_time("until a human names an alternative legal source", NOW) is None
    assert agents.classify(1, CLAUDE_CREDITS, "").reset_at is None


def test_the_reset_time_is_read_from_the_limit_message_not_from_the_sources_quoted_before_it():
    err = "the lecture resumes at 9am (UTC) after the break\n" * 3 + CODEX_LIMIT + "\n"
    result = agents.classify(1, "", err, seconds=100.0)
    assert result.reset_at.replace(tzinfo=None) == dt.datetime(2026, 10, 27, 6, 34)


def test_run_agent_reads_the_whole_log_files(tmp_path):
    out, err = tmp_path / "run-1.out", tmp_path / "run-1.err"
    # The CLI's stream: text events of work, then the limit as the error result of the run.
    work = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "partial work"}]}}] * 3000
    lines = [json.dumps(e) + "\n" for e in work] + [json.dumps({"type": "result", "subtype": "error_during_execution",
                                                                 "is_error": True, "result": CLAUDE_CREDITS}) + "\n"]
    task = agents.AgentRun(agent="claude", model="fable", workdir=tmp_path, brief="b", log_out=out, log_err=err,
                           claude_hook=False)
    result = agents.run_agent(task, launch=lambda *a, **k: FakeProc(lines, 1))
    assert result.limited and result.exit_code == 1 and result.message == CLAUDE_CREDITS
    assert out.read_text().startswith("START ") and "EXIT 1 " in out.read_text()
    assert out.read_text().count("partial work") == 3000 and (tmp_path / "run-1.jsonl").read_text().count("\n") == 3001


class FakeProc:
    """A process whose stdout is the given lines and that exits with `code`."""

    def __init__(self, lines, code):
        import io
        self.stdin, self.stdout, self.code = io.StringIO(), iter(lines), code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        pass


@needs_git
def test_a_limit_after_work_is_limited_not_failed_and_the_output_stays(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(f"PKG-{NAME}")
    job.number = 1
    lane = lane_for(job)
    seen = []

    def agent(task, base_env=None):
        seen.append(task)
        task.log_out.parent.mkdir(parents=True, exist_ok=True)
        task.log_out.write_text("START\npartial report\n" + CLAUDE_CREDITS + "\nEXIT 1\n")
        (task.workdir / "partial.md").write_text("work done before the limit\n")
        return agents.classify(1, task.log_out.read_text(), "", seconds=18 * 60.0)
    monkeypatch.setattr(agents, "run_agent", agent)
    monkeypatch.setattr(lane, "validate", lambda job, ctx, ws: pytest.fail("the checks ran on a limited run"))
    result = lane.run(job, ctx)
    assert result.status == "limited" and result.detail == CLAUDE_CREDITS and result.retry_at is None
    assert result.worked == 18 * 60.0 and len(seen) == 1
    assert result.describe() == f"limited (after 18 min of work): {CLAUDE_CREDITS}"
    assert "partial report" in seen[0].log_out.read_text()                     # the run's output is kept
    assert (seen[0].workdir / "partial.md").exists()


@needs_git
def test_a_limit_in_a_repair_run_counts_the_whole_round_as_work(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(f"PKG-{NAME}")
    job.number = 1
    lane = lane_for(job)
    runs = iter([agents.AgentResult(exit_code=0, seconds=600.0),
                 agents.classify(1, "", CODEX_LIMIT + "\n", seconds=300.0)])
    monkeypatch.setattr(agents, "run_agent", lambda task, base_env=None: next(runs))
    monkeypatch.setattr(lane, "validate", lambda job, ctx, ws: ["readme-ids-missing: id1"])
    monkeypatch.setattr(lane, "push_wip", lambda job, ctx, ws: pytest.fail("a limited run is not a failed one"))
    result = lane.run(job, ctx)
    assert result.status == "limited" and result.detail == CODEX_LIMIT + " (repair)" and result.worked == 900.0
    assert result.retry_at.replace(tzinfo=None) == dt.datetime(2026, 10, 27, 6, 34)
    assert result.describe().startswith("limited (after 15 min of work): ERROR: You’ve hit your usage limit.")


def test_a_limited_round_releases_the_job_without_counting_an_attempt(round_ctx, monkeypatch):  # noqa: F811
    ctx, gh = round_ctx
    outcome = LaneResult("TARGET-QSeries", "limited", CLAUDE_CREDITS, worked=18 * 60.0)
    monkeypatch.setattr(worker, "lane_for", lambda job: Lane(outcome))
    for _ in range(4):                                                          # more rounds than MAX_ATTEMPTS
        result = worker.work_once(ctx, None, log=lambda line: None)
        assert result.status == "limited" and result.worked == 18 * 60.0
    assert worker.failures(ctx) == {} and gh.states(1) == ["available"]
    released = [c for c in gh.comments[1] if c.startswith("Released by worker `chris-1`: limited (You're out of usage credits.")]
    assert len(released) == 4                                                   # every round released it, none blocked it


def test_the_log_line_shows_the_time_wasted_only_when_there_was_work():
    assert LaneResult("J", "limited", CLAUDE_MODEL, worked=1.0).describe() == f"limited: {CLAUDE_MODEL}"
    assert LaneResult("J", "limited", CLAUDE_CREDITS, worked=1105.0).describe() == \
        f"limited (after 18 min of work): {CLAUDE_CREDITS}"
    assert LaneResult("J", "failed", "agent exited 1", worked=1105.0).describe() == "failed: agent exited 1"
