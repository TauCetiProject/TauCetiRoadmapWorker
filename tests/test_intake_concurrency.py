"""Two intake runs at once created the same jobs twice (board 2026-10-21: the hosted run and a
maintainer's shell both created red-team round 1 of K3SurfacesAndSymplecticBoundedness, #569/#571
and #570/#572; the next scan then concluded the twin passes and created the round's fix twice,
#580/#581). Three guards: a creation re-checks the live board, a board run holds a lock, and open
twins are repaired."""

import datetime as dt
from types import SimpleNamespace

from tauceti_roadmap_worker import claims, cli, intake
from tauceti_roadmap_worker.board import Board, Outcome, new_job, title_key

from .fakes import FakeClaims, FakeIssuesGH, bot_context, example_config, job_issue

FUTURE = 4_102_444_800


def issue_titles(gh):
    return [i["title"] for i in gh.issues.values()]


def test_title_key():
    assert title_key("[Fix: red-team findings] K3Surfaces (round 1)") == "fix red team findings k3surfaces round 1"
    assert title_key("  [Red  team]  Foo (round 1, pass a) ") == title_key("[Red team] Foo (round 1, pass a)")


def test_a_creation_rechecks_the_live_board_not_the_cached_listing():
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "RT-Foo~1a", "done", inputs={"board_pr": 12, "operator": "alice"}),
                       job_issue(2, "RT-Foo~1b", "done", inputs={"board_pr": 12, "operator": "alice"})])
    mine = Board(cfg, gh)
    jobs = mine.all_jobs()                                   # the listing this run started from: no fix yet
    other = Board(cfg, gh)                                   # another run creates the round's fix meanwhile
    assert other.create(new_job("FIX-RT-Foo~1", after=["RT-Foo~1a", "RT-Foo~1b"])) == 3
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=mine, operator="maintainer", dry_run=False)
    line = intake.advance(ctx, jobs[1], Outcome(round_complete=True), jobs)
    assert line == "created FIX-RT-Foo~1 (already on the board as #3)"
    assert issue_titles(gh).count("[Fix: red-team findings] Foo (round 1)") == 1
    assert mine.reused == {"FIX-RT-Foo~1": 3}
    assert [w for w in gh.writes if w[0] == "POST" and w[1].endswith("/issues")] == gh.writes[:1]   # one creation in all
    # A closed twin counts too (a done job is never created again).
    gh.issues[3]["state"] = "closed"
    assert Board(cfg, gh).create(new_job("FIX-RT-Foo~1")) == 3 and len(gh.issues) == 3


def test_an_issue_this_run_created_counts_before_the_listing_shows_it():
    """The same scan concluded both twin passes: the listing read between them did not show the fix
    created seconds earlier (#580 at 13:33:31Z, the twin #581 at 13:33:48Z)."""
    cfg = example_config()
    gh = FakeIssuesGH([])
    b = Board(cfg, gh)
    first = b.create(new_job("FIX-RT-Foo~1"))
    real = gh.api
    gh.api = lambda path, paginate=False: [i for i in real(path, paginate) if i["number"] != first] \
        if "/issues?" in path else real(path, paginate)      # the listing lags the creation
    assert b.create(new_job("FIX-RT-Foo~1")) == first and len(gh.issues) == 1 and b.reused == {"FIX-RT-Foo~1": first}
    assert Board(cfg, gh).existing(new_job("FIX-RT-Foo~1")) == 0   # another process really would not see it yet


class Lock:
    def __init__(self, status, lease=None):
        self.status, self.lease, self.acquired, self.released = status, lease, [], []

    def acquire(self, key, ttl=claims.JOB_TTL, extra=None):
        self.acquired.append((key, ttl))
        return self.status

    def read(self, key):
        return self.lease

    def release(self, key, force=False):
        self.released.append(key)


def board_args(*extra):
    return cli.parser().parse_args(["board", *extra])


def run_context(cfg):
    ctx = bot_context(cfg)
    ctx.sources_fetcher = lambda: None
    return ctx


def test_a_held_lock_makes_a_run_a_no_op(monkeypatch, capsys):
    cfg = example_config()
    since = int(dt.datetime(2026, 10, 21, 13, 33, tzinfo=dt.UTC).timestamp())
    lock = Lock(claims.HELD, {"owner": "intake:run-39608967295", "acquired_at": since, "expires_at": since + 900})
    monkeypatch.setattr(intake, "board_lock", lambda cfg, ctx: lock)
    def never(name):
        return lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"{name} ran"))
    for name in ("repair_duplicates", "intake_open", "sync_targets"):
        monkeypatch.setattr(intake, name, never(name))
    for action in ("intake", "sync"):
        assert cli.board_run(board_args(action, "--yes"), cfg, run_context(cfg)) == 0
        assert capsys.readouterr().out.strip() == \
            "intake already running (intake:run-39608967295, since 2026-10-21 13:33:00Z); nothing done"
    assert lock.acquired == [(intake.lock_key(cfg.project.name), intake.LOCK_TTL)] * 2 and lock.released == []


def test_a_free_lock_is_taken_for_the_run_and_released_after_it(monkeypatch, capsys):
    cfg = example_config()
    lock = Lock(claims.ACQUIRED)
    ran = []
    monkeypatch.setattr(intake, "board_lock", lambda cfg, ctx: lock)
    monkeypatch.setattr(intake, "repair_duplicates", lambda ctx, shared, yes=False: ran.append("repair") or [])
    monkeypatch.setattr(intake, "intake_open", lambda ctx, write, only=0: ran.append("intake") or [])
    monkeypatch.setattr(cli.references, "resume_all", lambda ctx, fetcher, yes=False: ran.append("references") or [])
    monkeypatch.setattr(cli.boardpr, "scan", lambda ctx, shared, yes=False: ran.append("scan") or [])
    assert cli.board_run(board_args("intake", "--yes"), cfg, run_context(cfg)) == 0
    assert ran == ["repair", "intake", "references", "scan"]
    assert lock.released == [intake.lock_key(cfg.project.name)]
    # A lock that cannot be written never stops a run (fail-open, said so); a dry run takes none.
    monkeypatch.setattr(intake, "board_lock", lambda cfg, ctx: Lock(claims.ERROR))
    assert cli.board_run(board_args("intake", "--yes"), cfg, run_context(cfg)) == 0
    assert capsys.readouterr().out.startswith("board lock not taken")
    monkeypatch.setattr(intake, "board_lock",
                        lambda cfg, ctx: (_ for _ in ()).throw(AssertionError("locked a dry run")))
    assert cli.board_run(board_args("intake"), cfg, run_context(cfg)) == 0


def test_the_lock_lives_in_the_board_repository_under_the_run_or_host(monkeypatch, tmp_path):
    cfg = example_config()
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path))
    ctx = SimpleNamespace(operator="CBirkbeck", dry_run=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "39608967295")
    lock = intake.board_lock(cfg, ctx)
    assert lock.repo == cfg.repositories.board_repo() and lock.owner == "intake:run-39608967295"
    assert intake.lock_key(cfg.project.name) == f"roadmap/{cfg.project.name}/board/intake"
    monkeypatch.delenv("GITHUB_RUN_ID")
    assert intake.lock_owner().startswith("intake:") and "run-" not in intake.lock_owner()


def test_duplicate_repair_closes_only_the_newer_open_twin():
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(10, "FIX-RT-Foo~1", "available"), job_issue(11, "FIX-RT-Foo~1", "claimed"),
                       job_issue(20, "RT-Bar~1a", "available"), job_issue(21, "RT-Bar~1a", "done"),
                       job_issue(30, "PORT-Baz", "available")])
    key = claims.job_key(cfg.project.name, "FIX-RT-Foo~1")
    shared = FakeClaims(leases={key: {"owner": "CBirkbeck/chris-4", "expires_at": FUTURE}})
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=Board(cfg, gh), operator="maintainer", dry_run=False)
    assert intake.repair_duplicates(ctx, shared) == ["FIX-RT-Foo~1: would close #11 as a duplicate of #10 (dry run)",
                                                     "RT-Bar~1a: #21 duplicates #20 but is done; left as it is"]
    assert all(i["state"] == "open" for i in gh.issues.values()) and shared.released == []
    out = intake.repair_duplicates(ctx, shared, yes=True)
    assert out == ["FIX-RT-Foo~1: closed #11 as a duplicate of #10; lease of CBirkbeck/chris-4 released",
                   "RT-Bar~1a: #21 duplicates #20 but is done; left as it is"]
    assert gh.issues[11]["state"] == "closed" and [gh.issues[n]["state"] for n in (10, 20, 21, 30)] == ["open"] * 4
    assert gh.comments[11][0].startswith("Duplicate of #10") and "duplicate" in gh.labels(11) and 10 not in gh.comments
    assert shared.released == [key]
    assert [j.number for j in ctx.board.all_jobs()] == [10, 20, 21, 30]      # the closed twin is no longer a job
    again = intake.repair_duplicates(ctx, shared, yes=True)
    assert again == ["RT-Bar~1a: #21 duplicates #20 but is done; left as it is"]
