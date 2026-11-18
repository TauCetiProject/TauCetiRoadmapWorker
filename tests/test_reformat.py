"""The posted layout of a pull-request review, read in both forms, and `reformat-reviews`, which rebuilds
the latest review of each folder in place without changing what the pipeline reads from it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import prreviews, reformat, replies
from tauceti_roadmap_worker.gh import GHRateLimited

from .fakes import FakeBoard, example_config
from .test_prreview import ContentGH, node, review_job

FIXTURES = Path(__file__).parent / "fixtures" / "prreview-comments"
NAMES = ("accepted-incremental", "p3-incremental", "pass-p1-p2-p3", "full-p2-p3")


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.md").read_text(encoding="utf-8")


def finding_blocks(body: str) -> list[str]:
    """Each finding of a comment as its item line and indented body, as posted."""
    p = prreviews.read_posted(body)
    return ["\n".join(f.lines(k)[1:]) for k, f in enumerate(p.findings, 1)]


@pytest.mark.parametrize("name", NAMES)
def test_a_real_review_is_rebuilt_in_the_posted_layout_and_reads_the_same(name):
    old = fixture(name)
    new = prreviews.reformat_posted(old, replies.text_version(old))
    assert new is not None and len(new) < len(old)
    # What the pipeline reads is unchanged, and so is the text version (the pin).
    assert prreviews.posted_identity(new) == prreviews.posted_identity(old) != ()
    assert replies.text_version(new) == replies.text_version(old)
    assert prreviews.pinned_version(new) == replies.text_version(old)
    # Top to bottom: the header lines, the summary, the sections, one collapsed block, the markers.
    lines = new.split("\n")
    assert lines[0] == old.split("\n")[0] and lines[2].startswith("Verdict: ")
    assert [x for x in lines if x.startswith(("Reviewed head:", "Roadmap folder:"))] == [
        x for x in old.split("\n") if x.startswith(("Reviewed head:", "Roadmap folder:"))]
    summary = next(x for x in lines if x.startswith("**Summary.**"))
    assert lines.index(summary) < min((lines.index(x) for x in lines if x.startswith("## ")), default=len(lines))
    assert new.count("<details><summary>What was checked</summary>") == 1 and new.count("</details>") == 1
    assert new.rstrip().endswith("-->") and "<!-- tauceti-roadmap-prreview" in new
    # Not posted any more.
    for gone in ("## Verified sound", "**Source independence:**", "**Research link:**", "| Instances tried |",
                 "every finding, P3 included", "this review raises"):
        assert gone not in new
    # Findings, the questions for the human reviewers and the source issues word for word.
    assert finding_blocks(new) == finding_blocks(old)
    for block in finding_blocks(old):
        assert block in new
    human = prreviews.read_posted(old).section(prreviews.HUMAN_SECTION)
    assert human.strip() in new
    # What was checked is kept, folded: the gate's sentence and the earlier findings.
    gate = prreviews.read_posted(old).gate
    assert gate and new.index(gate) > new.index("<details>")
    if "## Previous findings" in old:
        assert "**Earlier findings" in new and new.index("**Earlier findings") > new.index("<details>")
    # Idempotent.
    assert prreviews.reformat_posted(new, "0" * 12) == new


def test_the_summary_says_the_verdict_the_main_findings_and_what_is_left_for_a_human():
    def summary(name):
        old = fixture(name)
        return next(x for x in prreviews.reformat_posted(old, "a" * 12).split("\n") if x.startswith("**Summary.**"))
    accepted = summary("accepted-incremental")
    assert accepted.startswith("**Summary.** Accepted: this roadmap folder is ready for a human's final approval")
    assert accepted.endswith("It leaves 5 questions for the human reviewers, which do not change the verdict.")
    mixed = summary("pass-p1-p2-p3")
    assert "Changes needed: 7 findings block this head (1 P1, 1 P2, 5 P3)." in mixed
    assert "The main ones: F01, A character is the conjugate" in mixed and "; F02, §4.2 does not assume" in mixed
    one = summary("p3-incremental")
    assert "Changes needed: 1 finding blocks this head (1 P3). The main one: F01, " in one
    # Old reviews have no small corrections or format notes: their P3 findings stay findings.
    p = prreviews.read_posted(prreviews.reformat_posted(fixture("pass-p1-p2-p3"), "a" * 12))
    assert [f.severity for f in p.findings] == ["P1", "P2", "P3", "P3", "P3", "P3", "P3"]
    assert p.corrections == [] and p.notes == []


def test_the_new_form_reads_its_small_corrections_and_format_notes_apart_from_the_findings():
    body = "\n".join([
        f"\U0001f916 AI review (claude, fable) of {'a' * 40}", "", "Verdict: APPROVE", "", f"Reviewed head: {'a' * 40}",
        "", "Roadmap folder: `TauCetiRoadmap/Toy/`", "",
        ("**Summary.** Accepted: the roadmap is ready for a human's final approval. One small correction and one "
         "format note, none blocking."), "",
        "## Small corrections (non-blocking)", "", "1. **C01** Example 2 is labelled with the wrong group",
        "", "   It is the dihedral group of order 8, not 6. Relabel it.", "",
        "## Format notes (non-blocking)", "", "1. **N01** A link points at a pull request", "",
        "   Name the roadmap instead.", "",
        "<details><summary>What was checked</summary>", "", "The build passed.", "", "</details>", "",
        '<!-- tauceti-roadmap-prreview {"head": "' + "a" * 40 + '", "verdict": "accepted"} -->', ""])
    p = prreviews.read_posted(body)
    assert p.new_form and p.verdict == "accepted" and p.findings == []
    assert [(c.id, c.title) for c in p.corrections] == [("C01", "Example 2 is labelled with the wrong group")]
    assert [(n.id, n.body) for n in p.notes] == [("N01", ["", "   Name the roadmap instead."])]
    assert p.details == "The build passed."
    # Nothing reads a small correction or a format note as a finding.
    assert prreviews.findings_with_bodies(body) == [] and prreviews.posted_identity(body)[8] == ()
    assert prreviews.render_posted(p) == body


def test_an_answered_review_stays_answered_after_it_is_reformatted():
    old = fixture("p3-incremental")
    new = prreviews.reformat_posted(old, replies.text_version(old))
    cfg = example_config().replies
    review = replies.Event("2026-11-18T09:00:00Z", "carol", old, ref="comment-1", version=replies.text_version(old))
    reply = replies.Event("2026-11-18T10:00:00Z", "carol", "Fixed in `abc1234` (reply to the review):\n\n"
                          + replies.answered_marker("GATE-Foo~1", {"comment-1": replies.text_version(old)}))

    def unanswered(body):
        edited = replies.Event(review.t, "carol", body, ref="comment-1", version=replies.text_version(body))
        return replies.unanswered([edited, reply], {"carol", "dave"}, cfg.review_markers, cfg.housekeeping_prefixes,
                                  owner="dave")
    assert unanswered(old) == [] and unanswered(new) == []
    # An edit of a review that carries no pin is a new version, as before: it waits for an answer again.
    assert unanswered(old.replace("Replace the first sentence", "Rewrite the first sentence")) != []


def test_a_pin_counts_only_on_a_review_comment():
    pin = prreviews.pin_marker("abcdef012345")
    assert replies.text_version("A note.\n" + pin) != "abcdef012345"
    assert replies.text_version(fixture("p3-incremental") + "\n" + pin) == "abcdef012345"
    assert prreviews.pinned_version(fixture("p3-incremental") + "\n" + prreviews.pin_marker("not-a-version")) == ""


def test_a_rebuild_that_would_change_what_the_pipeline_reads_is_refused(monkeypatch):
    old = fixture("full-p2-p3")
    real = prreviews.render_posted

    def drops_a_finding(p, pin=""):
        p.findings = p.findings[1:]
        return real(p, pin)
    monkeypatch.setattr(prreviews, "render_posted", drops_a_finding)
    assert prreviews.reformat_posted(old, "a" * 12) is None
    assert prreviews.reformat_posted("Just a comment.", "a" * 12) is None


# --- the command -------------------------------------------------------------------------------------

def comment(cid, body, login="carol", t="2026-11-18T09:00:00Z"):
    return {"id": cid, "body": body, "user": {"login": login}, "created_at": t}


def command(gh, jobs=(), write=False, **kw):
    cfg = example_config()
    cfg.roadmap.pool = ["carol", "dave"]
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(jobs=list(jobs)), operator="carol", dry_run=not write)
    slept = []
    return reformat.run(ctx, write=write, report=lambda line: None, sleep=slept.append, **kw), slept


FOLDER = "TauCetiRoadmap/DenoisingDiffusion"


def threads():
    old = fixture("p3-incremental")
    other = fixture("full-p2-p3")
    # (#12: two reviews of one folder; only the later one is rebuilt)
    return {12: [comment(1, old, t="2026-11-17T09:00:00Z"), comment(2, old), comment(3, "A reply.")],
            13: [comment(4, other, login="erin")],
            14: [comment(5, fixture("accepted-incremental"))]}


def pulls_gh(comments=None):
    return ContentGH([node(12, "1" * 40, author="dave"), node(13, "2" * 40), node(14, "3" * 40)],
                     comments=comments or threads())


def test_the_dry_run_prints_each_latest_review_with_its_sizes_and_writes_nothing():
    gh = pulls_gh()
    lines, slept = command(gh)
    old = fixture("p3-incremental")
    new = prreviews.reformat_posted(old, replies.text_version(old))
    assert gh.writes == [] and slept == []
    assert lines[0] == f"[dry run] #12 {FOLDER}: comment 2: {len(old):,} -> {len(new):,} characters"
    assert lines[1] == "#13 TauCetiRoadmap/MarkovDecisionProcesses: the latest review is not ours (erin): left as it is"
    assert lines[2].startswith("[dry run] #14 TauCetiRoadmap/MatesDoubleCategories: comment 5: ")
    assert lines[3].startswith("[dry run] in all: ")


def test_the_command_edits_the_latest_review_of_each_folder_in_place_and_is_idempotent():
    gh = pulls_gh()
    lines, slept = command(gh, write=True, pace=0.5, logins=["erin"])
    assert [(m, p) for m, p, _x in gh.writes] == [("PATCH", f"repos/{gh_repo()}/issues/comments/{n}") for n in (2, 4, 5)]
    assert slept == [0.5, 0.5] and lines[-1].startswith("3 comments reformatted (")
    for (_m, _p, payload), cid in zip(gh.writes, (2, 4, 5)):
        old = next(c["body"] for t in threads().values() for c in t if c["id"] == cid)
        assert prreviews.posted_identity(payload["body"]) == prreviews.posted_identity(old)
    # A second pass finds them in the layout already.
    done = threads()
    for _m, path, payload in gh.writes:
        cid = int(path.rsplit("/", 1)[1])
        for t in done.values():
            for c in t:
                if c["id"] == cid:
                    c["body"] = payload["body"]
    gh = pulls_gh(done)
    lines, _slept = command(gh, write=True, logins=["erin"])
    assert gh.writes == [] and lines[:3] == [f"#12 {FOLDER}: comment 2: already in the posted layout",
                                             ("#13 TauCetiRoadmap/MarkovDecisionProcesses: comment 4: already in the "
                                              "posted layout"),
                                             ("#14 TauCetiRoadmap/MatesDoubleCategories: comment 5: already in the "
                                              "posted layout")]


def gh_repo():
    return example_config().repositories.content


def test_a_pull_request_with_a_round_under_way_is_skipped_and_a_rate_limit_stops_the_run():
    gh = pulls_gh()
    busy = review_job(40, 3, 12, "1" * 40, "claimed", name="DenoisingDiffusion")
    lines, _slept = command(gh, [busy], write=True, numbers=[12, 14])
    assert lines[0] == "#12: skipped: PRREVIEW-DenoisingDiffusion~3 is claimed (run again once it is over)"
    assert [p for _m, p, _x in gh.writes] == [f"repos/{gh_repo()}/issues/comments/5"]

    class Limited(ContentGH):
        def write(self, method, path, payload=None):
            raise GHRateLimited("API rate limit exceeded", 0)
    gh = Limited([node(12, "1" * 40), node(14, "3" * 40)], comments=threads())
    lines, _slept = command(gh, write=True)
    assert lines == ["stopped at #12: API rate limit exceeded; 0 edited, run again later",
                     "0 comments reformatted (0 -> 0 characters)"]
