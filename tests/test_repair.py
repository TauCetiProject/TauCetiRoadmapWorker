"""A failed check gets one repair run before the job fails; a retry starts from the earlier
attempt; a plan may name a missing supplier contract (`gap:<slug>`), which the intake announces
(the pilot's DESIGN-QWittVectors, refused for eight invented `gap:` prerequisites)."""

import dataclasses
import json

from tauceti_roadmap_worker import agents, intake
from tauceti_roadmap_worker.board import Outcome, new_job
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.design import validate_plan

from .fakes import FakeBoard, FakeGH, FakeTransport, bot_context
from .test_checkout import NAME, PKG, live, needs_git  # noqa: F401 - the shared fixture

JOB = f"TARGET-{NAME}"


def plan(**over):
    data = {"roadmap": "Foo", "layers": [{"key": "1", "targets": [
        {"id": "1.1", "kind": "theorem", "statement": "t", "locator": {"source": "A", "where": "Thm 1"},
         "prerequisites": ["gap:cyclotomic-contract", "roadmap:Bar §2"]}]}],
        "gaps": [{"slug": "cyclotomic-contract", "title": "t", "neededBy": [], "needs": "The ideal equals (Φ_m).",
                  "supplier": "HabiroCyclotomicCompletions", "section": "§2.3"}]}
    data.update(over)
    return data


def test_a_plan_may_name_a_missing_supplier_contract():
    assert validate_plan(plan(), "Foo") == []
    unknown = plan(gaps=[])
    assert validate_plan(unknown, "Foo") == ["plan.json: target 1.1: gap:cyclotomic-contract names no entry of `gaps`"]
    vague = plan(gaps=[{"id": "cyclotomic-contract", "title": "t", "detail": "..."}])          # the pilot's shape
    assert validate_plan(vague, "Foo") == [
        "plan.json: gap cyclotomic-contract: `needs` (one sentence: what is missing) is required",
        "plan.json: gap cyclotomic-contract: `supplier` must name a roadmap (CamelCase) or be \"none\""]
    unowned = plan(gaps=[{"slug": "cyclotomic-contract", "needs": "x", "supplier": "none"}])
    assert validate_plan(unowned, "Foo") == []
    assert "is not mathlib:/tauceti:/roadmap:/target:/gap:" in validate_plan(
        plan(layers=[{"key": "1", "targets": [{"id": "1.1", "kind": "theorem", "statement": "t",
                                               "locator": {"source": "A", "where": "x"},
                                               "prerequisites": ["contract:x"]}]}]), "Foo")[0]


def test_the_intake_announces_supplier_gaps():
    design = new_job("DESIGN-QWitt")
    design.number, design.state = 9, "done"
    supplier = new_job("PKG-HabiroCyclotomicCompletions")
    supplier.number, supplier.state = 4, "submitted"
    gh = FakeGH()
    gh.raw_file = lambda repo, path, ref="": json.dumps(plan(gaps=plan()["gaps"] + [
        {"slug": "derham", "needs": "The strict envelope.", "supplier": "DerivedDeRhamCohomology", "section": "§1"},
        {"slug": "local", "needs": "x", "supplier": "none"}]))
    board = FakeBoard(jobs=[design, supplier])
    note = intake.announce_gaps(bot_context(gh=gh, board=board), design, board.jobs)
    assert note == "2 supplier gap(s) announced"
    [(n1, theirs), (n2, mine)] = board.posted
    assert n1 == 4 and "`QWitt` needs from `HabiroCyclotomicCompletions` §2.3: The ideal equals (Φ_m)." in theirs
    assert n2 == 9 and "`DerivedDeRhamCohomology` has no open job" in mine
    assert "supplier gap(s) announced" in intake.advance(bot_context(gh=gh, board=FakeBoard(jobs=[design])), design,
                                                         Outcome(), [design])


def agent_script(*steps):
    """Each agent run carries out the next step; returns the briefs it was given."""
    briefs, it = [], iter(steps)

    def run(task, base_env=None):
        briefs.append(task.brief)
        next(it)(task.workdir / PKG)
        return agents.AgentResult(exit_code=0, seconds=1.0)
    return run, briefs


def bad(pkg):
    (pkg / "target.md").write_text("Verdict: maybe\nName: QSeries\n")
    (pkg / "reports" / f"{JOB}.md").write_text("# Triage\n")


def good(pkg):
    (pkg / "target.md").write_text("Verdict: design\nName: QSeries\nBase: none\nOwner: none\n")
    (pkg / "reports").mkdir(parents=True, exist_ok=True)
    (pkg / "reports" / f"{JOB}.md").write_text("# Triage\n")


@needs_git
def test_a_failed_check_gets_one_repair_run(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    run, briefs = agent_script(bad, good)
    monkeypatch.setattr(agents, "run_agent", run)
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(JOB, inputs={"form": {"statement": "s", "why": "w"}})
    job.number = 1
    lane = lane_for(job)
    monkeypatch.setattr(lane, "submit", lambda job, ctx, ws, summary: "https://example.invalid/1")
    result = lane.run(job, ctx)
    assert result.status == "submitted", result.detail
    assert len(briefs) == 2 and "## Repair" in briefs[1] and "target.md: Verdict must be one of" in briefs[1]
    assert not (ctx.wdir / "jobs" / JOB / "problems.txt").exists()


@needs_git
def test_a_retry_starts_from_the_earlier_attempt(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    run, briefs = agent_script(bad, bad, good)
    monkeypatch.setattr(agents, "run_agent", run)
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(JOB, inputs={"form": {"statement": "s", "why": "w"}})
    job.number = 1
    lane = lane_for(job)
    first = lane.run(job, ctx)
    assert first.status == "failed" and len(briefs) == 2 and "Verdict must be one of" in first.detail
    problems = ctx.wdir / "jobs" / JOB / "problems.txt"
    assert problems.is_file()
    lane = lane_for(job)
    monkeypatch.setattr(lane, "submit", lambda job, ctx, ws, summary: "https://example.invalid/1")
    assert lane.run(job, ctx).status == "submitted"
    retry = briefs[2]
    assert "## An earlier attempt" in retry and "problems.previous.txt" in retry
    assert str(ctx.wdir / "jobs" / JOB / "unsubmitted") in retry
