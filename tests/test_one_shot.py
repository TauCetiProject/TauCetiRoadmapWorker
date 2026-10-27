"""A one-shot `work` round's exit status names its outcome, and `--status-file` records it as JSON,
so a supervisor that starts one round at a time tells waiting from failing without reading the log."""

import datetime as dt
import json
from types import SimpleNamespace

from tauceti_roadmap_worker import cli, usage
from tauceti_roadmap_worker.lanes import LaneResult

BEGAN = dt.datetime(2026, 10, 25, 5, 0, tzinfo=dt.UTC)


def test_every_outcome_has_its_exit_status():
    args = SimpleNamespace(status_file="")
    codes = {status: cli.round_end(args, None, LaneResult("X", status), BEGAN)
             for status in ("submitted", "needs-references", "blocked", "idle", "skipped", "limited",
                            "github-limited", "halted", "failed", "setup-failed", "interrupted")}
    assert codes == {"submitted": 0, "needs-references": 0, "blocked": 0, "idle": 75, "skipped": 75, "limited": 76,
                     "github-limited": 77, "halted": 78, "failed": 1, "setup-failed": 1, "interrupted": 130}


def test_the_status_file_carries_the_outcome_and_the_rounds_usage(tmp_path):
    ctx = SimpleNamespace(state=tmp_path, worker_id="w1")
    wdir = tmp_path / "workers" / "w1"
    for at, out in ((BEGAN - dt.timedelta(hours=1), 999), (BEGAN + dt.timedelta(minutes=5), 10),
                    (BEGAN + dt.timedelta(minutes=9), 32)):
        usage.record(wdir, tmp_path / "job", {"at": f"{at:%Y-%m-%dT%H:%M:%SZ}", "job": "RT-Foo~1b", "worker": "w1",
                                              "seconds": 60, "output_tokens": out, "input_tokens": 1})
    reset = BEGAN + dt.timedelta(hours=3)
    result = LaneResult("RT-Foo~1b", "limited", "usage limit", retry_at=reset, agent="claude", worked=540.0)
    path = tmp_path / "out" / "round.json"
    assert cli.round_end(SimpleNamespace(status_file=str(path)), ctx, result, BEGAN) == 76
    data = json.loads(path.read_text())
    assert (data["job"], data["stage"], data["status"], data["exit"]) == ("RT-Foo~1b", "RT", "limited", 76)
    assert data["retry_at"] == "2026-10-25T08:00:00Z" and data["agent"] == "claude" and data["worked_seconds"] == 540
    assert data["usage"]["output_tokens"] == 42 and data["usage"]["runs"] == 2   # the run before the round is not counted
