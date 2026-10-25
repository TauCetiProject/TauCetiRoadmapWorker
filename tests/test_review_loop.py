"""The review loop settles instead of spinning (regression: QWittVectors on board PR #39, where the
same two findings came back every round, "six supplier interfaces remain unowned" and "the
dependency order cannot certify this roadmap", and each round cost one fix and one review):

- declared supplier gaps are contracts, not findings, and a review may accept conditional on them;
- a design-stage roadmap is checked as a merge-order candidate, so "not in the order" is no defect;
- a repair may mark a finding "not actionable in this package", and when the next review only
  re-raises such findings, the intake parks the next repair round for a human."""

import json
from types import SimpleNamespace

from tauceti_roadmap_worker import boardpr, intake, prompts, prreviews, replies
from tauceti_roadmap_worker.board import Outcome, encode_submission, new_job
from tauceti_roadmap_worker.cli import depcheck_lines
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.design import gaps_listing
from tauceti_roadmap_worker.lanes.review import verdict_problems

from .fakes import bot_context, example_config
from .test_boardpr import GH, Board
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

REPAIR = """# Repairs of REV-DESIGN-QWittVectors

## F01 (P1) — not actionable in this package: the six interfaces are declared gaps on LambdaRings and DeRhamWitt
The plan declares gap:frobenius-lift and five more; the suppliers must state them.

## F02 (P2) — not actionable in this package: QWittVectors enters the merge order when its design is accepted
No edit of the design can place it.

## F03 (P3) — applied
Locator fixed.
"""

REVIEW = """Review (codex) of abc1234

Verdict: needs_changes

1. **F01** (P1) Six essential interfaces still have no actual supplier target
   Re-raised: F01 (the suppliers have not stated them).
2. **F02** (P2) The dependency order still cannot certify this roadmap
   Re-raised: F02
3. **F03** (P3) A typo in Layer 2
"""


def test_not_actionable_findings_of_a_repair_report():
    settled = replies.not_actionable(REPAIR)
    assert set(settled) == {"F01", "F02"}
    assert settled["F01"][1].startswith("the six interfaces are declared gaps")
    assert replies.parse_findings(REPAIR)[2] == ("F03", "applied", "Locator fixed.")


def test_a_review_that_only_re_raises_settled_findings_is_stuck():
    stuck = intake.stuck_findings(REPAIR, REVIEW)
    assert [s.split(" ", 1)[0] for s in stuck] == ["`F01`", "`F02`"]           # the P3 does not count
    assert "re-raises `F01`, which the repair marked not actionable (the six interfaces" in stuck[0]
    fresh = REVIEW.replace("   Re-raised: F02\n", "")                         # F02 is now a new finding
    assert intake.stuck_findings(REPAIR, fresh) == []
    assert intake.stuck_findings("## F01 (P1) — applied\nok\n", REVIEW) == []   # nothing was settled
    # The same title counts as a re-raise too (a worker review that forgot the marker).
    repair = "## R01 (P1) Unowned Frobenius lift — not actionable in this package: LambdaRings must state it\n"
    assert intake.stuck_findings(repair, "## R04 (P1) — Unowned Frobenius lift\nstill unowned\n") != []
    assert intake.stuck_findings(repair, "## R04 (P1) — A false Check\n") == []


class LoopGH(GH):
    """The board's main with a review round's report and its repair report."""

    def __init__(self, files):
        super().__init__("f" * 40)
        self.pr["body"] = "Refs #9\n\n" + encode_submission("REV-DESIGN-QWittVectors~2", "w3", "carol", "codex",
                                                              "gpt-6-astra")
        self.pr["user"]["login"] = "carol"
        self.main = files

    def api(self, path, paginate=False):
        if "/pulls/12/files" in path:
            return [{"filename": f, "status": "modified"} for f in self.main if "REV-" in f or "review.json" in f]
        return super().api(path, paginate)

    def raw_file(self, repo, path, ref=""):
        return self.main.get(path, "")


def test_the_intake_parks_the_next_repair_round_for_a_human():
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    rev = new_job("REV-DESIGN-QWittVectors~2", independent_of=["DESIGN-QWittVectors", "FIX-DESIGN-QWittVectors~2"])
    rev.number, rev.state = 9, "submitted"
    pkg = "packages/QWittVectors"
    worker_review = (REVIEW.replace("1. **F01** (P1)", "## R01 (P1) —").replace("2. **F02** (P2)", "## R02 (P2) —")
                     .replace("3. **F03** (P3)", "## R03 (P3) —"))
    gh = LoopGH({f"{pkg}/reports/{rev.id}.md": worker_review, f"{pkg}/reports/FIX-DESIGN-QWittVectors~2.md": REPAIR,
                 f"{pkg}/design/review.json": json.dumps({"review": {"status": "needs_changes",
                                                                     "reviewer": f"independent-review-{rev.id}"}})})
    board = Board([rev])
    line = intake.intake(bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot"), 12, yes=True)
    assert "created FIX-DESIGN-QWittVectors~3 (blocked)" in line and "blocked: the review re-raises only" in line
    fix = board.find("FIX-DESIGN-QWittVectors~3")
    assert fix.state == "blocked"
    parked = [body for number, body in board.posted if number == 9 and body.startswith("Parked for a human")]
    assert parked and "`R01` (P1)" in parked[0] and "`R02` (P2)" in parked[0] and "R03" not in parked[0]


def test_a_board_review_reads_the_repair_at_the_head_it_reviewed():
    files = {("packages/Foo/reports/FIX-PKG-Foo~2.md", "c" * 40): REPAIR}
    reads = []

    def raw_file(repo, path, ref=""):
        reads.append((path, ref))
        return files.get((path, ref), "")
    bot = SimpleNamespace(cfg=example_config(), gh=SimpleNamespace(raw_file=raw_file))
    rev = new_job("REV-PKG-Foo~2", inputs={"board_pr": 12})
    stuck = intake.stuck_on(bot, rev, Outcome(verdict="needs_changes", report=REVIEW, head="c" * 40))
    assert len(stuck) == 2 and reads[0] == ("packages/Foo/reports/FIX-PKG-Foo~2.md", "c" * 40)
    assert intake.stuck_on(bot, new_job("REV-PKG-Foo"), Outcome(verdict="needs_changes", report=REVIEW)) == []   # round 1
    assert intake.stuck_on(bot, rev, Outcome(verdict="accepted", report=REVIEW)) == []


def test_a_design_is_checked_as_a_merge_order_candidate():
    order = ["LambdaRings", "DeRhamWitt", "Later"]
    siblings = {"LambdaRings": "", "DeRhamWitt": "", "Later": "*Needs:* QWittVectors §2.\n"}
    readme = "*Needs:* LambdaRings §3 and DeRhamWitt §1.\n"
    lines, problems = depcheck_lines("QWittVectors", readme, siblings, order, set(), candidate=True)
    assert problems == [] and lines[0].startswith("candidate: QWittVectors is not in the merge order yet; checked as "
                                                  "if placed after DeRhamWitt")
    _lines, plain = depcheck_lines("QWittVectors", readme, siblings, order, set())
    assert plain[0].startswith("NOT IN ORDER: QWittVectors")                 # the package-stage check is unchanged
    cyclic = "*Needs:* Later §1.\n"                                          # Later consumes QWittVectors
    lines, problems = depcheck_lines("QWittVectors", cyclic, siblings, order, set(), candidate=True)
    assert problems and problems[0].startswith("CYCLE:")


def test_design_stage_briefs_run_the_candidate_check(ctx):  # noqa: F811 - the shared fixture
    for job_id, flag in (("REV-DESIGN-Foo", "--candidate"), ("FIX-DESIGN-Foo~2", "--candidate"),
                         ("DESIGN-Foo", "--candidate"), ("REV-PKG-Foo", "--depcheck")):
        job = new_job(job_id)
        lane = lane_for(job)
        ws = lane.prepare(job, ctx)
        command = lane.common_values(job, ctx, ws)["DEPCHECK"]
        assert f" gate {flag} " in command and (" --only Foo " in command if flag == "--candidate" else True), command


def test_declared_gaps_are_listed_and_acceptance_may_be_conditional():
    plan = {"gaps": [{"slug": "frobenius-lift", "supplier": "LambdaRings", "section": "§3",
                      "needs": "a Frobenius lift on\n W(A) for every lambda-ring A"},
                     {"slug": "local", "supplier": "none", "needs": "a text"}]}
    listing = gaps_listing(plan)
    assert "- `gap:frobenius-lift`: supplier `LambdaRings` §3; needs: a Frobenius lift on W(A) for every" in listing
    assert "- `gap:local`: supplier `none`; needs: a text" in listing and gaps_listing({}) == "none declared"
    accepted = {"review": {"status": "accepted", "reviewer": "independent-review-REV-DESIGN-Foo", "blocking": 0,
                           "conditional_gaps": ["frobenius-lift"]}}
    assert verdict_problems(accepted, "REV-DESIGN-Foo", {"frobenius-lift", "local"}) == []
    assert "does not declare: lift" in verdict_problems(
        {"review": {**accepted["review"], "conditional_gaps": ["lift"]}}, "REV-DESIGN-Foo", {"frobenius-lift"})[0]
    assert "acceptance only" in verdict_problems(
        {"review": {**accepted["review"], "status": "needs_changes"}}, "REV-DESIGN-Foo", None)[0]
    # In the comment shape: rendered from the verdict file, parsed by the board, recorded on merge.
    body = prreviews.render(prreviews.review_head("codex", "a" * 40), "accepted", "# Review\n", {"job": "x"},
                            ["frobenius-lift"])
    parsed = prreviews.parse(body)
    assert parsed.verdict == "accepted" and parsed.conditional == ["frobenius-lift"]
    record = boardpr.verdict_record(new_job("REV-DESIGN-Foo"), [({"user": {"login": "carol"}}, parsed)], "accepted")
    assert record["conditional_gaps"] == ["frobenius-lift"]


def test_the_briefs_carry_the_rules():
    for lane in ("review", "redteam", "review-web"):
        text = prompts.template(lane)
        assert "it is not a finding" in text and "Re-raised: <its id in the repair report>" in text
        assert "__GAPS__" in text and "never a finding before then" in text
    assert "conditional_gaps" in prompts.template("review")
    assert "Conditional on the announced gaps:" in prompts.template("review-web")
    fix = prompts.template("fix")
    assert "not actionable in this package: <reason>" in fix and "declared as a supplier gap" in fix
