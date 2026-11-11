"""`board resume --names A,B`: a roadmap whose jobs are all done and whose last job released nothing
goes on from that job (regression: designs and packages whose board pull requests were merged on
GitHub were finished with no review, and reviews that asked for changes had no repair round, so
their roadmaps stood still with no open job). A red-team pass closed as superseded when its board
pull request was merged is reopened when its round resumes after the port."""

import pytest

from tauceti_roadmap_worker import intake
from tauceti_roadmap_worker.board import SUPERSEDED_LABEL, Outcome, new_job
from tauceti_roadmap_worker.gh import GHError

from .fakes import bot_context, example_config
from .test_max_rounds_park import Board


def report(verdict):
    return f"Review (claude) of abc1234\n\nVerdict: {verdict}\n\n1. **F01** (P2) A hypothesis is missing\n"


class GH:
    """The board's main (review reports) and its pull requests: `merged` lists the merged ones."""

    def __init__(self, reports=None, merged=()):
        self.files = {f"packages/{job.split('-')[-1].split('~')[0]}/reports/{job}.md": report(verdict)
                      for job, verdict in (reports or {}).items()}
        self.merged, self.down = set(merged), False

    def raw_file(self, repo, path, ref=""):
        return self.files.get(path, "")

    def api(self, path, paginate=False):
        if self.down:
            raise GHError("GET: 502")
        assert "/pulls/" in path, path
        return {"merged": int(path.rsplit("/", 1)[1]) in self.merged}

    def write(self, *a, **k):
        return {}


def done(job_id, number, **inputs):
    job = new_job(job_id, inputs=inputs)
    job.number, job.state = number, "done"
    return job


def ctx_for(jobs, gh):
    return bot_context(cfg=example_config(), board=Board(jobs=list(jobs)), gh=gh)


def test_two_roadmaps_merged_by_hand_go_on_on_the_boards_main():
    jobs = [done("DESIGN-Foo", 1, board_pr=12, operator="alice"),
            done("REV-DESIGN-Foo", 2, board_pr=12, operator="alice"),
            done("FIX-DESIGN-Foo~2", 3, board_pr=12, operator="alice"),
            done("REV-DESIGN-Foo~2", 4, board_pr=12, operator="alice", after_comment=7),
            done("DESIGN-Bar", 5), done("REV-DESIGN-Bar", 6),
            done("PKG-Bar", 7, board_pr=13, operator="bob"),
            done("ERRATA-Bar", 8)]
    gh = GH({"REV-DESIGN-Foo~2": "needs_changes", "REV-DESIGN-Bar": "accepted"}, merged={12, 13})
    ctx = ctx_for(jobs, gh)
    assert intake.resume_unfollowed(ctx, ("Foo", "Bar")) == [
        "Foo: REV-DESIGN-Foo~2 would release FIX-DESIGN-Foo~3 on the board's main (dry run)",
        "Bar: PKG-Bar would release REV-PKG-Bar on the board's main (dry run)"]
    assert ctx.board.created == []
    out = intake.resume_unfollowed(ctx, ("Foo", "Bar"), yes=True)
    assert out == ["Foo: REV-DESIGN-Foo~2: created FIX-DESIGN-Foo~3 on the board's main",
                   "Bar: PKG-Bar: created REV-PKG-Bar on the board's main"]
    [(fix, fix_state, _p), (rev, rev_state, _q)] = ctx.board.created
    assert fix.inputs == {"review": "REV-DESIGN-Foo~2", "operator": "alice"} and fix.requires_operator == "alice"
    assert rev.inputs == {"operator": "bob"} and (fix_state, rev_state) == ("available", "available")
    assert jobs[3].inputs["board_pr"] == 12                       # the finished review's own record is left as it is
    # What was created is under way now: a second run resumes nothing.
    assert intake.resume_unfollowed(ctx, ("Foo", "Bar"), yes=True) == [
        "Foo: not resumed: FIX-DESIGN-Foo~3 is available", "Bar: not resumed: REV-PKG-Bar is available"]


def test_on_an_open_pull_request_what_follows_stays_on_it():
    jobs = [done("DESIGN-Foo", 1, board_pr=12, operator="alice"), done("REV-DESIGN-Foo", 2, board_pr=12, operator="alice")]
    ctx = ctx_for(jobs, GH({"REV-DESIGN-Foo": "needs_changes"}))
    assert intake.resume_unfollowed(ctx, ("Foo",), yes=True) == ["Foo: REV-DESIGN-Foo: created FIX-DESIGN-Foo~2"]
    assert ctx.board.created[0][0].inputs == {"review": "REV-DESIGN-Foo", "board_pr": 12, "operator": "alice"}


def test_an_accepted_design_is_followed_by_its_package(monkeypatch):
    monkeypatch.setattr(intake, "add_to_order", lambda ctx, name, *a: f"{name} placed")
    ctx = ctx_for([done("DESIGN-Foo", 1), done("REV-DESIGN-Foo~5", 2, board_pr=12)], GH({"REV-DESIGN-Foo~5": "accepted"}, {12}))
    assert intake.resume_unfollowed(ctx, ("Foo",), yes=True) == ["Foo: REV-DESIGN-Foo~5: created PKG-Foo (Foo placed)"]


@pytest.fixture
def ports(monkeypatch):
    monkeypatch.setattr(intake, "order_ports_after_suppliers", lambda ctx, nxt, jobs: ["after PORT-Supplier"])
    monkeypatch.setattr(intake, "add_to_order_for_port", lambda ctx, name: f"{name} placed")


def test_an_accepted_package_merged_by_hand_is_ported(ports):
    jobs = [done("PKG-Foo", 1, board_pr=12, operator="alice"), done("REV-PKG-Foo~4", 2, board_pr=12, operator="alice")]
    ctx = ctx_for(jobs, GH({"REV-PKG-Foo~4": "accepted"}, {12}))
    assert intake.resume_unfollowed(ctx, ("Foo",)) == ["Foo: REV-PKG-Foo~4 would release PORT-Foo (dry run)"]
    assert intake.resume_unfollowed(ctx, ("Foo",), yes=True) == [
        "Foo: REV-PKG-Foo~4: created PORT-Foo (#601); after PORT-Supplier; Foo placed"]
    port = ctx.board.created[0][0]
    assert port.after == ["REV-PKG-Foo~4"] and "redteam_round" not in port.inputs      # its red team: round 1 on the roadmap PR
    # On an open pull request the board red team follows instead.
    ctx = ctx_for(jobs, GH({"REV-PKG-Foo~4": "accepted"}))
    assert intake.resume_unfollowed(ctx, ("Foo",)) == ["Foo: REV-PKG-Foo~4 would release RT-Foo~1a, RT-Foo~1b (dry run)"]


def test_a_package_merged_between_red_team_rounds_is_ported_with_the_next_round(ports):
    jobs = [done("PKG-Foo", 1, board_pr=12), done("REV-PKG-Foo", 2, board_pr=12),
            done("RT-Foo~1a", 3, board_pr=12, operator="alice"), done("RT-Foo~1b", 4, board_pr=12, operator="alice")]
    ctx = ctx_for(jobs, GH({"REV-PKG-Foo": "accepted"}, {12}))
    assert intake.resume_unfollowed(ctx, ("Foo",), yes=True) == [
        "Foo: RT-Foo~1a: created PORT-Foo (#601); after PORT-Supplier; Foo placed"]
    port = ctx.board.created[0][0]
    assert port.after == ["RT-Foo~1a", "RT-Foo~1b"]
    assert port.inputs == {"redteam_round": 2, "previous": ["RT-Foo~1a", "RT-Foo~1b"], "repair": ""}
    # After the round's repair, the next round verifies it.
    fix = done("FIX-RT-Foo~1", 5, board_pr=12, operator="alice", reviews=["RT-Foo~1a", "RT-Foo~1b"])
    ctx = ctx_for(jobs + [fix], GH({}, {12}))
    intake.resume_unfollowed(ctx, ("Foo",), yes=True)
    assert ctx.board.created[0][0].inputs == {"redteam_round": 2, "previous": ["RT-Foo~1a", "RT-Foo~1b"],
                                              "repair": "FIX-RT-Foo~1"}
    # The last round, unrepaired, is a human's.
    last = [done("RT-Foo~2a", 6, board_pr=12), done("RT-Foo~2b", 7, board_pr=12)]
    ctx = ctx_for(jobs + [fix] + last, GH({}, {12}))
    [line] = intake.resume_unfollowed(ctx, ("Foo",), yes=True)
    assert "the last red-team round of Foo has no repair" in line and ctx.board.created == []


def test_what_is_not_resumed():
    held = done("FIX-DESIGN-Held~2", 3)
    held.state = "blocked"
    jobs = [done("REV-DESIGN-Held", 2), held,
            done("PKG-Ported", 4), done("PORT-Ported", 5),
            done("PKG-Dup", 6), done("REV-PKG-Dup", 7),
            done("DESIGN-Mute", 8), done("REV-DESIGN-Mute", 9),
            done("DESIGN-Fine", 10), done("REV-DESIGN-Fine", 11), done("FIX-DESIGN-Fine~2", 12)]
    gh = GH({"REV-PKG-Dup": "rejected"})
    ctx = ctx_for(jobs, gh)
    assert intake.resume_unfollowed(ctx, ("Held", "Ported", "Dup", "Mute", "Absent"), yes=True) == [
        "Held: not resumed: FIX-DESIGN-Held~2 is blocked",
        "Ported: not resumed: it has been ported",
        "Dup: nothing follows REV-PKG-Dup (rejected); a human decides how to continue",
        "Mute: not resumed: the report of REV-DESIGN-Mute on the board's main names no verdict",
        "Absent: no job on the board"]
    assert ctx.board.created == [] and ctx.board.posted == []
    # GitHub's trouble with one roadmap leaves the others their turn.
    ctx = ctx_for(jobs + [done("PKG-Down", 13, board_pr=14)], gh)
    gh.down = True
    out = intake.resume_unfollowed(ctx, ("Down", "Fine"), yes=True)
    assert out[0].startswith("Down: skipped: GitHub did not answer") and out[1] == "Fine: FIX-DESIGN-Fine~2: created " \
        "REV-DESIGN-Fine~2 (no plan.json to read gaps from)"


def test_a_pass_superseded_by_a_merge_is_reopened_when_its_round_resumes_after_the_port():
    port = done("PORT-Foo", 1, redteam_round=2, previous=["RT-Foo~1a", "RT-Foo~1b"], repair="FIX-RT-Foo~1")
    a = done("RT-Foo~2a", 2, board_pr=12, operator="alice")
    b = done("RT-Foo~2b", 3, board_pr=12, operator="alice")
    a.labels, b.labels, a.closed, b.closed = [SUPERSEDED_LABEL], [SUPERSEDED_LABEL], True, True
    ran = done("RT-Foo~1a", 4, board_pr=12)                       # a pass that ran is never reopened
    jobs = [port, a, b, ran]
    ctx = ctx_for(jobs, GH())
    line = intake.advance(ctx, port, Outcome(pr=40, operator="alice"), jobs)
    assert line == "created RT-Foo~2a (reopened, #2), RT-Foo~2b (reopened, #3)"
    assert ctx.board.created == [] and ctx.board.reopened == ["RT-Foo~2a", "RT-Foo~2b"]
    for job in (a, b):
        assert job.state == "available" and not job.closed and SUPERSEDED_LABEL not in job.labels
        assert job.inputs["pr"] == 40 and "board_pr" not in job.inputs and job.inputs["repair"] == "FIX-RT-Foo~1"
        assert job.after == ["PORT-Foo"]
    assert intake.advance(ctx, port, Outcome(pr=40, operator="alice"), jobs) == "created nothing"
