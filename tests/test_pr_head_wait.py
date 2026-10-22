"""After a push, the pull request's head is read until it is the pushed commit (regression:
GATE-GanGrossPrasadUnitaryGroups~1 pushed d603998 to #1404, read the pull request once right after
the push, saw the previous head 4623561 that GitHub still showed for a few seconds, did not reply,
and its second attempt redid the work on top of the pushed commit)."""

from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import gitops
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import Context, lane_for
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import gate as lane_gate
from tauceti_roadmap_worker.lanes.base import LaneError, Workspace

from .fakes import example_config

OLD, NEW = "4623561" + "0" * 33, "d603998" + "0" * 33


class HeadGH:
    """GitHub answering pulls/N with the given heads in turn (the last one from then on); writes recorded."""

    def __init__(self, *heads):
        self.heads, self.reads, self.writes = list(heads), [], []

    def api(self, path, paginate=False):
        if "/pulls/" in path:
            self.reads.append(path)
            head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            if isinstance(head, Exception):
                raise head
            return {"number": int(path.rsplit("/", 1)[1]), "head": {"sha": head}}
        if path.endswith("/labels"):
            return []
        raise GHError(f"GET {path}: not faked")

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        return {}


def lane_and_ctx(monkeypatch, gh):
    lane = lane_for(new_job("GATE-GanGrossPrasadUnitaryGroups~1", inputs={"pr": 1404, "operator": "alice"}))
    sleeps = []
    monkeypatch.setattr(lane, "sleep", sleeps.append)
    ctx = Context(cfg=example_config(), gh=gh, board=None, claims=None, worker_id="w1", operator="alice",
                  agent="claude", model="m")
    return lane, ctx, sleeps


def test_a_head_that_becomes_current_on_the_third_read_is_waited_for(monkeypatch):
    gh = HeadGH(OLD, OLD, NEW)
    lane, ctx, sleeps = lane_and_ctx(monkeypatch, gh)
    lane.await_pr_head(ctx, "o/TauCetiRoadmap", 1404, NEW)
    assert len(gh.reads) == 3 and sleeps == [lane_base.HEAD_POLL] * 2


def test_a_head_that_never_updates_fails_after_the_window(monkeypatch):
    gh = HeadGH(OLD)
    lane, ctx, sleeps = lane_and_ctx(monkeypatch, gh)
    with pytest.raises(LaneError) as info:
        lane.await_pr_head(ctx, "o/TauCetiRoadmap", 1404, NEW, then="not replying")
    assert str(info.value) == ("#1404: the pull request head did not update to the pushed d603998 within 90 s "
                               "(it is still 4623561); not replying")
    assert sum(sleeps) == lane_base.HEAD_WAIT and len(gh.reads) == len(sleeps) + 1


def test_the_window_and_the_interval_are_parameters_and_a_transient_error_is_a_read_without_it(monkeypatch):
    gh = HeadGH(GHError("GET pulls/1404: HTTP 502"), OLD, NEW)
    lane, ctx, sleeps = lane_and_ctx(monkeypatch, gh)
    lane.await_pr_head(ctx, "o/TauCetiRoadmap", 1404, NEW, wait=6, poll=2)
    assert sleeps == [2, 2]
    gh = HeadGH(GHError("GET pulls/1404: 404 Not Found"))
    lane, ctx, _ = lane_and_ctx(monkeypatch, gh)
    with pytest.raises(GHError):                     # anything but a transient error stands
        lane.await_pr_head(ctx, "o/TauCetiRoadmap", 1404, NEW)


def test_a_head_already_in_hand_saves_the_read(monkeypatch):
    gh = HeadGH(OLD)
    lane, ctx, sleeps = lane_and_ctx(monkeypatch, gh)
    lane.await_pr_head(ctx, "o/TauCetiRoadmap", 1404, NEW, seen=NEW)
    assert gh.reads == [] and sleeps == []


def gate_finish(tmp_path, monkeypatch, gh):
    lane, ctx, sleeps = lane_and_ctx(monkeypatch, gh)
    pushed = []
    monkeypatch.setattr(gitops, "git", lambda cwd, *args, **k: NEW + "\n" if args[:1] == ("rev-parse",) else "")
    monkeypatch.setattr(gitops, "ensure_fork", lambda gh_, repo, login: f"{login}/TauCetiRoadmap")
    monkeypatch.setattr(gitops, "push", lambda wt, fork, branch, expected=None: pushed.append((branch, expected)))
    monkeypatch.setattr(lane_gate, "timeline", lambda ctx_, pr: [])
    report = tmp_path / "report.md"
    report.write_text("## H01 (P1) — applied\nThe hypothesis on p is added.\n")
    ws = Workspace(job_dir=tmp_path, scratch=tmp_path, board=tmp_path, content=tmp_path, workdir=tmp_path,
                   report=report, branch="w1/GATE", extra={
                       "pr": 1404, "head": OLD, "branch": "roadmap/ggp-unitary", "owner": "alice",
                       "gate": SimpleNamespace(counts={"build": 0, "depcheck": 0, "readme-ids-missing": 0}, ok=True)})
    return lane, ctx, ws, pushed, sleeps


def test_the_gate_replies_once_the_pushed_head_shows(tmp_path, monkeypatch):
    gh = HeadGH(OLD, OLD, NEW)
    lane, ctx, ws, pushed, sleeps = gate_finish(tmp_path, monkeypatch, gh)
    job = new_job("GATE-GanGrossPrasadUnitaryGroups~1", inputs={"pr": 1404})
    assert lane.finish(job, ctx, ws).startswith("answered the review on #1404 at d603998")
    assert pushed == [("roadmap/ggp-unitary", OLD)] and len(sleeps) == 2
    [(_m, _p, reply)] = [w for w in gh.writes if w[1].endswith("/issues/1404/comments")]
    assert reply["body"].startswith("Fixed in `d603998`")


def test_the_gate_does_not_reply_when_the_head_never_updates(tmp_path, monkeypatch):
    gh = HeadGH(OLD)
    lane, ctx, ws, pushed, _sleeps = gate_finish(tmp_path, monkeypatch, gh)
    job = new_job("GATE-GanGrossPrasadUnitaryGroups~1", inputs={"pr": 1404})
    with pytest.raises(LaneError, match=r"did not update to the pushed d603998 within 90 s .*; not replying$"):
        lane.finish(job, ctx, ws)
    assert pushed and not gh.writes
