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

LIMIT = re.compile(r"hit your [\w ]*limit|usage limit|limit will reset|reached your [\w ]*limit|rate.limit|"
                   r"Credit balance is too low|overloaded_error|API Error: (?:429|5\d\d)|model (?:is )?at capacity", re.I)
TRANSIENT = re.compile(r"Request timed out|ECONNRESET|socket hang up|fetch failed|Connection error", re.I)
RESET = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                   r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)\s*(?:\((?P<tz>[^)]+)\))?", re.I)
PASS_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SSL_CERT_FILE", "ELAN_HOME",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
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
    tail = _tail(run.log_out) + _tail(run.log_err)
    limited = code != 0 and bool(LIMIT.search(tail))
    return AgentResult(exit_code=code, seconds=time.time() - started, limited=limited,
                       transient=code != 0 and not limited and bool(TRANSIENT.search(tail)), timed_out=timed_out,
                       reset_at=reset_time(tail) if limited else None)


def _tail(path: Path, size: int = 6000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-size:]
    except OSError:
        return ""


def reset_time(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """When a usage limit resets, from the CLI's message ("resets 3pm (Europe/Paris)", "resets Oct 21,
    9am"). A message without a time zone is read in the host's local zone."""
    matches = list(RESET.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    try:
        tz = ZoneInfo(m.group("tz").strip()) if m.group("tz") else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    hour = int(m.group("hour")) % 12 + (12 if m.group("ampm").lower() == "pm" else 0)
    minute = int(m.group("min") or 0)
    if m.group("mon"):
        month = dt.datetime.strptime(m.group("mon"), "%b").month
        candidate = now.replace(month=month, day=int(m.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < now - dt.timedelta(days=1):
            candidate = candidate.replace(year=candidate.year + 1)
    else:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
    return candidate
