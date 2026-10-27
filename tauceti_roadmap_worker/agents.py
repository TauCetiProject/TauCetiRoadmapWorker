"""Run the operator's agent on a brief: Codex or Claude, optionally sandboxed.

Port of the maintainer's `agentrun` (dispatch by agent) and `jailrun` (network jail with a proxy
fallback), with the usage-limit handling of the explorer's lane.py. The brief goes to the agent on
stdin; stdout and stderr go to log files that start with a START line and end with an EXIT line.

The agent runs with a minimal environment: HOME, PATH, locale and terminal settings, the agent's
own config directory (CODEX_HOME / CLAUDE_CONFIG_DIR) and its API key if one is set. GitHub tokens
are never passed to an agent; the worker itself does every GitHub write.

Both CLIs stream their events as JSON Lines (`claude -p --output-format stream-json --verbose`,
`codex exec --json`). The raw events are kept beside the log (`run-N.jsonl`); the log itself
(`run-N.out`) gets a readable rendering as they arrive: the agent's messages, the commands and tools
it runs, and any error the CLI reports, but never a tool's output (which can quote anything, a
usage-limit message included). The run's token usage is read from the events (Claude's final
`result` event, Codex's `turn.completed` events), written as a `USAGE` line before the `EXIT` line,
and returned with the result.

Sandbox profiles (`[sandbox] profile`): "off" (default) runs on the host; "proxy" points the agent's
HTTP(S)_PROXY at the allow-everything-and-log proxy (bundled sandbox/allowproxy.py); "netjail" runs
the agent in a private network namespace whose only route out is that proxy. A requested profile that
cannot run here (no usable jail, nothing listening at the proxy) stops the worker: no agent runs
without the sandbox the configuration asks for. No profile isolates the filesystem or credentials:
the agent runs as the operator, with the operator's home directory and permissions. Claude agents
also get a PreToolUse hook (sandbox/url-guard.py) that refuses to execute fetched content, raw
network tools, proxy changes and credential access in shell commands.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .paths import SANDBOX

# Usage-limit messages of the two CLIs, one row each: (what prints it, pattern, failed runs only).
# A run is "limited" when any row matches anywhere in its stdout or stderr, case-insensitively,
# whatever its exit code; the rows marked True are generic service phrases an agent can quote while
# working around them (an HTTP 429 it retried), so they count only when the run exited non-zero.
LIMIT_PATTERNS: tuple[tuple[str, str, bool], ...] = (
    # Claude CLI, credits exhausted: "You're out of usage credits. Switch to another model, or manage
    # usage credits at https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue."
    # (seen 2026-10-21 on stdout, exit 1, after 18 minutes of work)
    ("claude: credits exhausted", r"out of usage credits", False),
    # Claude CLI, every limit message ends with this link text
    ("claude: manage usage credits link", r"manage usage credits", False),
    # Claude CLI, the tracking parameter of that link (survives a reworded sentence)
    ("claude: limit link parameter", r"cc_cli_limit_message", False),
    # Both CLIs: "… usage limit. Switch to another model …" (Claude), "You’ve hit your usage limit.
    # Visit https://chatgpt.com/settings/usage … or try again at Oct 27th, 2026 6:34 AM." (Codex,
    # stderr, exit 1), "usage limit reached"
    ("usage limit", r"usage limit", False),
    # Claude CLI, a per-model or per-window limit: "You've reached your Fable limit. Switch to another
    # model …", "You've hit your weekly limit"
    ("claude: reached your <model|window> limit", r"(?:hit|reached) your [\w ]*limit", False),
    # Claude CLI, "Your limit will reset at 3pm (Europe/Paris)"
    ("claude: limit will reset", r"limit will reset", False),
    # Claude API key with no credit: "Credit balance is too low"
    ("claude api: credit balance", r"credit balance is too low", False),
    # Codex: "ERROR: Selected model is at capacity. Please try a different model." (stderr, exit 1)
    ("codex: model at capacity", r"model (?:is )?at capacity", False),
    # Claude API overload error type, printed when the CLI gives up on it
    ("claude api: overloaded", r"overloaded_error", True),
    # HTTP 429 spelled out, from either CLI
    ("http: too many requests", r"too many requests", True),
    # HTTP 429 as a status code, with a word that makes it one: a bare "429" is a line number or a
    # page number in the sources the agents quote
    ("http: status 429", r"(?:http|status|error|code)\W{0,3}429\b|\b429\W{0,3}(?:too many|rate)", True),
    # Claude CLI API errors: "API Error: 429 …", "API Error: 529 …"
    ("claude cli: api error 429/5xx", r"API Error: (?:429|5\d\d)", True),
    # "rate limit", "rate-limited", "rate limiting", from either CLI
    ("rate limit", r"rate.?limit", True),
)
LIMIT_ALWAYS = re.compile("|".join(p for _, p, failed_only in LIMIT_PATTERNS if not failed_only), re.I)
LIMIT_ON_FAILURE = re.compile("|".join(p for _, p, _ in LIMIT_PATTERNS), re.I)
LIMIT = LIMIT_ON_FAILURE     # every pattern, for callers that already know the run failed
TRANSIENT = re.compile(r"Request timed out|ECONNRESET|socket hang up|fetch failed|Connection error", re.I)
MESSAGE_WIDTH = 240          # characters of the limit message's line kept for the log
# Service-side, momentary limits: the model is busy right now, not this account's quota. With no reset
# time in the message these pause the family MOMENTARY_PAUSE seconds, not the usage-limit default.
MOMENTARY = re.compile(r"model (?:is )?at capacity|overloaded_error|too many requests|"
                       r"(?:http|status|error|code)\W{0,3}429\b|\b429\W{0,3}(?:too many|rate)|"
                       r"API Error: (?:429|5\d\d)|rate.?limit", re.I)
MOMENTARY_PAUSE = 300        # seconds

# Reset-time hints, read from the limit message's line and the text after it. Absolute: "resets 3pm
# (Europe/Paris)", "resets Oct 21, 9am", "resets at 3:30pm", "try again at Oct 27th, 2026 6:34 AM",
# "until 6:34 AM", "resets at 15:30 UTC"; relative: "try again in 2 hours 30 minutes", "resets in
# 45 minutes", "retry after 30 seconds"; ISO: "resets at 2026-10-22T03:00:00Z".
_LEAD = r"(?:resets?|resetting|try again|retry|available again|until)\s*(?:at|on)?\s*"
_MONTH = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
RESET = re.compile(
    _LEAD + r"(?:" + _MONTH + r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:(?P<year>\d{4}),?\s*)?(?:at\s+)?)?"
    r"(?:(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>[ap])\.?m\b\.?|(?P<hour24>[01]?\d|2[0-3]):(?P<min24>[0-5]\d)\b)"
    r"\s*(?:\((?P<tz>[^)]+)\)|(?P<utc>UTC|GMT)\b)?", re.I)
RESET_IN = re.compile(_LEAD.replace(r"\s*(?:at|on)?\s*", r"\s+") + r"(?:in|after)\s+"
                      r"(?P<dur>(?:\d+\s*(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,]*(?:and\s+)?)+)", re.I)
RESET_ISO = re.compile(_LEAD + r"(?P<iso>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)", re.I)
RESET_WINDOW = 600           # characters after the limit message searched for a reset time
PASS_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SSL_CERT_FILE", "ELAN_HOME",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
            "TAUCETI_PROJECT_STATE")
AGENT_ENV = {"codex": ("CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY"),
             "claude": ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY")}


@dataclass
class AgentRun:
    agent: str
    model: str
    workdir: Path
    brief: str
    log_out: Path
    log_err: Path
    timeout: int = 6 * 3600
    sandbox_profile: str = "off"
    proxy_url: str = "http://127.0.0.1:3128"
    netjail: str = ""
    claude_hook: bool = True
    settings_dir: Path | None = None
    extra_env: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    exit_code: int
    seconds: float
    limited: bool = False
    transient: bool = False
    timed_out: bool = False
    reset_at: dt.datetime | None = None
    message: str = ""       # the first line of the usage-limit message, for the log
    usage: dict = field(default_factory=dict)    # the run's tokens (see `usage_from_events`); {} when unknown

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.limited


def agent_env(agent: str, base: dict | None = None, extra: dict | None = None) -> dict:
    base = os.environ if base is None else base
    env = {k: base[k] for k in PASS_ENV + AGENT_ENV.get(agent, ()) if k in base}
    home = base.get("HOME", str(Path.home()))
    path = [f"{home}/.elan/bin", f"{home}/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    extra_path = [p for p in base.get("PATH", "").split(os.pathsep) if p and p not in path]
    env["PATH"] = os.pathsep.join(path + extra_path)
    env.setdefault("LANG", "C.UTF-8")
    env.update(extra or {})
    return env


def proxy_env(url: str) -> dict:
    return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url, "ALL_PROXY": url,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}


def claude_settings(settings_dir: Path) -> Path:
    """Write the worker settings file with the bundled url-guard hook and return its path."""
    settings_dir.mkdir(parents=True, exist_ok=True)
    guard = SANDBOX / "url-guard.py"
    data = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": f"python3 -I {guard}", "timeout": 10}]}]}}
    path = settings_dir / "claude-worker-settings.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


class SandboxUnavailable(SystemExit):
    """The sandbox `[sandbox] profile` asks for cannot run agents on this host. That is so for every
    job, so it ends the worker (exit status 1, the reason on stderr) once the job at hand is released,
    instead of running the agent without it or releasing one job after another."""


def netjail_usable(binary: str) -> bool:
    if not binary or not Path(binary).is_file():
        return False
    try:
        return subprocess.run([binary, "/usr/bin/true"], capture_output=True, timeout=20,
                              env={**os.environ, "NETJAIL_FORWARDER": str(SANDBOX / "nsforward.py")}).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def proxy_listening(url: str) -> bool:
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 3128), timeout=3):
            return True
    except (OSError, ValueError):
        return False


def sandbox_problem(profile: str, proxy_url: str, netjail: str) -> str:
    """Why the sandbox profile cannot run an agent on this host, '' when it can (always for "off")."""
    if profile == "netjail" and not netjail:
        return 'profile "netjail" is requested and [sandbox] netjail names no jail binary'
    if profile == "netjail" and not netjail_usable(netjail):
        return (f'profile "netjail" is requested and {netjail} cannot start a jail here (build sandbox/netjail.c '
                "and grant it user namespaces: docs/OPERATORS.md, The sandbox profile)")
    if profile in ("proxy", "netjail") and not proxy_listening(proxy_url):
        return f'profile "{profile}" is requested and nothing listens at {proxy_url} (start sandbox/allowproxy.py)'
    return ""


def command_for(run: AgentRun, base_env: dict | None = None) -> tuple[list[str], dict, Path]:
    """(argv, environment, cwd) for one agent run. Pure apart from writing the Claude settings file."""
    env = agent_env(run.agent, base_env, run.extra_env)
    if run.agent == "codex":
        argv = ["codex", "exec", "-m", run.model, "--skip-git-repo-check", "--json",
                "--dangerously-bypass-approvals-and-sandbox", "-C", str(run.workdir), "-"]
    elif run.agent == "claude":
        argv = ["claude", "-p", "--model", run.model, "--dangerously-skip-permissions",
                "--output-format", "stream-json", "--verbose"]
        if run.claude_hook and run.settings_dir:
            argv += ["--settings", str(claude_settings(run.settings_dir))]
    else:
        raise ValueError(f"unknown agent {run.agent!r}")
    if run.sandbox_profile == "netjail":
        env["NETJAIL_FORWARDER"] = str(SANDBOX / "nsforward.py")
        argv = [run.netjail, *argv]
    elif run.sandbox_profile == "proxy":
        env.update(proxy_env(run.proxy_url))
    return argv, env, run.workdir


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def events_path(log_out: Path) -> Path:
    """Where a run's raw JSON events are kept: `run-N.jsonl` beside `run-N.out`."""
    return log_out.with_suffix(".jsonl")


def run_agent(run: AgentRun, launch=None) -> AgentResult:
    """Run the agent on the brief. Its events are rendered into the log as they arrive (`tail` follows
    it) and kept raw beside it; the usage-limit check reads the rendered log and stderr, and the
    token usage comes from the events. `launch` (tests) replaces `subprocess.Popen`. Raises
    SandboxUnavailable, before anything runs, when the requested sandbox cannot run here."""
    problem = sandbox_problem(run.sandbox_profile, run.proxy_url, run.netjail)
    if problem:
        raise SandboxUnavailable(f"sandbox: {problem}; no agent runs without it")
    argv, env, cwd = command_for(run)
    if launch is None and not shutil.which(argv[0]) and not Path(argv[0]).is_file():
        raise FileNotFoundError(f"{argv[0]} is not installed (run `tauceti-roadmap doctor`)")
    run.log_out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    events: list[dict] = []
    with open(run.log_out, "a", encoding="utf-8") as out, open(run.log_err, "a", encoding="utf-8") as err, \
            open(events_path(run.log_out), "a", encoding="utf-8") as raw:
        out.write(f"START {stamp()} {run.agent} {run.model} {cwd}\n")
        out.flush()
        timed_out = False
        # Its own process group, so that a timeout or an interrupt stops the agent's children too.
        proc = (launch or subprocess.Popen)(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                            text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
                                            start_new_session=True)
        feeder = threading.Thread(target=_feed, args=(proc.stdin, run.brief), daemon=True)
        reader = threading.Thread(target=_read_events, args=(proc.stdout, run.agent, out, raw, events), daemon=True)
        feeder.start()
        reader.start()
        try:
            code = proc.wait(timeout=run.timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            code, timed_out = 124, True
        except BaseException:              # Ctrl-C or a stop signal: the agent goes too, then the round ends
            _kill_group(proc)
            raise
        finally:
            reader.join(timeout=60)
            try:
                proc.stdout.close()        # after the reader: a child that kept the pipe cannot hold it open
            except (OSError, ValueError, AttributeError):
                pass
        if timed_out:
            out.write("\nwall-clock timeout\n")
        usage = usage_from_events(run.agent, events)
        if usage:
            out.write(f"USAGE {json.dumps(usage, sort_keys=True)}\n")
        out.write(f"EXIT {code} {stamp()}\n")
    result = classify(code, _text(run.log_out), _text(run.log_err), time.time() - started, timed_out)
    result.usage = usage
    return result


def _kill_group(proc) -> None:
    """Kill the agent and every process it started (its process group), then reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, TypeError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def _feed(stdin, brief: str) -> None:
    """Write the brief to the agent's stdin and close it (in a thread: a large brief must not block
    the reader)."""
    try:
        stdin.write(brief)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def _read_events(stream, agent: str, out, raw, events: list[dict]) -> None:
    """Read the agent's stdout line by line: keep each line raw, render it into the log, and keep the
    parsed events for the usage. A line that is not JSON is logged as it is."""
    for line in stream:
        raw.write(line)
        raw.flush()
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            if line.strip():
                out.write(line if line.endswith("\n") else line + "\n")
                out.flush()
            continue
        events.append(event)
        for text in render_event(agent, event):
            out.write(text.rstrip("\n") + "\n")
        out.flush()


TOOL_SUMMARY = 200      # characters of a tool call's input shown in the log


def _short(value) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= TOOL_SUMMARY else text[:TOOL_SUMMARY - 1] + "…"


def render_event(agent: str, event: dict) -> list[str]:
    """The readable lines of one streamed event: the agent's messages, the tools and commands it
    runs, and any error the CLI reports (where usage-limit messages arrive). Tool output, thinking
    and bookkeeping events are not shown."""
    kind = event.get("type")
    if agent == "claude":
        if kind == "assistant":
            lines = []
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    lines.append(block["text"])
                elif block.get("type") == "tool_use":
                    data = block.get("input") or {}
                    what = next((data[k] for k in ("command", "file_path", "path", "pattern", "url", "query",
                                                   "description") if data.get(k)), "")
                    tool = block.get("name", "tool")
                    lines.append(f"→ {tool}: {_short(what)}" if what else f"→ {tool}")
            return lines
        if kind == "result" and (event.get("is_error") or event.get("subtype") not in (None, "success")):
            return [str(event.get("result") or event.get("subtype") or "error")]
        return []
    if kind in ("item.started", "item.completed"):
        item = event.get("item") or {}
        itype = item.get("type")
        if itype == "agent_message" and kind == "item.completed":
            return [str(item.get("text") or "")]
        if itype == "command_execution":
            if kind == "item.started":
                return [f"$ {_short(item.get('command', ''))}"]
            code = item.get("exit_code")
            return [f"  (exit {code})"] if code not in (None, 0) else []
        if itype == "error":
            return [str(item.get("message") or "error")]
        if itype == "file_change" and kind == "item.completed":
            paths = [c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict)]
            return [f"edited {', '.join(p for p in paths if p)}"] if paths else []
        return []
    if kind == "error":
        return [str(event.get("message") or "error")]
    if kind == "turn.failed":
        error = event.get("error") or {}
        return [str(error.get("message") if isinstance(error, dict) else error)]
    return []


USAGE_KEYS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "thinking_tokens")


def usage_from_events(agent: str, events: list[dict]) -> dict:
    """A run's token usage from its events, in one shape for both CLIs:

    * `input_tokens`: input read without the cache; `cache_read_tokens`, `cache_write_tokens`: input
      read from and written to the prompt cache;
    * `output_tokens`: tokens generated, thinking included; `thinking_tokens`: the thinking part;
    * `cost_usd` (Claude only: the CLI's own figure), `turns` (Claude only).

    Claude reports the run's totals in its final `result` event (summed over models, subagents
    included, from `modelUsage` when present); Codex reports each turn's usage in `turn.completed`,
    with `input_tokens` counting the cached part too. {} when the events carry no usage."""
    if agent == "claude":
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if not result:
            return {}
        out = dict.fromkeys(USAGE_KEYS, 0)
        models = result.get("modelUsage") or {}
        if isinstance(models, dict) and models:
            for m in models.values():
                if not isinstance(m, dict):
                    continue
                out["input_tokens"] += int(m.get("inputTokens") or 0)
                out["cache_read_tokens"] += int(m.get("cacheReadInputTokens") or 0)
                out["cache_write_tokens"] += int(m.get("cacheCreationInputTokens") or 0)
                out["output_tokens"] += int(m.get("outputTokens") or 0)
                out["thinking_tokens"] += int(m.get("thinkingTokens") or 0)
            out["models"] = sorted(str(k) for k in models)
        else:
            u = result.get("usage") or {}
            out["input_tokens"] = int(u.get("input_tokens") or 0)
            out["cache_read_tokens"] = int(u.get("cache_read_input_tokens") or 0)
            out["cache_write_tokens"] = int(u.get("cache_creation_input_tokens") or 0)
            out["output_tokens"] = int(u.get("output_tokens") or 0)
            out["thinking_tokens"] = int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        if result.get("total_cost_usd") is not None:
            out["cost_usd"] = round(float(result["total_cost_usd"]), 6)
        if result.get("num_turns") is not None:
            out["turns"] = int(result["num_turns"])
        return out
    turns = [e.get("usage") or {} for e in events if e.get("type") == "turn.completed"]
    if not turns:
        return {}
    out = dict.fromkeys(USAGE_KEYS, 0)
    for u in turns:
        cached = int(u.get("cached_input_tokens") or 0)
        out["input_tokens"] += max(int(u.get("input_tokens") or 0) - cached, 0)
        out["cache_read_tokens"] += cached
        out["cache_write_tokens"] += int(u.get("cache_write_input_tokens") or 0)
        out["output_tokens"] += int(u.get("output_tokens") or 0)
        out["thinking_tokens"] += int(u.get("reasoning_output_tokens") or 0)
    return out


def classify(code: int, out: str, err: str, seconds: float = 0.0, timed_out: bool = False) -> AgentResult:
    """The result of a run from its exit code and its whole stdout and stderr. A usage limit is
    recognised first, wherever its message appears and whatever the exit code (the Claude CLI
    prints it on stdout and exits 1 after any amount of work; Codex on stderr), so a limited run is
    never a plain failure; its reset time, when the message gives one, is the pause's end."""
    found = limit_message(out, err, code)
    if found:
        line, context = found
        reset_at = reset_time(context)
        if reset_at is None and MOMENTARY.search(line):
            reset_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=MOMENTARY_PAUSE)
        return AgentResult(exit_code=code, seconds=seconds, limited=True, timed_out=timed_out,
                           reset_at=reset_at, message=line)
    tail = out[-6000:] + err[-6000:]
    return AgentResult(exit_code=code, seconds=seconds, timed_out=timed_out,
                       transient=code != 0 and bool(TRANSIENT.search(tail)))


def limit_message(out: str, err: str, code: int) -> tuple[str, str] | None:
    """The usage-limit message in a run's output, if any: (its line, cut to MESSAGE_WIDTH, and the
    text from the match on, for the reset time). Stdout first, then stderr; the whole of each."""
    pattern = LIMIT_ON_FAILURE if code != 0 else LIMIT_ALWAYS
    for text in (out, err):
        m = pattern.search(text)
        if not m:
            continue
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        line = text[start:end if end >= 0 else None].strip()
        if len(line) > MESSAGE_WIDTH:
            line = line[:MESSAGE_WIDTH - 1] + "…"
        return line, text[start:m.end() + RESET_WINDOW]
    return None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def reset_time(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """When a usage limit resets, from the CLI's message: an absolute time ("resets 3pm
    (Europe/Paris)", "resets Oct 21, 9am", "try again at Oct 27th, 2026 6:34 AM", "until 15:30
    UTC"), a duration ("try again in 2 hours 30 minutes", "retry after 30 seconds") or an ISO stamp.
    The last hint in the text wins. A clock time without a zone is read in the host's local zone."""
    now = now or dt.datetime.now(dt.timezone.utc)
    hints = [(m.start(), _absolute(m, now)) for m in RESET.finditer(text)]
    hints += [(m.start(), now + _duration(m.group("dur"))) for m in RESET_IN.finditer(text)]
    hints += [(m.start(), _iso(m.group("iso"), now)) for m in RESET_ISO.finditer(text)]
    hints = [(pos, when) for pos, when in hints if when is not None]
    return max(hints)[1] if hints else None


def _absolute(m: re.Match, now: dt.datetime) -> dt.datetime | None:
    tz = None
    if m.group("tz"):
        try:
            tz = ZoneInfo(m.group("tz").strip())
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    elif m.group("utc"):
        tz = dt.timezone.utc
    local = now.astimezone(tz)
    if m.group("ampm"):
        hour = int(m.group("hour")) % 12 + (12 if m.group("ampm").lower() == "p" else 0)
        minute = int(m.group("min") or 0)
    else:
        hour, minute = int(m.group("hour24")), int(m.group("min24"))
    if hour > 23:
        return None
    if m.group("mon"):
        month = dt.datetime.strptime(m.group("mon")[:3].title(), "%b").month
        year = int(m.group("year")) if m.group("year") else local.year
        try:
            candidate = local.replace(year=year, month=month, day=int(m.group("day")), hour=hour, minute=minute,
                                      second=0, microsecond=0)
        except ValueError:
            return None
        if not m.group("year") and candidate < local - dt.timedelta(days=1):
            candidate = candidate.replace(year=candidate.year + 1)
    else:
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += dt.timedelta(days=1)
    return candidate


def _duration(text: str) -> dt.timedelta:
    seconds = 0
    for amount, unit in re.findall(r"(\d+)\s*([a-z]+)", text, re.I):
        unit = unit.lower()
        seconds += int(amount) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    return dt.timedelta(seconds=seconds)


def _iso(text: str, now: dt.datetime) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=now.astimezone().tzinfo)
