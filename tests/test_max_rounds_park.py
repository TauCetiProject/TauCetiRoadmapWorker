"""At `[board] max_rounds` the next repair round is created as state:blocked, like a re-raise park,
so a human has one documented way on: move the fix to state:available to run the next round, or
close it (regression: DESIGN-QWittVectors and DESIGN-ColemanIntegrationPartII were parked with a
comment and no job, their board pull requests open, and nothing to move)."""

import json

from tauceti_roadmap_worker import intake
from tauceti_roadmap_worker.board import Outcome, new_job

from .fakes import FakeBoard, bot_context, example_config

REVIEW = """Review (codex) of abc1234

Verdict: needs_changes

1. **F01** (P1) The abelian logarithm's field of definition is wrong
   Witness: ...
2. **F02** (P3) A locator typo
"""


class Board(FakeBoard):
    """Jobs that `create` adds to `all_jobs`, with numbers."""

    def create(self, job, state="available", preamble=""):
        super().create(job, state, preamble)
        job.number, job.state = 600 + len(self.created), state
        self.jobs.append(job)
        return job.number


class RawGH:
    """The board's main: no reports beyond those given."""

    def __init__(self, files=None):
        self.files = dict(files or {})

    def raw_file(self, repo, path, ref=""):
        return self.files.get(path, "")

    def write(self, *a, **k):
        return {}


def ctx_for(board, gh=None, max_rounds=3):
    cfg = example_config()
    cfg.board.max_rounds = max_rounds
    return bot_context(cfg=cfg, board=board, gh=gh or RawGH())


def review(round_, **inputs):
    job = new_job(f"REV-DESIGN-ColemanIntegrationPartII~{round_}", inputs=inputs)
    job.number, job.state = 40 + round_, "done"
    return job


def test_the_example_allows_five_rounds():
    assert example_config().board.max_rounds == 5


def test_at_max_rounds_the_next_fix_is_parked_for_a_human():
    board = Board(jobs=[])
    rev = review(3, board_pr=39, operator="alice")
    line = intake.advance(ctx_for(board), rev, Outcome(verdict="needs_changes", report=REVIEW, head="c" * 40), [rev])
    assert line == "created FIX-DESIGN-ColemanIntegrationPartII~4 (blocked) (parked after 3 rounds)"
    [(fix, state, preamble)] = board.created
    assert state == "blocked" and fix.inputs == {"review": rev.id, "board_pr": 39, "operator": "alice", "base_head": "c" * 40}
    assert preamble.startswith("Parked for a human after 3 rounds: move this fix to state:available to run round 4, "
                               "or close it to stop.")
    assert "- `F01` (P1) The abelian logarithm's field of definition is wrong" in preamble and "`F02` (P3)" in preamble
    [(number, comment)] = board.posted
    assert number == rev.number and comment.startswith("Parked for a human after 3 rounds: "
                                                       "`FIX-DESIGN-ColemanIntegrationPartII~4` (#601) is blocked")
    # The human moved it on: round 4 ran, its review still asks for changes, and it parks again.
    rev4 = review(4, board_pr=39, operator="alice")
    line = intake.advance(ctx_for(board), rev4, Outcome(verdict="needs_changes", report=REVIEW), board.jobs + [rev4])
    assert "FIX-DESIGN-ColemanIntegrationPartII~5 (blocked)" in line
    assert board.created[-1][2].startswith("Parked for a human after 4 rounds: move this fix to state:available to run "
                                           "round 5")
    # Below the cap a review that asks for changes gets an ordinary fix.
    fresh = Board(jobs=[])
    intake.advance(ctx_for(fresh, max_rounds=5), review(3), Outcome(verdict="needs_changes", report=REVIEW), [])
    assert [(f.id, s) for f, s, _p in fresh.created] == [("FIX-DESIGN-ColemanIntegrationPartII~4", "available")]


class PullGH(RawGH):
    def api(self, path, paginate=False):
        assert path.endswith("/pulls/39"), path
        return {"head": {"sha": "d" * 40}}


def test_board_sync_parks_reviews_that_were_capped_before():
    old_note = ("Parked for a human: QWittVectors: the design still needs changes after 3 rounds; a human decides how "
                "to continue.")
    q3 = new_job("REV-DESIGN-QWittVectors~3", inputs={"board_pr": 39, "operator": "alice"})
    q2 = new_job("REV-DESIGN-QWittVectors~2", inputs={"board_pr": 39, "operator": "alice"})
    accepted = new_job("REV-DESIGN-Accepted~3")
    quiet = new_job("REV-DESIGN-Quiet~3")
    for n, job in enumerate((q3, q2, accepted, quiet), 1):
        job.number, job.state = 70 + n, "done"
    board = Board(jobs=[q3, q2, accepted, quiet, new_job("PKG-Accepted")],
                  comments={71: [{"body": "Intake: needs_changes on #39 by @roed-math (chatgpt)."}, {"body": old_note}],
                            73: [{"body": old_note.replace("QWittVectors", "Accepted")}],
                            74: [{"body": "Intake: needs_changes on #5."}]})
    gh = PullGH()
    gh.files["packages/QWittVectors/reports/REV-DESIGN-QWittVectors~3.md"] = REVIEW
    ctx = ctx_for(board, gh, max_rounds=5)
    expected = "REV-DESIGN-QWittVectors~3: parked at max_rounds; created FIX-DESIGN-QWittVectors~4 (blocked)"
    assert intake.repark_capped(ctx) == [expected]
    [(fix, state, preamble)] = board.created
    assert state == "blocked" and fix.inputs["base_head"] == "d" * 40 and "`F01` (P1)" in preamble
    assert intake.repark_capped(ctx) == []                                   # idempotent: the fix exists now
    # A blocked fix a human moved to available is theirs: nothing re-parks it, and it is claimable.
    fix.state = "available"
    assert intake.repark_capped(ctx) == [] and fix.state == "available"
    json.dumps(fix.spec())                                                   # a plain job spec
