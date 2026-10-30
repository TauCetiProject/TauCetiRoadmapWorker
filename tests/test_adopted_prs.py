"""The pool (2026-10-27): operators who act for one another on managed roadmap pull requests. A pull
request whose author is in the pool gets gate rounds any pool member may take, a pool member's reply
counts as the author's, and the repair is pushed onto the pull request's own branch (which lives in the
content repository, not in a fork)."""

from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import intake
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes.base import pr_answerers, pr_operator

from .fakes import FakeBoard, example_config

AUTHOR, CUSTODIAN, THIRD = "kim-em", "CBirkbeck", "roed-math"
SHA = "abc1234" + "0" * 33
PR = {"pr": 1398, "name": "PlanarHodge", "head": SHA, "branch": "roadmap/planar-hodge", "owner": AUTHOR,
      "operator": "", "head_repo": "TauCetiProject/TauCetiRoadmap"}
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
        if "/pulls/1398/comments" in path:
            return []
        raise GHError(f"GET {path}: not faked")

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        return {}


def adopted_config():
    cfg = example_config()
    cfg.roadmap.pool = [CUSTODIAN, AUTHOR, THIRD]
    return cfg


def test_pool_authors_are_answered_by_any_pool_member():
    cfg = adopted_config()
    assert pr_operator(cfg, AUTHOR) == "" and pr_operator(cfg, CUSTODIAN) == ""
    assert pr_answerers(cfg, AUTHOR) == {AUTHOR, CUSTODIAN, THIRD}
    assert pr_operator(cfg, "someone-else") == "someone-else"
    assert pr_answerers(cfg, "someone-else") == {"someone-else"}
    plain = example_config()
    assert pr_operator(plain, AUTHOR) == AUTHOR and pr_answerers(plain, AUTHOR) == {AUTHOR}


@pytest.fixture
def sync(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "open_roadmap_prs", lambda ctx: [PR])

    def run(gh, board):
        return intake.sync_pull_requests(SimpleNamespace(cfg=adopted_config(), gh=gh, board=board, state=tmp_path))
    return run


def test_a_review_on_a_pool_pull_request_creates_a_gate_job_any_pool_member_may_take(sync):
    gh, board = PullGH(reviews=[REVIEW]), FakeBoard()
    out = sync(gh, board)
    [(job, _state, _)] = board.created
    assert job.id == "GATE-PlanarHodge~1"
    assert job.requires_operator == ""
    assert job.inputs == {"pr": 1398, "operator": "", "owner": AUTHOR}
    assert "#1398: GATE-PlanarHodge~1 created for the pool" in out
    assert ("POST", LABELS, {"labels": ["awaiting-author"]}) in gh.writes


def test_a_pool_members_reply_counts_as_the_authors_reply(sync):
    reply = {"id": 2, "user": {"login": CUSTODIAN}, "created_at": "2026-10-27T10:00:00Z",
             "body": "Fixed in `def5678` (reply to the review):\n\n- **H01** (applied) — sign corrected.\n"}
    gh, board = PullGH(reviews=[REVIEW], comments=[reply], labels=("awaiting-author",)), FakeBoard()
    out = sync(gh, board)
    assert board.created == []
    assert "#1398 PlanarHodge: awaiting-review" in out
    assert ("POST", LABELS, {"labels": ["awaiting-review"]}) in gh.writes


def test_a_cycle_elsewhere_is_not_this_roadmaps_problem():
    from tauceti_roadmap_worker import depgraph
    edges = {"A": {"B": {1}}, "B": {"A": {2}}, "C": {"D": {3}}, "D": {}}
    order = ["D", "C", "A", "B"]
    assert depgraph.check_order(edges, order, only={"C"}) == []
    assert any(p.startswith("CYCLE") for p in depgraph.check_order(edges, order, only={"A"}))
    assert any(p.startswith("CYCLE") for p in depgraph.check_order(edges, order))


def test_a_pool_member_may_take_a_gate_round_that_names_another_pool_member():
    from tauceti_roadmap_worker.board import Job
    from tauceti_roadmap_worker.lanes.base import Lane
    import tempfile
    from pathlib import Path
    cfg = adopted_config()
    state = Path(tempfile.mkdtemp())
    ctx = SimpleNamespace(cfg=cfg, agent="claude", model="opus", operator=CUSTODIAN, claims=None, state=state)
    job = Job(id="GATE-X~1", kind="gate", name="X", requires_operator=AUTHOR)
    assert Lane().eligible(job, ctx) == ""
    job.requires_operator = "someone-else"
    assert Lane().eligible(job, ctx).startswith("needs the branch owner")
    plain = SimpleNamespace(cfg=example_config(), agent="claude", model="opus", operator=CUSTODIAN, claims=None, state=state)
    job.requires_operator = AUTHOR
    assert Lane().eligible(job, plain).startswith("needs the branch owner")


def test_a_gate_submission_may_carry_the_merge_order_file():
    from tauceti_roadmap_worker import board
    from tauceti_roadmap_worker.board import Job
    job = Job(id="GATE-X~1", kind="gate", name="X")
    assert "merge-order/ORDER.txt" in board.allowed_paths(job, "packages", "merge-order/ORDER.txt")
    review = Job(id="REV-PKG-X~1", kind="review", name="X")
    assert "merge-order/ORDER.txt" not in board.allowed_paths(review, "packages", "merge-order/ORDER.txt")


def _event(t, user, body, cid):
    from tauceti_roadmap_worker import replies
    return replies.Event(t=t, user=user, body=body, ref=f"comment-{cid}", version=replies.text_version(body))


def test_another_pool_members_own_words_are_a_review_not_a_reply():
    """A pool member who reviews another member's pull request in their own words is a reviewer: only
    a comment in reply form (the worker's, the operator's scripts') answers for the author."""
    from tauceti_roadmap_worker import replies
    cfg = adopted_config()
    r = cfg.replies
    ours = pr_answerers(cfg, AUTHOR)
    theirs = _event("2026-10-19T03:58:08Z", THIRD,
                    "Shouldn't the Lean code all go in Suggested.lean, which is the standard place? "
                    "Layer 2 also cites a theorem the paper does not prove.", 1)
    kw = dict(markers=r.review_markers, housekeeping=r.housekeeping_prefixes, approvers=r.approvers, owner=AUTHOR)
    assert [e.ref for e in replies.unanswered([theirs], ours, r.review_markers, r.housekeeping_prefixes,
                                              owner=AUTHOR)] == ["comment-1"]
    assert replies.wanted_label([theirs], ours, SHA, **kw)[0] == "awaiting-author"
    for_them = _event("2026-10-20T04:08:24Z", CUSTODIAN,
                      "Fixed in `def5678` (reply to the review):\n\n- **F01** (applied) — moved to Suggested.lean.", 2)
    assert replies.unanswered([theirs, for_them], ours, r.review_markers, r.housekeeping_prefixes, owner=AUTHOR) == []
    assert replies.wanted_label([theirs, for_them], ours, SHA, **kw)[0] == "awaiting-review"
    # The author's own words answer, as before; without an owner every answerer's do.
    own = _event("2026-10-20T04:08:24Z", AUTHOR, "Moved the code to Suggested.lean and fixed the citation.", 3)
    assert replies.unanswered([theirs, own], ours, r.review_markers, r.housekeeping_prefixes, owner=AUTHOR) == []
    assert replies.unanswered([theirs], ours, r.review_markers, r.housekeeping_prefixes) == []


def test_a_gate_round_on_a_family_or_nested_name_works_in_the_nested_roadmap_folder():
    from tauceti_roadmap_worker.lanes.gate import nested_folder
    readmes = {"RepresentationTheory", "RepresentationTheory/SemisimpleAlgebras", "RepresentationTheory/SchurWeyl", "Foo"}
    changed = ["TauCetiRoadmap/RepresentationTheory/SemisimpleAlgebras/README.md",
               "TauCetiRoadmap/RepresentationTheory/SemisimpleAlgebras/Suggested.lean",
               "TauCetiRoadmap/RepresentationTheory/Suggested.lean"]      # the index's stray file belongs to no roadmap
    assert nested_folder("RepresentationTheory", changed, readmes, "TauCetiRoadmap") == "RepresentationTheory/SemisimpleAlgebras"
    assert nested_folder("SemisimpleAlgebras", changed, readmes, "TauCetiRoadmap") == "RepresentationTheory/SemisimpleAlgebras"
    assert nested_folder("SchurWeyl", [], readmes, "TauCetiRoadmap") == "RepresentationTheory/SchurWeyl"
    assert nested_folder("Foo", ["TauCetiRoadmap/Foo/README.md"], readmes, "TauCetiRoadmap") == "Foo"
    assert nested_folder("Bar", [], readmes, "TauCetiRoadmap") == "Bar"


def test_sibling_readmes_find_nested_roadmaps_and_a_family_index_contributes_its_members(tmp_path):
    from tauceti_roadmap_worker.lanes.base import sibling_readmes
    state = tmp_path / "state"
    fam = state / "rev" / "1145" / "TauCetiRoadmap" / "RepresentationTheory"
    (fam / "SmoothBanachRepresentations").mkdir(parents=True)
    (fam / "SchurWeyl").mkdir()
    (fam / "README.md").write_text("# index\n")
    (fam / "metadata.toml").write_text('topic = "math.RT"\n')
    (fam / "SmoothBanachRepresentations" / "README.md").write_text("*Needs:* Alpha §1.\n")
    (fam / "SchurWeyl" / "README.md").write_text("x\n")
    plain = state / "rev" / "7" / "TauCetiRoadmap" / "Alpha"
    plain.mkdir(parents=True)
    (plain / "README.md").write_text("alpha\n")
    prs = [{"pr": 1145, "name": "RepresentationTheory"}, {"pr": 7, "name": "Alpha"}, {"pr": 8, "name": "SchurWeyl"}]
    (state / "rev" / "8" / "TauCetiRoadmap" / "RepresentationTheory" / "SchurWeyl").mkdir(parents=True)
    (state / "rev" / "8" / "TauCetiRoadmap" / "RepresentationTheory" / "SchurWeyl" / "README.md").write_text("sw8\n")
    out = sibling_readmes(state, "TauCetiRoadmap", prs)
    assert out["SmoothBanachRepresentations"] == "*Needs:* Alpha §1.\n" and out["Alpha"] == "alpha\n"
    assert out["SchurWeyl"] == "x\n" and "RepresentationTheory" not in out      # the family's member wins over the later snapshot
