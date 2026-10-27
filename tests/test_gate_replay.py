"""A gate round whose board submission stopped after its content push and reply is finished by the
next attempt: the reply is not posted again, and the attempt is not turned away for finding no
review since the last reply (its own)."""

from types import SimpleNamespace

from tauceti_roadmap_worker import gitops, replies
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import gate as lane_gate

from .test_checkout import needs_git
from .test_submission_recovery import BOARD, ROADMAP_BRANCH, hub, no_agent, process, sh  # noqa: F401 (fixtures)


@needs_git
def test_a_gate_round_whose_board_submission_failed_is_finished_without_a_second_reply(hub, monkeypatch, no_agent):
    job = new_job("GATE-Foo~3", inputs={"pr": 905, "operator": "alice"})
    job.number = 41
    monkeypatch.setattr(lane_gate, "open_roadmap_prs", lambda ctx: [
        {"pr": 905, "name": "Foo", "head": hub.head(905), "branch": ROADMAP_BRANCH, "owner": "alice"}])
    monkeypatch.setattr(lane_gate, "timeline", lambda ctx, pr: [
        replies.Event("2026-10-27T01:00:00Z", "reviewer", "Review (chatgpt)\n\nVerdict: needs_changes\n\n1. **F01** x")]
        + [replies.Event(f"2026-10-27T02:00:0{i}Z", c["user"]["login"], c["body"]) for i, c in enumerate(hub.comments[905])])
    monkeypatch.setattr(lane_gate, "inline_comments", lambda ctx, pr: [])
    monkeypatch.setattr(lane_gate, "flip_labels", lambda *a, **k: "awaiting-review")

    def check(self, job_, ctx_, ws):
        ws.extra["gate"] = SimpleNamespace(counts={"build": 0, "depcheck": 0, "readme-ids-missing": 0}, ok=True)
        return []
    monkeypatch.setattr(lane_gate.GateLane, "check", check)
    real_push = gitops.push
    refused = []

    def push(wt, fork, branch, dry_run=False, expected=None):
        if fork == "alice/TauCetiRoadmapJobs" and len(refused) < 3:
            refused.append(branch)
            raise gitops.PushError("git push: ! [remote rejected] (failed)")
        return real_push(wt, fork, branch, dry_run=dry_run, expected=expected)
    monkeypatch.setattr(gitops, "push", push)

    ctx = process(hub)
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    (ws.workdir / "TauCetiRoadmap" / "Foo" / "README.md").write_text("# Foo\n\nanswered\n")
    sh(ws.workdir, "commit", "-q", "-am", "Foo: answer the review")
    ws.report.write_text("## F01 (P1) — applied\nDone.\n")
    assert lane.validate(job, ctx, ws) == []
    result = lane.deliver(job, ctx, ws)
    answered = hub.head(905)
    assert result.status == "failed" and "still owed: push, pull-request, submitted" in result.detail
    assert lane_base.read_pending(ctx, job)["done"] == ["content-push", "content-comment"]
    assert len(hub.comments[905]) == 1

    ctx = process(hub)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    assert hub.head(905) == answered and len(hub.comments[905]) == 1
    [opened] = hub.opened
    assert result.url == opened["html_url"] and ctx.board.states == [(job.id, "submitted")]
    assert lane_base.read_pending(ctx, job) is None
