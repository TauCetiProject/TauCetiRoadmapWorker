"""A review answered without moving the head (a dispute) gets exactly one more round: the intake files a
full review by another worker when the latest verdict of the head does not accept and a reply's answered
record names that head; a head whose repair moved it, an accepted head, an open round or a second dispute
round do not qualify. The answer to that second round gets a check round only when it rewrote the pull
request's title or description, which a head that did not move hides from the intake."""

from tauceti_roadmap_worker import intake, replies
from tauceti_roadmap_worker.board import Job


def _job(state="done", head="aaaaaaa", **inputs):
    return Job(id="PRREVIEW-Foo~2", kind="prreview", name="Foo", state=state, inputs={"head": head, "pr": 7, **inputs})


def _reply(head, worker="w1", job="PRREVIEW-Foo~2", text=False):
    return {"body": "Fixed in `" + head + "` (reply to the review, no change needed):\n\n- **H01** (answered) — …\n\n"
            + replies.answered_marker(job, {"c1": "v"}, worker=worker, head=head, text=text)}


NEEDS = {"status": "needs_changes", "head": "aaaaaaa"}
ACCEPT = {"status": "accepted", "head": "aaaaaaa"}


def test_a_dispute_at_the_reviewed_head_is_pending():
    assert intake.dispute_pending([_job()], [_reply("aaaaaaa")], "aaaaaaa", NEEDS)
    assert intake.head_worker([_reply("aaaaaaa")], "aaaaaaa") == "w1"


def test_a_repair_that_moved_the_head_an_acceptance_or_an_open_round_is_not():
    assert not intake.dispute_pending([_job()], [_reply("bbbbbbb")], "aaaaaaa", NEEDS)       # the reply moved the head
    assert not intake.dispute_pending([_job()], [_reply("aaaaaaa")], "aaaaaaa", ACCEPT)      # accepted
    assert not intake.dispute_pending([_job()], [_reply("aaaaaaa")], "aaaaaaa", None)        # no verdict read
    assert not intake.dispute_pending([_job(state="claimed")], [_reply("aaaaaaa")], "aaaaaaa", NEEDS)
    assert not intake.dispute_pending([], [_reply("aaaaaaa")], "aaaaaaa", NEEDS)
    assert not intake.dispute_pending([_job()], [], "aaaaaaa", NEEDS)                        # nobody answered


def test_the_record_says_when_the_answer_rewrote_the_text():
    assert replies.answered_record(_reply("aaaaaaa", text=True)["body"])["text"] is True
    assert "text" not in replies.answered_record(_reply("aaaaaaa")["body"])


def test_a_rewritten_description_at_the_reviewed_head_is_pending():
    first = Job(id="PRREVIEW-Foo~1", kind="prreview", name="Foo", state="done", inputs={"head": "aaaaaaa", "pr": 7})
    jobs = [first, _job(dispute_rereview=True)]
    assert intake.text_pending(jobs, [_reply("aaaaaaa", text=True)], "aaaaaaa", NEEDS)
    # Only the latest answer at the head counts, and only as the answer to the latest review of it.
    assert intake.text_pending(jobs, [_reply("aaaaaaa", job="PRREVIEW-Foo~1"), _reply("aaaaaaa", text=True)], "aaaaaaa", NEEDS)
    assert not intake.text_pending(jobs, [_reply("aaaaaaa", text=True), _reply("aaaaaaa")], "aaaaaaa", NEEDS)
    assert not intake.text_pending(jobs, [_reply("aaaaaaa", job="PRREVIEW-Foo~1", text=True)], "aaaaaaa", NEEDS)
    assert not intake.text_pending(jobs, [_reply("aaaaaaa")], "aaaaaaa", NEEDS)              # a plain dispute
    assert not intake.text_pending(jobs, [_reply("bbbbbbb", text=True)], "aaaaaaa", NEEDS)   # the head moved
    assert not intake.text_pending(jobs, [_reply("aaaaaaa", text=True)], "aaaaaaa", ACCEPT)
    assert not intake.text_pending([first, _job(state="claimed")], [_reply("aaaaaaa", text=True)], "aaaaaaa", NEEDS)
