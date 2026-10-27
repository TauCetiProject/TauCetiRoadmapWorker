"""Human-filed roadmaps are never starved by imports, an import goes on past a failure, and a
GitHub that answers slowly never throws a run away (regressions from the explorer import: a design
push "[remote rejected] (failed)" stopped the whole import, and a `gh api -X POST` timeout in
FIX-PKG-CohomologyComparisonsPartII~2 counted as a failed attempt)."""

import pytest

from tauceti_roadmap_worker import explorer, gitops, intake
from tauceti_roadmap_worker.board import Outcome, new_job, origins
from tauceti_roadmap_worker.gh import GHError, transient
from tauceti_roadmap_worker.lanes import base as lane_base

from .fakes import FakeBoard, bot_context
from .test_explorer import bot, checkout, fake_git  # noqa: F401 - the explorer fixtures
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture
from .test_push_recovery import lane_with


def test_transient_github_errors():
    assert transient(GHError("gh api -X POST: HTTP 502: Bad Gateway"))
    assert transient(GHError("gh api: We couldn't respond to your request in time. Sorry about that."))
    assert transient(GHError("gh api: Post https://api.github.com: read tcp: i/o timeout"))
    assert not transient(GHError("gh api: HTTP 422: Validation Failed")) and not transient(ValueError("502"))


def test_an_existing_pull_request_for_the_head_is_reused():
    class GH:
        def write(self, method, path, payload=None):
            raise GHError("gh api: HTTP 422: A pull request already exists for alice:w1/PKG-Foo.")

        def api(self, path, paginate=False):
            assert "head=alice:w1/PKG-Foo" in path
            return [{"number": 77, "html_url": "https://x/77"}]
    assert gitops.open_pull_request(GH(), "o/jobs", "alice:w1/PKG-Foo", "main", "t", "b", False)["number"] == 77


def test_a_slow_github_in_the_submission_keeps_the_run(ctx, monkeypatch):  # noqa: F811
    job, lane, calls = lane_with(ctx, monkeypatch, lambda ws: "repaired")
    monkeypatch.setattr(lane, "submit", lambda job_, ctx_, ws, note: lane.push(
        lambda: calls.append("post") or (_ for _ in ()).throw(GHError("gh api -X POST: HTTP 504: Gateway Timeout"))))
    ws = lane.prepare(job, ctx)
    result = lane.deliver(job, ctx, ws)
    assert calls.count("post") == 3 and calls.count(("sleep", 60)) == 2
    assert result.status == "failed" and "next attempt pushes it without running the agent" in result.detail
    assert lane_base.read_pending(ctx, job)["tries"] == 1
    # An error GitHub will repeat (a validation failure) is not retried and keeps nothing.
    lane_base.clear_pending(ctx, job)
    monkeypatch.setattr(lane, "submit", lambda job_, ctx_, ws, note: lane.push(
        lambda: (_ for _ in ()).throw(GHError("gh api -X POST: HTTP 422: Validation Failed"))))
    result = lane.deliver(job, ctx, ws)
    assert result.status == "failed" and "422" in result.detail and lane_base.read_pending(ctx, job) is None


def test_the_import_goes_on_past_a_failure_and_retries_a_rejected_push(checkout, tmp_path, fake_git,  # noqa: F811
                                                                       monkeypatch):
    sleeps, pushes = [], []
    monkeypatch.setattr(explorer, "SLEEP", sleeps.append)

    def push(wt, fork, branch):
        pushes.append(branch)
        if len(pushes) <= 2:
            raise gitops.PushError("! [remote rejected] HEAD -> import/explorer-alpha (failed)")
    monkeypatch.setattr(gitops, "push", push)
    steps = explorer.import_explorer(bot(tmp_path), checkout, names=("Alpha", "Eps"), yes=True)
    assert [s.outcome for s in steps] == ["pull request #61; Alpha inserted into the merge order", "created #501"]
    assert sleeps == [30, 30]

    monkeypatch.setattr(gitops, "push", lambda wt, fork, branch: (_ for _ in ()).throw(
        gitops.PushError("! [remote rejected] HEAD -> import/explorer-alpha (failed)")))
    steps = explorer.import_explorer(bot(tmp_path), checkout, names=("Alpha", "Eps"), yes=True)
    assert steps[0].failed and steps[0].outcome.startswith("failed: ! [remote rejected]")
    assert steps[1].outcome.startswith("created #")                                # the next roadmap was imported
    assert explorer.summary(steps).splitlines()[-1] == "failed: 1 (Alpha); run the import again to retry them"


def test_origins_are_recorded_and_carried():
    target = new_job("TARGET-Witt")
    target.number = 12
    board = FakeBoard(jobs=[target])
    intake.advance(bot_context(board=board), target, Outcome(verdict="design", name="QWittVectors"), [target])
    [(design, _state, _p)] = board.created
    assert design.inputs["origin"] == "target:12"                                  # a renamed design keeps it
    assert explorer.explorer_inputs(type("E", (), {"issue": lambda self, s: 7})(), "pkg-3")["origin"] == "explorer:pkg-3"
    # `board sync` records the origin of an older job from its roadmap's target.
    legacy = new_job("REV-PKG-QWittVectors")
    legacy.number, legacy.state = 30, "available"
    old_design = new_job("DESIGN-QWittVectors", inputs={"target": "TARGET-Witt"})
    old_design.number, old_design.state = 20, "done"
    board = FakeBoard(jobs=[target, old_design, legacy])
    assert origins(board.jobs)["QWittVectors"] == "target:12"
    assert intake.sync_origins(bot_context(board=board)) == ["REV-PKG-QWittVectors: origin target:12"]
    assert legacy.inputs["origin"] == "target:12" and board.specs[-1][0] == "REV-PKG-QWittVectors"


@pytest.mark.parametrize("stage", ["GATE", "PORT", "FIX-RT", "PRREVIEW", "RT", "FIX-PKG", "REV-PKG", "PKG",
                                   "FIX-DESIGN", "REV-DESIGN", "DESIGN", "TARGET", "ERRATA"])
def test_every_stage_is_known(stage):
    from tauceti_roadmap_worker.board import stage_of
    from tauceti_roadmap_worker.config import STAGE_ORDER
    ids = {"GATE": "GATE-Foo~1", "PORT": "PORT-Foo", "FIX-RT": "FIX-RT-Foo~1", "RT": "RT-Foo~1a", "FIX-PKG": "FIX-PKG-Foo~2",
           "REV-PKG": "REV-PKG-Foo", "PKG": "PKG-Foo", "FIX-DESIGN": "FIX-DESIGN-Foo~2", "REV-DESIGN": "REV-DESIGN-Foo",
           "DESIGN": "DESIGN-Foo", "TARGET": "TARGET-Foo", "ERRATA": "ERRATA-Foo", "PRREVIEW": "PRREVIEW-Foo~1"}
    assert stage_of(ids[stage]) == stage and stage in STAGE_ORDER
