"""Attempts are fenced: a worker whose lease is lost writes nothing more for its job and never resets
a state a newer attempt set; a board run holds its lock for as long as it writes; one worker id runs
one process at a time.

- `claims.Heartbeat` renews a lease and records its loss (a renewal that finds the lease gone or
  another owner's, or an expiry passed with every renewal failing); from then on every write through
  the fenced `gh` raises `LeaseLost`;
- a renewal that cannot read the namespace is an error, never a loss;
- a stale round keeps its run (`pending-push/`) and leaves the board as the newer attempt left it;
- a board lock renewed by its heartbeat outlives its 15 minutes, so no second writer takes it;
- a second process started with a worker id already running exits at once."""

import dataclasses
import subprocess

import pytest

from tauceti_roadmap_worker import claims, cli, config, intake, worker
from tauceti_roadmap_worker.board import Board
from tauceti_roadmap_worker.gh import GH
from tauceti_roadmap_worker.lanes import Context, LaneResult

from .fakes import FakeClaims, FakeIssuesGH, example_config, job_issue
from .test_checkout import needs_git

JOB = "TARGET-QSeries"
KEY = claims.job_key(example_config().project.name, JOB)
TTL, RENEW = example_config().claims.job_ttl_seconds, example_config().claims.renew_seconds
FAR = 4_102_444_800


class Namespace:
    """Leases in memory under one clock, with the protocol of `claims.Claims`: create, renew our own,
    take over an expired one, release our own (or any, by force). `down` owners cannot reach it."""

    def __init__(self, now: list[float]):
        self.now, self.leases, self.down = now, {}, set()

    def owner(self, name: str) -> "Owner":
        return Owner(self, name)


@dataclasses.dataclass
class Owner:
    ns: Namespace
    owner: str

    def acquire(self, key, ttl=claims.JOB_TTL, extra=None):
        if self.owner in self.ns.down:
            return claims.ERROR
        now = self.ns.now[0]
        if claims.held_by_other(self.ns.leases.get(key), self.owner, now):
            return claims.HELD
        self.ns.leases[key] = {"owner": self.owner, "acquired_at": now, "expires_at": now + ttl}
        return claims.ACQUIRED

    def renew(self, key, ttl=claims.JOB_TTL):
        if self.owner in self.ns.down:
            return claims.ERROR
        lease = self.ns.leases.get(key)
        if not lease or lease["owner"] != self.owner:
            return claims.HELD
        lease["expires_at"] = self.ns.now[0] + ttl
        return claims.ACQUIRED

    def read(self, key):
        return self.ns.leases.get(key)

    def release(self, key, force=False):
        if force or (self.ns.leases.get(key) or {}).get("owner") == self.owner:
            self.ns.leases.pop(key, None)


def namespace():
    now = [1_000_000.0]
    return now, Namespace(now)


# --- the heartbeat --------------------------------------------------------------------------------

def test_a_renewal_that_finds_another_owner_loses_the_lease_for_good():
    now, ns = namespace()
    alice, bob = ns.owner("alice/w1"), ns.owner("bob/w2")
    gh, lines = FakeIssuesGH([job_issue(1, JOB, "claimed")]), []
    assert alice.acquire(KEY, TTL) == claims.ACQUIRED
    with claims.Heartbeat(alice, KEY, TTL, RENEW, gh=gh, log=lines.append, clock=lambda: now[0]) as beat:
        now[0] += RENEW
        beat.beat()
        beat.check()                                          # renewed: still ours
        alice.release(KEY, force=True)                        # the board closed the job; bob takes it up
        assert bob.acquire(KEY) == claims.ACQUIRED
        now[0] += RENEW
        beat.beat()
        with pytest.raises(claims.LeaseLost, match="it is gone or another owner holds it"):
            beat.check()
        with pytest.raises(claims.LeaseLost):
            gh.write("POST", "repos/o/board/issues/1/comments", {"body": "a stale result"})
        assert gh.writes == []
        assert bob.acquire(KEY) == claims.ACQUIRED and alice.renew(KEY) == claims.HELD
        beat.beat()                                           # loss is final: no more renewals
    assert lines == [f"lease {KEY} lost: it is gone or another owner holds it; nothing more is written for it"]
    assert gh.fence is None                                   # outside the block the writes are the caller's again
    gh.write("POST", "repos/o/board/issues/1/comments", {"body": "after the round"})
    assert len(gh.writes) == 1


def test_renewal_errors_lose_the_lease_only_when_it_expires():
    now, ns = namespace()
    alice = ns.owner("alice/w1")
    alice.acquire(KEY, TTL)
    start = now[0]
    beat = claims.Heartbeat(alice, KEY, TTL, RENEW, clock=lambda: now[0])
    ns.down.add("alice/w1")                                   # the namespace cannot be reached
    for _ in range(TTL // RENEW - 1):                         # every renewal before the expiry fails
        now[0] += RENEW
        beat.beat()
        beat.check()
    now[0] = start + TTL - 1
    beat.check()
    now[0] += 1                                               # the lease's expiry, between two beats
    with pytest.raises(claims.LeaseLost, match="no renewal succeeded before it expired"):
        beat.check()
    ns.down.clear()
    beat.beat()
    assert beat.lost and alice.read(KEY)["expires_at"] == now[0]      # never renewed after the loss


def test_a_transient_renewal_error_keeps_the_lease():
    now, ns = namespace()
    alice = ns.owner("alice/w1")
    alice.acquire(KEY, TTL)
    beat = claims.Heartbeat(alice, KEY, TTL, RENEW, clock=lambda: now[0])
    ns.down.add("alice/w1")
    now[0] += RENEW
    beat.beat()
    ns.down.clear()
    now[0] += RENEW
    beat.beat()                                               # renewed: good for TTL from here
    now[0] += TTL - 1                                         # past the first expiry
    beat.check()
    assert not beat.lost


def test_an_unclaimed_attempt_takes_its_lease_at_a_beat_or_yields_to_its_holder():
    now, ns = namespace()
    alice, bob = ns.owner("alice/w1"), ns.owner("bob/w2")
    ns.down.add("alice/w1")
    assert alice.acquire(KEY) == claims.ERROR                 # fail-open: the round runs unclaimed
    beat = claims.Heartbeat(alice, KEY, TTL, RENEW, held=False, clock=lambda: now[0])
    now[0] += 2 * TTL
    beat.beat()
    beat.check()                                              # no lease to lose
    ns.down.clear()
    beat.beat()
    assert beat.held and alice.read(KEY)["owner"] == "alice/w1"
    other = claims.Heartbeat(bob, KEY, TTL, RENEW, held=False, clock=lambda: now[0])
    other.beat()
    with pytest.raises(claims.LeaseLost, match="another owner holds it"):
        other.check()


def test_without_claims_nothing_is_guarded():
    gh = FakeIssuesGH([job_issue(1, JOB, "claimed")])
    with claims.Heartbeat(None, KEY, TTL, 0.01, gh=gh) as beat:
        beat.beat()
        beat.check()
        gh.write("POST", "repos/o/board/issues/1/comments", {"body": "a dry run's write"})
    assert not beat.thread.is_alive() and not beat.thread.ident and gh.fence is None


def test_a_fenced_write_never_reaches_gh(monkeypatch):
    gh, ran = GH(), []
    monkeypatch.setattr(gh, "_run", lambda args, stdin=None: ran.append(args) or "{}")

    def lost():
        raise claims.LeaseLost("the lease roadmap/P/board/intake was lost: another owner holds it")
    gh.fence = lost
    for write in (lambda: gh.write("PATCH", "repos/o/b/issues/1", {"state": "closed"}),
                  lambda: gh.cli_write("pr", "ready", "1", "--undo")):
        with pytest.raises(claims.LeaseLost):
            write()
    assert ran == []
    gh.api("repos/o/b")                                       # reads are never fenced
    assert len(ran) == 1


def test_the_lease_lifetime_and_renewal_come_from_the_configuration():
    assert (TTL, RENEW) == (3600, 1200)                       # TauCetiWorker's measured values
    assert config.parse('[project]\nname = "P"\n').claims == config.Claims(3600, 1200)     # the defaults
    cfg = config.parse('[project]\nname = "P"\n[claims]\njob_ttl_seconds = 900\nrenew_seconds = 300\n')
    assert (cfg.claims.job_ttl_seconds, cfg.claims.renew_seconds) == (900, 300) and cfg.warnings == []
    for bad in ("renew_seconds = 3600", "renew_seconds = 10"):
        with pytest.raises(config.ConfigError, match="claims.renew_seconds must be at least 30 and below"):
            config.parse(f'[project]\nname = "P"\n[claims]\n{bad}\n')


# --- renewals against a real namespace ------------------------------------------------------------

def local_claims(tmp_path, remote, owner):
    """A claims namespace in a local bare repository (the protocol is the same as on GitHub)."""
    c = claims.Claims(repo=str(remote), gitdir=tmp_path / (owner.replace("/", "_") + ".git"), owner=owner,
                      operator=owner.split("/")[0], project="P")

    def ensure_repo():
        if not c.gitdir.is_dir():
            subprocess.run(["git", "init", "-q", "--bare", str(c.gitdir)], check=True)
        c._git("remote", "remove", "origin")
        c._git("remote", "add", "origin", c.repo)
    c.ensure_repo = ensure_repo
    return c


@needs_git
def test_a_renewal_reads_the_namespace_before_it_calls_the_lease_lost(tmp_path, monkeypatch):
    remote = tmp_path / "claims.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    alice, bob = local_claims(tmp_path, remote, "alice/w1"), local_claims(tmp_path, remote, "bob/w2")
    assert alice.acquire(KEY, ttl=600) == claims.ACQUIRED
    taken = alice.read(KEY)
    assert bob.acquire(KEY) == claims.HELD
    later = taken["acquired_at"] + 100
    monkeypatch.setattr(claims.time, "time", lambda: later)
    assert alice.renew(KEY, ttl=600) == claims.ACQUIRED
    renewed = alice.read(KEY)
    assert renewed["acquired_at"] == taken["acquired_at"] and renewed["expires_at"] == later + 600
    monkeypatch.undo()
    alice.repo = str(tmp_path / "unreachable.git")
    assert alice.renew(KEY) == claims.ERROR                   # unreadable: maybe still ours, never lost
    alice.repo = str(remote)
    bob.release(KEY, force=True)
    assert alice.renew(KEY) == claims.HELD                    # gone
    assert bob.acquire(KEY) == claims.ACQUIRED
    assert alice.renew(KEY) == claims.HELD                    # another owner's


# --- the worker's round ---------------------------------------------------------------------------

def bob_takes_over_and_submits(gh, ns, number=1):
    """Bob's worker, once alice's lease is gone: it takes the lease, claims the job and submits it."""
    ns.leases[KEY] = {"owner": "bob/w2", "acquired_at": ns.now[0], "expires_at": FAR}
    gh.comments.setdefault(number, []).append(f"Claimed for `{JOB}` by worker `w2` (`bob`, codex).")
    gh.issues[number]["labels"] = [x for x in gh.issues[number]["labels"] if not x["name"].startswith("state:")]
    gh.add_label(number, "state:submitted")


class Superseded:
    """A lane whose lease bob's worker takes over while its agent runs; `then` is what the round
    does afterwards (a write, or a result)."""

    agent_started = True

    def __init__(self, ns, then):
        self.ns, self.then = ns, then

    def eligible(self, job, ctx):
        return ""

    def run(self, job, ctx):
        bob_takes_over_and_submits(ctx.gh, self.ns)
        ctx.gh.fence.__self__.beat()                          # the round's heartbeat: its next renewal
        return self.then(job, ctx)


@pytest.fixture
def stale_round(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = example_config()
    _now, ns = namespace()
    gh = FakeIssuesGH([job_issue(1, JOB, "available")])
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=ns.owner("alice/chris-1"), worker_id="chris-1",
                  operator="alice", agent="codex", model="m")
    return ctx, gh, ns


@pytest.mark.parametrize("then", [
    lambda job, ctx: ctx.board.comment(job.number, "a review verdict"),
    lambda job, ctx: LaneResult(job.id, "failed", "the checks failed"),
], ids=["a write after the loss", "a release after it"])
def test_a_superseded_round_neither_publishes_nor_resets_the_newer_state(stale_round, monkeypatch, then):
    ctx, gh, ns = stale_round
    monkeypatch.setattr(worker, "lane_for", lambda job: Superseded(ns, then))
    lines = []
    result = worker.work_once(ctx, None, log=lines.append)
    assert gh.states(1) == ["submitted"]
    assert gh.comments[1] == [f"Claimed for `{JOB}` by worker `chris-1` (`alice`, codex).",
                              f"Claimed for `{JOB}` by worker `w2` (`bob`, codex)."]     # nothing of alice's after
    assert ns.leases[KEY]["owner"] == "bob/w2"                                 # bob's lease stays bob's
    assert f"{JOB}: left as it is, not set to available: its last claim is by worker `w2` (`bob`, codex)" in lines
    if result.status == "lease-lost":
        assert any(line.startswith(f"lease {KEY} lost") for line in lines)
        assert worker.failures(ctx) == {}


def test_a_release_never_overwrites_a_state_set_after_the_claim(stale_round, monkeypatch):
    """A local call with a newer `submitted` reproduced `submitted -> available` (review R04); a
    blocked round leaves a done job done as well."""
    ctx, gh, _ns = stale_round

    class Overtaken:
        agent_started = True

        def __init__(self, state, result):
            self.state, self.result = state, result

        def eligible(self, job, ctx_):
            return ""

        def run(self, job, ctx_):
            gh.issues[1]["labels"] = [x for x in gh.issues[1]["labels"] if not x["name"].startswith("state:")]
            gh.add_label(1, f"state:{self.state}")          # the board moved the job on meanwhile
            return self.result

    for state, status in (("submitted", "failed"), ("done", "blocked")):
        gh.issues[1]["labels"] = [x for x in gh.issues[1]["labels"] if not x["name"].startswith("state:")]
        gh.add_label(1, "state:available")
        lane = Overtaken(state, LaneResult(JOB, status, "stopped"))
        monkeypatch.setattr(worker, "lane_for", lambda job, lane=lane: lane)
        lines = []
        worker.work_once(ctx, None, log=lines.append)
        assert gh.states(1) == [state]
        target = "blocked" if status == "blocked" else "available"
        assert f"{JOB}: left as it is, not set to {target}: its issue is no longer `state:claimed`" in lines
    assert not any(c.startswith(("Released by worker", "Blocked by worker")) for c in gh.comments[1])


def test_a_round_still_holding_its_claim_is_released_as_before(stale_round, monkeypatch):
    ctx, gh, _ns = stale_round

    class Fails:
        agent_started = True

        def eligible(self, job, ctx_):
            return ""

        def run(self, job, ctx_):
            return LaneResult(job.id, "failed", "the checks failed")
    monkeypatch.setattr(worker, "lane_for", lambda job: Fails())
    worker.work_once(ctx, None, log=lambda line: None)
    assert gh.states(1) == ["available"] and gh.comments[1][-1].startswith("Released by worker `chris-1`: failed")


def test_a_round_leases_and_renews_its_job_as_configured(stale_round, monkeypatch):
    ctx, _gh, ns = stale_round
    ctx.cfg.claims.job_ttl_seconds, ctx.cfg.claims.renew_seconds = 900, 300
    seen = {}

    class Looks:
        agent_started = True

        def eligible(self, job, ctx_):
            return ""

        def run(self, job, ctx_):
            beat, lease = ctx_.gh.fence.__self__, ns.leases[KEY]
            seen.update(lease=lease["expires_at"] - lease["acquired_at"], beat=(beat.ttl, beat.every))
            return LaneResult(job.id, "failed", "the checks failed")
    monkeypatch.setattr(worker, "lane_for", lambda job: Looks())
    worker.work_once(ctx, None, log=lambda line: None)
    assert seen == {"lease": 900, "beat": (900, 300)}


# --- the board lock -------------------------------------------------------------------------------

def test_a_renewed_board_lock_outlives_its_ttl_and_keeps_the_other_writer_out():
    now, ns = namespace()
    hosted, shell = ns.owner("intake:run-1"), ns.owner("intake:laptop-42")
    key = intake.lock_key("P")
    assert hosted.acquire(key, intake.LOCK_TTL) == claims.ACQUIRED
    with claims.Heartbeat(hosted, key, intake.LOCK_TTL, intake.LOCK_RENEW, clock=lambda: now[0]) as beat:
        for _ in range(10):                                   # 30 minutes of board work
            now[0] += intake.LOCK_RENEW
            beat.beat()
            assert shell.acquire(key, intake.LOCK_TTL) == claims.HELD
        beat.check()
    now[0] += intake.LOCK_TTL                                 # an unrenewed lock is free after its 15 minutes
    assert shell.acquire(key, intake.LOCK_TTL) == claims.ACQUIRED


# --- one process per worker id --------------------------------------------------------------------

def test_one_worker_id_runs_one_process(tmp_path):
    wdir = tmp_path / "workers" / "alice-1"
    held = worker.hold_worker_dir(wdir)
    with pytest.raises(worker.WorkerBusy, match=r"worker alice-1 is already running here \(pid \d+ on "):
        worker.hold_worker_dir(wdir)
    held.close()
    worker.hold_worker_dir(wdir).close()                      # free again once the process lets go


def test_a_second_worker_process_exits_before_it_touches_the_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli.GH, "login", lambda self: "alice")
    monkeypatch.setattr(cli.claims, "open_claims", lambda *a, **k: FakeClaims(owner="alice/alice-1"))
    wdir = example_config().state_dir() / "workers" / "alice-1"
    held = worker.hold_worker_dir(wdir)
    with pytest.raises(SystemExit, match="worker alice-1 is already running here"):
        cli.main(["--conf", str(cli.paths.HERE / "tauceti-roadmap.toml.example"), "work", "--worker-id", "alice-1"])
    held.close()
    assert sorted(p.name for p in wdir.iterdir()) == ["worker.lock"]        # no build snapshot, no round log
