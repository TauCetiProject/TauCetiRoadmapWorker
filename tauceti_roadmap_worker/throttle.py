"""A host-wide throttle on GitHub content writes.

GitHub's secondary rate limit blocks an account from creating content (comments, issues, pull requests,
labels, merges) after bursts of roughly eighty writes a minute or five hundred an hour. Every worker,
timer and script on a host writes as the same operator, so no process can keep the account under the
limit alone. The throttle is a token bucket in one file under the project's state directory, shared by
every process that writes through `GH` (and by scripts, through `tauceti-roadmap throttle`): a write
takes a slot, and a process whose slot is not free sleeps until it is.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

MINUTE, HOUR = 60.0, 3600.0


class WriteThrottle:
    def __init__(self, path: Path, per_minute: int, per_hour: int, clock=time.time, sleep=time.sleep):
        self.path, self.per_minute, self.per_hour, self.clock, self.sleep = Path(path), per_minute, per_hour, clock, sleep
        self.waited = 0.0

    def _slot(self) -> float:
        """Take a slot now, returning 0, else the seconds until one frees up (nothing taken)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    stamps = [float(t) for t in json.loads(f.read() or "[]")]
                except ValueError:
                    stamps = []
                now = self.clock()
                stamps = [t for t in stamps if now - t < HOUR]
                recent = [t for t in stamps if now - t < MINUTE]
                wait = 0.0
                if self.per_minute and len(recent) >= self.per_minute:
                    wait = max(wait, MINUTE - (now - recent[0]))
                if self.per_hour and len(stamps) >= self.per_hour:
                    wait = max(wait, HOUR - (now - stamps[0]))
                if wait <= 0:
                    stamps.append(now)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(stamps))
                f.flush()
                return max(wait, 0.0)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def wait(self) -> float:
        """Block until a write slot is taken; returns the seconds slept."""
        if not self.per_minute and not self.per_hour:
            return 0.0
        slept = 0.0
        while True:
            wait = self._slot()
            if wait <= 0:
                self.waited += slept
                return slept
            step = min(wait, MINUTE) + 0.05       # in short steps: another process may free a slot sooner
            self.sleep(step)
            slept += step


def for_config(cfg) -> WriteThrottle | None:
    """The host's throttle for a configuration, or None when both rates are 0."""
    w = cfg.worker
    if not w.writes_per_minute and not w.writes_per_hour:
        return None
    return WriteThrottle(cfg.state_dir() / "gh-writes.json", w.writes_per_minute, w.writes_per_hour)
