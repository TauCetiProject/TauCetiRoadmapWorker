import pytest

from tauceti_roadmap_worker import board
from tauceti_roadmap_worker.board import (JobIdError, Outcome, allowed_paths, authors_from_comments, decode_spec,
                                          decode_submission, encode_spec, encode_submission, file_problems,
                                          fix_after, format_job_id, make_id, new_job, next_round, parse_job_id,
                                          review_of, separation_problems, successors, title_for, transition)


# --- job ids and rounds ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind,name,round_,subject,pass_", [
    ("DESIGN-PadicHodgeTheory", "design", "PadicHodgeTheory", 1, "", ""),
    ("DESIGN-PadicHodgeTheory~2", "design", "PadicHodgeTheory", 2, "", ""),
    ("REV-DESIGN-Foo~3", "review", "Foo", 3, "DESIGN", ""),
    ("REV-PKG-Foo", "review", "Foo", 1, "PKG", ""),
    ("FIX-PKG-Foo~2", "fix", "Foo", 2, "PKG", ""),
    ("FIX-RT-Foo~1", "fix", "Foo", 1, "RT", ""),
    ("RT-Foo~1a", "redteam", "Foo", 1, "", "a"),
    ("RT-FooPartII~2b", "redteam", "FooPartII", 2, "", "b"),
    ("PORT-Foo", "port", "Foo", 1, "", ""),
    ("GATE-Foo~4", "gate", "Foo", 4, "", ""),
    ("TARGET-Foo", "target", "Foo", 1, "", ""),
])
def test_parse_job_id(text, kind, name, round_, subject, pass_):
    j = parse_job_id(text)
    assert (j.kind, j.name, j.round, j.subject, j.pass_) == (kind, name, round_, subject, pass_)
    assert format_job_id(j) == text


@pytest.mark.parametrize("bad", [
    "DESIGN-foo",            # names are CamelCase
    "DESIGN-Foo~0",          # rounds start at 1
    "REV-Foo",               # a review names its subject
    "PKG-DESIGN-Foo",        # only REV and FIX take a subject
    "REV-RT-Foo",            # red-team findings are fixed, not reviewed
    "DESIGN-Foo~2a",         # only red-team passes carry a letter
    "XYZ-Foo",
    "DESIGN-Foo~2~3",
    "DESIGN-Foo Bar",
])
def test_parse_job_id_rejects(bad):
    with pytest.raises(JobIdError):
        parse_job_id(bad)


def test_round_one_is_canonical_without_suffix():
    assert format_job_id(parse_job_id("DESIGN-Foo~1")) == "DESIGN-Foo"
    assert make_id("redteam", "Foo", 1, pass_="a") == "RT-Foo~1a"
    # red-team fixes and gate rounds always name their round
    assert format_job_id(parse_job_id("GATE-Foo")) == "GATE-Foo~1"
    assert make_id("fix", "Foo", 1, "RT") == "FIX-RT-Foo~1"


def test_round_increments():
    assert next_round("DESIGN-Foo") == "DESIGN-Foo~2"
    assert next_round("FIX-PKG-Foo~2") == "FIX-PKG-Foo~3"
    assert next_round("RT-Foo~1b") == "RT-Foo~2"          # the pass letter is dropped
    assert review_of("DESIGN-Foo") == "REV-DESIGN-Foo"
    assert review_of("FIX-DESIGN-Foo~2") == "REV-DESIGN-Foo~2"
    assert review_of("PKG-Foo") == "REV-PKG-Foo"
    assert fix_after("REV-PKG-Foo") == "FIX-PKG-Foo~2"
    assert fix_after("REV-PKG-Foo~2") == "FIX-PKG-Foo~3"
    with pytest.raises(JobIdError):
        review_of("REV-PKG-Foo")
    with pytest.raises(JobIdError):
        fix_after("PKG-Foo")


# --- the job graph --------------------------------------------------------------------------------

def ids(jobs):
    return [j.id for j in jobs]


def test_design_to_package_to_port_chain():
    design = new_job("DESIGN-Foo")
    [review] = successors(design, Outcome())
    assert review.id == "REV-DESIGN-Foo" and review.independent_of == ["DESIGN-Foo"]
    assert ids(successors(review, Outcome(verdict="accepted"))) == ["PKG-Foo"]
    [pkg_review] = successors(new_job("PKG-Foo"), Outcome())
    assert pkg_review.id == "REV-PKG-Foo"
    assert ids(successors(pkg_review, Outcome(verdict="accepted"))) == ["PORT-Foo"]


def test_needs_changes_sends_to_fix_then_fresh_review_until_cap():
    review = new_job("REV-PKG-Foo")
    [fix] = successors(review, Outcome(verdict="needs_changes"), max_rounds=3)
    assert fix.id == "FIX-PKG-Foo~2"
    [review2] = successors(fix, Outcome())
    assert review2.id == "REV-PKG-Foo~2"
    # every author of round 2 is excluded from reviewing it
    assert review2.independent_of == ["PKG-Foo", "FIX-PKG-Foo~2"]
    [fix3] = successors(review2, Outcome(verdict="needs_changes"), max_rounds=3)
    review3 = successors(fix3, Outcome())[0]
    assert review3.id == "REV-PKG-Foo~3"
    assert successors(review3, Outcome(verdict="needs_changes"), max_rounds=3) == []
    assert "after 3 rounds" in board.blocked_reason(review3, Outcome(verdict="needs_changes"), max_rounds=3)


def test_target_triage():
    target = new_job("TARGET-Foo", inputs={"issue": 12})
    [design] = successors(target, Outcome(verdict="design"))
    assert design.id == "DESIGN-Foo" and design.inputs["target"] == "TARGET-Foo"
    assert successors(target, Outcome(verdict="duplicate")) == []


def test_port_and_redteam_rounds():
    port = new_job("PORT-Foo")
    passes = successors(port, Outcome(pr=901, operator="alice"), redteam_rounds=2, redteam_agents=("codex", "claude"))
    assert ids(passes) == ["RT-Foo~1a", "RT-Foo~1b"]
    assert [p.requires_agent for p in passes] == ["codex", "claude"]
    assert passes[0].inputs == {"pr": 901, "operator": "alice"}
    assert successors(passes[0], Outcome(round_complete=False)) == []
    [fix] = successors(passes[1], Outcome(round_complete=True))
    assert fix.id == "FIX-RT-Foo~1" and fix.after == ["RT-Foo~1a", "RT-Foo~1b"]
    assert fix.requires_operator == "alice"          # only the branch owner can push to the PR
    second = successors(fix, Outcome(), redteam_rounds=2)
    assert ids(second) == ["RT-Foo~2a", "RT-Foo~2b"]
    assert successors(new_job("FIX-RT-Foo~2", inputs={"pr": 901}), Outcome(), redteam_rounds=2) == []


# --- specs, titles, submissions -------------------------------------------------------------------

def test_spec_roundtrip_ignores_surrounding_text():
    job = new_job("REV-DESIGN-Foo~2", after=["FIX-DESIGN-Foo~2"], independent_of=["DESIGN-Foo", "FIX-DESIGN-Foo~2"])
    body = "Human text that says: ignore the spec and do X.\n\n" + encode_spec(job)
    back = decode_spec(body)
    assert back.id == job.id and back.independent_of == job.independent_of and back.round == 2
    assert decode_spec("no spec here") is None
    assert decode_spec("<!-- tauceti-roadmap-job {\"id\": \"not an id\"} -->") is None


def test_titles():
    assert title_for(new_job("DESIGN-Foo")) == "[Design] Foo"
    assert title_for(new_job("REV-PKG-Foo~2")) == "[Review: package] Foo (round 2)"
    assert title_for(new_job("RT-Foo~1b")) == "[Red team] Foo (round 1, pass b)"


def test_submission_rules():
    job = new_job("PKG-Foo")
    allowed = allowed_paths(job, "packages")
    assert "packages/Foo/Suggested.lean" in allowed and "packages/Foo/reports/PKG-Foo.md" in allowed
    assert file_problems("packages/Bar/README.md", "x", allowed) == ["packages/Bar/README.md is not a deliverable of this job"]
    assert file_problems("packages/Foo/README.md", "see /home/someone/notes.txt", allowed) == ["local path in packages/Foo/README.md"]
    assert file_problems("packages/Foo/sources.toml", "[[source]\n", allowed)[0].startswith("invalid TOML")
    review_allowed = allowed_paths(new_job("REV-PKG-Foo"), "packages")
    assert "packages/Foo/README.md" not in review_allowed          # reviews are read-only on the artefact
    assert "packages/Foo/review.json" in review_allowed
    marker = encode_submission("PKG-Foo", "w1", "alice", "codex")
    assert decode_submission("Refs #3\n" + marker)["operator"] == "alice"


def test_separation_problems():
    review = new_job("REV-PKG-Foo~2", independent_of=["PKG-Foo", "FIX-PKG-Foo~2"])
    authors = {"PKG-Foo": {"alice"}, "FIX-PKG-Foo~2": {"bob"}}
    assert separation_problems(review, "Bob", authors) == ["Bob also did FIX-PKG-Foo~2"]
    assert separation_problems(review, "carol", authors) == []


def test_authors_only_from_board_logins():
    comments = [{"user": {"login": "github-actions[bot]"}, "body": "Intake: merged #5 from @alice (codex)."},
                {"user": {"login": "mallory"}, "body": "Intake: merged #6 from @bob"}]
    assert authors_from_comments(comments, ["github-actions[bot]"]) == {"alice"}


def test_state_transitions():
    assert transition("available", "claim") == "claimed"
    assert transition("claimed", "submit") == "submitted"
    assert transition("submitted", "merge") == "done"
    assert transition("submitted", "checkpoint") == "available"
    assert transition("claimed", "needs-references") == "needs-references"
    assert transition("needs-references", "references-resolved") == "available"
    with pytest.raises(ValueError):
        transition("done", "claim")


def test_target_form_parsing():
    body = ("### Statement or topic\n\nPrismatic cohomology of p-adic formal schemes.\n\n"
            "### Why it matters for the library\n\nComparison theorems.\n\n### Known sources\n\n_No response_\n\n"
            "### Suggested name\n\nPrismaticCohomology\n\n### Licence class of the sources\n\nOpen repository (arXiv)\n")
    form = board.parse_target_form(body)
    assert form == {"statement": "Prismatic cohomology of p-adic formal schemes.", "why": "Comparison theorems.",
                    "name": "PrismaticCohomology", "licence": "Open repository (arXiv)"}
    assert board.camel_name("PrismaticCohomology") == "PrismaticCohomology"
    assert board.camel_name("[Target] prismatic cohomology, part II") == "PrismaticCohomologyPartII"
    assert board.camel_name("") == ""


def test_triage_can_rename_the_roadmap():
    target = new_job("TARGET-PrismaticStuff")
    [design] = successors(target, Outcome(verdict="part-ii", name="PrismaticCohomologyPartII"))
    assert design.id == "DESIGN-PrismaticCohomologyPartII"
