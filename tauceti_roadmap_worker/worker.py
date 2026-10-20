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
from .lanes.base import LaneBlocked

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


def for_job(ctx: Context, job: Job) -> tuple[Context | None, str]:
    """(the context a job runs in, '') or (None, why this worker leaves it). The family is the
    operator's `--agent`, else the agent the job requires, else its lane's. The model is the one the
    job requires (a red-team pass), else the one its lane pins for that family (`[agents.lanes.<kind>]
    model`, `codex_model`, `claude_model`), else the family's default. A worker pinned with `--model`
    leaves a job that needs another model; one pinned with `--agent` only lets the lane pick it."""
    cfg = ctx.cfg
    agent = ctx.agent if ctx.agent_fixed else (job.requires_agent or cfg.lane_agent(job.kind)[0])
    if job.requires_agent and job.requires_agent != agent:
        return None, f"needs agent {job.requires_agent}"
    want = job.requires_model or cfg.lane_model(job.kind, agent)
    if ctx.model_fixed:
        if want and ctx.model != want:
            allowed = [job.requires_model] if job.requires_model else list(cfg.lane_models(job.kind).values())
            return None, f"{job.id} needs {' or '.join(dict.fromkeys(allowed))}; this worker is pinned to {ctx.model}"
        model = ctx.model
    else:
        model = want or (ctx.model if agent == ctx.agent else cfg.model_for(agent))
    if agent == ctx.agent and model == ctx.model:
        return ctx, ""
    if agent != ctx.agent and not shutil.which(agent):
        return None, "its agent is not installed here"
    return dataclasses.replace(ctx, agent=agent, model=model), ""


def work_once(base: Context, only: tuple[str, ...] | None, job_id: str = "", order: list[str] | None = None,
              log=print, avoid: set[str] | None = None) -> LaneResult:
    """One round. `avoid`: jobs this process already failed to set up (left alone, silently)."""
    jobs = base.board.list_jobs(("available",), tuple(only) if only else None)
    if job_id:
        jobs = [j for j in jobs if j.id == job_id]
    failed_before = failures(base)
    for job in rank(jobs, order or []):
        if avoid and job.id in avoid:
            continue
        if failed_before.get(job.id, 0) >= MAX_ATTEMPTS:
            log(f"skip {job.id}: failed {MAX_ATTEMPTS} times on this worker (see its jobs/ folder)")
            continue
        ctx, why = for_job(base, job)
        if ctx is None:
            log(f"skip {job.id}: {why}")
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
# `setup-failed` is an exception before any agent ran (a brief that does not render, a checkout):
# the operator's side, so it is not counted against the job, and this process leaves the job alone.
RELEASED = ("failed", "setup-failed", "limited", "skipped", "interrupted")


def run_job(ctx: Context, job: Job, lane, key: str, log=print) -> LaneResult:
    """Claim, run the lane, and end the round in one place: the lease is always released, and every
    way a lane can stop before it submits (a failure it reports, an exception from git, GitHub or
    anything else, a usage limit, an interrupt) goes through `release`."""
    interrupt = None
    try:
        if not ctx.dry_run:
            try:
                ctx.board.set_state(job, "claimed", f"Claimed for `{job.id}` by worker `{ctx.worker_id}` "
                                                    f"(@{ctx.operator}, {ctx.agent}).")
            except GHError:
                pass  # no write access to the board: the lease is the claim; `board sync` mirrors it
        log(f"start {job.id} ({lane.__class__.__name__}, {ctx.agent} {ctx.model})")
        with Heartbeat(ctx, key):
            result = lane.run(job, ctx)
    except LaneBlocked as exc:
        result = LaneResult(job.id, "blocked", str(exc))
    except Exception as exc:  # noqa: BLE001 - git, GitHub, the lane's own errors or a bug: all release the job
        status = "failed" if getattr(lane, "agent_started", True) else "setup-failed"
        result = LaneResult(job.id, status, f"{type(exc).__name__}: {exc}")
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
    `state:available` (the only state label it keeps); a job that needs a human is blocked."""
    if not ctx.dry_run and result.status == "blocked":
        try:
            ctx.board.set_state(job, "blocked", f"Blocked by worker `{ctx.worker_id}`: {result.detail[:600]} "
                                                "A human decides how to continue.")
        except GHError:
            pass
        return
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
PARKED_WATCH = 3 * 3600   # idle seconds a parked job is watched for its answer; then the idle poll finds it


def release_leftovers(ctx: Context, log=print) -> list[str]:
    """Leases this worker id still holds from a process that ended without releasing them (killed
    before it could, or crashed). A worker id runs one process at a time, so at startup they are
    released and their jobs put back to `state:available`. Returns the jobs released."""
    if not ctx.claims or ctx.dry_run:
        return []
    prefix = f"roadmap/{ctx.cfg.project.name}/job/"
    try:
        keys = ctx.claims.live_keys(prefix)
    except Exception:  # noqa: BLE001 - fail-open, like every lease operation
        return []
    mine = [k for k in keys if (ctx.claims.read(k) or {}).get("owner") == ctx.claims.owner]
    if not mine:
        return []
    claimed = {claims.job_key(ctx.cfg.project.name, j.id): j for j in ctx.board.list_jobs(("claimed",))}
    out = []
    for key in mine:
        ctx.claims.release(key)
        job = claimed.get(key)
        if job:
            try:
                ctx.board.set_state(job, "available", f"Released by worker `{ctx.worker_id}` at its start: its previous "
                                                      "process ended without releasing the job.")
            except GHError:
                pass
        out.append(job.id if job else key[len(prefix):])
        log(f"released {out[-1]}: left claimed by this worker's previous process")
    return out


def current_build() -> str:
    from .build import build_id
    return build_id()


def work_loop(ctx: Context, only, order_fn, idle_sleep: int | None = None, log=print, sleep=time.sleep,
              build: str = "") -> None:
    """Rounds until interrupted: sleep through usage limits and idle periods. An idle worker sleeps
    `[worker] idle_seconds`, except that the next poll comes within QUICK_POLL seconds after a
    failure (the job was released and may be due again), and after the board answers a job this
    worker parked for references (looked up every QUICK_POLL seconds of idle time, for at most
    PARKED_WATCH seconds per job). A board that cannot be read counts as an idle round. With `build`
    (the build it started on), every round boundary compares it with the installed build and
    returns, for a restart, when they differ."""
    idle = idle_sleep or ctx.cfg.worker.idle_seconds
    parked: dict[str, float] = {}   # jobs this worker parked -> idle seconds left to watch for the answer
    quick = False                    # the next idle wait is short
    avoid: set[str] = set()          # jobs this process failed to set up
    while True:
        if build:
            installed = current_build()
            if installed != build:
                log(f"{now():%Y-%m-%dT%H:%MZ} installed build changed ({build} → {installed}); exiting for restart")
                return
        try:
            result = work_once(ctx, only, order=order_fn(), log=log, avoid=avoid)
        except GHError as exc:
            result = LaneResult("", "idle", f"the board could not be read: {exc}")
        log(f"{now():%Y-%m-%dT%H:%MZ} {result.job or '-'} {result.status}: {result.detail}")
        if result.status == "needs-references":
            parked[result.job] = PARKED_WATCH
        else:
            parked.pop(result.job, None)
        if result.status == "setup-failed":
            avoid.add(result.job)
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


def wait_idle(ctx: Context, seconds: float, parked: dict[str, float], sleep=time.sleep) -> set[str]:
    """Sleep up to `seconds`. While jobs this worker parked are waiting for references, look every
    QUICK_POLL seconds whether one has left `state:needs-references` (the board answered it): then
    stop early, forget those jobs and return them. A job watched for its whole PARKED_WATCH is
    forgotten too: the ordinary idle poll finds it when it is answered."""
    left = seconds
    while left > 0:
        step = min(left, QUICK_POLL) if parked else left
        sleep(step)
        left -= step
        for job_id in list(parked):
            parked[job_id] -= step
            if parked[job_id] <= 0:
                del parked[job_id]
        if not parked:
            continue
        try:
            waiting = {job.id for job in ctx.board.list_jobs(("needs-references",))}
        except GHError:
            continue
        answered = set(parked) - waiting
        if answered:
            for job_id in answered:
                del parked[job_id]
            return answered
    return set()


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


__all__ = ["work_once", "work_loop", "rank"]
