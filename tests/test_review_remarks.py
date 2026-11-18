"""The three kinds of remark of a review (findings, small corrections, format notes), the rules table's
classification of the automatic checks, and the plain-language rule (spec, section 3)."""

from __future__ import annotations

import pytest

from tauceti_roadmap_worker import gates, prreviews

from .test_prreview import gate_tree


def test_every_rule_of_the_gate_is_described_and_classified(tmp_path):
    """Each count the gate can report has a plain description and is about substance or about form."""
    assert set(gates.GATE_RULES) == set(gates.RULE_KINDS)
    assert set(gates.RULE_KINDS.values()) == {gates.SUBSTANCE, gates.FORMAT}
    opts = gates.GateOptions(order=["Toy"], open_readmes=[("main", "Other", "# Roadmap: other\n")])
    keys = set(gates.scan_folder("Toy", "# Roadmap: toy\n", "import Mathlib\n", opts).counts)
    root = gate_tree(tmp_path, "Toy", "Toy")
    keys |= set(gates.run_gate(root, "Toy", gates.GateOptions(), base="HEAD", build_cmd="true {module}").counts)
    keys |= set(gates.run_gate(tmp_path / "nowhere", "Toy", gates.GateOptions()).counts)      # missing-files
    keys.add("git")                                     # (a git failure, which the gate reports under this key)
    assert keys <= set(gates.RULE_KINDS), keys - set(gates.RULE_KINDS)
    # The spec's classification, checked rule by rule (readme-ids-missing is form: names in code font
    # declared nowhere are a matter of saying they are proposed, never a reason to delete them).
    for key in ("build", "depcheck", "sorry-data", "sorry-examples", "set_option", "statement-hygiene",
                "private-paths", "forbidden", "dropped", "outside-folder", "stray-files", "dirty"):
        assert gates.rule_kind(key) == gates.SUBSTANCE, key
    for key in ("latex", "tex", "prnum", "residue", "round-labels", "long-fences", "layer-ids", "empty-sections",
                "imports", "mathlib-subimports", "no-import-mathlib", "lean-lines", "metadata", "blob",
                "readme-ids-missing", "target-ids"):
        assert gates.rule_kind(key) == gates.FORMAT, key


REPORT = """# Review of Foo (pull request #905 at 1111111)

**Verdict:** accepted

**Summary.** Accepted: no finding. One small correction and one format note, none blocking.

## C01 — Example 2 is labelled with the wrong group

It is the dihedral group of order 8, not 6 (line 120). Relabel it.

## N01 — A link points at a pull request

Name the roadmap instead (line 30).

## Questions for the author

- Is the page of the locator in 1.2 the published one? Only the preprint could be read.

## Verified sound

| Statement | Instances tried | Values | Result | Change |
|---|---|---|---|---|
| 1.1 | ℤ/6 | 6 = 6 | true | none |

## Not verifiable

- Lang, p. 10: not read.

Automatic checks set aside as false positives: a process word that names Mathlib's own file.

## Source issues

- none

## For the human reviewers

- none
"""


def test_a_report_reads_into_the_posted_parts():
    p = prreviews.posted_from_report(REPORT)
    assert p.findings == [] and p.summary.startswith("**Summary.** Accepted")
    assert [(c.id, c.title) for c in p.corrections] == [("C01", "Example 2 is labelled with the wrong group")]
    assert [(n.id, n.title) for n in p.notes] == [("N01", "A link points at a pull request")]
    assert p.section(prreviews.QUESTIONS_HEADING).startswith("- Is the page of the locator")
    assert p.loose == ["Automatic checks set aside as false positives: a process word that names Mathlib's own file."]
    assert p.section("Not verifiable") == "- Lang, p. 10: not read." and p.section("Verified sound") == ""
    body = prreviews.render_pr_review(prreviews.pr_review_head("claude", "fable", "1" * 40), "accepted", REPORT,
                                      {"job": "PRREVIEW-Foo~1", "head": "1" * 40}, "1" * 40, "At `1111111`: fine.")
    # Nothing a tool reads as a finding, and the acceptance reads as one.
    assert prreviews.findings_with_bodies(body) == [] and "Verdict: APPROVE" in body
    assert body.index("## Small corrections (non-blocking)") < body.index("## Format notes (non-blocking)") < \
        body.index("## Questions for the author") < body.index("<details>")
    assert "## For the human reviewers" not in body and "## Source issues" not in body      # `- none`: left out
    assert prreviews.posted_identity(body)[4] == "accepted"


@pytest.mark.parametrize("text,named", [
    ("The `prnum` check flagged a number.", "`prnum`"),
    ("readme-ids-missing reports seven names.", "readme-ids-missing"),
    ("gate: residue", "gate: r"),
    ("A gate rule refuses it.", "gate rule"),
    ("As the worker's gate says.", "the worker's gate"),
    ("See gate.md for the list.", "gate.md"),
    ("The scratch/overlaps.md file lists it.", "scratch/"),
    ("Raised by PRREVIEW-Foo~2 before.", "PRREVIEW-Foo~2"),
    ("The file /home/someone/x.lean has it.", "/home/"),
])
def test_a_remark_that_names_a_check_label_a_worker_file_a_job_or_a_path_is_refused(text, named):
    report = REPORT.replace("Relabel it.", "Relabel it. " + text)
    problems = prreviews.plain_language_problems(report)
    assert problems and problems[0].startswith("report: C01 names ") and repr(named)[1:-1] in problems[0], problems


def test_plain_words_pass():
    words = ("A `set_option` changes how the statement elaborates. The build fails at line 3. LaTeX delimiters "
             "remain in the heading. The supplier list names a roadmap later in the merge order. The imports "
             "of the file are fine. Its metadata is filed under the wrong topic.")
    assert prreviews.plain_language_problems(REPORT.replace("Relabel it.", words)) == []
