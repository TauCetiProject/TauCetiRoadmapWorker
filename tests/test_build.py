"""A worker renders from the briefs of the build it started on, exits for a restart when the
installed build changes, and does not count an operator-side fault against a job (the pilot's
`uv tool install --force` under running workers)."""

from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import build, paths, prompts, worker
from tauceti_roadmap_worker.board import Board
from tauceti_roadmap_worker.lanes import Context
from tauceti_roadmap_worker.prompts import PromptError

from .fakes import FakeBoard, FakeClaims, FakeIssuesGH, example_config, job_issue


@pytest.fixture(autouse=True)
def install_root():
    yield
    prompts.use(None)                 # the module-level snapshot must not leak into other tests
    from tauceti_roadmap_worker import references
    references.use_templates(None)


def test_briefs_come_from_the_startup_snapshot(tmp_path, monkeypatch):
    install = tmp_path / "install"
    (install / "prompts" / "sections").mkdir(parents=True)
    (install / "prompts" / "target.md").write_text("Old brief for __JOB__.\n")
    (install / "templates").mkdir()
    (install / "templates" / "needs-references.md").write_text("---\n---\nRequest for __JOB__.\n")
    monkeypatch.setattr(paths, "PROMPTS", install / "prompts")
    monkeypatch.setattr(paths, "ISSUE_TEMPLATES", install / "templates")
    snap = build.snapshot(tmp_path / "w1", "0.1.0+aaaa")
    assert snap == tmp_path / "w1" / "build" / "0.1.0+aaaa"
    # A reinstall replaces the brief with one the running code cannot fill.
    (install / "prompts" / "target.md").write_text("New brief for __JOB__ and __ABSORBED__.\n")
    assert prompts.render("target", {"JOB": "TARGET-Foo"}) == "Old brief for TARGET-Foo.\n"
    build.snapshot(tmp_path / "w1", "0.1.0+bbbb")              # the next start takes the new build
    assert sorted(p.name for p in (tmp_path / "w1" / "build").iterdir()) == ["0.1.0+bbbb"]
    with pytest.raises(PromptError):
        prompts.render("target", {"JOB": "TARGET-Foo"})


def test_the_build_id_names_version_and_content():
    first = build.build_id()
    assert first.startswith("0.1.0+") and len(first.split("+")[1]) == 10 and build.build_id() == first


def test_the_loop_exits_when_the_installed_build_changes(monkeypatch):
    builds = iter(["0.1.0+aaaa", "0.1.0+bbbb"])
    monkeypatch.setattr(worker, "current_build", lambda: next(builds))
    rounds, lines = [], []
    monkeypatch.setattr(worker, "work_once", lambda *a, **k: rounds.append(1) or worker.LaneResult("", "idle", "x"))
    ctx = SimpleNamespace(cfg=example_config(), board=FakeBoard())
    worker.work_loop(ctx, None, list, log=lines.append, sleep=lambda s: None, build="0.1.0+aaaa")
    assert rounds == [1] and lines[-1].endswith("installed build changed (0.1.0+aaaa → 0.1.0+bbbb); exiting for restart")


class BrokenBrief:
    agent_started = False

    def eligible(self, job, ctx):
        return ""

    def run(self, job, ctx):
        raise PromptError("brief target: no value for __ABSORBED__")


def test_a_fault_before_the_agent_runs_is_not_counted_and_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "TARGET-Gan", "available"), job_issue(2, "TARGET-Next", "available")])
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=FakeClaims(), worker_id="chris-2", operator="chris",
                  agent="codex", model="m")
    monkeypatch.setattr(worker, "lane_for", lambda job: BrokenBrief())
    result = worker.work_once(ctx, None, log=lambda line: None)
    assert (result.job, result.status) == ("TARGET-Gan", "setup-failed") and "PromptError" in result.detail
    assert worker.failures(ctx) == {} and gh.states(1) == ["available"] and ctx.claims.released
    nxt = worker.work_once(ctx, None, log=lambda line: None, avoid={"TARGET-Gan"})
    assert nxt.job == "TARGET-Next"                               # the process leaves the faulty job alone
