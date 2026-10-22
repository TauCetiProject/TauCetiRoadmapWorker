"""The maintainer acts as the same login as the workers (CBirkbeck), so the worker-ported drafts are
theirs: a review they post there (`gh pr review --comment`, "Maintainer review of `Suggested.lean`")
must still flip the draft to awaiting-author and create a GATE job, while their workers' replies and
their other comments stay replies (2026-10-22: 13 such reviews and no gate job)."""

from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import intake
from tauceti_roadmap_worker.gh import GHError

from .fakes import FakeBoard, example_config

OWNER = "CBirkbeck"
SHA = "abc1234" + "0" * 33
PR = {"pr": 905, "name": "Foo", "head": SHA, "branch": "roadmap/foo", "owner": OWNER}
LABELS = f"repos/{example_config().repositories.content}/issues/905/labels"
REVIEW = {"id": 71, "user": {"login": OWNER}, "state": "COMMENTED", "commit_id": SHA,
          "submitted_at": "2026-10-22T12:58:00Z",
          "body": "Maintainer review of `Suggested.lean`: H1 the hypothesis on p is missing from `foo`."}


class PullGH:
    """GitHub REST for one roadmap pull request: its reviews, its issue comments, label writes."""

    def __init__(self, reviews=(), comments=(), labels=("awaiting-review",)):
        self.reviews, self.comments = list(reviews), list(comments)
        self.labels = list(labels)
        self.writes: list[tuple[str, str, dict]] = []

    def api(self, path: str, paginate: bool = False):
        n = PR["pr"]
        if path.endswith(f"/pulls/{n}"):
            return {"number": n, "draft": True, "labels": [{"name": label} for label in self.labels]}
        if f"/pulls/{n}/reviews" in path:
            return list(self.reviews)
        if f"/issues/{n}/comments" in path:
            return list(self.comments)
        raise GHError(f"GET {path}: not faked")

    def write(self, method: str, path: str, payload: dict | None = None):
        self.writes.append((method, path, payload or {}))
        return {}


@pytest.fixture
def sync(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "open_roadmap_prs", lambda ctx: [PR])

    def run(gh, board, maintainers=(OWNER,)):
        cfg = example_config()
        cfg.replies.maintainer_logins = list(maintainers)
        return intake.sync_pull_requests(SimpleNamespace(cfg=cfg, gh=gh, board=board, state=tmp_path))
    return run


def comment(at: str, body: str, user: str = OWNER) -> dict:
    return {"id": 1, "user": {"login": user}, "created_at": at, "body": body}


def test_a_maintainer_review_from_the_owner_login_creates_one_gate_job(sync):
    gh, board = PullGH(reviews=[REVIEW], comments=[comment("2026-10-21T09:00:00Z", "Fixed in `0000000`.")]), FakeBoard()
    out = sync(gh, board)
    assert gh.writes == [("POST", LABELS, {"labels": ["awaiting-author"]}), ("DELETE", f"{LABELS}/awaiting-review", {})]
    [(job, state, _)] = board.created
    assert job.id == "GATE-Foo~1" and job.requires_operator == OWNER and job.inputs == {"pr": 905, "operator": OWNER}
    assert f"#905: GATE-Foo~1 created for @{OWNER}" in out and "#905 Foo: awaiting-author" in out
    # The next sync sees the open gate job and creates no second one.
    job.state = state
    board.jobs.append(job)
    sync(PullGH(reviews=[REVIEW], labels=("awaiting-author",)), board)
    assert len(board.created) == 1


def test_without_the_login_configured_the_owners_review_stays_a_reply(sync):
    board = FakeBoard()
    assert "#905 Foo: awaiting-review" in sync(PullGH(reviews=[REVIEW]), board, maintainers=())
    assert board.created == []


def test_the_gate_lanes_reply_from_the_same_login_creates_no_gate_job(sync):
    reply = comment("2026-10-22T14:00:00Z", "Fixed in `def5678` (reply to the review):\n\n"
                                            "- **H01** (applied) — Maintainer review asked for the hypothesis.\n")
    gh, board = PullGH(reviews=[REVIEW], comments=[reply], labels=("awaiting-author",)), FakeBoard()
    assert "#905 Foo: awaiting-review" in sync(gh, board)
    assert board.created == []
    assert gh.writes == [("POST", LABELS, {"labels": ["awaiting-review"]}), ("DELETE", f"{LABELS}/awaiting-author", {})]


def test_a_plain_owner_comment_without_the_prefix_stays_a_reply(sync):
    note = comment("2026-10-22T13:00:00Z", "Rebased on main; the merge order is unchanged.")
    board = FakeBoard()
    assert "#905 Foo: awaiting-review" in sync(PullGH(comments=[note]), board)
    assert board.created == []
