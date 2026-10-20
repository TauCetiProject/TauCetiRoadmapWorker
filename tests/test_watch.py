"""`tail` and `status`, on files and faked GitHub data."""

import datetime as dt

from tauceti_roadmap_worker import claims, config, watch
from tauceti_roadmap_worker.board import Board
from tauceti_roadmap_worker.paths import HERE

from .fakes import job_issue


def test_the_round_logger_prints_and_keeps_a_timestamped_log(tmp_path):
    printed = []
    log = watch.round_logger(tmp_path, out=printed.append)
    log("start TARGET-Foo (TargetLane, codex m)")
    assert printed == ["start TARGET-Foo (TargetLane, codex m)"]
    [line] = (tmp_path / "rounds.log").read_text().splitlines()
    assert line.endswith("Z start TARGET-Foo (TargetLane, codex m)") and line[:4].isdigit()


def test_tail_follows_the_rounds_and_the_current_jobs_agent_output(tmp_path):
    log = watch.round_logger(tmp_path, out=lambda line: None)
    log("worker w1 as @alice")
    log("start TARGET-Foo (TargetLane, codex m)")
    run = tmp_path / "jobs" / "TARGET-Foo" / "run-1.out"
    run.parent.mkdir(parents=True)
    run.write_text("START 2026-10-20T20:00Z\nreading packages/Foo\n")
    seen = []

    def sleep(_):
        if len(seen) < 6:
            with open(run, "a") as f:
                f.write("writing target.md\npartial")
            log("2026-10-20T21:00Z TARGET-Foo submitted: triage")
    watch.tail(tmp_path, out=seen.append, sleep=sleep, rounds=2)
    assert seen[0].startswith("[round] ") and "worker w1 as @alice" in seen[0]
    assert f"[tail] following TARGET-Foo: {run}" in seen
    assert "[TARGET-Foo] reading packages/Foo" in seen and "[TARGET-Foo] writing target.md" in seen
    assert not any("partial" in line for line in seen)               # an unfinished line waits
    assert any(line.endswith("TARGET-Foo submitted: triage") for line in seen)


def test_status_lists_jobs_by_state_with_ages_and_claimants():
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")
    at = dt.datetime(2026, 10, 20, 21, 0, tzinfo=dt.UTC)
    issues = [{**job_issue(3, "PKG-Foo", "claimed"), "updated_at": "2026-10-20T20:30:00Z"},
              {**job_issue(1, "TARGET-Bar", "available"), "updated_at": "2026-10-18T21:00:00Z"},
              {**job_issue(2, "DESIGN-Baz", "needs-references"), "updated_at": "2026-10-20T20:59:30Z"},
              {**job_issue(4, "REV-PKG-Qux", "done"), "updated_at": "2026-10-19T00:00:00Z"}]
    lease = {"owner": "alice/w1", "acquired_at": int(at.timestamp()) - 1200, "expires_at": int(at.timestamp()) + 600}
    text = watch.status(Board(cfg, gh=None), issues, {claims.job_key(cfg.project.name, "PKG-Foo"): lease},
                        cfg.project.name, at=at)
    assert text.splitlines() == ["available (1)", "  #1      TARGET-Bar     2d",
                                 "claimed (1)", "  #3      PKG-Foo    30m  alice/w1 for 20m",
                                 "needs-references (1)", "  #2      DESIGN-Baz    30s"]
    assert "REV-PKG-Qux" in watch.status(Board(cfg, gh=None), issues, {}, cfg.project.name, at=at, include_done=True)
