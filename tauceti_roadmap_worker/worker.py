"""The worker round: pick the most useful eligible job, take its lease, run its lane, release.

Order: KIND_PRIORITY (humans waiting on a pull request first, then work in flight, then new work),
then the roadmap's rank in the merge order (bottom-up), then the oldest issue. A lease is renewed
by a heartbeat while the agent runs, so a long run is not mistaken for a dead one; it is released
when the round ends, whatever the outcome (a submitted job is then held by its pull request, a
parked one by its request). A round that stops before either puts the job back to `state:available`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import shutil
import threading
import time

import json
from pathlib import Path

from . import claims
from .board import KIND_PRIORITY, Job
from .gh import GHError
from .lanes import Context, LaneResult, lane_for

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


def for_job(ctx: Context, job: Job) -> Context | None:
    """The context a job runs in: the operator's pinned agent, or (unpinned) the agent the job
    requires or its lane's configured agent, if that agent is installed here."""
    if ctx.agent_fixed:
        return ctx
    agent, model = ctx.cfg.lane_agent(job.kind)
    if job.requires_agent and job.requires_agent != agent:
        agent, model = job.requires_agent, ctx.cfg.model_for(job.requires_agent)
    if agent == ctx.agent and model == ctx.model:
        return ctx
    if not shutil.which(agent):
        return None
    return dataclasses.replace(ctx, agent=agent, model=model)


def work_once(base: Context, only: tuple[str, ...] | None, job_id: str = "", order: list[str] | None = None,
              log=print) -> LaneResult:
    jobs = base.board.list_jobs(("available",), tuple(only) if only else None)
    if job_id:
        jobs = [j for j in jobs if j.id == job_id]
    failed_before = failures(base)
    for job in rank(jobs, order or []):
        if failed_before.get(job.id, 0) >= MAX_ATTEMPTS:
            log(f"skip {job.id}: failed {MAX_ATTEMPTS} times on this worker (see its jobs/ folder)")
            continue
        ctx = for_job(base, job)
        if ctx is None:
            log(f"skip {job.id}: its agent is not installed here")
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
        return run_job(ctx, job, lane, key, log)
    return LaneResult("", "idle", "no eligible job")


# Outcomes after which the job goes back to `state:available`: the lane stopped before it submitted
# or parked the job (a submission holds it as `submitted`, a request as `needs-references`).
RELEASED = ("failed", "limited", "skipped", "interrupted")


def run_job(ctx: Context, job: Job, lane, key: str, log=print) -> LaneResult:
    """Claim, run the lane, and end the round in one place: the lease is always released, and every
    way a lane can stop before it submits (a failure it reports, an exception from git, GitHub or
    anything else, a usage limit, an interrupt) goes through `release`."""
    if not ctx.dry_run:
        try:
            ctx.board.set_state(job, "claimed", f"Claimed for `{job.id}` by worker `{ctx.worker_id}` "
                                                f"(@{ctx.operator}, {ctx.agent}).")
        except GHError:
            pass  # no write access to the board: the lease is the claim; `board sync` mirrors it
    log(f"start {job.id} ({lane.__class__.__name__}, {ctx.agent} {ctx.model})")
    interrupt = None
    try:
        with Heartbeat(ctx, key):
            result = lane.run(job, ctx)
    except Exception as exc:  # noqa: BLE001 - git, GitHub, the lane's own errors or a bug: all release the job
        result = LaneResult(job.id, "failed", f"{type(exc).__name__}: {exc}")
    except BaseException as exc:  # noqa: BLE001 - Ctrl-C or SystemExit: release the job, then stop
        interrupt, result = exc, LaneResult(job.id, "interrupted", type(exc).__name__)
    finally:
        if ctx.claims and not ctx.dry_run:
            ctx.claims.release(key)
    release(ctx, job, result)
    if interrupt is not None:
        raise interrupt
    return result


def release(ctx: Context, job: Job, result: LaneResult) -> None:
    """End a round that did not submit or park the job: count a failure, and put the job back to
    `state:available` (the only state label it keeps)."""
    if ctx.dry_run or result.status not in RELEASED:
        return
    if result.status == "failed":
        record_failure(ctx, job.id)
    try:
        ctx.board.set_state(job, "available", f"Released by worker `{ctx.worker_id}`: {result.status}"
                                              f" ({result.detail[:300]}).")
    except GHError:
        pass  # no write access to the board: `board stale --release` frees the claim later


QUICK_POLL = 60     # seconds: the latest first poll after a failure or after a parked job is answered
AFTER_ROUND = 20    # seconds between a round that did something and the next


def work_loop(ctx: Context, only, order_fn, idle_sleep: int | None = None, log=print, sleep=time.sleep) -> None:
    """Rounds until interrupted: sleep through usage limits and idle periods. An idle worker sleeps
    `[worker] idle_seconds`, except that the next poll comes within QUICK_POLL seconds after a
    failure (the job was released and may be due again), and after the board answers a job this
    worker parked for references (looked up every QUICK_POLL seconds while one waits)."""
    idle = idle_sleep or ctx.cfg.worker.idle_seconds
    parked: set[str] = set()     # jobs this worker parked, until the board answers them
    quick = False                # the next idle wait is short
    while True:
        result = work_once(ctx, only, order=order_fn(), log=log)
        log(f"{now():%Y-%m-%dT%H:%MZ} {result.job or '-'} {result.status}: {result.detail}")
        if result.status == "needs-references":
            parked.add(result.job)
        else:
            parked.discard(result.job)
        if result.status == "limited":
            wake = result.retry_at
            sleep(max(300, (wake - dt.datetime.now(wake.tzinfo)).total_seconds() + 300) if wake else 1800)
        elif result.status == "idle" and quick:
            quick = False
            sleep(min(QUICK_POLL, idle))
        elif result.status == "idle":
            answered = wait_idle(ctx, idle, parked, sleep)
            if answered:
                log(f"{now():%Y-%m-%dT%H:%MZ} {', '.join(sorted(answered))} answered on the board: polling now")
            quick = bool(answered)
        else:
            quick = result.status == "failed"    # the job was released: if the next poll misses it, retry soon
            sleep(AFTER_ROUND)


def wait_idle(ctx: Context, seconds: float, parked: set[str], sleep=time.sleep) -> set[str]:
    """Sleep up to `seconds`. While jobs this worker parked are waiting for references, look every
    QUICK_POLL seconds whether one has left `state:needs-references` (the board answered it): then
    stop early, forget those jobs and return them."""
    left = seconds
    while left > 0:
        step = min(left, QUICK_POLL) if parked else left
        sleep(step)
        left -= step
        if not parked:
            continue
        try:
            waiting = {job.id for job in ctx.board.list_jobs(("needs-references",))}
        except GHError:
            continue
        answered = parked - waiting
        if answered:
            parked -= answered
            return answered
    return set()


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


__all__ = ["work_once", "work_loop", "rank"]
