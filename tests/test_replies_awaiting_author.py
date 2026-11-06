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
