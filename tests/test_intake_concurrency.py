"""Two intake runs at once created the same jobs twice (board 2026-10-21: the hosted run and a
maintainer's shell both created red-team round 1 of K3SurfacesAndSymplecticBoundedness, #569/#571
and #570/#572; the next scan then concluded the twin passes and created the round's fix twice,
#580/#581). Three guards: a creation re-checks the live board, a board run holds a lock, and open
twins are repaired."""

import datetime as dt
from types import SimpleNamespace

from tauceti_roadmap_worker import board as board_mod
from tauceti_roadmap_worker import boardpr, claims, cli, intake
from tauceti_roadmap_worker.board import Board, Outcome, new_job, title_key
from tauceti_roadmap_worker.gh import GHError

from .fakes import FakeClaims, FakeIssuesGH, bot_context, example_config, job_issue
from .test_boardpr import review

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
    monkeypatch.setattr(intake, "replace_upward_roadmaps", lambda ctx, yes=False: ran.append("order") or [])
    assert cli.board_run(board_args("intake", "--yes"), cfg, run_context(cfg)) == 0
    assert ran == ["repair", "intake", "references", "scan", "order"]
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


def repair_context(issues, leases=None, pulls=()):
    cfg = example_config()
    gh = FakeIssuesGH(issues)
    gh.pulls = list(pulls)
    shared = FakeClaims(leases={claims.job_key(cfg.project.name, jid): lease for jid, lease in (leases or {}).items()})
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=Board(cfg, gh), operator="maintainer", dry_run=False)
    return ctx, gh, shared


def submission(number: int, job_id: str, refs: int, state: str = "open") -> dict:
    return {"number": number, "state": state, "draft": False, "user": {"login": "CBirkbeck"},
            "body": f"Refs #{refs}\n\npackage\n\n"
            + board_mod.encode_submission(job_id, "chris-1", "CBirkbeck", "codex", "gpt"),
            "labels": [], "head": {"sha": "a" * 40, "ref": f"chris-1/{job_id}",
                                   "repo": {"full_name": "CBirkbeck/TauCetiRoadmapJobs"}}}


def test_duplicate_repair_keeps_the_twin_that_got_furthest_else_the_oldest():
    """Twins both untouched keep the older issue; a twin a worker holds (its own claim note, the
    job's lease live) beats an older untouched one; a done twin is never closed and its untouched
    twin is the duplicate."""
    lease = {"owner": "CBirkbeck/chris-4", "expires_at": FUTURE}
    ctx, gh, shared = repair_context(
        [job_issue(10, "FIX-RT-Foo~1", "available"), job_issue(11, "FIX-RT-Foo~1", "claimed"),
         job_issue(20, "RT-Bar~1a", "available"), job_issue(21, "RT-Bar~1a", "done"),
         job_issue(30, "PORT-Baz", "available"), job_issue(31, "PORT-Baz", "available"),
         job_issue(40, "PORT-Qux", "available")], leases={"FIX-RT-Foo~1": lease})
    gh.comments[11] = ["Claimed for `FIX-RT-Foo~1` by worker `chris-4` (@CBirkbeck, claude)."]
    assert intake.repair_duplicates(ctx, shared) == [
        "FIX-RT-Foo~1: would close #10 as a duplicate of #11 (a worker holds it; dry run)",
        "RT-Bar~1a: would close #20 as a duplicate of #21 (it is done; dry run)",
        "PORT-Baz: would close #31 as a duplicate of #30 (it is the older issue; dry run)"]
    assert all(i["state"] == "open" for i in gh.issues.values()) and shared.released == []
    out = intake.repair_duplicates(ctx, shared, yes=True)
    assert out == [("FIX-RT-Foo~1: closed #10 as a duplicate of #11 (a worker holds it); the lease of "
                    "CBirkbeck/chris-4 stays with #11"),
                   "RT-Bar~1a: closed #20 as a duplicate of #21 (it is done)",
                   "PORT-Baz: closed #31 as a duplicate of #30 (it is the older issue)"]
    assert [gh.issues[n]["state"] for n in (10, 20, 31)] == ["closed"] * 3
    assert [gh.issues[n]["state"] for n in (11, 21, 30, 40)] == ["open"] * 4
    assert gh.comments[10][0].startswith("Duplicate of #11: this job was created twice; #11 is the job (a worker holds "
                                         "it)")
    assert all("duplicate" in gh.labels(n) for n in (10, 20, 31)) and "duplicate" not in gh.labels(11)
    assert shared.released == []                                          # the lease is the job's, not the twin's
    assert gh.states(11) == ["claimed"] and gh.states(21) == ["done"] and gh.states(30) == ["available"]
    assert [j.number for j in ctx.board.all_jobs()] == [11, 21, 30, 40]    # the closed twins are no longer jobs
    assert intake.repair_duplicates(ctx, shared, yes=True) == []
    # A closed duplicate still stops a re-creation, and the issue named is the kept twin's.
    assert ctx.board.create(new_job("FIX-RT-Foo~1")) == 11 and ctx.board.reused == {"FIX-RT-Foo~1": 11}


def test_the_twin_with_the_submission_is_kept_whatever_its_age_or_label():
    """Board 2026-10-21: the twin that carried the package (PR #589) must survive the repair, and
    the kept twin's label is raised to the submission's state (a sync had knocked #582 back to
    available after its submission)."""
    lease = {"owner": "CBirkbeck/chris-1", "expires_at": FUTURE}
    # The newer twin carries the pull request and says so; the older one was never worked.
    ctx, gh, shared = repair_context([job_issue(582, "PKG-Selmer", "available"),
                                      job_issue(583, "PKG-Selmer", "submitted")],
                                     pulls=[submission(589, "PKG-Selmer", 583)])
    gh.comments[583] = ["Claimed for `PKG-Selmer` by worker `chris-1` (@CBirkbeck, codex)."]
    assert intake.repair_duplicates(ctx, shared, yes=True) == [
        "PKG-Selmer: closed #582 as a duplicate of #583 (it carries the submission #589)"]
    assert gh.issues[582]["state"] == "closed" and gh.issues[583]["state"] == "open" and gh.states(583) == ["submitted"]
    # The older twin carries the pull request but was knocked back to available; the newer one has
    # the lease-mirrored claim: the older is kept and moved to submitted, the lease stays.
    ctx, gh, shared = repair_context([job_issue(582, "PKG-Selmer", "available", inputs={"review_pr": 589}),
                                      job_issue(583, "PKG-Selmer", "claimed")],
                                     leases={"PKG-Selmer": lease}, pulls=[submission(589, "PKG-Selmer", 582)])
    gh.comments[583] = ["Claimed for `PKG-Selmer` by `CBirkbeck/chris-1` (lease)."]
    assert intake.repair_duplicates(ctx, shared, yes=True) == [
        ("PKG-Selmer: closed #583 as a duplicate of #582 (it carries the submission #589); the lease of "
         "CBirkbeck/chris-1 stays with #582"),
        "PKG-Selmer: #582 moved to submitted (#589 is open)"]
    assert gh.states(582) == ["submitted"] and gh.issues[583]["state"] == "closed" and shared.released == []
    assert gh.comments[582][-1].startswith("Submitted: #589 is this job's open submission")
    # The pull request names neither twin (an older body): the submitted label decides, the kept
    # twin takes over the inputs the closed twin gained, and an available kept twin with a live
    # lease is claimed.
    ctx, gh, shared = repair_context([job_issue(10, "PKG-Foo", "available"),
                                      job_issue(11, "PKG-Foo", "submitted", inputs={"review_pr": 12, "handled": [7]})],
                                     pulls=[submission(12, "PKG-Foo", 0)])
    assert intake.repair_duplicates(ctx, shared, yes=True) == [
        "PKG-Foo: closed #10 as a duplicate of #11 (it carries the submission #12)"]
    ctx, gh, shared = repair_context([job_issue(10, "PKG-Foo", "available", inputs={"handled": [5]}),
                                      job_issue(11, "PKG-Foo", "claimed", inputs={"review_pr": 12, "handled": [7]})],
                                     leases={"PKG-Foo": lease})
    assert intake.repair_duplicates(ctx, shared, yes=True) == [
        "PKG-Foo: closed #10 as a duplicate of #11 (it is claimed); the lease of CBirkbeck/chris-1 stays with #11"]
    [kept] = ctx.board.all_jobs()
    assert kept.number == 11 and kept.inputs == {"review_pr": 12, "handled": [5, 7]}
    ctx, gh, shared = repair_context([job_issue(10, "PKG-Foo", "available"), job_issue(11, "PKG-Foo", "available")],
                                     leases={"PKG-Foo": lease})
    assert intake.repair_duplicates(ctx, shared, yes=True) == [
        ("PKG-Foo: closed #11 as a duplicate of #10 (it is the older issue); the lease of CBirkbeck/chris-1 stays with "
         "#10"),
        "PKG-Foo: #10 moved to claimed (the lease of CBirkbeck/chris-1 is live)"]
    assert gh.states(10) == ["claimed"]


class ReviewGH(FakeIssuesGH):
    """The board's issues and, on the same repository, the submission pull request with its comments,
    files, checks and contents."""

    def __init__(self, issues, pr):
        super().__init__(issues)
        self.pulls, self.pr_comments, self.files = [pr], [], {}

    def say(self, login, body):
        pr = self.pulls[0]["number"]
        self.pr_comments.append({"id": 900 + len(self.pr_comments), "body": body, "user": {"login": login},
                                 "html_url": f"https://github.com/o/r/pull/{pr}#c{len(self.pr_comments)}"})

    def api(self, path, paginate=False):
        pr = self.pulls[0]["number"]
        if f"/issues/{pr}/comments" in path:
            return list(self.pr_comments)
        if f"/pulls/{pr}/files" in path:
            return [{"filename": f, "status": "added"}
                    for f in ("packages/Foo/README.md", "packages/Foo/reports/PKG-Foo.md")]
        if "/check-runs" in path:
            return {"check_runs": []}
        if "/contents/" in path:
            raise GHError("404")
        return super().api(path, paginate)

    def raw_file(self, repo, path, ref=""):
        return "# Foo\n"

    def write(self, method, path, payload=None):
        pr = self.pulls[0]["number"]
        if path.endswith(f"/issues/{pr}/comments"):
            self.say("board-bot", payload["body"])
            return {"id": self.pr_comments[-1]["id"]}
        if path.endswith(f"/issues/{pr}/labels"):
            self.pulls[0]["labels"] += [{"name": n} for n in payload["labels"]]
            return list(self.pulls[0]["labels"])
        if "/issues/comments/" in path and method == "PATCH":
            return {}
        if "/contents/" in path and method == "PUT":
            self.files[path.split("/contents/", 1)[1]] = payload["content"]
            return {}
        return super().write(method, path, payload)


def test_a_review_created_from_the_closed_twins_pull_request_still_advances():
    """Every transition keys off the pull request (`board_pr`, `Refs #n`) and the job id: the review
    scan and the merge find the kept twin, never the closed one."""
    cfg = example_config()
    cfg.review.comment_reviewers, cfg.board.bot_logins = ["roed-math"], ["board-bot"]
    pr = submission(89, "PKG-Foo", 11)
    gh = ReviewGH([job_issue(10, "PKG-Foo", "available"), job_issue(11, "PKG-Foo", "submitted")], pr)
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=Board(cfg, gh), operator="board-bot", dry_run=False)
    # The intake took the submission before the repair: the review names the pull request.
    assert intake.intake(ctx, 89, yes=True).startswith("#89: open for review; created REV-PKG-Foo (#12)")
    assert gh.comments[11] == ["Intake: authored by @CBirkbeck (codex, gpt) in #89."]    # the twin it names
    assert intake.repair_duplicates(ctx, None, yes=True) == [
        "PKG-Foo: closed #10 as a duplicate of #11 (it carries the submission #89)"]
    jobs = ctx.board.all_jobs()
    assert [j.number for j in jobs] == [11, 12] and ctx.board.find("PKG-Foo", jobs).number == 11
    assert [j.number for j in boardpr.authors_of(89, "PKG-Foo", jobs)] == [11]   # what the merge will close
    gh.say("roed-math", review("chatgpt", "a" * 40, "accepted"))
    out = boardpr.scan(ctx, None, yes=True)
    assert out[0].startswith("#89: REV-PKG-Foo accepted; created RT-Foo~1a, RT-Foo~1b")
    jobs = ctx.board.all_jobs()
    assert ctx.board.find("REV-PKG-Foo", jobs).state == "done"
    assert ctx.board.find("RT-Foo~1a", jobs).inputs["board_pr"] == 89
    assert gh.issues[10]["state"] == "closed" and gh.states(10) == ["available"]   # the closed twin was never touched
    assert gh.states(11) == ["submitted"]


def test_a_submission_taken_in_while_its_job_reads_available_moves_it_to_submitted():
    """The worker set `submitted` on a twin closed under it (`Refs #10`), or could not write the
    board: the job the intake finds is under review, so it is never left for another worker."""
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    gh = ReviewGH([job_issue(11, "PKG-Foo", "available")], submission(89, "PKG-Foo", 10))
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=Board(cfg, gh), operator="board-bot", dry_run=False)
    assert intake.intake(ctx, 89, yes=True).startswith("#89: open for review")
    assert gh.states(11) == ["submitted"]
    assert gh.comments[11][-1] == "Submitted: #89 is this job's open submission (this issue said `state:available`)."
