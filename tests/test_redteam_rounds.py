"""The passes of a red-team round review one package (design review R06, item 1). A round used to be
complete when its pass ids were done: pass a reviewed one head, the branch changed, pass b reviewed
another, and the board created the round's fix with pass a never invalidated. Each pass now records
the head it reviewed, and a pass done at a head whose package has changed since is reopened."""

import pytest

from tauceti_roadmap_worker import boardpr, prreviews
from tauceti_roadmap_worker.board import new_job, transition
from tauceti_roadmap_worker.gh import GHError

from .fakes import bot_context, example_config
from .test_boardpr import GH, TABLE, Board
from .test_review_contract import not_counted

H1, H2 = "a" * 40, "b" * 40


class CompareGH(GH):
    """The package's board pull request, with GitHub's compare of two heads answered."""

    def __init__(self, head):
        super().__init__(head)
        self.pr["labels"] = [{"name": "needs-review"}]
        self.compare_files: list[str] | Exception = []

    def api(self, path, paginate=False):
        if "/compare/" in path:
            if isinstance(self.compare_files, Exception):
                raise self.compare_files
            return {"files": [{"filename": f} for f in self.compare_files]}
        return super().api(path, paginate)


def round_one():
    """Round 1 of the red team open on #12 (passes codex/gpt-6-astra and claude/fable) at H1."""
    cfg = example_config()
    cfg.review.comment_reviewers, cfg.board.bot_logins = ["roed-math"], ["board-bot"]
    pkg = new_job("PKG-Foo", inputs={"review_pr": 12})
    pkg.number, pkg.state = 3, "submitted"
    inputs = {"board_pr": 12, "operator": "alice", "authors": ["PKG-Foo"], "after_comment": 0}
    rt_a = new_job("RT-Foo~1a", requires_agent="codex", requires_model="gpt-6-astra", inputs=dict(inputs))
    rt_b = new_job("RT-Foo~1b", requires_agent="claude", requires_model="fable", inputs=dict(inputs))
    rt_a.number, rt_b.number, rt_a.state, rt_b.state = 7, 8, "available", "available"
    board, gh = Board([pkg, rt_a, rt_b]), CompareGH(H1)
    return bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot"), board, gh, rt_a, rt_b


def pass_(gh, letter, agent, head):
    gh.say("roed-math", prreviews.redteam_head(1, letter, agent, head) + "\n\nVerdict: accepted\n\n" + TABLE)


def test_a_pass_done_at_a_head_whose_package_changed_is_reopened_and_the_round_waits_for_it():
    ctx, board, gh, rt_a, rt_b = round_one()
    pass_(gh, "a", "chatgpt", H1)
    assert boardpr.scan(ctx, yes=True) == ["#12: RT-Foo~1a done; created nothing"]
    assert rt_a.state == "done" and rt_a.inputs["reviewed_head"] == H1
    # The owner pushes a change to a reviewed statement; pass b reviews the new head.
    gh.pr["head"]["sha"], gh.compare_files = H2, ["packages/Foo/README.md"]
    pass_(gh, "b", "claude", H2)
    last = gh.comments[-1]["id"]
    out = boardpr.scan(ctx, yes=True)
    assert out == ["#12: RT-Foo~1b done; RT-Foo~1a reopened for `bbbbbbb` (it reviewed `aaaaaaa`, and "
                   "packages/Foo/README.md changed since); created nothing"]
    assert board.find("FIX-RT-Foo~1") is None and rt_b.inputs["reviewed_head"] == H2
    assert rt_a.state == "available" and rt_a.inputs["after_comment"] == last
    assert ("PATCH", "repos/TauCetiProject/TauCetiRoadmapJobs/issues/7", {"state": "open"}) in gh.writes
    assert any(c["body"].startswith("Intake: `RT-Foo~1a` is open again for `bbbbbbb`: it reviewed `aaaaaaa`")
               for c in gh.comments)
    assert "Red team round 1 pass a (<agent>) of bbbbbbb" in next(c["body"] for c in gh.comments
                                                                   if boardpr.BRIEF_MARK in c["body"])
    # Pass a's comment of H1 answered the earlier head; its review of H2 completes the round.
    assert boardpr.scan(ctx, yes=True) == [] and not_counted(gh) == []
    pass_(gh, "a", "chatgpt", H2)
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: RT-Foo~1a done; created FIX-RT-Foo~1")


def test_a_rebase_that_left_the_package_alone_keeps_the_earlier_pass():
    ctx, board, gh, rt_a, _rt_b = round_one()
    pass_(gh, "a", "chatgpt", H1)
    boardpr.scan(ctx, yes=True)
    gh.pr["head"]["sha"] = H2
    gh.compare_files = ["packages/Foo/sources.toml", "packages/Other/README.md", "merge-order/ORDER.txt"]
    pass_(gh, "b", "claude", H2)
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: RT-Foo~1b done; created FIX-RT-Foo~1")
    assert rt_a.state == "done" and board.find("FIX-RT-Foo~1") is not None


def test_a_change_that_cannot_be_read_leaves_the_pass_for_the_next_scan():
    ctx, board, gh, _rt_a, rt_b = round_one()
    pass_(gh, "a", "chatgpt", H1)
    boardpr.scan(ctx, yes=True)
    gh.pr["head"]["sha"], gh.compare_files = H2, GHError("gh api: HTTP 502: Bad Gateway")
    pass_(gh, "b", "claude", H2)
    assert boardpr.scan(ctx, yes=True) == [("#12: RT-Foo~1b not concluded yet: the changes since an earlier pass's "
                                            "head could not be read; the next scan tries again")]
    assert rt_b.state == "available" and "reviewed_head" not in rt_b.inputs
    gh.compare_files = []
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: RT-Foo~1b done; created FIX-RT-Foo~1")


def test_a_reopen_that_stops_part_way_is_done_again_by_the_next_scan():
    ctx, board, gh, rt_a, rt_b = round_one()
    pass_(gh, "a", "chatgpt", H1)
    boardpr.scan(ctx, yes=True)
    gh.pr["head"]["sha"], gh.compare_files = H2, ["packages/Foo/Suggested.lean"]
    pass_(gh, "b", "claude", H2)
    write = gh.write

    def failing(method, path, payload=None):
        if path.endswith("/issues/7") and payload == {"state": "open"}:
            raise GHError("gh api: HTTP 502: Bad Gateway")
        return write(method, path, payload)
    gh.write = failing
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: skipped: GitHub did not answer")
    assert rt_a.state == "done" and rt_b.state == "available"           # the pass is not concluded
    gh.write = write
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: RT-Foo~1b done; RT-Foo~1a reopened for `bbbbbbb` (it "
                                                     "reviewed `aaaaaaa`, and packages/Foo/Suggested.lean changed")
    assert rt_a.state == "available" and board.find("FIX-RT-Foo~1") is None


def test_a_pass_concluded_before_heads_were_recorded_keeps_the_head_its_report_names():
    ctx, board, gh, rt_a, _rt_b = round_one()
    rt_a.state = "done"
    report = ("<!-- from @roed-math, https://example.invalid -->\n" + prreviews.redteam_head(1, "a", "codex", H1)
              + "\n\nVerdict: accepted\n\n" + TABLE)
    gh.raw_file = lambda repo, path, ref="": report if path.endswith("/reports/RT-Foo~1a.md") else "# Foo\n"
    pass_(gh, "b", "claude", H1)
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: RT-Foo~1b done; created FIX-RT-Foo~1")
    assert rt_a.state == "done"


def test_a_pass_whose_reviewed_head_is_unknown_is_reviewed_again():
    ctx, board, gh, rt_a, _rt_b = round_one()
    rt_a.state = "done"
    pass_(gh, "b", "claude", H1)
    out = boardpr.scan(ctx, yes=True)
    assert out == ["#12: RT-Foo~1b done; RT-Foo~1a reopened for `aaaaaaa` (the head it reviewed is unknown); "
                   "created nothing"]
    assert rt_a.state == "available" and board.find("FIX-RT-Foo~1") is None


def test_only_the_board_reopens_a_done_job():
    assert transition("done", "reopen") == "available"
    with pytest.raises(ValueError):
        transition("done", "claim")
