"""Watching the work: `tail` follows one worker, `status` lists the board.

* `tail --worker-id ID [--job JOB]` follows the worker's round log (`workers/<id>/rounds.log`, every
  line the worker prints, timestamped) and the live output of the agent on the current job (the
  newest `jobs/<JOB>/run-*.out`). Without `--job` it follows whichever job the latest `start` line
  of the round log names, and moves on when the worker starts another. Ctrl-C stops it.
* `status [--all]` lists the board's open jobs by state with their age (since the issue last
  changed) and, for claimed jobs, the lease's owner and how long it has held the job.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

from . import claims
from .board import STATES

START = re.compile(r"\bstart (\S+) \(")
ROUND_LOG = "rounds.log"


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def round_logger(wdir: Path, out=print):
    """A `log` for the worker loop: print the line and append it, timestamped, to the round log."""
    path = Path(wdir) / ROUND_LOG

    def log(line: str) -> None:
        out(line)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{now():%Y-%m-%dT%H:%M:%SZ} {line}\n")
        except OSError:
            pass
    return log


class Follower:
    """New lines of a growing file, from where the last read stopped (from the start again if the
    file was replaced or truncated)."""

    def __init__(self, path: Path, last: int = 20):
        self.path, self.offset, self.last = Path(path), None, last

    def read(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        with open(self.path, encoding="utf-8", errors="replace") as f:
            if self.offset is None:              # first look: the last few lines, like tail
                lines = f.read().splitlines()[-self.last:] if self.last else []
                self.offset = size
                return lines
            if size < self.offset:
                self.offset = 0
            f.seek(self.offset)
            text = f.read()
        complete, _, rest = text.rpartition("\n")
        self.offset = size - len(rest.encode("utf-8"))
        return complete.splitlines() if complete or text.endswith("\n") else []


def current_job(round_log: Path) -> str:
    """The job the latest `start` line of a round log names, or ''."""
    try:
        text = round_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = START.findall(text)
    return found[-1] if found else ""


def latest_run(job_dir: Path) -> Path | None:
    runs = sorted(Path(job_dir).glob("run-*.out"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def tail(wdir: Path, job: str = "", out=print, sleep=time.sleep, rounds: int = 0, last: int = 20) -> None:
    """Follow the round log and the current job's agent output until interrupted (or for `rounds`
    polls, for tests)."""
    rounds_log = Follower(Path(wdir) / ROUND_LOG, last)
    following, agent = "", None
    n = 0
    while True:
        for line in rounds_log.read():
            out(f"[round] {line}")
        target = job or current_job(Path(wdir) / ROUND_LOG)
        run = latest_run(Path(wdir) / "jobs" / claims.ref_safe(target)) if target else None
        if run and (target != following or agent is None or agent.path != run):
            following, agent = target, Follower(run, last)
            out(f"[tail] following {target}: {run}")
        for line in agent.read() if agent else []:
            out(f"[{following}] {line}")
        n += 1
        if rounds and n >= rounds:
            return
        sleep(1)


def age(stamp: str, at: dt.datetime | None = None) -> str:
    try:
        then = dt.datetime.fromisoformat(str(stamp))
    except ValueError:
        return "?"
    seconds = int(((at or now()) - then).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


def status(board, issues: list[dict], leases: dict[str, dict], project: str, at: dt.datetime | None = None,
           include_done: bool = False) -> str:
    """The board's jobs by state: number, id, age, and for claimed jobs the lease owner and its age."""
    rows: dict[str, list[tuple[str, ...]]] = {}
    for issue in issues:
        if issue.get("pull_request"):
            continue
        job = board._job_from_issue(issue)
        if not job or (job.state == "done" and not include_done):
            continue
        lease = leases.get(claims.job_key(project, job.id))
        who = ""
        if lease and claims.lease_live(lease, int((at or now()).timestamp())):
            held = dt.datetime.fromtimestamp(int(lease.get("acquired_at", 0)), dt.UTC).isoformat()
            who = f"{lease.get('owner', '?')} for {age(held, at)}"
        rows.setdefault(job.state, []).append((f"#{job.number}", job.id, age(issue.get("updated_at", ""), at), who))
    out = []
    for state in (*STATES, *sorted(set(rows) - set(STATES))):
        if state not in rows:
            continue
        out.append(f"{state} ({len(rows[state])})")
        width = max(len(r[1]) for r in rows[state])
        for number, job_id, since, who in sorted(rows[state], key=lambda r: int(r[0][1:])):
            out.append(f"  {number:<7} {job_id:<{width}}  {since:>5}" + (f"  {who}" if who else ""))
    return "\n".join(out) or "no open jobs"
