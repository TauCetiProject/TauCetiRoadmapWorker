"""A review answered without moving the head (a dispute) gets exactly one more round: the intake files a
full review by another worker when the latest verdict of the head does not accept and a reply's answered
record names that head; a head whose repair moved it, an accepted head, an open round or a second dispute
round do not qualify."""

from tauceti_roadmap_worker import intake, replies
from tauceti_roadmap_worker.board import Job


def _job(state="done", head="aaaaaaa", **inputs):
    return Job(id="PRREVIEW-Foo~2", kind="prreview", name="Foo", state=state, inputs={"head": head, "pr": 7, **inputs})


def _reply(head, worker="w1"):
    return {"body": "Fixed in `" + head + "` (reply to the review, no change needed):\n\n- **H01** (answered) — …\n\n"
            + replies.answered_marker("PRREVIEW-Foo~2", {"c1": "v"}, worker=worker, head=head)}


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
