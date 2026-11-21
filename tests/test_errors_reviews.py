"""A review that points out errors takes the AI review's acceptance label off the pull request until it
is answered, whoever wrote it (the owner's rule of 2026-11-21). Twice a contributor's review in state
CHANGES_REQUESTED, with inline threads giving counterexamples, left `ai-found-no-errors` beside
`awaiting-author` until the fix was pushed. `replies.unanswered_errors` is that review, `intake.errors_hold`
the one decision every writer of the label reads: `board sync`'s two steps and the review lane."""

import re
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from tauceti_roadmap_worker import config, intake, replies
from tauceti_roadmap_worker.board import new_job

from .fakes import FakeBoard, example_config
from .test_prreview import (
    H1,
    H2,
    REPO,
    ContentGH,
    LabelGH,
    finished_ws,
    lane_ctx,
    node,
    review_env,  # noqa: F401 (fixture)
    review_job,
    verdict_file,
)

AUTHOR, REVIEWER, OTHER, BOT = "alice", "reviewer1", "contributor2", "lint-bot"
PR = 905
LABEL, APPROVAL = "ai-found-no-errors", "awaiting-human-approval"
CUES = config.Replies().error_cues
MARKERS, HOUSE = config.Replies().review_markers, config.Replies().housekeeping_prefixes

# Three reviews posted on accepted pull requests (shortened): each says it found no errors and asks the
# author something. The second and third list gaps against the source and ask for a change of an
# assumption; a gap against a source is not an error of the text, so none of them takes the label off.
FOUND_NONE = [
    ("I read this against definition 17 of the source, clause by clause, and I found no errors in it. One choice "
     "is open: the README takes the cover to be finite, the source allows any cover. Which would you prefer?"),
    ("I compared G3a with definition 57 of the source, and I found no errors. G3a lacks two things that the "
     "definition itself mentions: the compactness condition and the base point. Does that suit you?"),
    ("I read 7.8 against definition 65 and the two lemmas before it, and I found no errors. Three gaps against the "
     "definition: it asks for a separable extension, a finite base and a reduced ring, and 7.8 assumes a domain. "
     "Could the definition drop that assumption, or is it needed later?"),
]
LISTING = ("Two false statements in Suggested.lean: 3.4 fails for p = 2, and 5.1 is stated over any ring. "
           "Missing hypotheses: 5.1 and 5.2 need the ring to be a domain.")


def review(rid, at, body="", user=REVIEWER, state="COMMENTED", head=H1):
    return {"id": rid, "user": {"login": user}, "state": state, "commit_id": head, "submitted_at": at, "body": body}


def inline(cid, rid, at, body, user=REVIEWER, reply_to=None):
    return {"id": cid, "pull_request_review_id": rid, "user": {"login": user}, "created_at": at,
            "path": "TauCetiRoadmap/Foo/Suggested.lean", "line": 12, "original_line": 12, "body": body,
            **({"in_reply_to_id": reply_to} if reply_to else {})}


def comment(cid, at, body, user=AUTHOR):
    return {"id": cid, "user": {"login": user}, "created_at": at, "body": body}


CHANGES = review(71, "2026-11-21T09:00:00Z", "Two of the statements do not hold as written.", state="CHANGES_REQUESTED")
THREADS = [inline(501, 71, "2026-11-21T09:00:00Z", "Counterexample: p = 2, the quotient is not a field."),
           inline(502, 71, "2026-11-21T09:00:00Z", "This needs `[Fact p.Prime]`.")]
REPLY = comment(801, "2026-11-21T10:00:00Z", "Reply to the review: 3.4 assumes p odd in the line above it, so p = 2 is "
                                             "excluded; and the instance is in the section variables.")


# --- the text test -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", FOUND_NONE)
def test_a_review_that_found_no_errors_and_asks_a_question_does_not_state_one(text):
    assert not replies.states_errors(text, CUES)


@pytest.mark.parametrize("text", [
    LISTING, "The sign in (3) is reversed.", "Please fix the locator of Theorem 2.", "This is incorrect for p = 2.",
    "I found no errors in 3.1, but 3.2 does not hold for p = 2.", "There is a typo in the title.",
    "No errors in the layers; the statement of 4.2 is wrongly quantified.", "Lemma 2 is FALSE as stated.",
    "The construction fails when the base is empty.", "Please replace `Finset` by `Set` in 2.1."])
def test_words_that_state_an_error_or_ask_for_a_correction_do(text):
    assert replies.states_errors(text, CUES)


@pytest.mark.parametrize("text", [
    "No errors found.", "I did not find any errors.", "I didn't find any mathematical errors in the layers.",
    "Checked line by line, without error.", "No issues.", "Errors: none.", "I could not find a counterexample.",
    "I did not spot anything wrong.", "I didn't find anything obviously incorrect in the layers.",
    "Nothing wrong here; the bound is the error term of the prime number theorem.", "The layers are error-free.",
    "> 3.4 is false for p = 2\n\nThe quoted claim is answered above.",
    "See `false_of_ne` and `Nat.Prime.two_le`.",
    "```lean\nexample : False := by\n  exact absurd rfl (by decide : (1 : ℕ) ≠ 2) -- error\n```\nLooks right.",
    "<!-- error: 3 -->Looks right."])
def test_negated_forms_code_quotes_and_records_state_none(text):
    assert not replies.states_errors(text, CUES)


def test_the_cue_list_is_the_configured_one():
    assert replies.states_errors("This step is bogus.", ["bogus"]) and not replies.states_errors("This is wrong.", [])
    cfg = config.parse('[project]\nname = "P"\n[replies]\nerror_cues = ["bogus", "does not follow"]\n', "x.toml")
    assert cfg.replies.error_cues == ["bogus", "does not follow"]
    assert replies.states_errors("Step 3 does  not\nfollow from step 2.", cfg.replies.error_cues)
    with pytest.raises(config.ConfigError, match="replies.error_cues must list words or phrases"):
        config.parse('[project]\nname = "P"\n[replies]\nerror_cues = ["(wrong|false)"]\n', "x.toml")


# --- the predicate -------------------------------------------------------------------------------------

def events(reviews=(), comments=(), threads=()):
    from tauceti_roadmap_worker.lanes.pr import events_from_api
    return events_from_api(list(reviews), list(comments), list(threads))


def pending(evts, ours=(AUTHOR,), **kw):
    return [(e.ref, why) for e, why in replies.unanswered_errors(evts, set(ours), MARKERS, HOUSE, CUES, owner=AUTHOR,
                                                                 **kw)]


def test_changes_requested_with_inline_threads_points_out_errors_until_a_reply_answers_it():
    assert pending(events([CHANGES], threads=THREADS)) == [
        ("review-71", "changes requested"), ("inline-501", "an inline comment its thread has not answered"),
        ("inline-502", "an inline comment its thread has not answered")]
    # (a) alone: the state, whatever the words and with no inline comment.
    assert pending(events([review(72, "2026-11-21T09:00:00Z", "See above.", state="CHANGES_REQUESTED")])) == [
        ("review-72", "changes requested")]
    # A reply in reply form answers it, threads and all.
    assert pending(events([CHANGES], [REPLY], THREADS)) == []


def test_an_inline_comment_counts_until_its_thread_answers_it():
    asked = review(73, "2026-11-21T09:00:00Z")                       # a review with no text of its own
    question = inline(503, 73, "2026-11-21T09:00:00Z", "Why `Nat.Prime` rather than `Prime`?")
    assert pending(events([asked], threads=[question])) == [
        ("review-73", "an inline comment its thread has not answered"),
        ("inline-503", "an inline comment its thread has not answered")]
    # The author answers in the thread. On a pull request a gate round has answered before (its reply carries
    # the answered record), the review GitHub wraps that answer in acknowledges nothing: the review stays
    # unanswered (awaiting the author), but its thread no longer points out errors.
    earlier = comment(808, "2026-11-20T09:00:00Z", "Fixed in `1111111` (reply to the review): done.\n"
                      + replies.answered_marker("GATE-Foo~1", {}))
    answer = inline(504, 74, "2026-11-21T09:30:00Z", "The roadmap follows the source here.", user=AUTHOR, reply_to=503)
    mine = review(74, "2026-11-21T09:30:00Z", user=AUTHOR)
    both = events([asked, mine], [earlier], threads=[question, answer])
    assert pending(both) == [] and [e.ref for e in replies.unanswered(both, {AUTHOR}, MARKERS, HOUSE)] == [
        "review-73", "inline-503"]
    [asked_inline] = [e for e in both if e.ref == "inline-503"]
    assert replies.answered_in_thread(asked_inline, both, {AUTHOR}, AUTHOR)
    # A reply in the thread by someone else does not answer it.
    other = inline(505, 75, "2026-11-21T09:40:00Z", "Agreed.", user=OTHER, reply_to=503)
    assert ("inline-503", "an inline comment its thread has not answered") in pending(
        events([asked, review(75, "2026-11-21T09:40:00Z", user=OTHER)], [earlier], threads=[question, other]))


def test_the_filters_of_unanswered_hold_for_errors_too():
    blocking = review(76, "2026-11-21T09:00:00Z", "3.4 is false.", user=BOT, state="CHANGES_REQUESTED")
    assert pending(events([blocking], threads=[inline(506, 76, "2026-11-21T09:00:00Z", "wrong", user=BOT)]),
                   ignore={BOT.upper()}) == []                                       # the ignore list, any case
    approval = review(77, "2026-11-21T09:00:00Z", "Looks right.", state="APPROVED")
    assert pending(events([approval], threads=[inline(507, 77, "2026-11-21T09:00:00Z", "typo: Noetherian")])) == []
    assert pending(events(comments=[comment(802, "2026-11-21T09:00:00Z", "Fixed the error in 3.2.")])) == []
    note = comment(803, "2026-11-21T09:00:00Z", "Housekeeping at abc1234: fixed a typo in the title.", user="bob")
    assert pending(events(comments=[note]), ours=(AUTHOR, "bob")) == []                # an answerer's note
    assert pending(events(comments=[comment(804, "2026-11-21T09:00:00Z", LISTING, user=OTHER)])) == [
        ("comment-804", "its words state an error")]
    for text in FOUND_NONE:
        assert pending(events([review(78, "2026-11-21T09:00:00Z", text)])) == []


# --- board sync: both steps, twice in each state ----------------------------------------------------------

class LiveGH(ContentGH):
    """ContentGH whose pull request #905 lives: its labels follow the writes, its head moves, and its
    reviews, issue comments and inline comments are lists the test adds to."""

    def __init__(self, labels, **kw):
        super().__init__([node(PR, H1, author=AUTHOR, labels=labels)], **kw)
        self.reviews, self.inline, self.comments = {PR: []}, {PR: []}, {PR: []}

    @property
    def labels(self):
        return [x["name"] for x in self.nodes[0]["labels"]["nodes"]]

    def move(self, head):
        self.nodes[0]["headRefOid"] = head

    def write(self, method, path, payload=None):
        got = super().write(method, path, payload)
        if method == "POST" and path.endswith(f"/issues/{PR}/labels"):
            self.nodes[0]["labels"]["nodes"] += [{"name": x} for x in payload["labels"] if x not in self.labels]
        elif method == "DELETE" and f"/issues/{PR}/labels/" in path:
            gone = unquote(path.rsplit("/", 1)[1])
            assert gone in self.labels, f"{gone} removed twice"
            self.nodes[0]["labels"]["nodes"] = [x for x in self.nodes[0]["labels"]["nodes"] if x["name"] != gone]
        return got


@pytest.fixture
def live(tmp_path, monkeypatch):
    """`live(**prreview)`: the accepted pull request #905 (its review of H1 accepts Foo) with the
    acceptance and approval labels, and a `sync()` that runs `board sync`'s two steps and files what
    they create on the board."""
    def make(**over):
        done = review_job(40, 1, PR, H1, "done", owner=AUTHOR)
        gh = LiveGH([LABEL, "awaiting-review", APPROVAL], files=verdict_file(done, "accepted", H1))
        cfg = example_config()
        cfg.prreview.enabled, cfg.prreview.approval_label, cfg.prreview.text_per_run = True, APPROVAL, 0
        cfg.replies.ignore_logins = [*cfg.replies.ignore_logins, BOT]
        for key, value in over.items():
            setattr(cfg.prreview, key, value)
        board = FakeBoard(jobs=[done])
        monkeypatch.setattr(intake, "open_roadmap_prs", lambda ctx: [
            {"pr": PR, "name": "Foo", "head": gh.nodes[0]["headRefOid"], "branch": "roadmap/foo", "owner": AUTHOR}])
        ctx = SimpleNamespace(cfg=cfg, gh=gh, board=board, operator="board-bot", dry_run=False, state=tmp_path)

        def sync():
            """Both steps twice: (the first run's log, its label writes); the second writes no label."""
            logs, writes = [], []
            for _ in range(2):
                before = len(gh.writes)
                logs.append([line for line in intake.sync_pull_requests(ctx) + intake.sync_pr_reviews(ctx)
                             if not line.startswith("pull requests read")])
                writes.append([(m, unquote(p.rsplit("/labels", 1)[1]) or str(b.get("labels")))
                               for m, p, b in gh.writes[before:] if "/labels" in p])
                for job, state, _pre in board.created:      # the board lists what was filed
                    job.state, job.number = state, 600 + len(board.jobs)
                    board.jobs.append(job)
                board.created.clear()
            assert writes[1] == [], writes[1]
            return logs[0], writes[0]
        sync.ctx = ctx
        return gh, board, sync
    return make


def test_the_label_is_steady_on_an_accepted_head_with_no_review_to_answer(live):
    gh, _board, sync = live()
    assert sync() == (["#905 Foo: awaiting-review"], [])
    assert gh.labels == [LABEL, "awaiting-review", APPROVAL]


def gate_done(board):
    for job in board.jobs:
        if job.kind == "gate":
            job.state = "done"


def test_changes_requested_takes_the_label_off_and_a_reply_puts_it_back(live):
    gh, board, sync = live()
    gh.reviews[PR].append(CHANGES)
    gh.inline[PR] += THREADS
    out, writes = sync()
    held = "a review that points out errors waits for an answer: review-71 by reviewer1, changes requested, and 2 more"
    assert writes == [("POST", "['awaiting-author']"), ("DELETE", "/awaiting-review"), ("DELETE", f"/{APPROVAL}"),
                      ("DELETE", f"/{LABEL}")]
    assert f"#905: {LABEL} removed ({held})" in out and "#905: GATE-Foo~1 created for @alice" in out
    assert gh.labels == ["awaiting-author"]
    # Answered in reply form, nothing pushed: the head is still the accepted one, and the label comes back
    # with no new review (the approval label is `roadmaps dependency-labels`' to put back).
    gh.comments[PR].append(REPLY)
    gate_done(board)
    out, writes = sync()
    assert writes == [("POST", "['awaiting-review']"), ("DELETE", "/awaiting-author"), ("POST", f"['{LABEL}']")]
    assert f"#905: {LABEL} added (the AI review of {H1[:7]} accepts it)" in out
    assert [j.id for j in board.jobs] == ["PRREVIEW-Foo~1", "GATE-Foo~1"]          # no review filed


def test_after_a_push_and_a_reply_the_label_waits_for_the_new_heads_acceptance(live):
    gh, board, sync = live()
    gh.reviews[PR].append(CHANGES)
    gh.inline[PR] += THREADS
    sync()
    gh.move(H2)
    gh.comments[PR].append(comment(805, "2026-11-21T11:00:00Z", "Fixed in `2222222` (reply to the review): 3.4 now "
                                                                "assumes p odd."))
    gate_done(board)
    out, writes = sync()
    assert writes == [("POST", "['awaiting-review']"), ("DELETE", "/awaiting-author")] and LABEL not in gh.labels
    assert "#905: PRREVIEW-Foo~2 created" in out
    # The review of the new head accepts it: the label comes back.
    second = board.jobs[-1]
    second.state = "done"
    gh.files.update(verdict_file(second, "accepted", H2))
    out, writes = sync()
    assert writes == [("POST", f"['{LABEL}']")] and f"#905: {LABEL} added (the AI review of {H2[:7]} accepts it)" in out


def test_after_a_push_and_a_reply_a_carried_acceptance_brings_the_label_back(live):
    gh, board, sync = live(carry_accept=True)
    gh.reviews[PR].append(CHANGES)
    gh.inline[PR] += THREADS
    sync()
    assert LABEL not in gh.labels
    gh.move(H2)
    gh.compares[(H1, H2)] = [{"filename": "TauCetiRoadmap/Foo/Suggested.lean", "additions": 4, "deletions": 3}]
    gh.comments[PR].append(comment(806, "2026-11-21T11:00:00Z", "Fixed in `2222222` (reply to the review): 3.4 "
                                                                "assumes p odd.\n\n" + replies.carried_marker(H1, H2)))
    gate_done(board)
    out, writes = sync()
    assert writes == [("POST", "['awaiting-review']"), ("DELETE", "/awaiting-author"), ("POST", f"['{LABEL}']")]
    assert (f"#905: {LABEL} added (the acceptance of {H1[:7]} carries to {H2[:7]}: 7 lines of repairs inside the "
            "roadmap folders)") in out
    assert not any(j.kind == "prreview" and j.inputs.get("head") == H2 for j in board.jobs)   # no review filed


def test_a_review_that_found_no_errors_and_asks_a_question_leaves_the_label(live):
    for n, text in enumerate(FOUND_NONE):
        gh, _board, sync = live()
        gh.reviews[PR].append(review(80 + n, "2026-11-21T09:00:00Z", text))
        out, writes = sync()
        assert writes == [("POST", "['awaiting-author']"), ("DELETE", "/awaiting-review"), ("DELETE", f"/{APPROVAL}")]
        assert gh.labels == [LABEL, "awaiting-author"] and not any("removed" in line for line in out)


def test_another_contributors_comment_listing_false_statements_takes_the_label_off(live):
    gh, _board, sync = live()
    gh.comments[PR].append(comment(807, "2026-11-21T09:00:00Z", LISTING, user=OTHER))
    out, writes = sync()
    assert ("DELETE", f"/{LABEL}") in writes and gh.labels == ["awaiting-author"]
    assert (f"#905: {LABEL} removed (a review that points out errors waits for an answer: comment-807 by "
            "contributor2, its words state an error)") in out


def test_an_ignored_login_or_an_approval_changes_no_label(live):
    gh, board, sync = live()
    gh.reviews[PR].append(review(90, "2026-11-21T09:00:00Z", "3.4 is false.", user=BOT, state="CHANGES_REQUESTED"))
    gh.inline[PR].append(inline(508, 90, "2026-11-21T09:00:00Z", "Counterexample: p = 2.", user=BOT))
    assert sync() == (["#905 Foo: awaiting-review"], []) and board.jobs[1:] == []
    gh, board, sync = live()
    gh.reviews[PR].append(review(91, "2026-11-21T09:00:00Z", "Looks right.", user=OTHER, state="APPROVED"))
    assert sync() == (["#905 Foo: awaiting-review"], []) and gh.labels == [LABEL, "awaiting-review", APPROVAL]


def test_a_review_being_finished_does_not_keep_the_label_on(live):
    """The label rule leaves a head whose review is under way to that review's lane; the hold does not
    wait for it (the second step alone, as when the first could not run)."""
    gh, board, sync = live()
    board.jobs.append(review_job(41, 2, PR, H1, "claimed", owner=AUTHOR))
    gh.reviews[PR].append(CHANGES)
    out = intake.sync_pr_reviews(sync.ctx)
    assert gh.writes == [("DELETE", f"repos/{REPO}/issues/905/labels/{LABEL}", {}),
                         ("DELETE", f"repos/{REPO}/issues/905/labels/{APPROVAL}", {})]
    assert out == [(f"#905: {LABEL} removed (a review that points out errors waits for an answer: review-71 by "
                    "reviewer1, changes requested)"), f"#905: {APPROVAL} removed ({LABEL} is off)"]
    assert intake.sync_pr_reviews(sync.ctx) == [] and len(gh.writes) == 2


# --- the review lane -------------------------------------------------------------------------------------

def test_the_lane_does_not_label_an_accepted_head_while_a_review_points_out_errors(review_env):  # noqa: F811
    """The review lane labels an accepted head itself; it reads the same hold, so the next sync does not
    take off a label it has just put on."""
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": PR, "head": H1, "owner": "carol", "name": "Foo"})
    held = "a review that points out errors waits for an answer: review-71 by reviewer1, changes requested"
    for labels, ends, after in (([], f"not labelled {LABEL}: {held}", []),
                                ([LABEL], f"{LABEL} removed ({held})", [])):
        gh = LabelGH(labels=labels)
        gh.reviews = {PR: [CHANGES]}
        ctx = lane_ctx(review_env, gh=gh)
        lane, ws = finished_ws(review_env, ctx, job, "accepted")
        assert lane.finish(job, ctx, ws).endswith(ends) and gh.labels == after
    # Answered: the acceptance labels.
    gh = LabelGH()
    gh.reviews = {PR: [CHANGES]}
    gh.comments.append(dict(REPLY, user={"login": "carol"}))
    ctx = lane_ctx(review_env, gh=gh)
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    assert lane.finish(job, ctx, ws).endswith(f"labelled {LABEL}") and gh.labels == [LABEL]


def test_the_hold_names_the_review_and_its_signal():
    cfg = example_config()
    held = intake.errors_hold(cfg, AUTHOR, events([CHANGES]))
    assert held == "a review that points out errors waits for an answer: review-71 by reviewer1, changes requested"
    assert intake.errors_hold(cfg, AUTHOR, events([CHANGES], [REPLY])) == ""
    assert re.fullmatch(r"[^@]*", held)                              # a login, never a mention
