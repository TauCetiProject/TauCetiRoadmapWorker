"""The worker round: pick the most useful eligible job, take its lease, run its lane, release.

Order: KIND_PRIORITY (humans waiting on a pull request first, then work in flight, then new work),
then the roadmap's rank in the merge order (bottom-up), then the oldest issue. A lease is renewed
by a heartbeat while the agent runs, so a long run is not mistaken for a dead one; it is released
when the round ends, whatever the outcome (a submitted job is then held by its pull request).
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import json
from pathlib import Path

from . import claims
from .board import KIND_PRIORITY, Job
from .gh import GHError
from .lanes import Context, LaneError, LaneResult, lane_for

HEARTBEAT = 300
MAX_ATTEMPTS = 3


def attempts_path(ctx: Context) -> Path:
    return ctx.wdir / "attempts.json"


def failures(ctx: Context) -> dict[str, int]:
    try:
        return json.loads(attempts_path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_failure(ctx: Context, job_id: str) -> int:
    data = failures(ctx)
    data[job_id] = data.get(job_id, 0) + 1
    attempts_path(ctx).parent.mkdir(parents=True, exist_ok=True)
    attempts_path(ctx).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return data[job_id]


def rank(jobs: list[Job], order: list[str]) -> list[Job]:
    pos = {n: i for i, n in enumerate(order)}
    return sorted(jobs, key=lambda j: (KIND_PRIORITY.get(j.kind, 9), pos.get(j.name, len(pos)), j.number))


class Heartbeat:
    def __init__(self, ctx: Context, key: str):
        self.ctx, self.key, self.stop = ctx, key, threading.Event()
        self.thread = threading.Thread(target=self._beat, daemon=True)

    def _beat(self):
        while not self.stop.wait(HEARTBEAT):
            if self.ctx.claims:
                self.ctx.claims.renew(self.key)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()


def work_once(ctx: Context, only: tuple[str, ...] | None, job_id: str = "", order: list[str] | None = None,
              log=print) -> LaneResult:
    kinds = tuple(only) if only else None
    jobs = ctx.board.list_jobs(("available",), kinds)
    if job_id:
        jobs = [j for j in jobs if j.id == job_id]
    failed_before = failures(ctx)
    for job in rank(jobs, order or []):
        if failed_before.get(job.id, 0) >= MAX_ATTEMPTS:
            log(f"skip {job.id}: failed {MAX_ATTEMPTS} times on this worker (see its jobs/ folder)")
            continue
        lane = lane_for(job)
        reason = lane.eligible(job, ctx)
        if reason:
            log(f"skip {job.id}: {reason}")
            continue
        key = claims.job_key(ctx.cfg.project.name, job.id)
        status = ctx.claims.acquire(key) if ctx.claims and not ctx.dry_run else claims.ERROR
        if status == claims.HELD:
            log(f"skip {job.id}: another worker holds it")
            continue
        if status == claims.ERROR and ctx.claims and not ctx.dry_run:
            log(f"{job.id}: the lease could not be registered; proceeding unclaimed (fail-open)")
        if not ctx.dry_run:
            try:
                ctx.board.set_state(job, "claimed", f"Claimed for `{job.id}` by worker `{ctx.worker_id}` "
                                                    f"(@{ctx.operator}, {ctx.agent}).")
            except GHError:
                pass  # no write access to the board: the lease is the claim; `board sync` mirrors it
        log(f"start {job.id} ({lane.__class__.__name__}, {ctx.agent} {ctx.model})")
        try:
            with Heartbeat(ctx, key):
                result = lane.run(job, ctx)
        except (LaneError, OSError) as exc:
            result = LaneResult(job.id, "failed", str(exc))
        finally:
            if ctx.claims and not ctx.dry_run:
                ctx.claims.release(key)
        if result.status == "failed" and not ctx.dry_run:
            record_failure(ctx, job.id)
        if result.status in ("failed", "limited", "skipped") and not ctx.dry_run:
            try:
                ctx.board.set_state(job, "available", f"Released by worker `{ctx.worker_id}`: {result.status}"
                                                      f" ({result.detail[:300]}).")
            except GHError:
                pass
        return result
    return LaneResult("", "idle", "no eligible job")


def work_loop(ctx: Context, only, order_fn, idle_sleep: int = 600, log=print) -> None:
    """Rounds until interrupted: sleep through usage limits and idle periods."""
    while True:
        result = work_once(ctx, only, order=order_fn(), log=log)
        log(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ} {result.job or '-'} {result.status}: {result.detail}")
        if result.status == "limited":
            wake = result.retry_at
            delay = max(300, (wake - dt.datetime.now(wake.tzinfo)).total_seconds() + 300) if wake else 1800
            time.sleep(delay)
        elif result.status == "idle":
            time.sleep(idle_sleep)
        else:
            time.sleep(20)


__all__ = ["work_once", "work_loop", "rank"]
