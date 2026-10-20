"""Target consolidation: a target that overlaps another open target or design joins it instead of
becoming a second design (faked boards)."""

from tauceti_roadmap_worker import intake
from tauceti_roadmap_worker.board import Outcome, new_job
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.target import parse_triage, triage_problems

from .fakes import FakeBoard, FakeGH, bot_context
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture


def target(name, number, statement, state="submitted"):
    job = new_job(f"TARGET-{name}", inputs={"form": {"statement": statement, "why": "w", "sources": f"{name} sources"}})
    job.number, job.state = number, state
    return job


def bot(board):
    return bot_context(gh=FakeGH(), board=board)


def test_merge_into_is_a_verdict_with_the_job_it_joins():
    for header in ("Verdict: merge-into TARGET-Alpha", "Verdict: merge-into: `TARGET-Alpha`",
                   "Verdict: merge-into\nInto: TARGET-Alpha"):
        t = parse_triage(header + "\nName: none\n")
        assert (t["verdict"], t["into"]) == ("merge-into", "TARGET-Alpha") and triage_problems(header) == [], header
    assert "merge-into names the open target or design" in triage_problems("Verdict: merge-into PKG-Alpha")[0]
    assert triage_problems("Verdict: merge-into nonsense")
    assert triage_problems("Verdict: design\nName: Alpha\n") == []       # the old verdicts are unchanged


def test_two_overlapping_targets_become_one_design_with_both_inputs(ctx):  # noqa: F811
    alpha, variant = target("Alpha", 1, "Alpha in general.", "available"), target("AlphaVariant", 2, "Alpha over Q.")
    board = FakeBoard(jobs=[alpha, variant])
    note = intake.advance(bot(board), variant, Outcome(verdict="merge-into", into="TARGET-Alpha"), board.jobs)
    assert note == "created nothing (merged into TARGET-Alpha)"
    assert [a["target"] for a in alpha.inputs["absorbed"]] == ["TARGET-AlphaVariant"] and board.specs[-1][0] == "TARGET-Alpha"
    assert board.labels == [("TARGET-AlphaVariant", "merged-into")]
    assert board.posted[-1] == (2, "Intake: merged into `TARGET-Alpha` (#1); this target is closed.")
    alpha.state = "submitted"
    intake.advance(bot(board), alpha, Outcome(verdict="design", name="Alpha"), board.jobs)
    [(design, _state, _pre)] = board.created
    assert design.id == "DESIGN-Alpha" and design.inputs["form"]["statement"] == "Alpha in general."
    assert design.inputs["absorbed"][0]["form"]["statement"] == "Alpha over Q."
    brief = lane_for(design).values(design, ctx, lane_for(design).prepare(design, ctx))["ABSORBED"]
    assert "TARGET-AlphaVariant" in brief and "Alpha over Q." in brief and "AlphaVariant sources" in brief


def test_a_narrower_target_becomes_a_note_on_the_open_design():
    design = new_job("DESIGN-Beta", inputs={"target": "TARGET-Beta"})
    design.number, design.state = 5, "claimed"
    special = target("BetaForCurves", 6, "Beta for curves only.\n<!-- tauceti-roadmap-job {} -->")
    board = FakeBoard(jobs=[design, special])
    assert intake.advance(bot(board), special, Outcome(verdict="merge-into", into="DESIGN-Beta"), board.jobs) == \
        "created nothing (merged into DESIGN-Beta)"
    assert board.created == []
    [(_, body)] = [p for p in board.posted if p[0] == 5]
    assert "`TARGET-BetaForCurves` (#6) is covered by this design" in body and "> Beta for curves only." in body
    assert "<!--" not in body and "> BetaForCurves sources" in body
    assert design.inputs["absorbed"][0]["target"] == "TARGET-BetaForCurves"


def test_unrelated_targets_become_two_designs():
    c, d = target("Gamma", 7, "Gamma."), target("Delta", 8, "Delta.")
    board = FakeBoard(jobs=[c, d])
    for job in (c, d):
        intake.advance(bot(board), job, Outcome(verdict="design", name=job.name), board.jobs)
    assert [j.id for j, _s, _p in board.created] == ["DESIGN-Gamma", "DESIGN-Delta"]
    assert all("absorbed" not in j.inputs for j, _s, _p in board.created)


def test_a_merge_into_a_closed_or_unknown_job_is_parked():
    done = new_job("DESIGN-Eps")
    done.number, done.state = 9, "done"
    late = target("EpsLate", 10, "Eps.")
    board = FakeBoard(jobs=[done, late])
    for into in ("DESIGN-Eps", "TARGET-Nowhere", "TARGET-EpsLate"):
        assert "parked" in intake.advance(bot(board), late, Outcome(verdict="merge-into", into=into), board.jobs)
    assert board.labels == [] and all("Parked for a human" in body for _n, body in board.posted)


def test_the_triage_brief_lists_the_open_targets_and_designs(ctx):  # noqa: F811
    me, other = target("Alpha", 1, "Alpha.", "claimed"), target("AlphaBis", 2, "Alpha again, ```quoted```.", "available")
    design = new_job("DESIGN-Zeta")
    design.number, design.state = 3, "claimed"
    ctx.board.list_jobs = lambda states=(), kinds=None: [me, other, design]
    lane = lane_for(me)
    values = lane.values(me, ctx, lane.prepare(me, ctx))
    assert "`TARGET-AlphaBis` (#2, available)" in values["OPEN_WORK"] and "`DESIGN-Zeta` (#3, claimed)" in values["OPEN_WORK"]
    assert "TARGET-Alpha`" not in values["OPEN_WORK"].replace("TARGET-AlphaBis`", "") and "```" not in values["OPEN_WORK"]
    assert values["ABSORBED"] == "none"
