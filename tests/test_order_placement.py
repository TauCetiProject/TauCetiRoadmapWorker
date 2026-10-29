"""A roadmap that is not in the merge order yet is placed after its last supplier by the lane that
first needs the order (the pilot's PORT-ArithmeticDynamics failed with "NOT IN ORDER")."""

import pytest

from tauceti_roadmap_worker import depgraph, worker
from tauceti_roadmap_worker.board import Board, allowed_paths, new_job, order_file
from tauceti_roadmap_worker.lanes import Context, lane_for
from tauceti_roadmap_worker.lanes.base import LaneBlocked

from .fakes import FakeClaims, FakeIssuesGH, example_config, job_issue
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

ORDER = "# Merge order\nAlpha\nBeta  # a comment\nGamma\n"


def test_no_suppliers_goes_to_the_end():
    text, notes = depgraph.place_in_order(ORDER, "Foo", set())
    assert depgraph.read_order(text) == ["Alpha", "Beta", "Gamma", "Foo"] and notes == []
    assert "Beta  # a comment" in text


def test_after_the_last_listed_supplier():
    text, notes = depgraph.place_in_order(ORDER, "Foo", {"Alpha", "Beta"}, {"Gamma"})
    assert depgraph.read_order(text) == ["Alpha", "Beta", "Foo", "Gamma"] and notes == []
    assert depgraph.place_in_order(text, "Foo", {"Gamma"}) == (text, [])            # already placed: untouched


def test_a_supplier_missing_from_the_order_is_warned_about():
    text, notes = depgraph.place_in_order(ORDER, "Foo", {"Alpha", "Delta"})
    assert depgraph.read_order(text) == ["Alpha", "Foo", "Beta", "Gamma"]
    assert notes == ["supplier Delta of Foo is not in the merge order; it must be inserted before Foo"]


def test_a_cycle_is_refused():
    with pytest.raises(depgraph.OrderCycle, match="Foo consumes Gamma .* is consumed by Beta"):
        depgraph.place_in_order(ORDER, "Foo", {"Gamma"}, {"Beta"})


def test_suppliers_come_from_the_readme_and_the_lean():
    readme = "## Layer 1\n\n*Needs:* Alpha §1.2 (the comparison map).\n"
    lean = "import Mathlib\nimport TauCetiRoadmap.Gamma.Suggested\n"
    assert depgraph.package_suppliers("Foo", readme, lean, {"Alpha", "Beta", "Gamma"}) == {"Alpha", "Gamma"}


def test_the_port_lane_places_the_roadmap_and_submits_the_order(ctx):  # noqa: F811
    board_dir = ctx.wdir / "board"
    (board_dir / "merge-order").mkdir(parents=True)
    (board_dir / "merge-order" / "ORDER.txt").write_text(ORDER)
    pkg = board_dir / "packages" / "Foo"
    pkg.mkdir(parents=True)
    (pkg / "README.md").write_text("*Needs:* Beta §2.\n")
    (pkg / "Suggested.lean").write_text("import Mathlib\n")
    job = new_job("PORT-Foo")
    ws = lane_for(job).prepare(job, ctx)
    assert depgraph.read_order((board_dir / "merge-order" / "ORDER.txt").read_text()) == ["Alpha", "Beta", "Foo", "Gamma"]
    assert ws.extra["order_notes"] == ["Foo placed in the merge order after Beta"]
    assert order_file(ctx.cfg) == "merge-order/ORDER.txt"
    assert "merge-order/ORDER.txt" in allowed_paths(job, "packages", order_file(ctx.cfg))
    assert "merge-order/ORDER.txt" not in allowed_paths(new_job("PKG-Foo"), "packages", order_file(ctx.cfg))


class Cycle:
    agent_started = False

    def eligible(self, job, run_ctx):
        return ""

    def run(self, job, run_ctx):
        raise LaneBlocked("merge order: Foo consumes Gamma (#3) and is consumed by Beta (#2), which comes first")


def test_a_cycle_blocks_the_job_without_counting_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "PORT-Foo", "available")])
    wctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=FakeClaims(), worker_id="w1", operator="alice",
                   agent="codex", model="m")
    monkeypatch.setattr(worker, "lane_for", lambda job: Cycle())
    result = worker.work_once(wctx, None, log=lambda line: None)
    assert result.status == "blocked" and gh.states(1) == ["blocked"] and worker.failures(wctx) == {}
    assert "A human decides" in gh.comments[1][-1] and wctx.claims.released


# --- a listed roadmap above one of its suppliers moves after the last of them (2026-10-22) ------------

PS_ORDER = ("# Merge order\nSchemeAndStackFoundations\nPeriodsAndSpecialValues  # imported\nMotives\n"
            "SelmerIwasawaCohomologyPartII\nShimura\nGanGrossPrasadUnitaryGroups\nRankinSelberg\n")


def test_a_listed_roadmap_above_its_suppliers_moves_right_after_the_last_of_them():
    text, note = depgraph.move_after_suppliers(PS_ORDER, "PeriodsAndSpecialValues",
                                               {"SchemeAndStackFoundations", "SelmerIwasawaCohomologyPartII",
                                                "GanGrossPrasadUnitaryGroups", "Unlisted"}, {"RankinSelberg"})
    assert depgraph.read_order(text) == ["SchemeAndStackFoundations", "Motives", "SelmerIwasawaCohomologyPartII",
                                         "Shimura", "GanGrossPrasadUnitaryGroups", "PeriodsAndSpecialValues",
                                         "RankinSelberg"]
    assert "PeriodsAndSpecialValues  # imported\n" in text and text.startswith("# Merge order\n")   # line and comment kept
    assert note == ("PeriodsAndSpecialValues moved in the merge order from #2 to #6, right after its last supplier "
                    "GanGrossPrasadUnitaryGroups (SelmerIwasawaCohomologyPartII also came after it)")
    # Idempotent; nothing to do for a roadmap above none of its suppliers, or not listed.
    assert depgraph.move_after_suppliers(text, "PeriodsAndSpecialValues", {"GanGrossPrasadUnitaryGroups"}) == (text, "")
    assert depgraph.move_after_suppliers(PS_ORDER, "Motives", {"SchemeAndStackFoundations"}) == (PS_ORDER, "")
    assert depgraph.move_after_suppliers(PS_ORDER, "Absent", {"Motives"}) == (PS_ORDER, "")
    # A duplicate line of the roadmap goes too: the order lists it once, after its suppliers.
    text, _ = depgraph.move_after_suppliers("A\nFoo\nB\nFoo\nC\n", "Foo", {"C"})
    assert text == "A\nB\nC\nFoo\n"


def test_a_move_that_would_put_a_consumer_first_is_refused():
    with pytest.raises(depgraph.OrderCycle, match="PeriodsAndSpecialValues consumes GanGrossPrasadUnitaryGroups "
                       r"\(#6\) and is consumed by Shimura \(#5\), which comes first"):
        depgraph.move_after_suppliers(PS_ORDER, "PeriodsAndSpecialValues", {"GanGrossPrasadUnitaryGroups"},
                                      {"Shimura", "RankinSelberg"})


def test_repair_upward_moves_every_roadmap_it_can_and_says_why_not_for_the_rest():
    readmes = {"PeriodsAndSpecialValues": "*Needs:* SelmerIwasawaCohomologyPartII §2; GanGrossPrasadUnitaryGroups §3.\n",
               "RankinSelberg": "*Needs:* PeriodsAndSpecialValues §1.\n",
               "Motives": "*Needs:* RankinSelberg §4.\n",            # consumed by Shimura, which comes first
               "Shimura": "*Needs:* Motives §1.\n"}
    text, notes = depgraph.repair_upward(PS_ORDER, readmes)
    # Motives sits above RankinSelberg, and Shimura (its consumer) above both: Shimura rides along.
    assert depgraph.read_order(text) == ["SchemeAndStackFoundations", "SelmerIwasawaCohomologyPartII",
                                         "GanGrossPrasadUnitaryGroups", "PeriodsAndSpecialValues", "RankinSelberg",
                                         "Motives", "Shimura"]
    assert notes["PeriodsAndSpecialValues"].startswith("PeriodsAndSpecialValues moved in the merge order from #2 to #6")
    assert notes["Motives"] == ("Motives moved in the merge order from #2 to #7, right after its last supplier "
                                "RankinSelberg; its consumer Shimura moved with it")
    assert set(notes) == {"PeriodsAndSpecialValues", "Motives"}
    assert depgraph.repair_upward(text, readmes) == (text, {})
    assert depgraph.repair_upward(text, {"PeriodsAndSpecialValues": readmes["PeriodsAndSpecialValues"]}) == (text, {})
    # `only` limits the moves to the named roadmaps.
    assert depgraph.repair_upward(PS_ORDER, readmes, only={"RankinSelberg"}) == (PS_ORDER, {})


def test_repair_upward_refuses_a_real_cycle_and_a_rider_it_did_not_read():
    readmes = {"PeriodsAndSpecialValues": "*Needs:* SelmerIwasawaCohomologyPartII §2; GanGrossPrasadUnitaryGroups §3.\n",
               "RankinSelberg": "*Needs:* PeriodsAndSpecialValues §1; Shimura §2.\n",
               "Motives": "*Needs:* RankinSelberg §4.\n",
               "Shimura": "*Needs:* Motives §1.\n"}          # Motives -> RankinSelberg -> Shimura -> Motives
    text, notes = depgraph.repair_upward(PS_ORDER, readmes)
    assert depgraph.read_order(text) == ["SchemeAndStackFoundations", "Motives", "SelmerIwasawaCohomologyPartII",
                                         "Shimura", "GanGrossPrasadUnitaryGroups", "PeriodsAndSpecialValues",
                                         "RankinSelberg"]
    assert notes["Motives"] == ("Motives not moved in the merge order: Motives consumes RankinSelberg (#7) and is "
                                "consumed by Shimura (#4), which comes first; Motives lies on a dependency cycle with "
                                "Shimura, RankinSelberg, so no move repairs the order: a cycle through Motives")
    # A consumer whose README was not read cannot ride along: the move is refused and says so.
    with pytest.raises(depgraph.OrderCycle, match="consumed by Shimura \\(#5\\), which comes first and whose README "
                       "was not read"):
        depgraph.move_after_suppliers(PS_ORDER, "PeriodsAndSpecialValues", {"GanGrossPrasadUnitaryGroups"},
                                      {"Shimura"}, consumer_closure={"PeriodsAndSpecialValues": {"Shimura"}},
                                      supplier_closure={}, movable={"PeriodsAndSpecialValues"})


def test_the_port_lane_moves_a_listed_roadmap_after_its_last_supplier(ctx):  # noqa: F811
    board_dir = ctx.wdir / "board"
    (board_dir / "merge-order").mkdir(parents=True)
    order = board_dir / "merge-order" / "ORDER.txt"
    order.write_text("# Merge order\nAlpha\nFoo\nBeta\nGamma\n")
    pkg = board_dir / "packages" / "Foo"
    pkg.mkdir(parents=True)
    (pkg / "README.md").write_text("*Needs:* Beta §2.\n")
    (pkg / "Suggested.lean").write_text("import Mathlib\n")
    job = new_job("PORT-Foo")
    ws = lane_for(job).prepare(job, ctx)
    assert depgraph.read_order(order.read_text()) == ["Alpha", "Beta", "Foo", "Gamma"]      # the submission carries it
    note = "Foo moved in the merge order from #2 to #3, right after its last supplier Beta"
    assert ws.extra["order_notes"] == [note] and note in (ws.job_dir / "requests.log").read_text()
    # A consumer of Foo listed between it and its supplier rides along with it.
    order.write_text("Alpha\nFoo\nGamma\nBeta\n")
    gamma = ctx.wdir / "content" / ctx.cfg.roadmap.active_dir / "Gamma"
    gamma.mkdir(parents=True)
    (gamma / "README.md").write_text("*Needs:* Foo §1.\n")
    ws = lane_for(job).prepare(job, ctx)
    assert order.read_text() == "Alpha\nBeta\nFoo\nGamma\n"
    assert ws.extra["order_notes"] == ["Foo moved in the merge order from #2 to #4, right after its last supplier Beta; "
                                       "its consumer Gamma moved with it"]


def test_a_gate_reads_the_order_moved_in_place_when_the_submission_carries_it_else_in_a_copy(ctx):  # noqa: F811
    from tauceti_roadmap_worker.lanes.base import MOVED_ORDER_COPY, Workspace, gate_order_file, repair_order_file
    board_dir = ctx.wdir / "board"
    (board_dir / "merge-order").mkdir(parents=True)
    order = board_dir / "merge-order" / "ORDER.txt"
    ws = Workspace(job_dir=ctx.wdir / "jobs" / "J", scratch=ctx.wdir / "jobs" / "J" / "scratch", board=board_dir,
                   content=ctx.wdir / "content", workdir=board_dir, report=ctx.wdir / "r.md", branch="w1/J")
    ws.job_dir.mkdir(parents=True)
    readme, lean = "*Needs:* Beta §1.\n", "import Mathlib\nimport TauCetiRoadmap.Gamma.Suggested\n"
    order.write_text("Alpha\nFoo\nBeta\nGamma\n")
    assert repair_order_file(ctx, ws, "Foo", readme, lean, order, carry=True) == order      # the Lean's supplier too
    assert order.read_text() == "Alpha\nBeta\nGamma\nFoo\n" and "order_file" not in ws.extra
    order.write_text("Alpha\nFoo\nBeta\nGamma\n")
    copy = repair_order_file(ctx, ws, "Foo", readme, "", order)
    assert copy == ws.scratch / MOVED_ORDER_COPY and copy.read_text() == "Alpha\nBeta\nFoo\nGamma\n"
    assert order.read_text() == "Alpha\nFoo\nBeta\nGamma\n" and gate_order_file(ctx.cfg, ws, "Foo") == copy
    assert repair_order_file(ctx, ws, "Foo", readme, "", copy) == copy                    # moved once, logged once
    log = (ws.job_dir / "requests.log").read_text()
    assert log.count("Foo moved in the merge order from #2 to #3, right after its last supplier Beta (in the copy") == 1
