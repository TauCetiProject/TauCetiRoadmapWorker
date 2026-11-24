"""A reviewer's cross-check that ends with a conditional settled phrase still asks for changes."""
from tauceti_roadmap_worker import replies


def test_a_comment_that_moves_the_pull_request_to_the_author_is_a_review():
    body = ("**Overlap with other open roadmap PRs**\n\n1. **Removing §6E leaves #1222 without a supplier** (with #1222)\n"
            "   *Suggested:* keep §6E or give the old → new numbering.\n\n"
            "Moving this to `awaiting-author`; please switch it back to `awaiting-review` once these are addressed.")
    assert replies.CHANGES.search(body)
    assert replies.SETTLED.search(body)          # "are addressed" alone would read as settled


def test_a_settled_comment_without_an_instruction_is_not_a_review():
    body = "Thanks, the two findings are addressed at the new head; no further change requested."
    assert not replies.CHANGES.search(body)
    assert replies.SETTLED.search(body)


REFEREE = (":robot: Claude says:\n\nAI-assisted review (Claude). What I checked: the whole README.\n\n"
           "**Verdict: minor changes.** The mathematics I checked is right.\n\n**Requested changes**\n\n"
           "1. **`Suggested.lean`: prototype §2.1 against the merged supplier.** Please add the definition.\n"
           "2. **Needs lines.** §7.1 uses §4.3, which is not listed.\n\n"
           "**Overlaps.** The boundary in §2.10 is clean once item 3 is addressed.\n")


def test_a_referee_report_with_a_settled_phrase_inside_a_condition_is_a_review():
    # 2026-11-19: this report was read as a note that its findings were addressed, and never answered.
    event = replies.Event("2026-11-19T00:54:28Z", "referee", REFEREE, ref="review-1")
    assert replies.SETTLED.search(REFEREE) and not replies.settles(REFEREE)
    assert replies.is_review(event, {"carol"}, [])
    assert replies.wanted_label([event], {"carol"}, "1" * 40, markers=[], housekeeping=[], approvers=[])[0] == "awaiting-author"
    # Each reading holds on its own: the heading and the verdict ask for changes, and the condition settles nothing.
    for body in ("**Requested changes**\n\n1. Rebase onto main.\n\nThe rest is fine; previous findings closed.",
                 "Verdict: major changes. The earlier comments are addressed.",
                 "The ownership line is clean once item 3 is addressed.",
                 "I would approve once the Needs lines are fixed.",
                 "Fine by me if the second bullet of §2.10 is resolved."):
        assert replies.is_review(replies.Event("t", "referee", body), {"carol"}, []), body


def test_a_settled_note_stays_settled():
    for body in ("Thanks, the two findings are addressed at the new head; no further change requested.",
                 "The requested changes are resolved.",
                 "Thanks, the other finding is addressed.",
                 "If anything else comes up I will say; previous findings closed.",
                 "I checked the head once more; comments resolved."):
        assert replies.settles(body) and not replies.CHANGES.search(body), body
        assert not replies.is_review(replies.Event("t", "referee", body), {"carol"}, []), body
