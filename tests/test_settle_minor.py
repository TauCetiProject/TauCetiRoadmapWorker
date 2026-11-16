"""A package review whose every finding is P3 ends with its repair (`[board] settle_minor`): the
repair round is not reviewed again, and the package goes on to its red team as an accepted one."""

import pytest

from tauceti_roadmap_worker import boardpr, intake, prreviews
from tauceti_roadmap_worker.board import SUPERSEDED_LABEL, Outcome, new_job, successors
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes.review import verdict_problems

from .fakes import bot_context, example_config
from .test_boardpr import GH, TABLE, Board

SMALL = [("packages/Foo/README.md", 6, 4), ("packages/Foo/reports/FIX-PKG-Foo~2.md", 80, 0)]


class Repo(GH):
    """The packages repository with a comparison between two heads, and reports read from its files."""

    def __init__(self, head, changed):
        super().__init__(head)
        self.changed, self.compared = changed, []

    def api(self, path, paginate=False):
        if "/compare/" in path:
            self.compared.append(path.rsplit("/compare/", 1)[1])
            if self.changed == "unreadable":
                raise GHError("502 Bad Gateway")
            return {"files": [{"filename": f, "additions": a, "deletions": d} for f, a, d in self.changed]}
        return super().api(path, paginate)

    def raw_file(self, repo, path, ref=""):
        return self.files.get(path, "# Foo\n")


class Jobs(Board):
    """The board with the conditional state write and the close that superseding a job uses."""

    def set_state(self, job, state, comment="", expect=""):
        if expect and job.state != expect:
            return False
        super().set_state(job, state, comment)
        return True

    def close(self, job, comment, label):
        self.comment(job.number, comment)
        job.labels = [*job.labels, label]


def asks(sha, *severities):
    """A review that asks for changes, with one finding of each severity given."""
    lines = [prreviews.review_head("chatgpt", sha), "", "Verdict: needs_changes", ""]
    lines += [f"{k}. **F{k:02d}** ({severity}) finding {k}" for k, severity in enumerate(severities, 1)]
    return "\n".join(lines) + "\n"


def repaired(severities=("P3", "P3"), changed=SMALL, settle=True, rounds=1):
    """A package whose first review asked for changes and whose repair round was pushed."""
    cfg = example_config()
    cfg.review.comment_reviewers = ["roed-math"]
    cfg.board.bot_logins = ["board-bot"]
    cfg.board.settle_minor = settle
    cfg.redteam.rounds = rounds
    pkg = new_job("PKG-Foo")
    pkg.number, pkg.state = 3, "submitted"
    b, gh = Jobs([pkg]), Repo("a" * 40, changed)
    ctx = bot_context(cfg=cfg, gh=gh, board=b, operator="board-bot")
    intake.intake(ctx, 12, yes=True)
    gh.say("roed-math", "reviewing (chatgpt)")
    boardpr.scan(ctx, yes=True)
    gh.say("roed-math", asks("a" * 40, *severities))
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: REV-PKG-Foo needs_changes; created FIX-PKG-Foo~2")
    fix = b.find("FIX-PKG-Foo~2")
    assert fix.inputs["base_head"] == "a" * 40 and fix.inputs["review"] == "REV-PKG-Foo"
    fix.state = "submitted"
    gh.pr["head"]["sha"] = "c" * 40
    return ctx, gh, b, fix


def test_a_review_with_p3_findings_only_ends_with_its_small_repair():
    ctx, gh, b, fix = repaired()
    out = boardpr.scan(ctx, yes=True)
    assert len(out) == 1 and out[0].startswith(
        "#12: FIX-PKG-Foo~2 done; REV-PKG-Foo settled (P3 only, 10 lines); no further review; created RT-Foo~1a")
    assert fix.state == "done" and b.find("REV-PKG-Foo~2") is None
    assert gh.compared == ["a" * 40 + "..." + "c" * 40]                 # the repair's own change, its report aside
    rt = b.find("RT-Foo~1a")
    assert rt.inputs["board_pr"] == 12 and rt.after == ["FIX-PKG-Foo~2"]
    assert rt.independent_of == ["PKG-Foo", "FIX-PKG-Foo~2"] and b.find("RT-Foo~1b") is not None
    record = b.find("REV-PKG-Foo").inputs["accepted_review"]
    assert record["status"] == "accepted" and record["settled"] == {
        "review": "REV-PKG-Foo", "fix": "FIX-PKG-Foo~2", "from": "a" * 40, "head": "c" * 40, "findings": 2, "lines": 10}
    assert verdict_problems({"review": record}, "REV-PKG-Foo", subject="package") == []
    said = [c["body"] for c in gh.comments if c["body"].startswith("Intake: every finding of `REV-PKG-Foo` is P3 (2)")]
    assert len(said) == 1 and "`FIX-PKG-Foo~2` changes 10 lines of the package at `ccccccc`" in said[0]
    assert "Every finding of `REV-PKG-Foo` was P3: no further review round." in b.posted[-1][1]
    assert boardpr.scan(ctx, yes=True) == []                              # nothing new: nothing happens


@pytest.mark.parametrize("over", [
    {"severities": ("P3", "P2")},                                         # a finding that is not P3
    {"changed": [("packages/Foo/README.md", 100, 51)]},                   # more than `small_change_lines`
    {"changed": [("packages/Foo/README.md", 1, 1), ("packages/Bar/README.md", 1, 0)]},   # a file outside the package
    {"changed": "unreadable"},                                            # GitHub does not show the change
    {"settle": False},                                                    # the switch is off
])
def test_another_review_round_follows_every_other_repair(over):
    ctx, _gh, b, fix = repaired(**over)
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: FIX-PKG-Foo~2 done; next round; created REV-PKG-Foo~2") and fix.state == "done"
    assert b.find("RT-Foo~1a") is None and "accepted_review" not in b.find("REV-PKG-Foo").inputs


def test_a_repair_of_exactly_the_limit_settles_the_review():
    ctx, _gh, b, _fix = repaired(changed=[("packages/Foo/README.md", 100, 50)])
    assert "REV-PKG-Foo settled (P3 only, 150 lines)" in boardpr.scan(ctx, yes=True)[0]
    assert b.find("REV-PKG-Foo~2") is None


def test_the_settled_package_is_merged_with_an_accepting_verdict_file():
    ctx, gh, b, _fix = repaired()
    boardpr.scan(ctx, yes=True)
    rt_b = b.find("RT-Foo~1b")
    gh.say("roed-math", prreviews.redteam_head(1, "a", "chatgpt", "c" * 40) + "\n\nVerdict: accepted\n\n" + TABLE)
    b.claimed_by[rt_b.id] = "bob"
    gh.say("bob", prreviews.render(prreviews.redteam_head(1, "b", "claude", "c" * 40), "needs_changes",
                                   "## R01 (P2) — An overclaim\nbody\n",
                                   {"job": rt_b.id, "worker": "bob-1", "agent": "claude", "model": "fable"}))
    assert boardpr.scan(ctx, yes=True)[1].startswith("#12: RT-Foo~1b done; created FIX-RT-Foo~1")
    fix_rt = b.find("FIX-RT-Foo~1")
    fix_rt.state = "submitted"
    gh.pr["head"]["sha"] = "d" * 40
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: FIX-RT-Foo~1 done; merged; created PORT-Foo") and gh.merged
    written = gh.files["packages/Foo/review.json"]
    assert '"status": "accepted"' in written and "independent-review-REV-PKG-Foo" in written
    assert '"fix": "FIX-PKG-Foo~2"' in written and len(gh.compared) == 1   # a red-team fix settles no review


def test_without_a_red_team_the_settled_package_is_merged_and_ported():
    ctx, gh, b, fix = repaired(rounds=0)
    out = boardpr.scan(ctx, yes=True)
    assert out[0].startswith("#12: FIX-PKG-Foo~2 done; merged; created PORT-Foo") and gh.merged and fix.state == "done"
    assert '"status": "accepted"' in gh.files["packages/Foo/review.json"] and b.find("REV-PKG-Foo~2") is None


def test_an_untaken_round_filed_before_the_switch_is_superseded():
    ctx, _gh, b, _fix = repaired(settle=False)
    boardpr.scan(ctx, yes=True)
    rev2 = b.find("REV-PKG-Foo~2")
    assert rev2.state == "available" and rev2.after == ["FIX-PKG-Foo~2"]
    ctx.cfg.board.settle_minor = True
    dry = "#12: REV-PKG-Foo~2 would be superseded: FIX-PKG-Foo~2 settled REV-PKG-Foo (dry run)"
    assert boardpr.scan(ctx, yes=False) == [dry]
    out = boardpr.scan(ctx, yes=True)
    assert len(out) == 1 and out[0].startswith(
        "#12: REV-PKG-Foo~2 superseded (FIX-PKG-Foo~2 settled REV-PKG-Foo: P3 only); created RT-Foo~1a")
    assert rev2.state == "done" and SUPERSEDED_LABEL in rev2.labels
    assert any("No further review round is needed; closed by the board." in body for _n, body in b.posted)
    assert b.find("REV-PKG-Foo").inputs["accepted_review"]["settled"]["lines"] == 10
    assert b.find("RT-Foo~1a").after == ["FIX-PKG-Foo~2"] and b.find("RT-Foo~1b").inputs["board_pr"] == 12
    assert boardpr.scan(ctx, yes=True) == []


def test_a_round_somebody_took_runs_to_its_end():
    ctx, gh, b, _fix = repaired(settle=False)
    boardpr.scan(ctx, yes=True)
    rev2 = b.find("REV-PKG-Foo~2")
    ctx.cfg.board.settle_minor = True
    rev2.state = "claimed"
    assert boardpr.scan(ctx, yes=True) == [] and b.find("RT-Foo~1a") is None
    gh.say("roed-math", asks("c" * 40, "P2"))                           # its review counts as any other
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: REV-PKG-Foo~2 needs_changes; created FIX-PKG-Foo~3")


def test_a_package_fix_with_an_accepting_outcome_releases_what_an_acceptance_does():
    fix = new_job("FIX-PKG-Foo~3", inputs={"board_pr": 12, "operator": "alice", "review": "REV-PKG-Foo~2"})
    nxt = successors(fix, Outcome(verdict="accepted", pr=12, operator="alice"), redteam_rounds=2)
    assert [j.id for j in nxt] == ["RT-Foo~1a", "RT-Foo~1b"] and nxt[0].after == ["FIX-PKG-Foo~3"]
    assert nxt[0].independent_of == ["PKG-Foo", "FIX-PKG-Foo~2", "FIX-PKG-Foo~3"]
    assert nxt[0].inputs == {"board_pr": 12, "operator": "alice", "authors": ["PKG-Foo", "FIX-PKG-Foo~2", "FIX-PKG-Foo~3"]}
    assert [j.id for j in successors(fix, Outcome(verdict="accepted"), redteam_rounds=0)] == ["PORT-Foo"]
    assert [j.id for j in successors(fix, Outcome())] == ["REV-PKG-Foo~3"]
    design = new_job("FIX-DESIGN-Foo~3", inputs={"board_pr": 12})        # a design repair is always reviewed again
    assert [j.id for j in successors(design, Outcome(verdict="accepted"))] == ["REV-DESIGN-Foo~3"]


def test_a_fix_that_settled_its_review_reads_as_an_acceptance_at_rest():
    rev = new_job("REV-PKG-Foo", inputs={"board_pr": 12, "accepted_review": {"settled": {"fix": "FIX-PKG-Foo~2"}}})
    fix = new_job("FIX-PKG-Foo~2", inputs={"board_pr": 12, "operator": "alice"})
    later = new_job("FIX-PKG-Foo~3", inputs={"board_pr": 12, "operator": "alice"})
    ctx = bot_context(cfg=example_config())
    found = intake.outcome_at_rest(ctx, fix, [rev, fix, later])
    assert (found.verdict, found.pr, found.operator) == ("accepted", 12, "alice")
    assert intake.outcome_at_rest(ctx, later, [rev, fix, later]).verdict == ""
