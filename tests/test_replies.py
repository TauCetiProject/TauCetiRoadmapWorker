from tauceti_roadmap_worker.gates import GateResult
from tauceti_roadmap_worker.replies import (REPLY_PREFIX, Event, build_reply, classify, guard_reply,
                                            is_maintainer_review, parse_findings, reply_is_safe, reviews_since_reply,
                                            validation_sentence, wanted_label)

MARKERS = ["ChatGPT", "GPT-", "Astra", "\U0001f916", ":robot:", "AI-assisted", "Deep re-review", "Deep review"]
HOUSE = ["Red-team repairs at", "Housekeeping at"]
US = {"author"}
HEAD = "abc1234def"


def label(events, approvers=(), head=HEAD):
    return wanted_label(events, US, head, markers=MARKERS, housekeeping=HOUSE, approvers=list(approvers),
                        ignore={"github-actions[bot]"})


# --- the label state machine ----------------------------------------------------------------------

def test_review_newer_than_reply_is_awaiting_author():
    events = [Event("2026-10-20T09:00:00Z", "author", "Fixed in `abc1234`: R01 applied."),
              Event("2026-10-20T10:00:00Z", "reviewer", "Changes needed: the hypothesis on p is missing.")]
    assert label(events) == ("awaiting-author", ["awaiting-review"])


def test_reply_newer_than_review_is_awaiting_review():
    events = [Event("2026-10-20T10:00:00Z", "reviewer", "Changes needed: Check 3 is false."),
              Event("2026-10-20T11:00:00Z", "author", "Fixed in `abc1234` (reply to the review): Check 3 replaced.")]
    assert label(events) == ("awaiting-review", ["awaiting-author"])


def test_no_events_is_awaiting_review():
    assert label([]) == ("awaiting-review", ["awaiting-author"])


def test_settled_notes_and_bots_do_not_count_as_reviews():
    events = [Event("2026-10-20T10:00:00Z", "author", "Fixed in `abc1234`."),
              Event("2026-10-20T11:00:00Z", "reviewer", "Thanks, all comments addressed; I would approve."),
              Event("2026-10-20T12:00:00Z", "github-actions[bot]", "Build report: something failed")]
    assert label(events)[0] == "awaiting-review"


def test_our_ai_review_counts_as_review_but_housekeeping_is_not_a_reply():
    events = [Event("2026-10-20T10:00:00Z", "author", "Fixed in `abc1234`."),
              Event("2026-10-20T11:00:00Z", "author", "\U0001f916 AI-assisted review: the sign in 2.3 is reversed."),
              Event("2026-10-20T12:00:00Z", "author", "Housekeeping at abc1234: rebased.")]
    assert label(events)[0] == "awaiting-author"


def test_approval_at_head_clears_both_labels():
    events = [Event("2026-10-20T10:00:00Z", "reviewer", "Changes needed."),
              Event("2026-10-20T11:00:00Z", "maintainer", "", approved=True, commit=HEAD)]
    assert label(events, approvers=["maintainer"]) == ("", ["awaiting-author", "awaiting-review"])
    # an approval of an older head does not clear them
    stale = [events[0], Event("2026-10-20T11:00:00Z", "maintainer", "", approved=True, commit="old")]
    assert label(stale, approvers=["maintainer"])[0] == "awaiting-author"


def test_unguarded_reply_naming_the_tool_is_read_as_a_review():
    reply = "ChatGPT's findings are fixed in `abc1234`."
    events = [Event("2026-10-20T10:00:00Z", "reviewer", "Changes needed."),
              Event("2026-10-20T11:00:00Z", "author", reply)]
    assert label(events)[0] == "awaiting-author"          # the bug the guard exists for
    guarded = [events[0], Event("2026-10-20T11:00:00Z", "author", guard_reply(reply, MARKERS))]
    assert label(guarded)[0] == "awaiting-review"


# --- reviews since the last reply ----------------------------------------------------------------

def test_every_review_since_the_last_reply_is_ingested():
    events = [Event("2026-10-19T10:00:00Z", "r1", "Changes needed: old."),
              Event("2026-10-19T12:00:00Z", "author", "Fixed in `0000000`."),
              Event("2026-10-20T09:00:00Z", "r1", "Changes needed: first new review."),
              Event("2026-10-20T10:00:00Z", "r2", "Request changes: second new review."),
              Event("2026-10-20T10:30:00Z", "r2", "Thanks, the other finding is addressed.")]
    got = reviews_since_reply(events, US, MARKERS, HOUSE)
    assert [e.body.split(":")[0] for e in got] == ["Changes needed", "Request changes"]
    assert classify(events, US, MARKERS, HOUSE)[1] == "2026-10-19T12:00:00Z"


# --- a maintainer's review under the owner's login ------------------------------------------------

MAINTAINER = {"author"}      # the maintainer acts as the same login as the workers (CBirkbeck)
M_REVIEW = "Maintainer review of `Suggested.lean`: H1 the hypothesis on p is missing."


def m_label(events, maintainers=MAINTAINER):
    return wanted_label(events, US, HEAD, markers=MARKERS, housekeeping=HOUSE, approvers=[],
                        ignore={"github-actions[bot]"}, maintainers=maintainers)


def test_maintainer_review_from_the_owner_login_is_awaiting_author():
    events = [Event("2026-10-22T09:00:00Z", "author", "Fixed in `abc1234` (reply to the review): R01 applied."),
              Event("2026-10-22T12:58:00Z", "author", M_REVIEW)]
    assert m_label(events) == ("awaiting-author", ["awaiting-review"])
    assert [e.body for e in reviews_since_reply(events, US, MARKERS, HOUSE, maintainers=MAINTAINER)] == [M_REVIEW]
    # Without the login configured it stays the author's own reply, as before.
    assert m_label(events, maintainers=set())[0] == "awaiting-review"


def test_the_gate_lanes_reply_from_the_same_login_is_not_a_maintainer_review():
    report = "## H01 (author) — applied\nMaintainer review asked for the hypothesis on p; it is added to `foo`.\n"
    reply = build_reply("def5678", report, "At `def5678`: gate passed.", markers=MARKERS)
    events = [Event("2026-10-22T12:58:00Z", "author", M_REVIEW), Event("2026-10-22T14:00:00Z", "author", reply)]
    assert not is_maintainer_review(events[1], MAINTAINER)
    assert m_label(events) == ("awaiting-review", ["awaiting-author"])
    assert reviews_since_reply(events, US, MARKERS, HOUSE, maintainers=MAINTAINER) == []


def test_an_owner_comment_without_the_prefix_stays_a_reply():
    events = [Event("2026-10-22T10:00:00Z", "reviewer", "Changes needed: Check 3 is false."),
              Event("2026-10-22T11:00:00Z", "author", "Answered below; see the Maintainer review of yesterday.")]
    assert m_label(events)[0] == "awaiting-review"


def test_the_maintainer_prefix_is_case_insensitive_after_emoji_and_exact_at_the_start():
    def review(body, user="author"):
        return is_maintainer_review(Event("2026-10-22T12:58:00Z", user, body), {"Author"})
    assert review("maintainer REVIEW of `Suggested.lean`")
    assert review("\U0001f50d\ufe0f  Maintainer review: two points")
    assert review(":mag: Maintainer review: two points")
    assert not review("Fixed in `abc1234`: Maintainer review points applied.")
    assert not review("> Maintainer review of `Suggested.lean`")
    assert not review("Maintainer review of `Suggested.lean`", user="someone-else")
    assert not is_maintainer_review(Event("t", "author", "anything"), {"author"}, prefix="")


# --- the reply guard -----------------------------------------------------------------------------

def test_reply_must_not_name_the_review_tool_in_its_first_40_characters():
    assert not reply_is_safe("Fixed in `abc1234` (reply to ChatGPT review): ...", MARKERS)
    assert reply_is_safe("Fixed in `abc1234` (reply to the review): the ChatGPT finding R01 ...", MARKERS)
    for unsafe in ("\U0001f916 reply", "Deep review findings are fixed", "x" * 36 + "GPT-6 later"):
        out = guard_reply(unsafe, MARKERS)
        assert reply_is_safe(out, MARKERS) and out.startswith(REPLY_PREFIX)
    assert len(REPLY_PREFIX) > 40
    safe = "Fixed in `abc1234` (reply to the review)."
    assert guard_reply(safe, MARKERS) == safe


# --- building the reply --------------------------------------------------------------------------

REPORT = """# Repairs

## R01 (P1) — applied
The hypothesis `[IsNoetherianRing A]` is added to `foo_bar`. See [the source](http://example.org). Third sentence.

## R02 (P3) — disputed
The locator is right: Thm 3.2 on p. 41 states it.
"""


def test_build_reply_has_one_bullet_per_finding_and_a_computed_validation():
    gate = GateResult("Toy", counts={"build": 0, "readme-ids-missing": 0, "depcheck": 0})
    text = build_reply("abc1234", REPORT, validation_sentence("abc1234", gate), markers=MARKERS)
    assert text.startswith("Fixed in `abc1234` (reply to the review):")
    assert "- **R01** (applied) — The hypothesis `[IsNoetherianRing A]` is added to `foo_bar`. See the source." in text
    assert "- **R02** (disputed)" in text and "gate passed" in text
    bad = GateResult("Toy", counts={"build": 2, "readme-ids-missing": 1, "depcheck": 0})
    assert "gate FAILED" in validation_sentence("abc1234", bad)


def test_parse_findings_other_styles():
    assert parse_findings("1. **R03 — applied.** Done.\n") == [("R03", "applied", "Done.")]
    assert parse_findings("- **S01 — applied differently.** Reason.\n")[0][:2] == ("S01", "applied differently")
    assert parse_findings("no findings here") == []
