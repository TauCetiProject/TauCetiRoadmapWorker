"""Token usage per agent run, recorded where the run happened, and summed by `tauceti-roadmap usage`.

Every agent run of a lane appends one JSON line to `jobs/<JOB>/usage.jsonl` and to the worker's
`usage.jsonl` (`workers/<id>/usage.jsonl` in the state directory): when, which job, kind and lane,
which run of the job (`run-2-repair`), the agent and model, the seconds it took, how it ended, the
characters of its brief, and the tokens the CLI reported (`agents.usage_from_events`). A run whose CLI
reported nothing still gets its line, with no token fields, so runs are counted all the same.

The point is to measure what each lane and each check costs, instead of estimating it: input read
from the cache dominates long agent runs (every turn re-reads the context), output includes the
thinking, and only the Claude CLI reports a cost of its own.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

from .agents import USAGE_KEYS

LOG = "usage.jsonl"
TOKEN_FIELDS = USAGE_KEYS + ("cost_usd",)
GROUPS = ("lane", "kind", "job", "agent", "model", "worker", "day", "label")


def entry(*, job: str, kind: str, lane: str, run: str, label: str, agent: str, model: str, worker: str, result,
          brief_chars: int = 0, at: dt.datetime | None = None) -> dict:
    """The record of one agent run."""
    record = {"at": (at or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"), "job": job, "kind": kind,
              "lane": lane, "run": run, "label": label or "main", "agent": agent, "model": model, "worker": worker,
              "seconds": round(float(getattr(result, "seconds", 0.0)), 1), "exit": getattr(result, "exit_code", None),
              "limited": bool(getattr(result, "limited", False)), "timed_out": bool(getattr(result, "timed_out", False)),
              "brief_chars": brief_chars}
    record.update(getattr(result, "usage", None) or {})
    return record


def record(wdir: Path, job_dir: Path, rec: dict) -> None:
    """Append the record to the job's and the worker's usage logs. Best effort: a full disk loses the
    record, never the round."""
    line = json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n"
    for path in (Path(job_dir) / LOG, Path(wdir) / LOG):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def load(state: Path, worker: str = "") -> list[dict]:
    """Every record in the state directory's worker logs (one worker's, with `worker`)."""
    pattern = f"workers/{worker}/{LOG}" if worker else f"workers/*/{LOG}"
    out = []
    for path in sorted(Path(state).glob(pattern)):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _key(rec: dict, by: str) -> str:
    if by == "day":
        return str(rec.get("at", ""))[:10] or "?"
    return str(rec.get(by) or "?")


def summarize(records: list[dict], by: str = "lane", since: str = "") -> list[dict]:
    """Sums per group: runs, hours, each token field, the cost the CLI reported, and how many runs
    reported usage at all; sorted by output tokens, largest first."""
    groups: dict[str, dict] = defaultdict(lambda: {"runs": 0, "reported": 0, "seconds": 0.0,
                                                   **dict.fromkeys(TOKEN_FIELDS, 0)})
    for rec in records:
        if since and str(rec.get("at", "")) < since:
            continue
        g = groups[_key(rec, by)]
        g["runs"] += 1
        g["seconds"] += float(rec.get("seconds") or 0)
        if any(k in rec for k in USAGE_KEYS):
            g["reported"] += 1
        for k in TOKEN_FIELDS:
            g[k] += rec.get(k) or 0
    rows = [{by: name, **values} for name, values in groups.items()]
    return sorted(rows, key=lambda r: (-r["output_tokens"], -r["runs"], str(r[by])))


def _m(n: float) -> str:
    """Tokens in millions with three decimals (`1.234M`), or `-` for none."""
    return f"{n / 1e6:.3f}M" if n else "-"


def table(rows: list[dict], by: str) -> str:
    """The summary as an aligned text table."""
    head = (by, "runs", "with usage", "hours", "input", "cache read", "cache write", "output", "thinking", "cost $")
    body = [(str(r[by]), str(r["runs"]), str(r["reported"]), f"{r['seconds'] / 3600:.1f}", _m(r["input_tokens"]),
             _m(r["cache_read_tokens"]), _m(r["cache_write_tokens"]), _m(r["output_tokens"]), _m(r["thinking_tokens"]),
             f"{r['cost_usd']:.2f}" if r["cost_usd"] else "-") for r in rows]
    widths = [max(len(row[i]) for row in [head, *body]) for i in range(len(head))]
    fmt = "  ".join("{:<%d}" % widths[0] if i == 0 else "{:>%d}" % w for i, w in enumerate(widths))
    return "\n".join(fmt.format(*row) for row in [head, *body])
