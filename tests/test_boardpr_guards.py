"""The guards an independent review asked for on the board pull request flow."""

import pytest

from tauceti_roadmap_worker import boardpr, intake, prreviews
from tauceti_roadmap_worker.board import allowed_paths, authorship_from_comments, new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.base import Lane, LaneError, checkout_board_pr

from .fakes import bot_context, example_config
from .test_boardpr import GH, Board
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture


def test_a_red_team_fix_on_a_board_pull_request_may_change_the_package():
    fix = new_job("FIX-RT-Foo~1", inputs={"board_pr": 12})
    paths = allowed_paths(fix, "packages", "merge-order/ORDER.txt")
    assert {"packages/Foo/README.md", "packages/Foo/Suggested.lean", "merge-order/ORDER.txt"} <= paths
    assert allowed_paths(new_job("FIX-RT-Foo~1", inputs={"pr": 905}), "packages") == {"packages/Foo/reports/FIX-RT-Foo~1.md"}


def test_the_intake_leaves_a_pull_request_under_review_to_the_scan():
    gh = GH("a" * 40)
    gh.pr["labels"] = [{"name": "needs-review"}]
    ctx_ = bot_context(cfg=example_config(), gh=gh, board=Board([]))
    assert intake.intake(ctx_, 12, yes=True) == "#12: under review on the board (read by the review scan)"
    assert gh.writes == []


def test_the_author_note_is_an_authorship_record():
    notes = [{"user": {"login": "github-actions[bot]"}, "body": "Intake: authored by @alice (codex, gpt-6.1-sol) in #12."}]
    assert authorship_from_comments(notes, ["github-actions[bot]"]) == [{"login": "alice", "agent": "codex",
                                                                          "model": "gpt-6.1-sol"}]


def flow(state="submitted"):
    cfg = example_config()
    cfg.review.comment_reviewers = ["roed-math"]
    cfg.board.bot_logins = ["board-bot"]
    cfg.redteam.rounds = 1
    pkg = new_job("PKG-Foo", inputs={"review_pr": 12})
    pkg.number, pkg.state = 3, "submitted"
    fix = new_job("FIX-RT-Foo~1", inputs={"board_pr": 12, "operator": "alice", "base_head": "c" * 40, "authors": ["PKG-Foo"]})
    fix.number, fix.state = 7, state
    gh = GH("d" * 40)
    gh.pr["labels"] = [{"name": "needs-review"}]
    b = Board([pkg, fix])
    return bot_context(cfg=cfg, gh=gh, board=b, operator="board-bot"), gh, b, pkg, fix


def test_a_failed_merge_keeps_the_fix_open_and_is_tried_again():
    c, gh, b, pkg, fix = flow()
    write = gh.write

    def refusing(method, path, payload=None):
        if path.endswith("/merge"):
            raise GHError("405 Pull Request is not mergeable")
        return write(method, path, payload)
    gh.write = refusing
    out = boardpr.scan(c, yes=True)
    assert out == ["#12: FIX-RT-Foo~1 pushed, but not merged yet (merge failed (405 Pull Request is not mergeable))"]
    assert fix.state == "submitted" and pkg.state == "submitted" and b.find("PORT-Foo") is None
    gh.write = write
    assert boardpr.scan(c, yes=True)[0].startswith("#12: FIX-RT-Foo~1 done; merged; created PORT-Foo")
    [merge] = [p for m, path, p in gh.writes if path.endswith("/merge")]
    assert merge == {"merge_method": "squash", "sha": "d" * 40}             # the head that was checked


def test_a_merge_waits_for_the_checks_and_refuses_strange_files():
    c, gh, _b, _pkg, fix = flow()
    api = gh.api
    gh.api = lambda path, paginate=False: ([{"filename": "packages/Foo/paper.pdf", "status": "added"}]
                                           if "/files" in path else api(path, paginate))
    out = boardpr.scan(c, yes=True)
    assert "not merged yet" in out[0] and "paper.pdf: a source document" in out[0] and not gh.merged
    assert fix.state == "submitted"


def test_the_boards_own_workflow_run_and_cancelled_runs_are_not_checks_on_a_submission():
    from tauceti_roadmap_worker.intake import _checks
    c, gh, _b, _pkg, _fix = flow()
    api = gh.api
    runs = [{"name": "board", "status": "completed", "conclusion": "failure", "started_at": "2026-10-23T00:21:53Z"},
            {"name": "board", "status": "completed", "conclusion": "cancelled", "started_at": "2026-10-23T00:31:00Z"},
            {"name": "lint", "status": "completed", "conclusion": "cancelled", "started_at": "2026-10-23T00:21:53Z"}]
    gh.api = lambda path, paginate=False: {"check_runs": runs} if "/check-runs" in path else api(path, paginate)
    assert _checks(c, "TauCetiProject/TauCetiRoadmapJobs", "d" * 40) == ([], False)
    runs.append({"name": "lint", "status": "completed", "conclusion": "failure", "started_at": "2026-10-23T00:40:00Z"})
    assert _checks(c, "TauCetiProject/TauCetiRoadmapJobs", "d" * 40) == (["lint"], False)
    runs.append({"name": "lint", "status": "queued", "conclusion": None, "started_at": "2026-10-23T00:50:00Z"})
    assert _checks(c, "TauCetiProject/TauCetiRoadmapJobs", "d" * 40) == ([], True)


def test_a_fix_that_did_not_move_the_head_is_released():
    c, gh, _b, _pkg, fix = flow()
    gh.pr["head"]["sha"] = "c" * 40
    assert boardpr.scan(c, yes=True) == ["#12: FIX-RT-Foo~1 did not move the head; available again"]
    assert fix.state == "available" and not gh.merged


def test_a_stale_worker_review_releases_its_job():
    rev = new_job("REV-PKG-Foo", inputs={"board_pr": 12})
    rev.number, rev.state = 5, "submitted"
    gh, b = GH("a" * 40), Board([rev])
    gh.pr["labels"] = [{"name": "needs-review"}]
    b.claimed_by[rev.id] = "bob"
    gh.say("bob", prreviews.render(prreviews.review_head("claude", "b" * 40), "accepted", "",
                                   {"job": rev.id, "worker": "bob-1", "agent": "claude", "model": "fable"}))
    c = bot_context(cfg=example_config(), gh=gh, board=b)
    assert boardpr.scan(c, yes=True) == [] and rev.state == "available"
    assert boardpr.scan(c, yes=True) == [] and len([x for x in gh.comments if "Not counted" in x["body"]]) == 1


def test_a_design_or_package_job_never_takes_the_board_pull_request_path():
    assert Lane.board_pr(new_job("PKG-Foo", inputs={"board_pr": 12, "review_pr": 12})) == 0
    assert Lane.board_pr(new_job("FIX-PKG-Foo~2", inputs={"board_pr": 12})) == 12


def test_a_fix_refuses_to_push_when_the_pull_request_cannot_be_read(ctx, monkeypatch):  # noqa: F811
    class Down:
        def api(self, path, paginate=False):
            raise GHError("403 rate limit")
    import dataclasses
    run_ctx = dataclasses.replace(ctx, gh=Down())
    job = new_job("FIX-PKG-Foo~2", inputs={"board_pr": 12, "review": "REV-PKG-Foo"})
    ws = Lane.prepare(lane_for(job), job, run_ctx)
    with pytest.raises(LaneError, match="cannot be read"):
        checkout_board_pr(run_ctx, ws, 12)


def test_an_unreadable_review_is_caught_before_it_is_posted():
    lane = lane_for(new_job("REV-PKG-Foo"))
    bad = prreviews.render(prreviews.review_head("codex", "a" * 40), "needs_changes", "Some prose, no findings.", {})
    assert lane.comment_problems(bad)[0].startswith("report: the board cannot read it as a review")
    assert boardpr.COMMENT_CLAIM_TTL == 4 * 3600


def test_a_review_longer_than_a_comment_is_caught_before_it_is_posted():
    lane = lane_for(new_job("REV-PKG-Foo"))
    report = "# Review of Foo (package, round 2)\n\n**Verdict:** needs changes\n\n## R01 (P1) — x\n\n" + ("| a | b |\n" * 20_000)
    body = prreviews.render(prreviews.review_head("codex", "a" * 40), "needs_changes", report, {})
    assert len(body) > 65_536
    assert lane.comment_problems(body)[0].startswith("report: too long to post as a pull-request comment")
