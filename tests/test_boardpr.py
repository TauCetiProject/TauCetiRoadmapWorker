"""A package on its open board pull request: review, fix, review, red team, fix, merge, port, all
driven by comments (from a browser model's listed login and from workers), with GitHub faked."""

import base64

from tauceti_roadmap_worker import boardpr, intake, prreviews
from tauceti_roadmap_worker.board import encode_submission, new_job

from .fakes import bot_context, example_config

REPO = "TauCetiProject/TauCetiRoadmapJobs"


class Board:
    """Jobs that come and go as the intake creates and finishes them."""

    def __init__(self, jobs):
        self.jobs, self.threads, self.posted = list(jobs), {}, []

    def all_jobs(self):
        return list(self.jobs)

    def find(self, job_id, jobs=None):
        return next((j for j in (jobs if jobs is not None else self.jobs) if j.id == job_id), None)

    def create(self, job, state="available", preamble=""):
        job.number, job.state = 100 + len(self.jobs), state
        self.jobs.append(job)
        return job.number

    def set_state(self, job, state, comment=""):
        job.state = state
        if comment:
            self.comment(job.number, comment)

    def comment(self, number, body, login="board-bot"):
        self.posted.append((number, body))
        self.threads.setdefault(number, []).append({"body": body, "user": {"login": login}})

    def comments(self, number):
        return list(self.threads.get(number, []))

    def update_spec(self, job):
        pass

    def authors(self, job_ids, jobs):
        return {}


class GH:
    """The packages repository: one pull request, its comments, files and checks; contents."""

    def __init__(self, head):
        self.pr = {"number": 12, "state": "open", "draft": False, "user": {"login": "alice"}, "labels": [],
                   "body": "Refs #3\n\n" + encode_submission("PKG-Foo", "w1", "alice", "codex", "gpt-6.1-sol"),
                   "head": {"sha": head, "ref": "w1/PKG-Foo", "repo": {"full_name": "alice/TauCetiRoadmapJobs"}}}
        self.comments, self.files, self.writes, self.merged = [], {}, [], False

    def say(self, login, body):
        self.comments.append({"id": 900 + len(self.comments), "body": body, "user": {"login": login},
                              "html_url": f"https://github.com/{REPO}/pull/12#c{len(self.comments)}"})

    def api(self, path, paginate=False):
        if path.endswith("/pulls/12"):
            return self.pr
        if "/pulls?state=open" in path:
            return [] if self.merged else [self.pr]
        if "/pulls/12/files" in path:
            return [{"filename": f, "status": "added"} for f in ("packages/Foo/README.md", "packages/Foo/reports/PKG-Foo.md")]
        if "/check-runs" in path:
            return {"check_runs": []}
        if "/issues/12/comments" in path:
            return list(self.comments)
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name not in self.files:
                from tauceti_roadmap_worker.gh import GHError
                raise GHError("404")
            return {"sha": "blob", "content": base64.b64encode(self.files[name].encode()).decode()}
        raise AssertionError(path)

    def raw_file(self, repo, path, ref=""):
        return "# Foo\n"

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        if path.endswith("/issues/12/labels"):
            self.pr["labels"] += [{"name": n} for n in payload["labels"]]
        elif path.endswith("/issues/12/comments"):
            self.say("board-bot", payload["body"])
        elif "/issues/comments/" in path and method == "PATCH":
            cid = int(path.rsplit("/", 1)[1])
            next(c for c in self.comments if c["id"] == cid)["body"] = payload["body"]
        elif path.endswith("/pulls/12/merge"):
            self.merged = True
        elif "/contents/" in path and method == "PUT":
            self.files[path.split("/contents/", 1)[1]] = base64.b64decode(payload["content"]).decode()
        return {"number": 12}


def review(agent, sha, verdict, findings=0):
    lines = [prreviews.review_head(agent, sha), "", f"Verdict: {verdict}", ""]
    lines += [f"{k}. **F{k:02d}** (P1) finding {k}" for k in range(1, findings + 1)]
    return "\n".join(lines) + "\n"


def test_a_package_through_review_fix_review_red_team_and_port():
    cfg = example_config()
    cfg.review.comment_reviewers = ["roed-math"]
    cfg.board.bot_logins = ["board-bot"]
    cfg.redteam.rounds = 1
    pkg = new_job("PKG-Foo")
    pkg.number, pkg.state = 3, "submitted"
    board, gh = Board([pkg]), GH("a" * 40)
    ctx = bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot")

    # The submission stays open: labelled, the brief pinned, the review created naming it.
    line = intake.intake(ctx, 12, yes=True)
    assert line == f"#12: open for review; created REV-PKG-Foo (#{board.find('REV-PKG-Foo').number})"
    rev = board.find("REV-PKG-Foo")
    assert rev.inputs == {"board_pr": 12, "operator": "alice", "after_comment": 0} and not gh.merged
    assert {"name": "needs-review"} in gh.pr["labels"] and boardpr.BRIEF_MARK in gh.comments[0]["body"]
    assert "Review (<agent>) of aaaaaaa" in gh.comments[0]["body"] and "/home/" not in gh.comments[0]["body"]
    assert board.posted[0] == (3, "Intake: authored by @alice (codex, gpt-6.1-sol) in #12.")

    # A listed browser reviewer claims it, reviews an old head (not counted), then the head.
    gh.say("roed-math", "reviewing (chatgpt)")
    gh.say("roed-math", review("chatgpt", "b" * 40, "accepted"))
    gh.say("mallory", review("chatgpt", "a" * 40, "accepted"))            # not a listed login: ignored
    out = boardpr.scan(ctx, yes=True)
    assert out == ["REV-PKG-Foo claimed by @roed-math"] and rev.state == "claimed"
    assert "Not counted: @roed-math's review is of `bbbbbbb`" in gh.comments[-1]["body"]
    gh.say("roed-math", review("chatgpt", "a" * 40, "needs_changes", findings=2))
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: REV-PKG-Foo needs_changes; created FIX-PKG-Foo~2")
    fix = board.find("FIX-PKG-Foo~2")
    assert fix.requires_operator == "alice" and fix.inputs["board_pr"] == 12 and rev.state == "done"
    assert "finding 2" in gh.files["packages/Foo/reports/REV-PKG-Foo.md"]
    assert boardpr.scan(ctx, yes=True) == []                                  # nothing new: nothing happens

    # The owner's fix lane pushes to the same branch; the next round reviews the new head.
    fix.state = "submitted"
    gh.pr["head"]["sha"] = "c" * 40
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: FIX-PKG-Foo~2 done; next round; created REV-PKG-Foo~2") and fix.state == "done"
    rev2 = board.find("REV-PKG-Foo~2")
    assert rev2.inputs["board_pr"] == 12 and rev2.independent_of == ["PKG-Foo", "FIX-PKG-Foo~2"]
    assert "Review (<agent>) of ccccccc" in gh.comments[0]["body"]          # the brief, refreshed in place
    gh.say("roed-math", review("chatgpt", "c" * 40, "accepted"))
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: REV-PKG-Foo~2 accepted; created RT-Foo~1a")
    rt_a, rt_b = board.find("RT-Foo~1a"), board.find("RT-Foo~1b")
    assert rt_a.inputs["board_pr"] == 12 and not gh.merged                 # the red team runs on the same PR

    # Pass a from the browser model; pass b from the worker that claimed it.
    gh.say("roed-math", prreviews.redteam_head(1, "a", "chatgpt", "c" * 40) + "\n\nVerdict: accepted\n")
    board.comment(rt_b.number, f"Claimed for `{rt_b.id}` by worker `bob-1` (@bob, claude).", login="bob")
    worker_pass = prreviews.render(prreviews.redteam_head(1, "b", "claude", "c" * 40), "needs_changes",
                                   "## R01 (P2) — An overclaim\nbody\n",
                                   {"job": rt_b.id, "worker": "bob-1", "agent": "claude", "model": "fable"})
    gh.say("bob", worker_pass)
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: RT-Foo~1a done; created nothing") and out[1].startswith("#12: RT-Foo~1b done; "
                                                                                         "created FIX-RT-Foo~1")
    fix_rt = board.find("FIX-RT-Foo~1")
    assert fix_rt.requires_operator == "alice" and fix_rt.inputs["board_pr"] == 12

    # After the last round's fix: merged, verdict written, authoring jobs done, the port created.
    fix_rt.state = "submitted"
    gh.pr["head"]["sha"] = "d" * 40
    out = boardpr.scan(ctx, yes=True)
    assert out == [f"#12: FIX-RT-Foo~1 done; merged; created PORT-Foo (#{board.find('PORT-Foo').number})"]
    assert gh.merged and pkg.state == "done" and '"status": "accepted"' in gh.files["packages/Foo/review.json"]
    assert "independent-review-REV-PKG-Foo~2" in gh.files["packages/Foo/review.json"]
    assert board.find("PORT-Foo").state == "available"


def test_a_worker_holding_the_review_is_not_preempted(monkeypatch):
    cfg = example_config()
    cfg.review.comment_reviewers = ["roed-math"]
    rev = new_job("REV-PKG-Foo", inputs={"board_pr": 12, "operator": "alice"})
    rev.number, rev.state = 5, "claimed"
    board, gh = Board([rev]), GH("a" * 40)
    gh.pr["labels"] = [{"name": "needs-review"}]
    ctx = bot_context(cfg=cfg, gh=gh, board=board)

    class Shared:
        repo, gitdir, project, dry_run = "o/claims", None, cfg.project.name, False

        def read(self, key):
            return {"owner": "bob/bob-1", "expires_at": 4_102_444_800}

        def recorded_authors(self, ids):
            return {}
    gh.say("roed-math", "reviewing")
    gh.say("roed-math", review("chatgpt", "a" * 40, "accepted"))
    assert boardpr.scan(ctx, Shared(), yes=True) == [] and rev.state == "claimed"     # waits for the worker


# --- the worker side: a review comments, a fix pushes to the pull request's branch ------------------

def test_a_worker_review_and_a_worker_fix_on_the_board_pull_request(tmp_path, monkeypatch):
    import dataclasses
    import json

    from tauceti_roadmap_worker import gitops
    from tauceti_roadmap_worker.lanes import lane_for

    from .fakes import FakeBoard, FakeClaims, FakeGH
    from .test_lane_dry_run import fake_checkout, fake_git
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(gitops, "git", fake_git)
    monkeypatch.setattr(gitops, "sync_checkout", fake_checkout)
    pushed = []
    monkeypatch.setattr(gitops, "commit_paths", lambda wt, paths, msg, login: pushed.append(("commit", sorted(paths))))
    monkeypatch.setattr(gitops, "ensure_fork", lambda gh, repo, login: f"{login}/TauCetiRoadmapJobs")
    monkeypatch.setattr(gitops, "push_branch", lambda wt, fork, branch: pushed.append(("push", fork, branch)))
    monkeypatch.setattr(gitops, "changed_files", lambda wt, base="HEAD": ["packages/Foo/README.md"])
    gh = FakeGH()
    gh.api = lambda path, paginate=False: {"head": {"ref": "w1/PKG-Foo", "repo": {"full_name": "alice/TauCetiRoadmapJobs"}},
                                           "user": {"login": "alice"}}
    gh.raw_file = lambda repo, path, ref="": "Review (chatgpt) of ccccccc\n\nVerdict: needs_changes\n\n1. **F01** (P1) x\n"
    from tauceti_roadmap_worker.lanes import Context
    ctx = Context(cfg=example_config(), gh=gh, board=FakeBoard(), claims=FakeClaims(), worker_id="alice-1",
                  operator="alice", agent="claude", model="fable")

    rev = new_job("REV-PKG-Foo~2", inputs={"board_pr": 12})
    lane = lane_for(rev)
    ws = lane.prepare(rev, ctx)
    lane.verdict_file(rev, ctx, ws).write_text(json.dumps({"review": {"status": "needs_changes"}}))
    ws.report.write_text("# Review of Foo (package, round 2)\n**Verdict:** needs changes\n\n## R01 (P1) — Wrong\nbody\n")
    lane.submit(rev, ctx, ws, "review")
    [(_m, _p, payload)] = [w for w in gh.writes if w[1].endswith("/issues/12/comments")]
    parsed = prreviews.parse(payload["body"])
    assert (parsed.kind, parsed.agent, parsed.verdict, parsed.findings) == ("review", "claude", "needs_changes", [("F01", "P1")])
    assert parsed.worker["job"] == "REV-PKG-Foo~2" and ctx.board.states == [("REV-PKG-Foo~2", "submitted")]

    fix = new_job("FIX-PKG-Foo~3", inputs={"board_pr": 12, "review": "REV-PKG-Foo~2"})
    lane = lane_for(fix)
    ws = lane.prepare(fix, ctx)
    gh.writes.clear()
    lane.submit(dataclasses.replace(fix), ctx, ws, "repairs")
    assert pushed == [("commit", ["packages/Foo/README.md"]), ("push", "alice/TauCetiRoadmapJobs", "w1/PKG-Foo")]
    [(_m, _p, note)] = [w for w in gh.writes if w[1].endswith("/issues/12/comments")]
    assert note["body"].startswith("Fix `FIX-PKG-Foo~3` pushed") and '"agent": "claude"' in note["body"]
    assert "Verdict: needs_changes" in (ws.scratch / "reviews" / "REV-PKG-Foo_r2.md").read_text()


def test_the_review_queue_lists_open_reviews_and_is_edited_in_place():
    rev = new_job("REV-PKG-Foo~2", inputs={"board_pr": 12})
    rev.number, rev.state = 5, "available"
    done = new_job("REV-PKG-Foo", inputs={"board_pr": 12})
    done.number, done.state = 4, "done"
    board, gh = Board([rev, done]), GH("c" * 40)
    gh.pr["labels"] = [{"name": "needs-review"}]
    issues, pinned = [], []
    api = gh.api
    gh.api = lambda path, paginate=False: issues if "labels=review-queue" in path else api(path, paginate)
    write = gh.write

    def write_(method, path, payload=None):
        if path.endswith("/issues") and method == "POST":
            issues.append({"number": 50, "body": payload["body"]})
            return {"number": 50}
        if path.endswith("/issues/50") and method == "PATCH":
            issues[0]["body"] = payload["body"]
            return {}
        return write(method, path, payload)
    gh.write = write_
    gh.cli_write = lambda *args: pinned.append(args)
    ctx = bot_context(cfg=example_config(), gh=gh, board=board)
    assert boardpr.review_queue(ctx, yes=True) == ["review queue #50 created"] and pinned == [("issue", "pin", "50", "-R",
                                                                                                "TauCetiProject/TauCetiRoadmapJobs")]
    assert "`REV-PKG-Foo~2` (available), head `ccccccc`" in issues[0]["body"] and "REV-PKG-Foo`" not in issues[0]["body"]
    assert boardpr.review_queue(ctx, yes=True) == []                     # unchanged: no write
    rev.state = "done"
    assert boardpr.review_queue(ctx, yes=True) == ["review queue #50 updated"]
    assert "Nothing is waiting for a review." in issues[0]["body"]
