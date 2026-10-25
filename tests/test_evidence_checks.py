"""Two checks on what a run hands on: an acceptance (or a red-team pass with no finding) carries its
adversarial table with computed values, and a repair report has a section for every finding of the
review it answers."""

import json
import subprocess

import pytest

from tauceti_roadmap_worker import config, prreviews, replies
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import base, lane_for
from tauceti_roadmap_worker.paths import HERE

TABLE = """## Verified sound

| Statement | Instances tried | Values | Result | Change |
|---|---|---|---|---|
| 1.2 `ghost_image` | ℤ, 𝔽₂, S = ∅ | (0,1) over 𝔽₂ has ghosts 0, 0 | needs torsion-freeness | none |
| 4.4 `relativeGhost` | A = ℤ[T], m = 2 | T⊗1 = 1⊗T² maps to T² | holds | none |
"""
SUMMARY_TABLE = """| Statement | Instances tried | Result |
|---|---|---|
| 1.2 | checked | fine |
"""


def test_adversarial_rows_count_only_rows_with_computed_values():
    assert prreviews.adversarial_rows(TABLE) == 2
    assert prreviews.adversarial_rows(SUMMARY_TABLE) == 0               # "checked", "fine": no value
    assert prreviews.adversarial_rows("```\n" + TABLE + "```\n") == 0   # a table quoted in a fence
    assert prreviews.adversarial_rows(TABLE.replace("Instances tried", "Locator")) == 0


REVIEW_COMMENT = """Review (codex) of a7f4b15

Verdict: needs_changes

1. **F01** (P1) A closed form need not be fixed coefficientwise
   Location: README:985.
2. **F02** (P2) Étale extension stated for noncommutative DGAs
3. **F03** (P3) Gap sentences omit hypotheses
"""
REVIEW_REPORT = "# Review of Foo (package, round 1)\n\n## R01 (P1) — a false identity\n\nx\n\n## R02 (P3) — a locator\n"
FIX = "## F01 (P1) — applied\n\nold → new\n\n## F03 (P3) — disputed\n\nthe source says so\n"


def test_a_repair_report_answers_every_finding():
    comment, report = ("REV-PKG-Foo", REVIEW_COMMENT), ("RT-Foo~1b", REVIEW_REPORT)
    assert replies.coverage_problems([comment], FIX) == [
        "report: no section for the review's finding F02: write one section per finding, headed `## <finding id> "
        "(<severity>) — <applied | applied differently | disputed | not actionable in this package: <reason>>`"]
    assert replies.coverage_problems([comment], FIX + "\n## F02 (P2) — applied differently\n\nwhy\n") == []
    assert "R01, R02" in replies.coverage_problems([report], FIX)[0]
    # two passes of a red-team round: an id no other pass has names its finding unqualified
    assert replies.coverage_problems([comment, report], FIX + "## F02 (P2) — applied\n\n"
                                     "## R01 (P1) — applied\n\n## R02 (P3) — applied\n") == []
    assert replies.coverage_problems([("REV-PKG-Foo", "(no report of REV-PKG-Foo on the board yet)\n")], FIX) == []


@pytest.fixture
def review_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")
    board = tmp_path / "board"
    pkg = board / "packages" / "Foo"
    (pkg / "reports").mkdir(parents=True)
    for name in ("README.md", "Suggested.lean", "metadata.toml"):
        (pkg / name).write_text("x\n")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")):
        subprocess.run(["git", "-C", str(board), *args], check=True, capture_output=True)
    job = new_job("REV-PKG-Foo")
    ws = base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "s", board=board, content=tmp_path,
                        workdir=board, report=pkg / "reports" / "REV-PKG-Foo.md", branch="b", job_id=job.id)

    class Ctx:
        agent, model, operator = "codex", "m", "alice"

    ctx = Ctx()
    ctx.cfg = cfg
    return job, ws, ctx, pkg


def verdict(pkg, status, blocking=0):
    (pkg / "review.json").write_text(json.dumps({"review": {"status": status, "reviewer": "independent-review-REV-PKG-Foo",
                                                            "findings": blocking, "blocking": blocking}}))


def test_an_acceptance_without_computed_evidence_is_refused(review_ws):
    job, ws, ctx, pkg = review_ws
    lane = lane_for(job)
    verdict(pkg, "accepted")
    ws.report.write_text("# Review of Foo (package, round 1)\n\n**Verdict:** accepted\n\nThe roadmap is sound. Good.\n")
    assert any("no adversarial table with computed values" in p for p in lane.check(job, ctx, ws))
    ws.report.write_text("# Review of Foo (package, round 1)\n\n**Verdict:** accepted\n\n" + TABLE)
    assert lane.check(job, ctx, ws) == []
    verdict(pkg, "needs_changes", 1)                                          # findings need no table
    ws.report.write_text("# Review of Foo (package, round 1)\n\n**Verdict:** needs changes\n\n## R01 (P1) — x\n\ny\n")
    assert not any("adversarial table" in p for p in lane.check(job, ctx, ws))
