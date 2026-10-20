"""Every lane's round ends with a one-line summary: the round log's `submitted:` line, the commit
subject and the submission's description (regression: the pilot logged
"DESIGN-QWittVectors submitted: " with nothing after the colon)."""

import dataclasses
import json
from types import SimpleNamespace

from tauceti_roadmap_worker import agents
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.base import Workspace, count
from tauceti_roadmap_worker.lanes.design import design_summary
from tauceti_roadmap_worker.lanes.package import package_summary
from tauceti_roadmap_worker.lanes.review import review_summary

from .fakes import FakeGH, FakeTransport, example_config
from .test_checkout import NAME, live, needs_git  # noqa: F401 - the shared fixture
from .test_repair import agent_script, good

PLAN = {"roadmap": "QWittVectors",
        "layers": [{"key": "1", "targets": [{"id": "1.1", "kind": "definition", "tests": [{}, {}, {}]},
                                            {"id": "1.2", "kind": "theorem"}]},
                   {"key": "2", "targets": [{"id": "2.1", "kind": "theorem", "prerequisites": ["gap:frobenius"]}]}],
        "gaps": [{"slug": "frobenius", "needs": "a Frobenius lift", "supplier": "LambdaRings", "section": "3"},
                 {"slug": "local", "needs": "a text", "supplier": "none"}]}


def test_count():
    assert (count(1, "gap"), count(0, "gap"), count(5, "target")) == ("1 gap", "0 gaps", "5 targets")


def test_a_design_names_its_layers_targets_tests_and_gaps():
    assert design_summary("QWittVectors", PLAN) == \
        "design of QWittVectors: 2 layers, 3 targets, 3 tests, 2 gaps (1 for suppliers)"
    assert design_summary("Empty", {"layers": []}) == "design of Empty: 0 layers, 0 targets, 0 tests, 0 gaps"


def test_a_package_names_its_targets_declarations_and_sources(tmp_path):
    (tmp_path / "README.md").write_text("# Foo\n\n**Theorem A.** text\n\n**Definition B.** text\n\n**Checks.** none\n")
    (tmp_path / "Suggested.lean").write_text("import Mathlib\n\ndef foo : Nat := 1\n\ntheorem bar : foo = 1 := rfl\n")
    (tmp_path / "sources.toml").write_text('roadmap = "Foo"\n\n[[source]]\nid = "A"\n\n[[source]]\nid = "B"\n')
    assert package_summary("Foo", tmp_path) == "package of Foo: 2 targets, 2 Lean declarations, 2 sources"
    (tmp_path / "sources.toml").write_text("not toml [")
    assert package_summary("Foo", tmp_path).endswith(", 0 sources")
    assert package_summary("Foo", tmp_path / "missing") == ""


def test_a_review_names_its_verdict_and_findings():
    report = "# Review of Foo (package)\n\n## R01 (P1) — wrong locator\n\nx\n\n## R02 (P3) — typo\n\ny\n"
    assert review_summary("review of the Foo package, round 1", {"status": "needs_changes"}, report) == \
        "review of the Foo package, round 1: needs changes, 2 findings (1 P1/P2)"
    assert review_summary("r", {"status": "accepted", "blocking": 0}, "# Review\n\nAll good.\n") == "r: accepted, 0 blocking"


def workspace(tmp_path, report_text: str) -> Workspace:
    report = tmp_path / "report.md"
    report.write_text(report_text)
    return Workspace(job_dir=tmp_path, scratch=tmp_path, board=tmp_path / "board", content=tmp_path, workdir=tmp_path,
                     report=report, branch="b", extra={"head": "abcdef1234567"})


def test_the_review_lane_reads_its_verdict_file_and_report(tmp_path):
    cfg = example_config()
    job = new_job("REV-DESIGN-Foo~2", inputs={"board_pr": 7})
    lane = lane_for(job)
    ws = workspace(tmp_path, "# Review of Foo (design)\n\n## R01 (P2) — gap\n\nx\n")
    verdict = ws.package(cfg, "Foo") / "design" / "review.json"
    verdict.parent.mkdir(parents=True)
    verdict.write_text(json.dumps({"review": {"status": "needs_changes", "blocking": 1}}))
    assert lane.finish(job, SimpleNamespace(cfg=cfg), ws) == \
        "review of the Foo design, round 2 at abcdef1: needs changes, 1 finding (1 P1/P2)"
    verdict.unlink()
    assert lane.finish(job, SimpleNamespace(cfg=cfg), ws) == ""     # falls back to the generic line


def test_a_design_or_package_fix_counts_what_it_applied(tmp_path):
    job = new_job("FIX-PKG-Foo~2", inputs={"review": "REV-PKG-Foo~1"})
    ws = workspace(tmp_path, "# Repairs\n\n## R01 (P1) wrong — applied\n\nok\n\n## R02 (P3) typo — disputed\n\nno\n")
    assert lane_for(job).finish(job, SimpleNamespace(cfg=example_config()), ws) == \
        "round 2 of the Foo package repaired: 1 of 2 findings applied"


@needs_git
def test_a_lane_without_a_summary_still_says_what_it_submitted(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    run, _briefs = agent_script(good)
    monkeypatch.setattr(agents, "run_agent", run)
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(f"TARGET-{NAME}", inputs={"form": {"statement": "s", "why": "w"}})
    job.number = 1
    lane = lane_for(job)
    seen = []
    monkeypatch.setattr(lane, "finish", lambda job, ctx, ws: "")
    monkeypatch.setattr(lane, "submit", lambda job, ctx, ws, summary: seen.append(summary) or "https://example.invalid/1")
    result = lane.run(job, ctx)
    assert result.status == "submitted" and result.detail == seen[0] == f"target of {NAME}", result.detail
