"""A job issue carries exactly one `state:` label (regression: the first pilot).

`Board.set_state` removes every other state label the issue carries now, not only those of the
job's possibly stale listing; `board sync` repairs an issue that carries several anyway; and every
way a worker's round can stop before it submits releases the job to `state:available` alone."""

import pytest

from tauceti_roadmap_worker import board, claims, config, intake, worker
from tauceti_roadmap_worker.board import Board
from tauceti_roadmap_worker.gitops import GitError
from tauceti_roadmap_worker.lanes import Context, LaneResult
from tauceti_roadmap_worker.paths import HERE

from .fakes import FakeClaims, FakeIssuesGH, job_issue

JOB = "TARGET-QSeries"


@pytest.fixture
def cfg():
    return config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")


def listed(b: Board, number: int):
    return next(j for j in b.all_jobs() if j.number == number)


def test_set_state_removes_every_state_label_the_issue_carries(cfg):
    gh = FakeIssuesGH([job_issue(1, JOB, "available")])
    b = Board(cfg, gh)
    job = listed(b, 1)                            # listed while the issue said only `available`
    gh.issues[1]["labels"].append({"name": "state:claimed"})   # someone else wrote since
    b.set_state(job, "needs-references", "parked")
    assert gh.states(1) == ["needs-references"] and job.state == "needs-references"
    assert board.state_labels(job.labels) == ["needs-references"] and gh.comments[1] == ["parked"]
    assert [m for m, *_ in gh.writes][:1] == ["POST"]      # the new label goes on before any removal


class SilentAdds(FakeIssuesGH):
    """A label add that answers nothing (as `gh` in dry-run mode)."""

    def write(self, method, path, payload=None):
        out = super().write(method, path, payload)
        return None if method == "POST" and path.endswith("/labels") else out


def test_set_state_reads_the_labels_when_the_add_does_not_report_them(cfg):
    gh = SilentAdds([job_issue(1, JOB, "available", "claimed")])
    job = listed(Board(cfg, gh), 1)
    job.labels = [label for label in job.labels if label != "state:claimed"]
    Board(cfg, gh).set_state(job, "available")
    assert gh.states(1) == ["available"]


def test_a_failed_removal_leaves_the_new_state_on(cfg):
    gh = FakeIssuesGH([job_issue(1, JOB, "claimed")])
    gh.fail_removals = True
    b = Board(cfg, gh)
    b.set_state(listed(b, 1), "available")
    assert sorted(gh.states(1)) == ["available", "claimed"]     # never no state: sync repairs this


@pytest.mark.parametrize("states,live,keep", [
    (["available", "claimed"], True, "claimed"),
    (["available", "claimed"], False, "available"),
    (["available", "claimed"], None, "claimed"),
    (["claimed", "needs-references"], True, "needs-references"),
    (["claimed", "submitted"], True, "submitted"),
    (["available", "needs-references"], False, "needs-references"),
    (["blocked", "available"], False, "blocked"),
    (["submitted", "done"], False, "done"),
    (["bogus", "available"], False, "available"),
])
def test_repaired_state(states, live, keep):
    assert board.repaired_state(states, live) == keep


def test_board_sync_repairs_issues_with_several_state_labels(cfg):
    gh = FakeIssuesGH([job_issue(1, "TARGET-Alpha", "available", "claimed"),
                       job_issue(2, "TARGET-Beta", "available", "claimed"),
                       job_issue(3, "DESIGN-Gamma", "claimed", "needs-references"),
                       job_issue(4, "PKG-Delta", "submitted"),
                       {**job_issue(5, "PKG-Eps", "available", "done"), "state": "closed"}])
    shared = FakeClaims(live=(claims.job_key(cfg.project.name, "TARGET-Alpha"),))
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=None, worker_id="board", operator="m",
                  agent="codex", model="m")
    out = intake.sync_states(ctx, shared)
    assert out == ["TARGET-Alpha: available + claimed -> claimed (a live lease)",
                   "TARGET-Beta: available + claimed -> available (no live lease)",
                   "DESIGN-Gamma: claimed + needs-references -> needs-references (no live lease)"]
    assert [gh.states(n) for n in (1, 2, 3, 4)] == [["claimed"], ["available"], ["needs-references"], ["submitted"]]
    assert sorted(gh.states(5)) == ["available", "done"]            # closed issues are left alone
    assert gh.comments[2] == [("Board: this job carried `state:available`, `state:claimed`; it keeps "
                               "`state:available` (no live lease).")]
    assert intake.sync_states(ctx, shared) == []                     # repaired: nothing left to do


# --- the worker's round ----------------------------------------------------------------------------

class Lane:
    """A lane that stops the way the pilot's did (or any other way)."""

    def __init__(self, outcome):
        self.outcome = outcome

    def eligible(self, job, ctx):
        return ""

    def run(self, job, ctx):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def round_ctx(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    gh = FakeIssuesGH([job_issue(1, JOB, "available")])
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=FakeClaims(), worker_id="chris-1",
                  operator="alice", agent="codex", model="m")
    return ctx, gh


CHECKOUT = GitError("git checkout -q --detach: error: The following untracked working tree files would be "
                    "overwritten by checkout: packages/QSeries/sources.toml")


@pytest.mark.parametrize("outcome,counted", [
    (CHECKOUT, True),                                                           # the pilot's second round
    (LaneResult(JOB, "failed", "the agent changed files outside the job's deliverables"), True),  # its third
    (ValueError("a bug in a lane"), True),
    (LaneResult(JOB, "limited", "usage limit"), False),
])
def test_a_round_that_stops_before_submitting_releases_the_job(round_ctx, monkeypatch, outcome, counted):
    ctx, gh = round_ctx
    monkeypatch.setattr(worker, "lane_for", lambda job: Lane(outcome))
    result = worker.work_once(ctx, None, log=lambda line: None)
    assert result.status == ("limited" if not counted else "failed")
    assert gh.states(1) == ["available"]
    assert ctx.claims.acquired == ctx.claims.released == [claims.job_key(ctx.cfg.project.name, JOB)]
    assert worker.failures(ctx).get(JOB, 0) == (1 if counted else 0)
    assert gh.comments[1][0].startswith("Claimed for") and gh.comments[1][1].startswith("Released by worker `chris-1`")


def test_an_interrupted_round_releases_the_job_and_stops(round_ctx, monkeypatch):
    ctx, gh = round_ctx
    monkeypatch.setattr(worker, "lane_for", lambda job: Lane(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        worker.work_once(ctx, None, log=lambda line: None)
    assert gh.states(1) == ["available"] and ctx.claims.released and worker.failures(ctx) == {}


def test_a_stale_listing_still_ends_with_one_state(round_ctx, monkeypatch):
    """The issue gained a second state label after the worker listed it: claim and release both
    leave exactly one."""
    ctx, gh = round_ctx
    listing = ctx.board.list_jobs

    def stale_listing(*args, **kwargs):
        jobs = listing(*args, **kwargs)
        gh.issues[1]["labels"].append({"name": "state:needs-references"})
        return jobs
    monkeypatch.setattr(ctx.board, "list_jobs", stale_listing)
    monkeypatch.setattr(worker, "lane_for", lambda job: Lane(CHECKOUT))
    worker.work_once(ctx, None, log=lambda line: None)
    assert gh.states(1) == ["available"]
