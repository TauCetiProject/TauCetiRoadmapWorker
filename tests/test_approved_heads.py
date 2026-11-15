"""A pull request a human has approved is left alone: no round is filed for it and nothing is pushed
to it, since the content repository dismisses an approval on any push."""
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import openroadmaps
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import gate as lane_gate
from tauceti_roadmap_worker.lanes.base import LaneError


def ctx_with(reviews):
    class GH:
        def api(self, path, paginate=False):
            assert path == "repos/o/r/pulls/7/reviews?per_page=100" and paginate
            if isinstance(reviews, Exception):
                raise reviews
            return reviews
    return SimpleNamespace(gh=GH())


def review(who, state):
    return {"user": {"login": who}, "state": state}


def test_a_reviewers_latest_decision_says_whether_the_approval_stands():
    assert lane_gate.approvers(ctx_with([]), "o/r", 7) == []
    assert lane_gate.approvers(ctx_with([review("ann", "COMMENTED")]), "o/r", 7) == []
    assert lane_gate.approvers(ctx_with([review("ann", "APPROVED")]), "o/r", 7) == ["ann"]
    # a comment after the approval changes nothing; a dismissal or a request for changes withdraws it
    assert lane_gate.approvers(ctx_with([review("ann", "APPROVED"), review("ann", "COMMENTED")]), "o/r", 7) == ["ann"]
    assert lane_gate.approvers(ctx_with([review("ann", "APPROVED"), review("ann", "DISMISSED")]), "o/r", 7) == []
    assert lane_gate.approvers(ctx_with([review("ann", "APPROVED"), review("ann", "CHANGES_REQUESTED")]), "o/r", 7) == []
    assert lane_gate.approvers(ctx_with([review("ann", "DISMISSED"), review("bob", "APPROVED"),
                                         review("ann", "APPROVED")]), "o/r", 7) == ["ann", "bob"]


def test_reviews_that_cannot_be_read_stop_the_push():
    with pytest.raises(LaneError, match="could not be read"):
        lane_gate.approvers(ctx_with(GHError("GET repos/o/r/pulls/7/reviews: HTTP 502")), "o/r", 7)


def test_nothing_is_pushed_onto_an_approved_head(monkeypatch):
    pushed = []
    monkeypatch.setattr(lane_gate, "owes", lambda ctx, job, effect: True)
    monkeypatch.setattr(lane_gate.gitops, "git", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(lane_gate.gitops, "push", lambda *a, **k: pushed.append(a))
    ctx = ctx_with([review("ann", "APPROVED")])
    ctx.cfg = SimpleNamespace(repositories=SimpleNamespace(content="o/r"))
    ws = SimpleNamespace(extra={"pr": 7, "head_repo": "o/r", "owner": "alice"}, workdir=".")
    lane = SimpleNamespace(push=lambda f: f())
    with pytest.raises(LaneError, match="ann approved it, and a push would dismiss the approval"):
        lane_gate.push_and_reply(lane, ctx, SimpleNamespace(id="GATE-Foo~1"), ws, "report", {}, None,
                                 branch="b", base="c" * 40, moved=True)
    assert pushed == []


def test_the_listing_of_open_pull_requests_marks_the_approved_ones():
    assert "reviewDecision" in openroadmaps.QUERY

    class GH:
        def graphql(self, query, **variables):
            node = {"title": "t", "isDraft": False, "headRefOid": "a" * 40, "author": {"login": "alice"},
                    "labels": {"nodes": []},
                    "files": {"totalCount": 1, "nodes": [{"path": "TauCetiRoadmap/Foo/README.md", "changeType": "ADDED"}]}}
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {**node, "number": 1, "reviewDecision": "APPROVED"},
                {**node, "number": 2, "reviewDecision": "REVIEW_REQUIRED"},
                {**node, "number": 3, "reviewDecision": None}]}}}
    listed = {e["pr"]: e["approved"] for e in openroadmaps.inventory(GH(), "o/r", ("TauCetiRoadmap",))}
    assert listed == {1: True, 2: False, 3: False}
