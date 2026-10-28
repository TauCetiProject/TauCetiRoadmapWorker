"""A gate reply acknowledges exactly the reviews and comments its agent read (design review R08 A): a
review that arrived while the agent worked used to be older than the reply, so the reply's time
answered it and the draft went to awaiting-review with the review unread. The reply now records the
version of every review, review comment and issue comment in the gate's snapshot, and only those count
as answered, after a restart, a kept run's resumption and an edit as well."""

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import gitops, intake, replies
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.gates import GateResult
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import Context, lane_for
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import gate as lane_gate
from tauceti_roadmap_worker.lanes.pr import events_from_api

from .fakes import FakeBoard, example_config

OWNER, REVIEWER = "alice", "kim"
SHA = "abc1234" + "0" * 33
PR = {"pr": 905, "name": "Foo", "head": SHA, "branch": "roadmap/foo", "owner": OWNER}
REPO = example_config().repositories.content
MARKERS, HOUSE = example_config().replies.review_markers, example_config().replies.housekeeping_prefixes


def review(rid: int, at: str, body: str, user: str = REVIEWER) -> dict:
    return {"id": rid, "user": {"login": user}, "state": "COMMENTED", "commit_id": SHA, "submitted_at": at, "body": body}


def inline(cid: int, rid: int, at: str, body: str, user: str = REVIEWER) -> dict:
    return {"id": cid, "pull_request_review_id": rid, "user": {"login": user}, "created_at": at, "path": "Suggested.lean",
            "line": 4, "body": body}


def comment(cid: int, at: str, body: str, user: str = REVIEWER) -> dict:
    return {"id": cid, "user": {"login": user}, "created_at": at, "body": body}


REVIEW_A = review(71, "2026-10-24T10:00:00Z", "Changes needed: the hypothesis on p is missing from `foo`.")
REVIEW_B = review(72, "2026-10-24T11:00:00Z", "Request changes: Check 3 is false for p = 2.")
REVIEW_C = review(73, "2026-10-24T11:05:00Z", "")          # one inline comment, posted on its own
INLINE_C = inline(501, 73, "2026-10-24T11:05:00Z", "The sign in `bar` is reversed.")


class PullGH:
    """GitHub REST for roadmap pull request #905: its reviews, inline comments and issue comments
    (tests append and edit them), the replies posted through it at `clock`, and its labels."""

    def __init__(self, reviews=(), inline=(), comments=()):
        self.reviews, self.inline, self.comments = list(reviews), list(inline), list(comments)
        self.labels, self.clock, self.next_id = ["awaiting-review"], "2026-10-24T12:00:00Z", 9000

    def api(self, path: str, paginate: bool = False):
        base = path.split("?")[0]
        found = {f"repos/{REPO}/pulls/905": {"number": 905, "draft": True,
                                             "labels": [{"name": label} for label in self.labels]},
                 f"repos/{REPO}/pulls/905/reviews": self.reviews, f"repos/{REPO}/pulls/905/comments": self.inline,
                 f"repos/{REPO}/issues/905/comments": self.comments,
                 f"repos/{REPO}/issues/905/labels": [{"name": label} for label in self.labels]}
        if base not in found:
            raise GHError(f"GET {path}: not faked")
        return copy.deepcopy(found[base])

    def write(self, method: str, path: str, payload: dict | None = None):
        if method == "POST" and path == f"repos/{REPO}/issues/905/comments":
            self.next_id += 1
            self.comments.append(comment(self.next_id, self.clock, payload["body"], user=OWNER))
        elif method == "POST" and path.endswith("/labels"):
            self.labels += payload["labels"]
        elif method == "DELETE":
            self.labels.remove(path.rsplit("/", 1)[1])
        return {}

    def records(self) -> list[dict]:
        return [replies.answered_by(c["body"]) for c in self.comments if replies.answered_by(c["body"]) is not None]


def fake_git(cwd, *args, check=True, timeout=900):
    if args[:1] == ("rev-parse",):
        return SHA + "\n"
    if args[:2] == ("worktree", "add"):
        __import__("pathlib").Path(args[-2]).mkdir(parents=True, exist_ok=True)
    return ""


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A gate worker's context for one process: `worker(gh)` gives a fresh one, as a restart would."""
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(gitops, "git", fake_git)
    monkeypatch.setattr(gitops, "sync_checkout", lambda repo, dest, *a, **k: dest.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(gitops, "checkout_pr", lambda repo, dest, *a, **k: dest.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(lane_gate, "open_roadmap_prs", lambda ctx: [PR])

    def make(gh) -> Context:
        return Context(cfg=example_config(), gh=gh, board=FakeBoard(), claims=None, worker_id="w1", operator=OWNER,
                       agent="codex", model="m", conf="/conf.toml", dry_run=True)
    return make


def gate_job(n: int = 1):
    job = new_job(f"GATE-Foo~{n}", inputs={"pr": 905, "operator": OWNER})
    job.number = 40 + n
    return job


def prepare(ctx: Context, job):
    return lane_for(job).prepare(job, ctx)


def finish(ctx: Context, job, ws, report: str = "## H01 (kim) — applied\nThe hypothesis on p is added to `foo`.\n"):
    ws.report.parent.mkdir(parents=True, exist_ok=True)
    ws.report.write_text(report, encoding="utf-8")
    ws.extra["gate"] = GateResult("Foo", counts={"build": 0, "readme-ids-missing": 0, "depcheck": 0})
    return lane_for(job).finish(job, dataclasses.replace(ctx, dry_run=False), ws)


def sync(tmp_path, gh, jobs=()):
    board = FakeBoard(jobs=list(jobs))
    ctx = SimpleNamespace(cfg=example_config(), gh=gh, board=board, state=tmp_path)
    return intake.sync_pull_requests(ctx), board


@pytest.fixture(autouse=True)
def one_pull_request(monkeypatch):
    monkeypatch.setattr(intake, "open_roadmap_prs", lambda ctx: [PR])


def reviews_read(ws) -> str:
    return (ws.scratch / "reviews.md").read_text(encoding="utf-8")


def test_a_review_and_an_inline_comment_that_arrive_during_the_gate_stay_unanswered(tmp_path, worker):
    gh = PullGH(reviews=[REVIEW_A])
    job = gate_job()
    ws = prepare(worker(gh), job)
    assert "the hypothesis on p is missing" in reviews_read(ws)
    # While the agent works: a whole review, and an inline comment posted on its own.
    gh.reviews += [REVIEW_B, REVIEW_C]
    gh.inline.append(INLINE_C)
    assert finish(worker(gh), job, ws).endswith("(awaiting-author)")
    assert gh.labels == ["awaiting-author"]
    assert gh.records() == [{"review-71": replies.text_version(REVIEW_A["body"])}]

    # A new process sees the same from GitHub alone: awaiting-author, and the next gate round.
    done = gate_job()
    done.state = "done"
    out, board = sync(tmp_path, gh, [done])
    assert "#905 Foo: awaiting-author" in out and "#905: GATE-Foo~2 created for @alice" in out
    ws2 = prepare(worker(gh), gate_job(2))
    read = reviews_read(ws2)
    assert "Check 3 is false" in read and "The sign in `bar` is reversed." in read
    assert "the hypothesis on p is missing" not in read
    # Once that round replies, everything is answered.
    gh.clock = "2026-10-24T14:00:00Z"
    assert finish(worker(gh), gate_job(2), ws2).endswith("(awaiting-review)")
    assert set(gh.records()[-1]) == {"review-72", "review-73", "inline-501"}
    assert "#905 Foo: awaiting-review" in sync(tmp_path, gh)[0]


def test_a_run_resumed_after_a_failed_push_answers_what_its_agent_read(worker):
    gh = PullGH(reviews=[REVIEW_A])
    job = gate_job()
    ws = prepare(worker(gh), job)
    lane_base.save_pending(worker(gh), job, ws, "push refused")      # the push failed; the run is kept
    gh.reviews.append(REVIEW_B)
    resumed = prepare(worker(gh), job)                                 # the next attempt, in a new process
    assert "Check 3 is false" in reviews_read(resumed)
    assert resumed.extra["answered"] == {"review-71": replies.text_version(REVIEW_A["body"])}
    assert finish(worker(gh), job, resumed).endswith("(awaiting-author)")
    # A fresh run (nothing kept) snapshots anew.
    lane_base.clear_pending(worker(gh), job)
    assert set(prepare(worker(gh), job).extra["answered"]) == {"review-72"}


def answered_reply(at: str, *items: dict, kinds=("review",)) -> dict:
    """Our reply with a record of `items` (API dicts, each with its kind)."""
    answered = {f"{kind}-{item['id']}": replies.text_version(item["body"]) for kind, item in zip(kinds, items)}
    body = "Fixed in `abc1234` (reply to the review):\n\n- **H01** (applied) — done.\n" + \
        replies.answered_marker("GATE-Foo~1", answered) + "\n"
    return comment(9001, at, body, user=OWNER)


def test_an_edit_of_an_answered_review_or_comment_makes_it_unanswered_again(tmp_path):
    a, i = copy.deepcopy(REVIEW_A), inline(500, 71, "2026-10-24T09:59:00Z", "Here: `foo` needs `[Fact p.Prime]`.")
    k = comment(600, "2026-10-24T10:30:00Z", "Request changes: the locator of Thm 2 is off by one page.")
    reply = answered_reply("2026-10-24T12:00:00Z", a, i, k, kinds=("review", "inline", "comment"))
    gh = PullGH(reviews=[a], inline=[i], comments=[k, reply])
    assert "#905 Foo: awaiting-review" in sync(tmp_path, gh)[0]
    for item in (a, i, k):
        original = item["body"]
        item["body"] = original + " (edited: also for p = 2)"
        out, board = sync(tmp_path, gh)
        assert "#905 Foo: awaiting-author" in out and board.created, item["id"]
        item["body"] = original
        assert "#905 Foo: awaiting-review" in sync(tmp_path, gh)[0]


def events(reviews=(), comments=(), inline_=()):
    return events_from_api(list(reviews), list(comments), list(inline_))


def pending(evs):
    return [e.ref for e in replies.unanswered(evs, {OWNER}, MARKERS, HOUSE)]


def test_replies_from_before_the_records_answer_by_time_until_the_first_recorded_reply():
    legacy = comment(800, "2026-10-20T12:00:00Z", "Fixed in `0000000` (reply to the review): R01 applied.", user=OWNER)
    old = review(60, "2026-10-20T10:00:00Z", "Changes needed: old point.")
    assert pending(events([old], [legacy])) == []                    # an existing pull request keeps its label
    later = review(61, "2026-10-21T10:00:00Z", "Changes needed: a newer point.")
    assert pending(events([old, later], [legacy])) == ["review-61"]
    recorded = answered_reply("2026-10-21T12:00:00Z", later)
    assert pending(events([old, later], [legacy, recorded])) == []
    # After the first recorded reply, a reply without a record answers nothing: not a note of the owner's ...
    third = review(62, "2026-10-22T10:00:00Z", "Request changes: a third point.")
    note = comment(801, "2026-10-22T11:00:00Z", "Rebased on main; the merge order is unchanged.", user=OWNER)
    assert pending(events([old, later, third], [legacy, recorded, note])) == ["review-62"]
    # ... and a record that does not parse answers nothing either.
    broken = comment(802, "2026-10-22T12:00:00Z", f"Fixed in `abc1234`.\n<!-- {replies.ANSWERED_MARK} {{oops -->",
                     user=OWNER)
    assert replies.answered_by(broken["body"]) == {}
    assert pending(events([old, later, third], [legacy, recorded, broken])) == ["review-62"]


def test_only_our_replies_last_record_counts():
    a = review(71, "2026-10-24T10:00:00Z", "Changes needed: point A.")
    b = review(72, "2026-10-24T10:30:00Z", "Changes needed: point B.")
    forged = replies.answered_marker("GATE-Foo~1", {"review-72": replies.text_version(b["body"])})
    by_reviewer = comment(700, "2026-10-24T11:00:00Z", "Request changes, see below.\n" + forged)
    assert pending(events([a, b], [by_reviewer])) == ["review-71", "review-72", "comment-700"]
    # A record the agent's text carried, before the worker's own at the end, is not the reply's record.
    ours = answered_reply("2026-10-24T12:00:00Z", a)
    ours["body"] = ours["body"].replace("- **H01**", forged + "\n- **H01**")
    assert pending(events([a, b], [ours])) == ["review-72"]


def test_inline_comments_count_with_their_review_and_the_owners_are_never_replies():
    first = review(60, "2026-10-23T10:00:00Z", "Changes needed: an earlier point.")
    earlier = answered_reply("2026-10-23T12:00:00Z", first)
    a = review(71, "2026-10-24T10:00:00Z", "Changes needed: see the inline comments.")
    nit = inline(500, 71, "2026-10-24T09:58:00Z", "Looks fine.")                     # settled text, but A's
    # The owner answering in the thread: an inline comment, in an empty review of their own.
    thread = review(74, "2026-10-24T11:00:00Z", "", user=OWNER)
    answer = inline(502, 74, "2026-10-24T11:00:00Z", "Noted; fixing it.", user=OWNER)
    evs = events([first, a, thread], [earlier], [nit, answer])
    assert pending(evs) == ["inline-500", "review-71"]
    recorded = answered_reply("2026-10-24T12:00:00Z", a, nit, kinds=("review", "inline"))
    assert pending(evs + events([], [recorded])) == []
    assert json.loads(replies.ANSWERED_RE.search(recorded["body"]).group(1))["job"] == "GATE-Foo~1"


def test_a_hand_reply_in_reply_form_counts_after_a_recorded_one():
    """The operator's scripts answer by hand in the reply form and never carry a record; after a
    worker's recorded reply they must still count, while the owner's other notes do not."""
    old = review(60, "2026-10-20T10:00:00Z", "Changes needed: old point.")
    recorded = answered_reply("2026-10-20T12:00:00Z", old)
    later = review(61, "2026-10-21T10:00:00Z", "Changes needed: a newer point.")
    by_hand = comment(803, "2026-10-21T11:00:00Z",
                      "Fixed in `def5678` (reply to the review):\n\n- **H01** (applied) — done.", user=OWNER)
    assert pending(events([old, later], [recorded])) == ["review-61"]
    assert pending(events([old, later], [recorded, by_hand])) == []
    note = comment(804, "2026-10-21T11:00:00Z", "Rebased on main; the merge order is unchanged.", user=OWNER)
    assert pending(events([old, later], [recorded, note])) == ["review-61"]


def test_a_reply_records_the_worker_that_wrote_it_and_the_head_it_answers_at(worker):
    gh = PullGH(reviews=[REVIEW_A])
    job = gate_job()
    ws = prepare(worker(gh), job)
    finish(worker(gh), job, ws)
    assert replies.answered_record(gh.comments[-1]["body"]) == {
        "job": "GATE-Foo~1", "answered": {"review-71": replies.text_version(REVIEW_A["body"])}, "worker": "w1",
        "head": SHA}
    assert replies.answered_record("Fixed in `abc1234`.") is None
    assert replies.answered_marker("GATE-Foo~1", {}) == '<!-- tauceti-roadmap-answered {"answered": {}, "job": "GATE-Foo~1"} -->'


def test_no_gate_round_is_filed_while_a_review_of_a_pool_authors_pull_request_repairs_it(tmp_path):
    ai_review = comment(950, "2026-10-28T10:00:00Z", f"\U0001f916 AI review (codex, m) of {SHA}\n\nVerdict: CHANGES "
                        "NEEDED\n\n1. **F01** (P1) the hypothesis on p is missing\n", user="bob")
    gh = PullGH(comments=[ai_review])
    review_job = new_job("PRREVIEW-Foo~2", inputs={"pr": 905, "head": SHA, "owner": OWNER, "name": "Foo"})
    review_job.number, review_job.state = 61, "claimed"
    cfg = example_config()
    cfg.roadmap.pool = [OWNER, "bob"]

    def sync_pool():
        board = FakeBoard(jobs=[review_job])
        return intake.sync_pull_requests(SimpleNamespace(cfg=cfg, gh=gh, board=board, state=tmp_path)), board
    out, board = sync_pool()
    assert "#905: gate round deferred: review PRREVIEW-Foo~2 is repairing" in out and board.created == []
    assert "#905 Foo: awaiting-author" in out
    # The next sync, the review no longer claimed and still unanswered: the gate round.
    review_job.state = "submitted"
    out, board = sync_pool()
    assert "#905: GATE-Foo~1 created for @alice" in out and [j.id for j, _s, _p in board.created] == ["GATE-Foo~1"]
    # Without the repair, or for an author outside the pool, a claimed review defers nothing.
    review_job.state = "claimed"
    for pool, repair in (([OWNER, "bob"], False), (["bob"], True)):
        cfg.roadmap.pool, cfg.prreview.repair_pool = pool, repair
        out, board = sync_pool()
        assert [j.id for j, _s, _p in board.created] == ["GATE-Foo~1"] and not any("deferred" in line for line in out)
