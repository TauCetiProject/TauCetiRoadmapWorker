"""An accepted design whose merge failed is merged later, and a pull request merged on GitHub finishes
its review (regressions on #63: the accepted verdict was consumed by the failed merge, nothing retried
it after a hand rebase, and after a hand merge the review job went back to `state:available`)."""

from tauceti_roadmap_worker import boardpr, depgraph
from tauceti_roadmap_worker.board import encode_submission, new_job
from tauceti_roadmap_worker.gh import GHError

from .fakes import bot_context, example_config
from .test_boardpr import GH, Board, review


class MergeGH(GH):
    """A design's board pull request: the merge can fail, the head can move, compare is answered."""

    def __init__(self, head):
        super().__init__(head)
        self.pr["body"] = "Refs #3\n\n" + encode_submission("DESIGN-Foo", "w1", "alice", "codex", "gpt-6.1-sol")
        self.pr["labels"] = [{"name": "needs-review"}]
        self.fail_merge, self.compare_files = "", []

    def api(self, path, paginate=False):
        if "/compare/" in path:
            return {"files": [{"filename": f} for f in self.compare_files]}
        if "/pulls/12/files" in path:
            return [{"filename": f, "status": "added"} for f in ("packages/Foo/design/README.md",
                                                                 "packages/Foo/reports/DESIGN-Foo.md")]
        if "/pulls?state=open" in path:
            return [] if self.merged or self.pr.get("merged") else [self.pr]
        return super().api(path, paginate)

    def write(self, method, path, payload=None):
        if path.endswith("/pulls/12/merge") and self.fail_merge:
            self.writes.append((method, path, payload))
            raise GHError(self.fail_merge)
        return super().write(method, path, payload)


def setup(head="a" * 40):
    cfg = example_config()
    cfg.review.comment_reviewers = ["roed-math"]
    cfg.board.bot_logins = ["board-bot"]
    design = new_job("DESIGN-Foo", inputs={"review_pr": 12})
    design.number, design.state = 3, "submitted"
    rev = new_job("REV-DESIGN-Foo", inputs={"board_pr": 12, "operator": "alice"})
    rev.number, rev.state = 4, "claimed"
    board, gh = Board([design, rev]), MergeGH(head)
    gh.say("roed-math", review("chatgpt", head, "accepted"))
    return bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot"), board, gh, design, rev


def test_a_failed_merge_is_retried_until_it_merges():
    ctx, board, gh, design, rev = setup()
    gh.fail_merge = "gh api -X PUT: gh: HTTP 502: Bad Gateway"
    out = boardpr.scan(ctx, yes=True)
    assert "accepted, but not merged yet" in out[0] and rev.inputs["merge_pending"]["head"] == "a" * 40
    assert rev.state != "done" and not gh.merged
    gh.fail_merge = ""
    out = boardpr.scan(ctx, yes=True)
    assert gh.merged and rev.state == "done" and design.state == "done" and "merge_pending" not in rev.inputs
    assert board.find("PKG-Foo") is not None and any("REV-DESIGN-Foo accepted" in line for line in out)


def test_a_rebase_that_only_merged_main_is_merged_without_a_fresh_review(monkeypatch):
    ctx, board, gh, _design, rev = setup()
    gh.fail_merge = "gh api -X PUT: gh: Pull Request has merge conflicts (HTTP 405)"
    monkeypatch.setattr(boardpr, "squash_with_main", lambda ctx, number, pr: (False, "could not merge it with main (test)"))
    boardpr.scan(ctx, yes=True)
    assert rev.inputs["merge_pending"]["head"] == "a" * 40
    # A human merged main into the branch: the head moved, only sources.toml (and main's own files) changed.
    gh.pr["head"]["sha"] = "b" * 40
    gh.compare_files = ["packages/Foo/sources.toml", "packages/Other/README.md", "merge-order/ORDER.txt"]
    gh.fail_merge = ""
    boardpr.scan(ctx, yes=True)
    assert gh.merged and rev.state == "done" and board.find("PKG-Foo") is not None
    merge_put = [w for w in gh.writes if w[1].endswith("/pulls/12/merge")][-1]
    assert merge_put[2]["sha"] == "b" * 40                                    # the rebased head is the one merged


def test_a_rebase_that_changed_the_design_needs_a_fresh_review():
    ctx, _board, gh, _design, rev = setup()
    gh.fail_merge = "gh api -X PUT: gh: HTTP 500"
    boardpr.scan(ctx, yes=True)
    gh.pr["head"]["sha"] = "c" * 40
    gh.compare_files = ["packages/Foo/design/README.md", "packages/Foo/sources.toml"]
    out = boardpr.scan(ctx, yes=True)
    assert "#12: REV-DESIGN-Foo needs a fresh review (the head changed packages/Foo/design/README.md)" in out
    assert "merge_pending" not in rev.inputs and not gh.merged and rev.state != "done"
    assert "review the new head" in gh.comments[-1]["body"]


def test_a_pull_request_merged_on_github_finishes_its_review_and_never_reopens_it():
    ctx, board, gh, design, rev = setup()
    rev.state = "available"                    # what the stale refusal had done to #292
    gh.pr["merged"], gh.pr["state"] = True, "closed"
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: merged on GitHub; #12: REV-DESIGN-Foo accepted")
    assert rev.state == "done" and design.state == "done" and board.find("PKG-Foo") is not None
    assert '"status": "accepted"' in gh.files["packages/Foo/design/review.json"]
    assert boardpr.scan(ctx, yes=True) == []                                   # nothing left to do


def test_a_package_merged_on_github_goes_on_to_its_port():
    ctx, board, gh, _design, _rev = setup()
    pkg_rev = new_job("REV-PKG-Foo", inputs={"board_pr": 12, "operator": "alice"})
    pkg_rev.number, pkg_rev.state = 5, "claimed"
    board.jobs = [pkg_rev]
    gh.pr["body"] = "Refs #6\n\n" + encode_submission("PKG-Foo", "w1", "alice", "codex", "gpt-6.1-sol")
    gh.pr["merged"] = True
    boardpr.scan(ctx, yes=True)
    assert pkg_rev.state == "done" and board.find("PORT-Foo") is not None and board.find("RT-Foo~1a") is None
    assert "Foo" in depgraph.read_order(gh.files["merge-order/ORDER.txt"])     # placed when its port was created


class ExpectBoard(Board):
    """A board whose conditional state moves are honoured (`supersede`)."""

    def set_state(self, job, state, comment="", expect=""):
        if expect and job.state != expect:
            return False
        super().set_state(job, state, comment)
        return True

    def close(self, job, comment="", label=""):
        self.comment(job.number, comment)


def merged_package(*jobs):
    """A package's board pull request, merged on GitHub with no accepting review, and the rounds it left."""
    ctx, _board, gh, _design, _rev = setup()
    gh.comments.clear()
    gh.pr["body"] = "Refs #6\n\n" + encode_submission("PKG-Foo", "w1", "alice", "codex", "gpt-6.1-sol")
    gh.pr["merged"], gh.pr["state"] = True, "closed"
    board = ExpectBoard(list(jobs))
    ctx.board = board
    return ctx, board, gh


def test_a_review_stranded_by_a_merge_on_github_reviews_the_boards_main():
    rev = new_job("REV-PKG-Foo~2", inputs={"board_pr": 12, "operator": "alice", "after_comment": 7})
    rev.number, rev.state = 5, "available"
    ctx, board, _gh = merged_package(rev)
    assert boardpr.scan(ctx) == ["#12: merged on GitHub; REV-PKG-Foo~2 would review the board's main (dry run)"]
    assert rev.inputs.get("board_pr") == 12                                    # a dry run writes nothing
    assert boardpr.scan(ctx, yes=True) == ["#12: merged on GitHub; REV-PKG-Foo~2 reviews the board's main"]
    assert rev.state == "available" and "board_pr" not in rev.inputs and "after_comment" not in rev.inputs
    assert rev.inputs["operator"] == "alice"
    assert "was merged on GitHub before this review ran" in board.posted[-1][1]
    assert boardpr.scan(ctx, yes=True) == []                                   # off the pull request: nothing to scan


def test_a_claimed_review_of_a_merged_pull_request_is_left_to_its_worker():
    rev = new_job("REV-PKG-Foo~2", inputs={"board_pr": 12, "operator": "alice"})
    rev.number, rev.state = 5, "claimed"
    ctx, _board, _gh = merged_package(rev)
    assert boardpr.scan(ctx, yes=True) == []
    assert rev.inputs["board_pr"] == 12


def test_a_repair_not_yet_run_goes_on_on_the_boards_main():
    fix = new_job("FIX-PKG-Foo~2", inputs={"board_pr": 12, "operator": "alice"})
    fix.number, fix.state = 7, "blocked"
    ctx, board, _gh = merged_package(fix)
    assert boardpr.scan(ctx, yes=True) == ["#12: merged on GitHub; FIX-PKG-Foo~2 repairs the board's main"]
    assert fix.state == "blocked" and "board_pr" not in fix.inputs             # a human's block stays a human's
    assert "It repairs the board's main" in board.posted[-1][1]


def test_a_port_that_follows_a_red_team_cut_short_resumes_it_on_the_roadmap_pull_request():
    from tauceti_roadmap_worker.board import Outcome, successors
    port = new_job("PORT-Foo", after=["FIX-RT-Foo~1"],
                   inputs={"redteam_round": 2, "previous": ["RT-Foo~1a", "RT-Foo~1b"], "repair": "FIX-RT-Foo~1"})
    nxt = successors(port, Outcome(pr=40, operator="alice"))
    assert sorted(j.id for j in nxt) == ["RT-Foo~2a", "RT-Foo~2b"]
    assert all(j.inputs["pr"] == 40 and j.inputs["repair"] == "FIX-RT-Foo~1" and "board_pr" not in j.inputs for j in nxt)
    assert all(j.inputs["previous"] == ["RT-Foo~1a", "RT-Foo~1b"] for j in nxt)
    first = new_job("PORT-Foo", inputs={"redteam_round": 1})
    assert sorted(j.id for j in successors(first, Outcome(pr=40, operator="alice"))) == ["RT-Foo~1a", "RT-Foo~1b"]
    # a port after a finished board red team (no round recorded) has no red team on the roadmap pull request
    assert successors(new_job("PORT-Foo", after=["FIX-RT-Foo~2"]), Outcome(pr=40, operator="alice")) == []
    assert successors(new_job("PORT-Foo", inputs={"redteam_round": 3}), Outcome(pr=40, operator="alice")) == []


def test_red_team_passes_stranded_by_a_merge_are_superseded_and_the_port_names_their_round():
    a = new_job("RT-Foo~2a", after=["FIX-RT-Foo~1"], inputs={"board_pr": 12, "operator": "alice", "repair": "FIX-RT-Foo~1",
                                                           "previous": ["RT-Foo~1a", "RT-Foo~1b"]})
    b = new_job("RT-Foo~2b", after=["FIX-RT-Foo~1"], inputs=dict(a.inputs))
    a.number, a.state, b.number, b.state = 8, "available", 9, "available"
    ctx, board, _gh = merged_package(a, b)
    out = boardpr.scan(ctx, yes=True)
    port = board.find("PORT-Foo")
    assert port is not None and port.inputs["redteam_round"] == 2 and port.inputs["repair"] == "FIX-RT-Foo~1"
    assert a.state == "done" and b.state == "done"
    assert any("RT-Foo~2a superseded" in line for line in out) and any("red team from round 2" in line for line in out)
    assert not [line for line in boardpr.scan(ctx, yes=True) if "created" in line]     # the port is made once
