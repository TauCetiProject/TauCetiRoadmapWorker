"""A transition the intake starts converges to one result, whichever of its GitHub writes fails: a
merged submission's job becomes done only after its successors exist, and `reconcile_merged`
finishes one left behind; a board pull request is labelled for review only after its review jobs
exist; and a package's last red-team fix becomes done only after its port exists, and
`unfinished_last_fixes` finishes one left behind. Nothing is created or recorded twice."""

import pytest

from tauceti_roadmap_worker import boardpr, intake
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.gh import GHError

from .fakes import bot_context, example_config
from .test_boardpr import Board
from .test_boardpr_guards import flow
from .test_intake_admission import SubmissionGH, target_files


class Unavailable(GHError):
    pass


class ClosedGH(SubmissionGH):
    """The submission of TARGET-Foo, listed among the closed pull requests once merged."""

    def api(self, path, paginate=False):
        if "/pulls?state=closed" in path:
            return [{**self.pr, "state": "closed", "merged_at": "2026-10-24T12:00:00Z"}] if self.merged else []
        return super().api(path, paginate)


def target_run(fail_on=None):
    """TARGET-Foo submitted and taken in, with `fail_on(board)` breaking one board write."""
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    gh = ClosedGH("TARGET-Foo", target_files(), association="COLLABORATOR")
    job = new_job("TARGET-Foo")
    job.number, job.state = 7, "submitted"
    b = Board([job])
    ctx = bot_context(cfg=cfg, gh=gh, board=b, operator="board-bot")
    restore = fail_on(b) if fail_on else None
    try:
        line = intake.intake(ctx, 12, yes=True)
    except Unavailable as exc:
        line = f"failed: {exc}"
    if restore:
        restore()
    return ctx, gh, b, job, line


def failing_create(b):
    create = b.create

    def refuse(job, state="available", preamble=""):
        raise Unavailable("HTTP 503 creating " + job.id)
    b.create = refuse
    return lambda: setattr(b, "create", create)


def failing_done(b):
    set_state = b.set_state

    def refuse(job, state, comment=""):
        if state == "done":
            raise Unavailable("HTTP 503 setting done")
        return set_state(job, state, comment)
    b.set_state = refuse
    return lambda: setattr(b, "set_state", set_state)


@pytest.mark.parametrize("fail_on", [failing_create, failing_done])
def test_a_merged_target_whose_transition_stopped_gets_exactly_one_design(fail_on):
    ctx, gh, b, job, line = target_run(fail_on)
    assert line.startswith("failed: HTTP 503") and gh.merged and job.state == "submitted"
    assert intake.reconcile_merged(ctx, yes=False) == ["#12: merged, but TARGET-Foo is submitted; would finish it (dry run)"]
    out = intake.reconcile_merged(ctx, yes=True)
    assert out == ["#12: merged; finished TARGET-Foo; created DESIGN-Foo"] or out[0].startswith(
        "#12: merged; finished TARGET-Foo; created nothing"), out
    assert [j.id for j in b.jobs].count("DESIGN-Foo") == 1 and job.state == "done"
    assert [body for _n, body in b.posted if body.startswith("Intake: merged #12")] == [
        "Intake: merged #12 from @bob (codex, m)."]                        # the authorship record, once
    assert intake.reconcile_merged(ctx, yes=True) == []                    # finished: stable


def test_a_lost_authorship_record_is_written_once_by_the_reconcile():
    def failing_note(b):
        comment = b.comment

        def refuse(number, body, login="board-bot"):
            if body.startswith("Intake: merged"):
                raise Unavailable("HTTP 503 commenting")
            return comment(number, body, login)
        b.comment = refuse
        return lambda: setattr(b, "comment", comment)
    ctx, gh, b, job, line = target_run(failing_note)
    assert line.startswith("failed") and gh.merged and not b.posted and job.state == "submitted"
    assert intake.reconcile_merged(ctx, yes=True) == ["#12: merged; finished TARGET-Foo; created DESIGN-Foo"]
    assert [body for _n, body in b.posted if body.startswith("Intake: merged")] == ["Intake: merged #12 from @bob (codex, m)."]


def test_a_merged_submission_whose_job_a_human_reopened_is_left_alone():
    ctx, gh, b, job, _line = target_run(failing_create)
    job.state = "available"
    assert intake.reconcile_merged(ctx, yes=True) == [] and b.find("DESIGN-Foo") is None


def test_the_job_is_submitted_before_its_pull_request_is_merged():
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    gh = ClosedGH("TARGET-Foo", target_files(), association="COLLABORATOR")
    job = new_job("TARGET-Foo")
    job.number, job.state = 7, "claimed"
    b = Board([job])
    states = []
    set_state = b.set_state
    b.set_state = lambda j, s, comment="": states.append((s, gh.merged)) or set_state(j, s, comment)
    intake.intake(bot_context(cfg=cfg, gh=gh, board=b, operator="board-bot"), 12, yes=True)
    assert states == [("submitted", False), ("done", True)]


def test_a_board_pull_request_is_labelled_only_once_its_review_exists():
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    pkg = new_job("PKG-Foo")
    pkg.number, pkg.state = 3, "submitted"
    gh = SubmissionGH("PKG-Foo", {"packages/Foo/README.md": "# Foo\n", "packages/Foo/Suggested.lean": "import Mathlib\n",
                                  "packages/Foo/metadata.toml": "", "packages/Foo/reports/PKG-Foo.md": "# Foo\n"},
                      association="COLLABORATOR")
    b = Board([pkg])
    ctx = bot_context(cfg=cfg, gh=gh, board=b, operator="board-bot")
    write = gh.write

    def no_labels(method, path, payload=None):
        if path.endswith("/issues/12/labels"):
            raise Unavailable("HTTP 503 labelling")
        return write(method, path, payload)
    gh.write = no_labels
    with pytest.raises(Unavailable):
        intake.intake(ctx, 12, yes=True)
    assert b.find("REV-PKG-Foo") and not gh.pr["labels"]                 # the review exists, the label not yet
    gh.write = write
    assert intake.intake(ctx, 12, yes=True) == "#12: open for review; created nothing"
    assert [j.id for j in b.jobs].count("REV-PKG-Foo") == 1 and {"name": "needs-review"} in gh.pr["labels"]
    assert [body for _n, body in b.posted if body.startswith("Intake: authored")] == [
        "Intake: authored by @bob (codex, m) in #12."]


def test_a_last_fix_merged_without_its_port_is_finished_by_the_next_scan():
    c, gh, b, pkg, fix = flow()
    gh.comments.append({"id": 950, "body": f"Fix `{fix.id}` pushed at dddddd\n\n<!-- tauceti-roadmap-review "
                                           '{"agent": "codex", "job": "FIX-RT-Foo~1", "model": "m"} -->',
                        "user": {"login": "alice"}})
    create = b.create

    def refuse(job, state="available", preamble=""):
        raise Unavailable("HTTP 503 creating " + job.id)
    b.create = refuse
    assert boardpr.scan(c, yes=True)[0].startswith("#12: skipped: GitHub did not answer (HTTP 503 creating PORT-Foo)")
    assert gh.merged and fix.state == "submitted" and pkg.state == "done" and b.find("PORT-Foo") is None
    b.create = create
    gh.pr["merged"] = True
    out = boardpr.scan(c, yes=True)
    assert out[0].startswith("#12: FIX-RT-Foo~1 done; merged; created PORT-Foo"), out
    assert fix.state == "done" and [j.id for j in b.jobs].count("PORT-Foo") == 1
    assert [body for n, body in b.posted if n == fix.number and body.startswith("Intake: authored")] == [
        "Intake: authored by @alice (codex, m) in #12."]
    assert boardpr.scan(c, yes=True) == []


def test_a_last_fix_squashed_onto_main_without_its_port_is_finished_too():
    c, gh, b, _pkg, fix = flow()
    gh.merged = True                                                       # closed, not merged, by squash_with_main
    gh.say("board-bot", "Intake: merged as `abc1234` on main: the pull request conflicted with main. Closing it.")
    assert boardpr.scan(c, yes=False) == ["#12: merged, but FIX-RT-Foo~1 is submitted; would finish it (dry run)"]
    out = boardpr.scan(c, yes=True)
    assert out[0].startswith("#12: FIX-RT-Foo~1 done; merged; created PORT-Foo") and fix.state == "done", out
