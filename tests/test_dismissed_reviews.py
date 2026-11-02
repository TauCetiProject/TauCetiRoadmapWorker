"""A dismissed review, or one submitted with no text, is not an event the author must answer."""

from tauceti_roadmap_worker.lanes.pr import events_from_api


def _r(i, state, body, user="CBirkbeck"):
    return {"id": i, "submitted_at": f"2026-10-29T20:5{i}:00Z", "user": {"login": user}, "state": state, "body": body,
            "commit_id": "d0eb5c6"}


def test_dismissed_and_empty_reviews_make_no_event():
    reviews = [_r(1, "COMMENTED", "Verdict: CHANGES NEEDED\n\n- **F01** …"), _r(2, "DISMISSED", ""),
               _r(3, "COMMENTED", ""), _r(4, "APPROVED", ""), _r(5, "CHANGES_REQUESTED", "")]
    refs = [e.ref for e in events_from_api(reviews, [])]
    assert refs == ["review-1", "review-3", "review-4", "review-5"]
