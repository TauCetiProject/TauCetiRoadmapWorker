"""Jobs created before the board pull requests run and are taken in as before: a red team on a
roadmap pull request (`pr`) keeps its fix and its next round there, a review with no board pull
request reviews the package folder on main and is merged by the intake, and a port that follows a
review (not a red-team fix) is red-teamed on its roadmap pull request as it was then. New jobs use
the board flow."""

import json

from tauceti_roadmap_worker import intake
from tauceti_roadmap_worker.board import Outcome, encode_submission, new_job, successors
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.base import Lane

from .fakes import bot_context, example_config
from .test_boardpr import GH, Board


def ids(jobs):
    return [j.id for j in jobs]


def test_a_red_team_on_a_roadmap_pull_request_finishes_there():
    rt_b = new_job("RT-ArithmeticDynamics~1b", independent_of=["PORT-ArithmeticDynamics"], requires_agent="claude",
                   inputs={"pr": 1336, "operator": "CBirkbeck"})
    [fix] = successors(rt_b, Outcome(round_complete=True), redteam_rounds=2)
    assert fix.id == "FIX-RT-ArithmeticDynamics~1" and fix.requires_operator == "CBirkbeck"
    assert fix.inputs == {"pr": 1336, "operator": "CBirkbeck",
                          "reviews": ["RT-ArithmeticDynamics~1a", "RT-ArithmeticDynamics~1b"]}
    second = successors(fix, Outcome(), redteam_rounds=2)
    assert ids(second) == ["RT-ArithmeticDynamics~2a", "RT-ArithmeticDynamics~2b"]
    assert second[0].inputs["pr"] == 1336 and "board_pr" not in second[0].inputs
    assert second[0].independent_of == ["PORT-ArithmeticDynamics", "FIX-RT-ArithmeticDynamics~1"]
    [fix2] = successors(second[1], Outcome(round_complete=True), redteam_rounds=2)
    assert fix2.inputs["pr"] == 1336 and successors(fix2, Outcome(), redteam_rounds=2) == []   # humans next
    for job in (rt_b, fix, second[0]):
        assert Lane.board_pr(job) == 0                       # the lanes take the roadmap pull request path
    assert type(lane_for(fix)).__name__ == "RedTeamFixLane" and type(lane_for(rt_b)).__name__ == "RedTeamLane"


def test_a_review_without_a_board_pull_request_goes_on_as_before():
    rev = new_job("REV-DESIGN-QWittVectors", independent_of=["DESIGN-QWittVectors"])
    assert Lane.board_pr(rev) == 0
    assert ids(successors(rev, Outcome(verdict="accepted"))) == ["PKG-QWittVectors"]   # a new-flow package
    [fix] = successors(rev, Outcome(verdict="needs_changes"))
    assert fix.id == "FIX-DESIGN-QWittVectors~2" and "board_pr" not in fix.inputs   # edits the folder on main
    assert ids(successors(new_job("REV-PKG-Foo"), Outcome(verdict="accepted"))) == ["PORT-Foo"]


def test_a_port_after_a_review_is_red_teamed_on_its_roadmap_pull_request():
    port = new_job("PORT-Foo", after=["REV-PKG-Foo~2"])
    passes = successors(port, Outcome(pr=905, operator="alice"), redteam_rounds=2, redteam_agents=("codex", "claude"))
    assert ids(passes) == ["RT-Foo~1a", "RT-Foo~1b"]
    assert passes[0].inputs == {"pr": 905, "operator": "alice"} and passes[0].independent_of == ["PORT-Foo"]
    assert successors(port, Outcome(pr=905), redteam_rounds=0) == []
    assert successors(new_job("PORT-Foo", after=["FIX-RT-Foo~2"]), Outcome(pr=905)) == []   # red-teamed already


class LegacyGH(GH):
    """A red-team pass's submission pull request on the board: its report only."""

    def __init__(self, job_id):
        super().__init__("e" * 40)
        self.job_id = job_id
        self.pr["body"] = "Refs #7\n\n" + encode_submission(job_id, "w2", "bob", "claude", "fable")
        self.pr["head"]["ref"] = f"w2/{job_id}"
        self.pr["user"] = {"login": "bob"}

    def api(self, path, paginate=False):
        if "/pulls/12/files" in path:
            return [{"filename": f"packages/ArithmeticDynamics/reports/{self.job_id}.md", "status": "added"}]
        return super().api(path, paginate)


def test_the_intake_merges_an_old_red_team_pass_and_creates_its_fix():
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    rt_a = new_job("RT-ArithmeticDynamics~1a", independent_of=["PORT-ArithmeticDynamics"],
                   inputs={"pr": 1336, "operator": "CBirkbeck"})
    rt_b = new_job("RT-ArithmeticDynamics~1b", independent_of=["PORT-ArithmeticDynamics"],
                   inputs={"pr": 1336, "operator": "CBirkbeck"})
    rt_a.number, rt_a.state, rt_b.number, rt_b.state = 6, "done", 7, "submitted"
    board, gh = Board([rt_a, rt_b]), LegacyGH(rt_b.id)
    ctx = bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot")
    line = intake.intake(ctx, 12, yes=True)
    assert line.startswith("#12: merged RT-ArithmeticDynamics~1b; created FIX-RT-ArithmeticDynamics~1"), line
    assert gh.merged and {"name": "needs-review"} not in gh.pr["labels"]
    fix = board.find("FIX-RT-ArithmeticDynamics~1")
    assert fix.inputs["pr"] == 1336 and not fix.inputs.get("board_pr") and fix.requires_operator == "CBirkbeck"


class LegacyReviewGH(GH):
    """A review's submission pull request on the board (report and verdict), then the next round's
    design fix submitted the old way (a pull request of its own)."""

    def __init__(self, job_id, files, verdict):
        super().__init__("f" * 40)
        self.pr["body"] = "Refs #9\n\n" + encode_submission(job_id, "w3", "carol", "codex", "gpt-6-astra")
        self.pr["head"]["ref"] = f"w3/{job_id}"
        self.pr["user"]["login"] = "carol"
        self.changed, self.verdict = files, verdict

    def api(self, path, paginate=False):
        if "/pulls/12/files" in path:
            return [{"filename": f, "status": "modified"} for f in self.changed]
        return super().api(path, paginate)

    def raw_file(self, repo, path, ref=""):
        if path.endswith("review.json"):
            return json.dumps({"review": {"status": self.verdict, "reviewer": "independent-review-REV-DESIGN-QWittVectors"}})
        return super().raw_file(repo, path, ref)


def test_an_old_design_review_is_merged_and_its_fix_enters_the_board_flow():
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    rev = new_job("REV-DESIGN-QWittVectors", independent_of=["DESIGN-QWittVectors"])
    rev.number, rev.state = 9, "submitted"
    pkg = "packages/QWittVectors"
    board = Board([rev])
    gh = LegacyReviewGH(rev.id, [f"{pkg}/reports/{rev.id}.md", f"{pkg}/design/review.json"], "needs_changes")
    ctx = bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot")
    line = intake.intake(ctx, 12, yes=True)
    assert line.startswith("#12: merged REV-DESIGN-QWittVectors; created FIX-DESIGN-QWittVectors~2"), line
    fix = board.find("FIX-DESIGN-QWittVectors~2")
    assert "board_pr" not in fix.inputs and fix.inputs["review"] == rev.id

    # The fix, done the old way on the folder on main, opens its own pull request: from here on the
    # design is reviewed on it (the new flow).
    fix.number, fix.state = 10, "submitted"
    gh2 = LegacyReviewGH(fix.id, [f"{pkg}/design/README.md", f"{pkg}/reports/{fix.id}.md"], "")
    gh2.pr["body"] = "Refs #10\n\n" + encode_submission(fix.id, "w4", "dave", "claude", "fable")
    gh2.pr["user"]["login"] = "dave"
    ctx2 = bot_context(cfg=cfg, gh=gh2, board=board, operator="board-bot")
    line = intake.intake(ctx2, 12, yes=True)
    assert line.startswith("#12: open for review; created REV-DESIGN-QWittVectors~2"), line
    rev2 = board.find("REV-DESIGN-QWittVectors~2")
    assert rev2.inputs["board_pr"] == 12 and not gh2.merged and fix.inputs["review_pr"] == 12


def test_an_old_design_review_that_accepts_releases_a_new_flow_package():
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    rev = new_job("REV-DESIGN-QWittVectors", independent_of=["DESIGN-QWittVectors"])
    rev.number, rev.state = 9, "submitted"
    pkg = "packages/QWittVectors"
    board = Board([rev])
    gh = LegacyReviewGH(rev.id, [f"{pkg}/reports/{rev.id}.md", f"{pkg}/design/review.json"], "accepted")
    line = intake.intake(bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot"), 12, yes=True)
    assert line.startswith("#12: merged REV-DESIGN-QWittVectors; created PKG-QWittVectors"), line
    assert "QWittVectors" in gh.files.get(cfg.roadmap.merge_order_file, "")       # placed in the merge order
    assert board.find("PKG-QWittVectors").inputs == {}                           # it opens its own pull request
