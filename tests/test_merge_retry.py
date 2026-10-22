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
