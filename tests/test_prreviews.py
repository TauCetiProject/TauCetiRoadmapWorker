"""The review comment shape: parsing what a browser model or a worker posts, and rendering a
worker's report into it."""

from tauceti_roadmap_worker import prreviews

REVIEW = """Review (chatgpt) of abc1234

Verdict: needs_changes

1. **F01** (P1) The comparison map is stated without its hypothesis
   Location: README.md line 40. Claim: ... Witness: ... Repair: add the hypothesis.
2. **F02** (P3) A locator names the wrong section

## Verified sound
- 15 locators of the sample.
"""


def test_a_review_comment():
    p = prreviews.parse(REVIEW)
    assert (p.kind, p.agent, p.sha, p.verdict) == ("review", "chatgpt", "abc1234", "needs_changes")
    assert p.findings == [("F01", "P1"), ("F02", "P3")] and p.blocking == 1 and p.worker == {}


def test_shapes_that_are_not_reviews():
    assert prreviews.parse("Review (chatgpt) of abc1234\n\nLooks fine to me.") is None          # no verdict
    assert prreviews.parse("Review (chatgpt) of abc1234\n\nVerdict: needs_changes\n\nfix it") is None   # no finding
    assert prreviews.parse("Great work!\n\nVerdict: accepted") is None
    assert prreviews.parse("") is None
    accepted = prreviews.parse("**Review (fable) of ABCDEF0123**\n\n**Verdict:** accepted\n")
    assert (accepted.verdict, accepted.sha, accepted.findings) == ("accepted", "abcdef0123", [])


def test_red_team_passes_and_claims():
    rt = prreviews.parse("Red team round 2 pass b (chatgpt) of 0123456\n\nVerdict: accepted\n")
    assert (rt.kind, rt.round, rt.pass_, rt.agent) == ("redteam", 2, "b", "chatgpt")
    assert prreviews.parse("reviewing").kind == "claim"
    assert prreviews.parse("Reviewing (chatgpt)").agent == "chatgpt"
    assert prreviews.parse("reviewing this now, and here is a long paragraph\nline\nline\nline") is None


def test_a_worker_report_is_rendered_into_the_shape():
    report = ("# Review of Foo (package, round 1)\n**Verdict:** needs changes\n\n"
              "## R01 (P1) — A false statement\nLocation: README.md line 3.\n\n```lean\ntheorem x : False\n```\n"
              "## R02 (P2) — An overclaim\nbody\n\n## Verified sound\n- the sample\n\n## Not verifiable\n- none\n")
    marker = {"job": "REV-PKG-Foo", "worker": "w1", "agent": "claude", "model": "fable"}
    text = prreviews.render(prreviews.review_head("claude", "abcdef0123456"), "needs_changes", report, marker)
    assert text.startswith("Review (claude) of abcdef0\n\nVerdict: needs_changes\n\n1. **F01** (P1) A false statement\n")
    assert "2. **F02** (P2) An overclaim" in text and "   theorem x : False" in text and "## Verified sound" in text
    p = prreviews.parse(text)
    assert p.findings == [("F01", "P1"), ("F02", "P2")] and p.worker == marker and p.blocking == 2
    assert prreviews.redteam_head(1, "a", "codex", "0123456789") == "Red team round 1 pass a (codex) of 0123456"
