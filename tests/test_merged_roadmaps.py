"""A roadmap already on the content repository's main is a supplier available to every pull request:
the dependency check never reads it as UPWARD, the merge order never places or moves it, and its
place there never decides another roadmap's. Gate rounds of pull requests that merely edited such
roadmaps had placed 37 of them in the order as if new, failing the push gate of every consumer listed
above them."""

import pytest

from tauceti_roadmap_worker import cli
from tauceti_roadmap_worker.lanes import Context
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes.base import Workspace
from tauceti_roadmap_worker.paths import HERE

from .fakes import FakeBoard, example_config
from .test_checkout import needs_git
from .test_gate_scanners import content_clone

EXAMPLE = str(HERE / "tauceti-roadmap.toml.example")
ORDER = "# Merge order: suppliers before consumers, one roadmap per line.\nAlpha\nBar\nFoo  # on main\nOmega\n"


def order_ws(tmp_path, merged):
    board_dir = tmp_path / "board"
    (board_dir / "merge-order").mkdir(parents=True)
    (board_dir / "merge-order" / "ORDER.txt").write_text(ORDER)
    ws = Workspace(job_dir=tmp_path / "job", scratch=tmp_path / "job" / "scratch", board=board_dir,
                   content=tmp_path / "content", workdir=tmp_path / "wt", report=tmp_path / "report.md", branch="b",
                   job_id="GATE-Foo~1")
    ws.extra["merged"] = set(merged)
    return ws


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    return Context(cfg=example_config(), gh=None, board=FakeBoard(), claims=None, worker_id="w1", operator="alice",
                   agent="codex", model="m")


def test_a_roadmap_on_main_is_never_placed_or_moved_and_never_decides_a_place(ctx, tmp_path):
    ws = order_ws(tmp_path, {"Foo", "Gamma"})
    path = ws.board / "merge-order" / "ORDER.txt"
    # An edit of Foo, on main: nothing placed or moved, whatever its README cites.
    assert lane_base.ensure_in_order(ctx, ws, "Foo", "*Needs:* Omega §1.\n", "") == [
        "Foo is on main; the merge order does not list it"]
    assert lane_base.ensure_in_order(ctx, ws, "Gamma", "*Needs:* Omega §1.\n", "") == [
        "Gamma is on main; the merge order does not list it"]
    assert path.read_text() == ORDER
    # Bar, listed above Foo, which it cites: Foo is on main, so Bar stays where it is.
    assert lane_base.ensure_in_order(ctx, ws, "Bar", "*Needs:* Foo §2, Alpha §1.\n", "") == []
    assert path.read_text() == ORDER
    # A new roadmap citing Foo and Alpha goes right after Alpha, not after Foo.
    assert lane_base.ensure_in_order(ctx, ws, "Nova", "*Needs:* Foo §2, Alpha §1.\n", "") == [
        "Nova placed in the merge order after Alpha"]
    assert lane_base.load_order(path) == ["Alpha", "Nova", "Bar", "Foo", "Omega"]
    # Without Foo on main, the same README moves Bar after Foo, as before.
    ws = order_ws(tmp_path / "again", set())
    assert lane_base.ensure_in_order(ctx, ws, "Bar", "*Needs:* Foo §2.\n", "")[0].startswith(
        "Bar moved in the merge order from #2 to #3, right after its last supplier Foo")


def test_a_review_of_an_edit_to_a_roadmap_on_main_checks_it_against_the_order_as_it_is(ctx, tmp_path):
    from tauceti_roadmap_worker.lanes.prreview import PRReviewLane
    ws = order_ws(tmp_path, {"Foo"})
    ws.extra.update(readme=tmp_path / "README.md", lean=tmp_path / "Suggested.lean")
    (tmp_path / "README.md").write_text("*Needs:* Omega §1.\n")
    assert PRReviewLane().order_for_review(ctx, ws, "Foo") == ws.board / "merge-order" / "ORDER.txt"
    assert ws.extra["notes"] == ["Foo is on main; the merge order does not list it"] and "order_file" not in ws.extra


@needs_git
def test_the_workspace_reads_the_roadmaps_on_main_from_its_content_checkout(ctx, tmp_path):
    ws = order_ws(tmp_path, set())
    del ws.extra["merged"]
    ws.content = content_clone(tmp_path)
    assert lane_base.merged_for(ctx, ws) == {"Later", "Family", "Deep", "Old"} == ws.extra["merged"]


@needs_git
def test_the_gate_command_takes_the_roadmaps_on_main_from_the_worktree_or_content(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    wt = content_clone(tmp_path)
    order = tmp_path / "ORDER.txt"
    order.write_text("Toy\nLater\n")
    cli.main(["--conf", EXAMPLE, "gate", str(wt), "Toy", "--order", str(order)])
    out = capsys.readouterr().out
    assert out.startswith("gate Toy:") and "depcheck=0" in out and "UPWARD" not in out
    readme = str(wt / "TauCetiRoadmap" / "Toy" / "README.md")
    args = ["--conf", EXAMPLE, "gate", "--depcheck", readme, "--name", "Toy", "--order", str(order)]
    assert cli.main(args) == 1 and "UPWARD: Toy (#1) -> Later (#2)" in capsys.readouterr().out
    assert cli.main(args + ["--content", str(wt)]) == 0 and "UPWARD" not in capsys.readouterr().out
    # The agents' check moves nothing after a roadmap on main either.
    assert cli.main(args + ["--movable", "--content", str(wt)]) == 0
    assert capsys.readouterr().out == "depcheck: 1 roadmaps, 1 edges, 0 problem(s)\n"
    # The agents' dependency check names the worker's content checkout.
    ws = order_ws(tmp_path / "ws", set())
    job = lane_base.board.new_job("FIX-RT-Toy~1", inputs={"pr": 905})
    command = lane_base.Lane().depcheck_command(job, Context(cfg=example_config(), gh=None, board=None, claims=None,
                                                             worker_id="w1", operator="alice", agent="codex", model="m"),
                                                ws, order)
    assert command.endswith(f"--movable --content {ws.content}")
