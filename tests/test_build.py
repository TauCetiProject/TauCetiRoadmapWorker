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


def test_an_unreadable_install_is_unknown_and_the_loop_goes_on(monkeypatch):
    monkeypatch.setattr(build, "PACKAGE", build.PACKAGE / "no-such-dir")
    monkeypatch.setattr(paths, "PROMPTS", paths.PROMPTS / "no-such-dir")
    monkeypatch.setattr(paths, "ISSUE_TEMPLATES", paths.ISSUE_TEMPLATES / "no-such-dir")
    assert build.build_id() == ""                       # never the hash of nothing
    builds = iter(["", "0.1.0+aaaa", "0.1.0+bbbb"])
    monkeypatch.setattr(worker, "current_build", lambda: next(builds))
    rounds, lines = [], []
    monkeypatch.setattr(worker, "work_once", lambda *a, **k: rounds.append(1) or worker.LaneResult("", "idle", "x"))
    worker.work_loop(SimpleNamespace(cfg=example_config(), board=FakeBoard()), None, list, log=lines.append,
                     sleep=lambda s: None, build="0.1.0+aaaa")
    assert rounds == [1, 1] and "cannot be read" in lines[0] and lines[-1].endswith("exiting for restart")


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


def test_the_board_workflow_runs_the_three_commands_with_its_own_token():
    text = (paths.HERE / "board" / "board.yml").read_text()
    for line in ("board intake --yes", "board sync --yes", "board stale --release --yes", "group: board",
                 "cancel-in-progress: false", "contents: write", "pull-requests: write", "issues: write",
                 "GH_TOKEN: ${{ github.token }}", "pull_request_target:", "workflow_dispatch:", '- cron: "*/10 * * * *"',
                 "uv tool install git+https://github.com/TauCetiProject/TauCetiRoadmapWorker@main"):
        assert line in text, line
    assert "ref: ${{ github.event.pull_request.head" not in text      # never checks out a pull request's code
    # Comments and issue edits are no trigger: on a busy board they spent the token's 1,000 requests an hour.
    assert "issue_comment:" not in text and "\n  issues:\n" not in text


# --- the overflow workers and the late reviews --------------------------------------------------------

def waiting_issue(number, job_id, filed, **spec):
    return {**job_issue(number, job_id, "available", **spec), "created_at": filed}


def test_an_overflow_worker_leaves_young_jobs_of_its_kinds_to_the_others(tmp_path, monkeypatch):
    import dataclasses
    import datetime as dt
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    now = dt.datetime.now(dt.UTC)
    stamp = lambda minutes: (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    cfg = example_config()
    gh = FakeIssuesGH([waiting_issue(1, "TARGET-Young", stamp(5)), waiting_issue(2, "TARGET-Old", stamp(45))])
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=FakeClaims(), worker_id="chris-2", operator="chris",
                  agent="codex", model="m", overflow={"target": 30})
    young, old = ctx.board.list_jobs(("available",))
    assert (young.filed, worker.held_back(ctx, young), worker.held_back(ctx, old)) == (stamp(5), True, False)
    assert 4.9 < worker.waited_minutes(young) < 5.5 and worker.waited_minutes(old) > 44
    monkeypatch.setattr(worker, "lane_for", lambda job: BrokenBrief())
    taken = worker.work_once(ctx, None, log=lambda line: None)
    assert taken.job == "TARGET-Old"                              # the young one is left, the old one taken
    idle = worker.work_once(ctx, None, log=lambda line: None, avoid={"TARGET-Old"})
    assert idle.status == "idle" and "1 left to the workers that have them first" in idle.detail
    named = worker.work_once(ctx, None, log=lambda line: None, job_id="TARGET-Young")
    assert named.job == "TARGET-Young"                            # a job asked for by name is taken whatever its wait
    # Another kind, a worker that is nobody's overflow, a job whose filing time the board did not give: taken.
    other = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=FakeClaims(), worker_id="chris-3", operator="chris",
                    agent="codex", model="m", overflow={"prreview": 30})
    assert not worker.held_back(other, young) and not worker.held_back(dataclasses.replace(ctx, overflow={}), young)
    young.filed = ""
    assert worker.waited_minutes(young) is None and not worker.held_back(ctx, young)


def test_the_overflow_option_parses_kinds_and_minutes():
    from tauceti_roadmap_worker import cli
    assert cli._overflow("") == {} and cli._overflow("prreview=30, review=45") == {"prreview": 30, "review": 45}
    for bad in ("prreview", "prreview=0", "prreview=soon", "nosuchkind=5"):
        with pytest.raises(SystemExit, match="--overflow"):
            cli._overflow(bad)


def test_reviews_of_late_authors_rank_after_everything_else():
    from tauceti_roadmap_worker.board import new_job
    cfg = example_config()
    cfg.prreview.late_authors, cfg.prreview.late_except = ["Dave"], [14]
    jobs = [new_job("PRREVIEW-A~1", inputs={"pr": 12, "owner": "dave"}), new_job("PRREVIEW-B~1", inputs={"pr": 13, "owner": "carol"}),
            new_job("PRREVIEW-C~1", inputs={"pr": 14, "owner": "dave"}), new_job("PKG-Delta")]
    for n, j in enumerate(jobs, 1):
        j.number = n
    assert [j.id for j in worker.rank(jobs, late=worker.late_review(cfg.prreview))][-1] == "PRREVIEW-A~1"
    assert [j.id for j in worker.rank(jobs)] == [j.id for j in worker.rank(jobs, late=worker.late_review(example_config().prreview))]
