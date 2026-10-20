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
