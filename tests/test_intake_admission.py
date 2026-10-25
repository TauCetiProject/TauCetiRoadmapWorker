"""Whoever sends a submission, the intake admits it only when its author is an operator of the board
(listed, with write access, or holding the job's claim), the job is open, unparked, ready and the
author's to do with the agent and model it needs, the pull request is against main, and the result
is the job's: its deliverables are at the head, its outcome is one the job's successors can follow,
and a merge-order change only places the roadmaps it carries."""

import json

import pytest

from tauceti_roadmap_worker import board, claims, intake
from tauceti_roadmap_worker.board import encode_submission, new_job

from .fakes import FakeClaims, bot_context, example_config, head_tree
from .test_boardpr import GH, Board

HEAD, BASE = "e" * 40, "b" * 40
PKG = "packages/Foo"
ORDER = "merge-order/ORDER.txt"


class SubmissionGH(GH):
    """A submission of `job_id` by @bob through his fork: its files at the head, the board's files at
    the merge base, and the roadmap repository's pull requests (number -> opener)."""

    def __init__(self, job_id, files, association="CONTRIBUTOR", marker_operator="bob", agent="codex", model="m",
                 at_base=None, content_prs=None):
        super().__init__(HEAD)
        self.pr.update(user={"login": "bob"}, author_association=association, base={"ref": "main", "sha": BASE},
                       body="Refs #7\n\n" + encode_submission(job_id, "w2", marker_operator, agent, model))
        self.head_files, self.at_base, self.content_prs = dict(files), dict(at_base or {}), dict(content_prs or {})

    def api(self, path, paginate=False):
        if "/pulls/12/files" in path:
            return [{"filename": f, "status": "added"} for f in self.head_files]
        if "/git/trees/" in path:
            return head_tree(self.head_files)
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": BASE}}
        if "TauCetiRoadmap/pulls/" in path:
            number = int(path.rsplit("/", 1)[1])
            if number not in self.content_prs:
                raise AssertionError(path)
            return {"number": number, "user": {"login": self.content_prs[number]}}
        return super().api(path, paginate)

    def raw_file(self, repo, path, ref=""):
        from tauceti_roadmap_worker.gh import GHError
        found = self.at_base.get(path) if ref == BASE else self.head_files.get(path)
        if found is None:
            raise GHError(f"{path}: 404")
        return found


TRIAGE = "Verdict: design\nName: Foo\n\nWhy: ...\n"


def target_files(triage=TRIAGE):
    return {f"{PKG}/target.md": triage, f"{PKG}/reports/TARGET-Foo.md": "# Triage of Foo\n"}


def run(gh, job_id="TARGET-Foo", state="claimed", jobs=(), operators=(), shared=None, **spec):
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    cfg.board.operators = list(operators)
    job = new_job(job_id, **spec)
    job.number, job.state = 7, state
    b = Board([job, *jobs])
    ctx = bot_context(cfg=cfg, gh=gh, board=b, operator="board-bot")
    return intake.intake(ctx, 12, yes=True, shared=shared), job, b


def test_an_outsider_is_left_for_the_maintainer():
    gh = SubmissionGH("TARGET-Foo", target_files(), association="NONE")
    line, job, _b = run(gh)
    assert "@bob is not an operator of this board" in line and not gh.merged and job.state == "claimed", line


def test_a_listed_operator_working_through_a_fork_is_taken_in():
    gh = SubmissionGH("TARGET-Foo", target_files(), association="NONE")
    line, job, b = run(gh, operators=["alice", "Bob"])
    assert line.startswith("#12: merged TARGET-Foo; created DESIGN-Foo"), line
    assert gh.merged and job.state == "done" and b.find("DESIGN-Foo")


def test_an_operator_holding_the_jobs_claim_is_taken_in():
    key = claims.job_key(example_config().project.name, "TARGET-Foo")
    gh = SubmissionGH("TARGET-Foo", target_files(), association="NONE")
    line, _job, _b = run(gh, shared=FakeClaims(leases={key: {"owner": "bob/w2", "operator": "bob"}}))
    assert line.startswith("#12: merged TARGET-Foo"), line
    gh = SubmissionGH("TARGET-Foo", target_files(), association="NONE")
    assert "not an operator" in run(gh, shared=FakeClaims(leases={key: {"owner": "carol/w1", "operator": "carol"}}))[0]


def test_the_label_of_a_claim_admits_its_author():
    gh = SubmissionGH("TARGET-Foo", target_files(), association="NONE")
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    job = new_job("TARGET-Foo")
    job.number, job.state = 7, "claimed"
    b = Board([job])
    b.claimed_by["TARGET-Foo"] = "bob"
    assert intake.intake(bot_context(cfg=cfg, gh=gh, board=b, operator="board-bot"), 12, yes=True).startswith(
        "#12: merged TARGET-Foo")


@pytest.mark.parametrize("kwargs,spec,state,why", [
    ({"marker_operator": "alice"}, {}, "claimed", "the submission names operator @alice, but @bob opened it"),
    ({}, {"requires_operator": "alice"}, "claimed", "TARGET-Foo is @alice's (it owns the pull request branch)"),
    ({"agent": "codex"}, {"requires_agent": "claude"}, "claimed", "TARGET-Foo needs agent claude, the submission records codex"),
    ({"model": "m"}, {"requires_model": "fable"}, "claimed", "TARGET-Foo needs model fable, the submission records m"),
    ({}, {"after": ["REV-PKG-Bar"]}, "claimed", "TARGET-Foo follows REV-PKG-Bar, not done yet"),
    ({}, {}, "needs-references", "TARGET-Foo is needs-references"),
])
def test_a_submission_the_job_does_not_admit_is_refused(kwargs, spec, state, why):
    gh = SubmissionGH("TARGET-Foo", target_files(), association="COLLABORATOR", **kwargs)
    line, job, _b = run(gh, state=state, **spec)
    assert why in line and not gh.merged and job.state == state, line


def test_a_finished_prerequisite_and_a_matching_agent_are_admitted():
    done = new_job("REV-PKG-Bar")
    done.number, done.state = 3, "done"
    gh = SubmissionGH("TARGET-Foo", target_files(), association="COLLABORATOR", agent="claude", model="fable")
    line, _job, _b = run(gh, jobs=[done], after=["REV-PKG-Bar"], requires_agent="claude", requires_model="fable")
    assert line.startswith("#12: merged TARGET-Foo"), line


def test_a_pull_request_against_another_branch_is_refused():
    gh = SubmissionGH("TARGET-Foo", target_files(), association="COLLABORATOR")
    gh.pr["base"]["ref"] = "staging"
    assert "the pull request is against `staging`, not `main`" in run(gh)[0]


@pytest.mark.parametrize("files,why", [
    ({f"{PKG}/reports/TARGET-Foo.md": "# Triage\n"}, f"{PKG}/target.md is missing"),
    ({f"{PKG}/target.md": TRIAGE}, f"no report ({PKG}/reports/TARGET-Foo.md)"),
    (target_files("Verdict: maybe\n"), "target.md: Verdict must be one of"),
    (target_files("Verdict: merge-into TARGET-Bar\n"), "target.md: merge-into names TARGET-Bar, which is not on the board"),
])
def test_a_target_without_its_deliverables_or_a_valid_triage_is_refused(files, why):
    gh = SubmissionGH("TARGET-Foo", files, association="COLLABORATOR")
    line, job, _b = run(gh)
    assert why in line and not gh.merged and job.state == "claimed", line


def test_a_duplicate_naming_its_owner_is_a_valid_outcome():
    gh = SubmissionGH("TARGET-Foo", target_files("Verdict: duplicate\nOwner: Bar\n"), association="COLLABORATOR")
    line, job, _b = run(gh)
    assert line.startswith("#12: merged TARGET-Foo") and job.state == "done", line


def port_files(report="# Port of Foo\n\nPull request: #905\n", order=None):
    files = {f"{PKG}/reports/PORT-Foo.md": report}
    if order is not None:
        files[ORDER] = order
    return files


def test_a_port_names_the_roadmap_pull_request_its_author_opened():
    ok = SubmissionGH("PORT-Foo", port_files(), association="COLLABORATOR", content_prs={905: "bob"})
    assert run(ok, "PORT-Foo")[0].startswith("#12: merged PORT-Foo")
    nothing = SubmissionGH("PORT-Foo", port_files("# Port of Foo\n\nNothing was ported.\n"), association="COLLABORATOR")
    assert "names no roadmap pull request" in run(nothing, "PORT-Foo")[0]
    other = SubmissionGH("PORT-Foo", port_files(), association="COLLABORATOR", content_prs={905: "mallory"})
    assert "names #905 on TauCetiProject/TauCetiRoadmap, which @bob did not open" in run(other, "PORT-Foo")[0]


def test_a_port_places_its_roadmap_in_the_merge_order_and_touches_no_other():
    main = "Base\nBar\n"
    placed = SubmissionGH("PORT-Foo", port_files(order="Base\nFoo\nBar\n"), association="COLLABORATOR",
                          at_base={ORDER: main}, content_prs={905: "bob"})
    assert run(placed, "PORT-Foo")[0].startswith("#12: merged PORT-Foo")
    replaced = SubmissionGH("PORT-Foo", port_files(order="Foo\n"), association="COLLABORATOR",
                            at_base={ORDER: main}, content_prs={905: "bob"})
    line = run(replaced, "PORT-Foo")[0]
    assert f"{ORDER}: removes Bar, Base from the merge order" in line and not replaced.merged, line
    smuggled = SubmissionGH("PORT-Foo", port_files(order="Base\nFoo\nBar\nQux\n"), association="COLLABORATOR",
                            at_base={ORDER: main}, content_prs={905: "bob"})
    assert f"{ORDER}: adds Qux, which this submission does not carry" in run(smuggled, "PORT-Foo")[0]


def test_a_review_whose_verdict_the_review_lane_would_refuse_is_refused():
    verdict = {"review": {"status": "accepted", "reviewer": "independent-review-REV-PKG-Foo", "blocking": 2}}
    files = {f"{PKG}/review.json": json.dumps(verdict), f"{PKG}/reports/REV-PKG-Foo.md": "# Review of Foo\n"}
    gh = SubmissionGH("REV-PKG-Foo", files, association="COLLABORATOR")
    assert "verdict file: accepted with blocking findings" in run(gh, "REV-PKG-Foo")[0]


def plan(gaps, prerequisites):
    return json.dumps({"roadmap": "Foo", "layers": [{"targets": [{"id": "T1", "prerequisites": prerequisites}]}],
                       "gaps": [{"slug": g, "needs": "x", "supplier": "none"} for g in gaps]})


def test_a_package_stage_plan_keeps_every_gap_its_targets_name():
    path = f"{PKG}/design/plan.json"
    assert board.plan_change_problems(path, plan(["a", "b"], ["gap:a"]), plan(["a"], ["gap:a"])) == []
    assert board.plan_change_problems(path, plan(["a", "b"], ["gap:a"]), plan(["b"], ["gap:a"])) == [
        f"{path}: removes the gaps a, which targets of the plan still name"]
    # A reference that was already unresolved is not this change's doing.
    assert board.plan_change_problems(path, plan(["b"], ["gap:a"]), plan(["b", "c"], ["gap:a"])) == []
