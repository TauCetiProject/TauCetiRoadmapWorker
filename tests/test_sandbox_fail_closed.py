"""A sandbox the configuration asks for and the host cannot provide stops the worker; it never runs
the agent without it (design review R03, 2026-10-24: an unusable netjail fell back to proxy variables)
and never releases one job after another as a setup failure."""

import pytest

from tauceti_roadmap_worker import agents, cli, config, worker
from tauceti_roadmap_worker.board import Board
from tauceti_roadmap_worker.lanes import Context
from tauceti_roadmap_worker.paths import HERE

from .fakes import FakeClaims, FakeIssuesGH, example_config, job_issue

TEXT = (HERE / "tauceti-roadmap.toml.example").read_text()
BROKEN = ('sandbox: profile "netjail" is requested and [sandbox] netjail names no jail binary; '
          "no agent runs without it")


class JailedLane:
    agent_started = True

    def __init__(self, ran):
        self.ran = ran

    def eligible(self, job, ctx):
        return ""

    def run(self, job, ctx):
        self.ran.append(job.id)
        raise agents.SandboxUnavailable(BROKEN)


def jobs_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "TARGET-Gan", "available"), job_issue(2, "TARGET-Next", "available")])
    return Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=FakeClaims(), worker_id="chris-2",
                   operator="chris", agent="codex", model="m"), gh


def test_a_broken_sandbox_releases_the_job_uncounted_and_ends_the_loop(tmp_path, monkeypatch):
    ctx, gh = jobs_ctx(tmp_path, monkeypatch)
    ran = []
    monkeypatch.setattr(worker, "lane_for", lambda job: JailedLane(ran))
    sleeps = []
    with pytest.raises(SystemExit, match="names no jail binary") as stopped:
        worker.work_loop(ctx, None, list, log=lambda line: None, sleep=sleeps.append)
    assert stopped.value.code == BROKEN and ran == ["TARGET-Gan"] and sleeps == []      # no second job
    assert gh.states(1) == ["available"] and worker.failures(ctx) == {} and ctx.claims.released
    assert gh.states(2) == ["available"]


def test_work_refuses_to_start_without_the_requested_sandbox(tmp_path, monkeypatch):
    conf = tmp_path / "tauceti-roadmap.toml"
    conf.write_text(TEXT.replace('profile = "off"', 'profile = "netjail"', 1))
    monkeypatch.setattr(cli, "GH", lambda **kw: pytest.fail("GitHub was contacted"))
    with pytest.raises(SystemExit, match=r"sandbox: profile \"netjail\" is requested and \[sandbox\] netjail names "
                                         r"no jail binary; no agent runs without it \(`tauceti-roadmap doctor`"):
        cli.main(["--conf", str(conf), "work", "--worker-id", "w1", "--loop"])


def test_doctor_fails_a_sandbox_that_cannot_run_and_states_what_off_guarantees(tmp_path):
    cfg = config.parse(TEXT, "example")
    [row] = cli._sandbox_rows(cfg)
    assert row[0] == cli.OK and row[2].startswith("off: agents run unconfined, as this user with its home directory")
    cfg.sandbox.profile, cfg.sandbox.netjail = "netjail", str(tmp_path / "missing")
    cfg.sandbox.proxy_url = "http://127.0.0.1:1"
    rows = {item: (status, detail) for status, item, detail in cli._sandbox_rows(cfg)}
    assert rows["sandbox"][1].endswith("no filesystem or credential isolation")
    assert rows["proxy"][0] == cli.FAIL and rows["netjail"][0] == cli.FAIL
    assert "workers will not start an agent" in rows["netjail"][1]
