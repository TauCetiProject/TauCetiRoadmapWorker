"""Adopted authors (2026-10-27): the maintainer took over another author's roadmap pull requests. Their
gate rounds go to the custodian, the custodian's replies count as the author's, and the repair is
pushed onto the pull request's own branch (which lives in the content repository, not in a fork)."""

from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import intake
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes.base import pr_operator

from .fakes import FakeBoard, example_config

AUTHOR, CUSTODIAN = "kim-em", "CBirkbeck"
SHA = "abc1234" + "0" * 33
PR = {"pr": 1398, "name": "PlanarHodge", "head": SHA, "branch": "roadmap/planar-hodge", "owner": AUTHOR,
      "operator": CUSTODIAN, "head_repo": "TauCetiProject/TauCetiRoadmap"}
LABELS = f"repos/{example_config().repositories.content}/issues/1398/labels"
REVIEW = {"id": 5, "user": {"login": CUSTODIAN}, "state": "COMMENTED", "commit_id": SHA,
          "submitted_at": "2026-10-27T09:00:00Z",
          "body": "🤖 AI-assisted review by ChatGPT\nReviewed head: " + SHA + "\n\nVerdict: CHANGES NEEDED\n\n1. H1 wrong sign."}


class PullGH:
    def __init__(self, reviews=(), comments=(), labels=("awaiting-review",)):
        self.reviews, self.comments, self.labels = list(reviews), list(comments), list(labels)
        self.writes = []

    def api(self, path, paginate=False):
        if path.endswith("/pulls/1398"):
            return {"number": 1398, "draft": False, "labels": [{"name": l} for l in self.labels]}
        if "/pulls/1398/reviews" in path:
            return list(self.reviews)
        if "/issues/1398/comments" in path:
            return list(self.comments)
        raise GHError(f"GET {path}: not faked")

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        return {}


def adopted_config():
    cfg = example_config()
    cfg.roadmap.adopted_authors = [AUTHOR]
    cfg.roadmap.custodian = CUSTODIAN
    return cfg


def test_pr_operator_maps_an_adopted_author_to_the_custodian():
    cfg = adopted_config()
    assert pr_operator(cfg, AUTHOR) == CUSTODIAN
    assert pr_operator(cfg, CUSTODIAN) == CUSTODIAN
    assert pr_operator(cfg, "someone-else") == "someone-else"
    plain = example_config()
    assert pr_operator(plain, AUTHOR) == AUTHOR


@pytest.fixture
def sync(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "open_roadmap_prs", lambda ctx: [PR])

    def run(gh, board):
        return intake.sync_pull_requests(SimpleNamespace(cfg=adopted_config(), gh=gh, board=board, state=tmp_path))
    return run


def test_a_review_on_an_adopted_pull_request_creates_a_gate_job_for_the_custodian(sync):
    gh, board = PullGH(reviews=[REVIEW]), FakeBoard()
    out = sync(gh, board)
    [(job, _state, _)] = board.created
    assert job.id == "GATE-PlanarHodge~1"
    assert job.requires_operator == CUSTODIAN
    assert job.inputs == {"pr": 1398, "operator": CUSTODIAN, "owner": AUTHOR}
    assert f"#1398: GATE-PlanarHodge~1 created for @{CUSTODIAN}" in out
    assert ("POST", LABELS, {"labels": ["awaiting-author"]}) in gh.writes


def test_the_custodians_reply_counts_as_the_authors_reply(sync):
    reply = {"id": 2, "user": {"login": CUSTODIAN}, "created_at": "2026-10-27T10:00:00Z",
             "body": "Fixed in `def5678` (reply to the review):\n\n- **H01** (applied) — sign corrected.\n"}
    gh, board = PullGH(reviews=[REVIEW], comments=[reply], labels=("awaiting-author",)), FakeBoard()
    out = sync(gh, board)
    assert board.created == []
    assert "#1398 PlanarHodge: awaiting-review" in out
    assert ("POST", LABELS, {"labels": ["awaiting-review"]}) in gh.writes
