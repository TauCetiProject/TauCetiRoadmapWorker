"""Run the operator's agent on a brief: Codex or Claude, optionally sandboxed.

Port of the maintainer's `agentrun` (dispatch by agent) and `jailrun` (network jail with a proxy
fallback), with the usage-limit handling of the explorer's lane.py. The brief goes to the agent on
stdin; stdout and stderr go to log files that start with a START line and end with an EXIT line.

The agent runs with a minimal environment: HOME, PATH, locale and terminal settings, the agent's
own config directory (CODEX_HOME / CLAUDE_CONFIG_DIR) and its API key if one is set. GitHub tokens
are never passed to an agent; the worker itself does every GitHub write.

Sandbox profiles (`[sandbox] profile`): "off" (default) runs on the host; "proxy" points the agent's
HTTP(S)_PROXY at the allow-everything-and-log proxy (bundled sandbox/allowproxy.py); "netjail" runs
the agent in a private network namespace whose only route out is that proxy, and falls back to
"proxy" when the jail cannot start. Claude agents also get a PreToolUse hook (sandbox/url-guard.py)
that refuses to execute fetched content, raw network tools, proxy changes and credential access.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
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


def netjail_usable(binary: str) -> bool:
    if not binary or not Path(binary).is_file():
        return False
    try:
        return subprocess.run([binary, "/usr/bin/true"], capture_output=True, timeout=20,
                              env={**os.environ, "NETJAIL_FORWARDER": str(SANDBOX / "nsforward.py")}).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def command_for(run: AgentRun, base_env: dict | None = None) -> tuple[list[str], dict, Path]:
    """(argv, environment, cwd) for one agent run. Pure apart from writing the Claude settings file."""
    env = agent_env(run.agent, base_env, run.extra_env)
    if run.agent == "codex":
        argv = ["codex", "exec", "-m", run.model, "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", "-C", str(run.workdir), "-"]
    elif run.agent == "claude":
        argv = ["claude", "-p", "--model", run.model, "--dangerously-skip-permissions", "--output-format", "text"]
        if run.claude_hook and run.settings_dir:
            argv += ["--settings", str(claude_settings(run.settings_dir))]
    else:
        raise ValueError(f"unknown agent {run.agent!r}")
    if run.sandbox_profile == "netjail" and netjail_usable(run.netjail):
        env["NETJAIL_FORWARDER"] = str(SANDBOX / "nsforward.py")
        argv = [run.netjail, *argv]
    elif run.sandbox_profile in ("proxy", "netjail"):
        env.update(proxy_env(run.proxy_url))
    return argv, env, run.workdir


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_agent(run: AgentRun) -> AgentResult:
    argv, env, cwd = command_for(run)
    if not shutil.which(argv[0]) and not Path(argv[0]).is_file():
        raise FileNotFoundError(f"{argv[0]} is not installed (run `tauceti-roadmap doctor`)")
    run.log_out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(run.log_out, "a", encoding="utf-8") as out, open(run.log_err, "a", encoding="utf-8") as err:
        out.write(f"START {stamp()} {run.agent} {run.model} {cwd}\n")
        out.flush()
        timed_out = False
        try:
            proc = subprocess.run(argv, input=run.brief, stdout=out, stderr=err, text=True, env=env, cwd=cwd,
                                  timeout=run.timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code, timed_out = 124, True
            out.write("\nwall-clock timeout\n")
        out.write(f"EXIT {code} {stamp()}\n")
    return classify(code, _text(run.log_out), _text(run.log_err), time.time() - started, timed_out)


def classify(code: int, out: str, err: str, seconds: float = 0.0, timed_out: bool = False) -> AgentResult:
    """The result of a run from its exit code and its whole stdout and stderr. A usage limit is
    recognised first, wherever its message appears and whatever the exit code (the Claude CLI
    prints it on stdout and exits 1 after any amount of work; Codex on stderr), so a limited run is
    never a plain failure; its reset time, when the message gives one, is the pause's end."""
    found = limit_message(out, err, code)
    if found:
        line, context = found
        return AgentResult(exit_code=code, seconds=seconds, limited=True, timed_out=timed_out,
                           reset_at=reset_time(context), message=line)
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
