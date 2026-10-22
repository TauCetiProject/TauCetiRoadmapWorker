"""Token usage per agent run: read from the CLIs' streamed events (the shapes below are the CLIs' own,
recorded on 2026-10-21 from `claude -p --output-format stream-json --verbose` 2.1.280 and `codex exec
--json` 0.155.1), rendered readably into the run log, recorded per run, and summed by `usage`."""

import json
import sys
import textwrap

from tauceti_roadmap_worker import agents, cli, usage
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for

CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init", "cwd": "/w", "session_id": "s", "tools": [], "model": "m"},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                   "input": {"command": "echo hello-probe"}}]}},
    {"type": "rate_limit_event", "rate_limit_info": {}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "You've hit your usage limit (a quote)"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "ok", "total_cost_usd": 0.021205,
     "usage": {"input_tokens": 9, "cache_creation_input_tokens": 8209, "cache_read_input_tokens": 13696,
               "output_tokens": 41, "output_tokens_details": {"thinking_tokens": 35}},
     "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 18, "outputTokens": 90, "cacheReadInputTokens": 27392,
                                                  "cacheCreationInputTokens": 8209, "thinkingTokens": 70,
                                                  "costUSD": 0.021205}}},
]
CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_0", "type": "command_execution",
                                      "command": "/bin/bash -lc 'echo hello-probe'", "aggregated_output": "",
                                      "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
                                        "command": "/bin/bash -lc 'echo hello-probe'",
                                        "aggregated_output": "You've hit your usage limit (a quote)\n", "exit_code": 0,
                                        "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "ok"}},
    {"type": "turn.completed", "usage": {"input_tokens": 27527, "cached_input_tokens": 13568,
                                         "cache_write_input_tokens": 0, "output_tokens": 41,
                                         "reasoning_output_tokens": 7}},
]


def rendered(agent, events):
    return [line for e in events for line in agents.render_event(agent, e)]


def test_the_log_shows_messages_and_tool_calls_never_tool_output():
    assert rendered("claude", CLAUDE_EVENTS) == ["→ Bash: echo hello-probe", "ok"]
    assert rendered("codex", CODEX_EVENTS) == ["$ /bin/bash -lc 'echo hello-probe'", "ok"]
    # an error the CLI reports is shown: that is where a usage-limit message arrives
    assert agents.render_event("claude", {"type": "result", "is_error": True, "result": "You're out of usage credits."}) \
        == ["You're out of usage credits."]
    assert agents.render_event("codex", {"type": "turn.failed", "error": {"message": "usage limit reached"}}) \
        == ["usage limit reached"]
    assert agents.render_event("codex", {"type": "error", "message": "model is at capacity"}) == ["model is at capacity"]


def test_usage_in_one_shape_for_both_clis():
    claude = agents.usage_from_events("claude", CLAUDE_EVENTS)
    assert claude["input_tokens"] == 18 and claude["cache_read_tokens"] == 27392 and claude["cache_write_tokens"] == 8209
    assert claude["output_tokens"] == 90 and claude["thinking_tokens"] == 70          # modelUsage: every model, summed
    assert claude["cost_usd"] == 0.021205 and claude["turns"] == 2
    without_models = [*CLAUDE_EVENTS[:-1], {k: v for k, v in CLAUDE_EVENTS[-1].items() if k != "modelUsage"}]
    assert agents.usage_from_events("claude", without_models)["output_tokens"] == 41
    codex = agents.usage_from_events("codex", CODEX_EVENTS + CODEX_EVENTS[-1:])       # two turns: summed
    assert codex["input_tokens"] == 2 * (27527 - 13568) and codex["cache_read_tokens"] == 2 * 13568
    assert codex["output_tokens"] == 82 and codex["thinking_tokens"] == 14 and "cost_usd" not in codex
    assert agents.usage_from_events("claude", CLAUDE_EVENTS[:-1]) == {} == agents.usage_from_events("codex", [])


def test_a_real_process_streams_into_the_log(tmp_path, monkeypatch):
    script = tmp_path / "fake_cli.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys
        brief = sys.stdin.read()
        for event in {CODEX_EVENTS!r}:
            print(json.dumps(event), flush=True)
        print("a line that is not JSON")
        print("brief chars:", len(brief), file=sys.stderr)
    """))
    task = agents.AgentRun(agent="codex", model="m", workdir=tmp_path, brief="x" * 200_000,
                           log_out=tmp_path / "run-1.out", log_err=tmp_path / "run-1.err")
    monkeypatch.setattr(agents, "command_for", lambda run: ([sys.executable, str(script)], {}, tmp_path))
    result = agents.run_agent(task, launch=__import__("subprocess").Popen)
    log = task.log_out.read_text()
    assert result.exit_code == 0 and not result.limited       # the quoted limit in a command's output is not the CLI's
    assert "$ /bin/bash -lc 'echo hello-probe'\nok\na line that is not JSON\nUSAGE {" in log and "EXIT 0 " in log
    assert result.usage["input_tokens"] == 27527 - 13568 and result.usage["thinking_tokens"] == 7
    assert len(task.log_out.with_suffix(".jsonl").read_text().splitlines()) == len(CODEX_EVENTS) + 1
    assert "brief chars: 200000" in task.log_err.read_text()   # a large brief reaches stdin whole


def test_command_lines_ask_for_the_event_streams(tmp_path):
    run = agents.AgentRun(agent="claude", model="m", workdir=tmp_path, brief="b", log_out=tmp_path / "o",
                          log_err=tmp_path / "e", claude_hook=False)
    argv, _env, _cwd = agents.command_for(run, {"HOME": "/h", "PATH": "/bin"})
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    argv, _env, _cwd = agents.command_for(agents.AgentRun(**{**run.__dict__, "agent": "codex"}), {"HOME": "/h"})
    assert "--json" in argv and argv[-1] == "-"


def test_every_run_of_a_lane_is_recorded(tmp_path, monkeypatch):
    from tauceti_roadmap_worker.lanes import base
    job = new_job("REV-PKG-Foo~2")
    lane = lane_for(job)
    ws = base.Workspace(job_dir=tmp_path / "w1" / "jobs" / "REV-PKG-Foo_r2", scratch=tmp_path / "s", board=tmp_path,
                        content=tmp_path, workdir=tmp_path, report=tmp_path / "r.md", branch="b", job_id=job.id)
    ws.job_dir.mkdir(parents=True)

    class Ctx:
        agent, model, worker_id, wdir = "claude", "fable", "w1", tmp_path / "w1"
        cfg = type("C", (), {"agents": type("A", (), {"timeout_minutes": 1})(),
                             "sandbox": type("S", (), {"profile": "off", "proxy_url": "", "netjail": "",
                                                       "claude_hook": False})()})()
    monkeypatch.setattr(agents, "run_agent", lambda task: agents.AgentResult(
        exit_code=0, seconds=12.5, usage={"input_tokens": 5, "output_tokens": 7, "cost_usd": 0.5}))
    lane.run_agent(Ctx(), ws, "the brief", "repair")
    lane.run_agent(Ctx(), ws, "the brief")
    rows = [json.loads(line) for line in (tmp_path / "w1" / "usage.jsonl").read_text().splitlines()]
    assert [r["run"] for r in rows] == ["run-1-repair", "run-1"]          # no .out written by the fake: same count
    assert rows[0]["job"] == "REV-PKG-Foo~2" and rows[0]["kind"] == "review" and rows[0]["label"] == "repair"
    assert rows[1]["label"] == "main" and rows[1]["output_tokens"] == 7 and rows[1]["brief_chars"] == len("the brief")
    assert (ws.job_dir / "usage.jsonl").read_text() == (tmp_path / "w1" / "usage.jsonl").read_text()


def test_summary_by_lane_and_the_command(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    rows = [{"at": "2026-10-20T10:00:00Z", "lane": "ReviewLane", "kind": "review", "seconds": 3600,
             "input_tokens": 1000, "cache_read_tokens": 2_000_000, "output_tokens": 50_000, "cost_usd": 1.5},
            {"at": "2026-10-21T10:00:00Z", "lane": "ReviewLane", "kind": "review", "seconds": 1800,
             "output_tokens": 25_000},
            {"at": "2026-10-21T11:00:00Z", "lane": "FixLane", "kind": "fix", "seconds": 60}]
    for name, recs in (("w1", rows[:2]), ("w2", rows[2:])):
        (state / "workers" / name).mkdir(parents=True)
        (state / "workers" / name / "usage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs) + "junk\n")
    summary = usage.summarize(usage.load(state))
    assert [r["lane"] for r in summary] == ["ReviewLane", "FixLane"]
    assert summary[0]["runs"] == 2 and summary[0]["output_tokens"] == 75_000 and summary[0]["cost_usd"] == 1.5
    assert summary[1]["reported"] == 0                                    # a run whose CLI reported nothing
    assert usage.summarize(usage.load(state), by="day", since="2026-10-21")[0]["day"] == "2026-10-21"
    assert len(usage.load(state, "w2")) == 1
    table = usage.table(summary, "lane")
    assert "ReviewLane" in table and "0.075M" in table and "2.000M" in table and "1.50" in table
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(state))
    from pathlib import Path
    conf = Path(__file__).resolve().parents[1] / "tauceti-roadmap.toml.example"
    assert cli.main(["--conf", str(conf), "usage", "--by", "kind"]) == 0
    assert "review" in capsys.readouterr().out


def test_a_timeout_kills_the_agent_and_what_it_started(tmp_path, monkeypatch):
    import time
    script = tmp_path / "slow_cli.py"
    # The agent starts a child that inherits its stdout and outlives it, then sleeps past the timeout.
    script.write_text(textwrap.dedent("""
        import subprocess, sys, time
        sys.stdin.read()
        print('{"type": "item.completed", "item": {"type": "agent_message", "text": "working"}}', flush=True)
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        time.sleep(300)
    """))
    task = agents.AgentRun(agent="codex", model="m", workdir=tmp_path, brief="b", timeout=2,
                           log_out=tmp_path / "run-1.out", log_err=tmp_path / "run-1.err")
    monkeypatch.setattr(agents, "command_for", lambda run: ([sys.executable, str(script)], {}, tmp_path))
    started = time.monotonic()
    result = agents.run_agent(task, launch=__import__("subprocess").Popen)
    assert result.timed_out and result.exit_code == 124
    assert time.monotonic() - started < 30                      # the grandchild did not hold the pipe open
    assert "working" in task.log_out.read_text() and "wall-clock timeout" in task.log_out.read_text()
