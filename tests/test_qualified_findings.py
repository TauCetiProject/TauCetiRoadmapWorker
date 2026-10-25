"""Findings of different reports, or of different comments of one review, are told apart (design
review R08 B): every report numbers its findings from F01, and the coverage check used to take the
union of the bare ids, so one `## F01` section settled pass a's F01 and pass b's F01 at once (QSeries
red-team round 2 has two such F01s). A finding is now named by its review (`RT-Foo~2a/F01`), and by
its comment when the review holds several; a bare id still names a finding no other report in scope
numbers alike, so single-review rounds and historical reports read as before."""

from tauceti_roadmap_worker import boardpr, intake, prreviews, replies
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for

from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

PASS_A = """Red team round 2 pass a (codex) of abc1234

Verdict: needs_changes

1. **F01** (P1) Theorem 2.3 is false for p = 2
   Witness: the 2-adic case.
2. **F02** (P3) A typo in Layer 1
"""
PASS_B = """Red team round 2 pass b (claude) of abc1234

Verdict: needs_changes

1. **F01** (P2) Lemma 4.1 lacks the hypothesis that K is perfect
"""
ROUND = [("RT-Foo~2a", PASS_A), ("RT-Foo~2b", PASS_B)]


def problems(reviews, report):
    return replies.coverage_problems(reviews, report)


def test_two_distinct_f01s_need_two_qualified_dispositions_or_one_naming_both():
    assert [f[:2] for f in prreviews.review_findings(ROUND)] == [
        ("RT-Foo~2a/F01", "P1"), ("RT-Foo~2a/F02", "P3"), ("RT-Foo~2b/F01", "P2")]
    # The reproduction: a repair that settles only the wrong theorem.
    bare = "## F01 (P1) — applied\nTheorem 2.3 now assumes p odd.\n\n## F02 (P3) — applied\ntypo.\n"
    found = problems(ROUND, bare)
    assert "findings RT-Foo~2a/F01, RT-Foo~2b/F01:" in found[0]
    assert found[1].startswith("report: `F01` names 2 findings (RT-Foo~2a/F01, RT-Foo~2b/F01)")
    one = bare.replace("## F01 (P1)", "## RT-Foo~2a/F01 (P1)")
    assert "finding RT-Foo~2b/F01:" in problems(ROUND, one)[0]           # F02 is pass a's alone
    assert problems(ROUND, one + "\n## RT-Foo~2b/F01 (P2) — disputed\nK is perfect by 1.2.\n") == []
    for both in ("RT-Foo~2a/F01, RT-Foo~2b/F01", "RT-Foo~2a/F01 and RT-Foo~2b/F01"):
        assert problems(ROUND, bare.replace("## F01 (P1)", f"## {both} (P1)")) == [], both
    # Over-qualified by the comment is still that finding; another pass's id is not.
    assert problems(ROUND, one.replace("RT-Foo~2a/F01", "RT-Foo~2a/c99/F01") + "## RT-Foo~2b/F01 (P2) — applied\nx\n") == []
    assert "finding RT-Foo~2a/F01:" in problems(ROUND, one.replace("RT-Foo~2a/F01", "RT-Foo~2c/F01")
                                                + "## RT-Foo~2b/F01 (P2) — applied\nx\n")[0]


def comments_report(*bodies: tuple[int, str]) -> str:
    """A review's report as the intake writes it from the comments it counted."""
    return boardpr.report_text([({"user": {"login": "kim"}, "body": body,
                                  "html_url": f"https://github.com/o/b/pull/12#issuecomment-{cid}"}, None)
                                for cid, body in bodies])


def test_repeated_ids_across_the_comments_of_one_review():
    first = "Review (codex) of abc1234\n\nVerdict: needs_changes\n\n1. **F01** (P1) A false identity\n2. **F02** (P2) X\n"
    second = "Review (gpt-pro) of abc1234\n\nVerdict: needs_changes\n\n1. **F01** (P1) An unowned prerequisite\n"
    review = [("REV-PKG-Foo~2", comments_report((111, first), (222, second)))]
    assert [f[0] for f in prreviews.review_findings(review)] == [
        "REV-PKG-Foo~2/c111/F01", "REV-PKG-Foo~2/c111/F02", "REV-PKG-Foo~2/c222/F01"]
    report = "## F01 (P1) — applied\nfixed.\n\n## F02 (P2) — applied\nfixed.\n"
    assert "findings REV-PKG-Foo~2/c111/F01, REV-PKG-Foo~2/c222/F01" in problems(review, report)[0]
    assert "REV-PKG-Foo~2/F01" in problems(review, report.replace("## F01", "## REV-PKG-Foo~2/F01"))[1]
    settled = report.replace("## F01 (P1)", "## REV-PKG-Foo~2/c111/F01, c222/F01 (P1)")
    assert problems(review, settled) == []
    # One comment with findings among several: its ids need no comment.
    assert [f[0] for f in prreviews.review_findings(
        [("REV-PKG-Foo~2", comments_report((111, first), (222, "Review (x) of abc1234\n\nVerdict: accepted\n")))])] \
        == ["F01", "F02"]


def test_one_review_keeps_its_own_ids():
    review = [("REV-PKG-Foo~2", PASS_A)]
    assert [f[0] for f in prreviews.review_findings(review)] == ["F01", "F02"]
    assert problems(review, "## F01 (P1) — applied\nx\n\n## F02 (P3) — applied\ny\n") == []
    assert problems(review, "## REV-PKG-Foo~2/F01 (P1) — applied\nx\n\n## F02 (P3) — applied\ny\n") == []
    assert "finding F02:" in problems(review, "## F01 (P1) — applied\nx\n")[0]


def test_a_section_naming_several_findings_counts_for_each():
    report = ("## RT-Foo~2a/F01, RT-Foo~2b/F01 (P1) — not actionable in this package: Bar must state the lemma\n"
              "Both ask for Bar's lemma.\n\n## RT-Foo~2a/F02 (P3) — applied\ntypo.\n")
    assert [r[0] for r in replies.parse_findings(report)] == ["RT-Foo~2a/F01, RT-Foo~2b/F01", "RT-Foo~2a/F02"]
    reason = "not actionable in this package: Bar must state the lemma"
    assert replies.dispositions(report) == [("RT-Foo~2a/F01", reason), ("RT-Foo~2b/F01", reason),
                                            ("RT-Foo~2a/F02", "applied")]
    assert set(replies.not_actionable(report)) == {"RT-Foo~2a/F01", "RT-Foo~2b/F01"}
    assert replies.not_actionable(report)["RT-Foo~2b/F01"][1] == "Bar must state the lemma"
    assert "- **RT-Foo~2a/F01, RT-Foo~2b/F01** (not actionable" in replies.build_reply("abc1234", report, "ok.")


REPAIR_OF_TWO_COMMENTS = """# Repairs of REV-PKG-Foo~2

## REV-PKG-Foo~2/c111/F01 (P1) — not actionable in this package: the lemma is Bar's, declared as gap:bar-lemma
Declared under the supplier contracts.

## REV-PKG-Foo~2/c222/F01 (P1) — applied
The prerequisite is assigned to Baz.
"""


def test_findings_re_raised_across_rounds_by_their_qualified_id():
    review = "Review (codex) of def5678\n\nVerdict: needs_changes\n\n1. **F01** (P1) Bar's lemma is still unowned\n" \
             "   Re-raised: {}\n"
    stuck = intake.stuck_findings(REPAIR_OF_TWO_COMMENTS, review.format("REV-PKG-Foo~2/c111/F01"))
    assert stuck and "re-raises `REV-PKG-Foo~2/c111/F01`, which the repair marked not actionable (the lemma" in stuck[0]
    assert intake.stuck_findings(REPAIR_OF_TWO_COMMENTS, review.format("c111/F01")) == stuck
    # Re-raising the applied one is a new round's work, and a bare F01 names both: neither parks.
    assert intake.stuck_findings(REPAIR_OF_TWO_COMMENTS, review.format("REV-PKG-Foo~2/c222/F01")) == []
    assert intake.stuck_findings(REPAIR_OF_TWO_COMMENTS, review.format("F01")) == []
    assert prreviews.reraised("", "Re-raised: REV-PKG-Foo~2/c111/F01 and F03") == {"REV-PKG-Foo~2/c111/F01", "F03"}


def test_a_red_team_fix_brief_lists_the_qualified_ids_and_its_check_reads_them(ctx):  # noqa: F811
    job = new_job("FIX-RT-Foo~2", inputs={"pr": 905, "operator": "alice", "reviews": ["RT-Foo~2a", "RT-Foo~2b"]})
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    reports = ws.package(ctx.cfg, job.name) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    for jid, text in ROUND:
        (reports / f"{jid}.md").write_text(text, encoding="utf-8")
    assert lane.review_texts(job, ctx, ws) == ROUND
    assert lane.values(job, ctx, ws)["REVIEWS"].endswith(
        "The findings, by the qualified ids that head the report's sections: `RT-Foo~2a/F01` (P1), "
        "`RT-Foo~2a/F02` (P3), `RT-Foo~2b/F01` (P2).")
    # One pass in scope: its own ids, no list.
    single = new_job("FIX-RT-Foo~2", inputs={"pr": 905, "operator": "alice", "reviews": ["RT-Foo~2a"]})
    assert "qualified ids" not in lane.values(single, ctx, ws)["REVIEWS"]


def test_a_package_fix_on_a_review_of_several_comments_lists_them(ctx):  # noqa: F811
    job = new_job("FIX-PKG-Foo~3", inputs={"board_pr": 12, "operator": "alice", "review": "REV-PKG-Foo~2"})
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    first = "Review (codex) of abc1234\n\nVerdict: needs_changes\n\n1. **F01** (P1) A false identity\n"
    second = "Review (gpt-pro) of abc1234\n\nVerdict: needs_changes\n\n1. **F01** (P2) A locator\n"
    [path] = ws.extra["reviews"]
    path.write_text(comments_report((111, first), (222, second)), encoding="utf-8")
    assert "`REV-PKG-Foo~2/c111/F01` (P1), `REV-PKG-Foo~2/c222/F01` (P2)." in lane.values(job, ctx, ws)["REVIEWS"]
    ws.report.parent.mkdir(parents=True, exist_ok=True)
    ws.report.write_text("## REV-PKG-Foo~2/c111/F01 (P1) — applied\nx\n\n## REV-PKG-Foo~2/c222/F01 (P2) — applied\n"
                         "y\n", encoding="utf-8")
    assert lane.finish(job, ctx, ws) == "round 3 of the Foo package repaired: 2 of 2 findings applied"
