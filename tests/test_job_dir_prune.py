"""A worker prunes its own old job folders (regression, 2026-11-13: nothing ever removed
`workers/<id>/jobs/<JOB>/`; 95 worker directories held 5091 of them, about 60 GB, and the host ran
out of disk). What a later attempt of a job still reads is kept, and a folder that cannot be removed
never costs a round."""

import json
import os
import time
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import cli, config, prompts, references, worker
from tauceti_roadmap_worker.lanes import LaneResult

from .fakes import FakeClaims, example_config
from .test_states import JOB, Lane, cfg, round_ctx  # noqa: F401 - the shared fixtures

DAY = 86400


def aged(path, days: float):
    """A job folder with a run's log in it, last written `days` days ago."""
    path.mkdir(parents=True)
    (path / "run-1.out").write_text("START\nEXIT 0\n")
    touch(path / "run-1.out", days)
    touch(path, days)
    return path


def touch(path, days: float) -> None:
    when = time.time() - days * DAY
    os.utime(path, (when, when), follow_symlinks=False)


@pytest.fixture
def w1(tmp_path):
    """A worker's context and its `jobs/` folder."""
    ctx = SimpleNamespace(cfg=example_config(), wdir=tmp_path / "workers" / "w1", dry_run=False)
    return ctx, ctx.wdir / "jobs"


def test_an_old_folder_goes_and_a_fresh_one_and_the_rounds_own_job_stay(w1):
    ctx, jobs = w1
    old, fresh, almost = aged(jobs / "PKG-Old", 4), aged(jobs / "PKG-Fresh", 1), aged(jobs / "PKG-Almost", 2.9)
    own = aged(jobs / "RT-Foo_r1b", 30)                                    # the folder of the job RT-Foo~1b
    lines = []
    assert worker.prune_job_dirs(ctx, lines.append, keep=("RT-Foo~1b",)) == ["PKG-Old"]
    assert not old.exists() and fresh.is_dir() and almost.is_dir() and (own / "run-1.out").is_file()
    assert lines == ["pruned 1 job folder(s) not written to for 3 days: PKG-Old"]
    assert worker.prune_job_dirs(ctx, lines.append) == ["RT-Foo_r1b"]     # no longer the round's job
    assert worker.prune_job_dirs(ctx, lines.append) == [] and len(lines) == 2  # nothing to do: no line


def test_a_folder_is_as_old_as_the_newest_of_itself_and_its_own_entries(w1):
    ctx, jobs = w1
    written = aged(jobs / "PKG-Written", 9)
    (written / "run-2.out").write_text("START\n")                           # a run is writing its log
    touch(written, 9)
    renamed = aged(jobs / "PKG-Renamed", 9)
    touch(renamed, 0)                                                       # an entry was added or removed
    deep = aged(jobs / "PKG-Deep", 9)
    (deep / "scratch" / "notes").mkdir(parents=True)
    for path in (deep / "scratch", deep):                                   # only below its own entries is anything new
        touch(path, 9)
    assert worker.prune_job_dirs(ctx, lambda line: None) == ["PKG-Deep"]
    assert written.is_dir() and renamed.is_dir()


def test_zero_days_keeps_every_folder(w1):
    ctx, jobs = w1
    old = aged(jobs / "PKG-Old", 400)
    ctx.cfg.worker.keep_job_days = 0
    assert worker.prune_job_dirs(ctx, lambda line: pytest.fail(line)) == [] and old.is_dir()
    ctx.cfg.worker.keep_job_days = 1
    assert worker.prune_job_dirs(ctx, lambda line: None) == ["PKG-Old"]


def test_the_setting_and_its_default(tmp_path):
    assert config.parse('[project]\nname = "P"\n').worker.keep_job_days == 3
    assert config.parse('[project]\nname = "P"\n[worker]\nkeep_job_days = 0\n').worker.keep_job_days == 0
    assert example_config().worker.keep_job_days == 3
    with pytest.raises(config.ConfigError, match="worker.keep_job_days must be 0 or more"):
        config.parse('[project]\nname = "P"\n[worker]\nkeep_job_days = -1\n')
    overlay = tmp_path / "overlay.toml"                                     # a host's setting: an overlay may set it
    overlay.write_text("[worker]\nkeep_job_days = 14\n")
    tables, _path = config.read_overlay(str(overlay))
    assert config.parse('[project]\nname = "P"\n', overlay=tables).worker.keep_job_days == 14


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_a_folder_that_cannot_be_read_stays_and_nothing_is_raised(w1):
    ctx, jobs = w1
    locked, old = aged(jobs / "PKG-Locked", 9), aged(jobs / "PKG-Old", 9)
    os.chmod(locked, 0)
    lines = []
    try:
        assert worker.prune_job_dirs(ctx, lines.append) == ["PKG-Old"]     # the others still go
    finally:
        os.chmod(locked, 0o755)
    assert locked.is_dir() and not old.exists() and len(lines) == 2
    assert lines[1].startswith("1 job folder(s) could not be pruned and stay (PKG-Locked: ")


def test_a_folder_that_cannot_be_removed_stays_and_nothing_is_raised(w1, monkeypatch):
    ctx, jobs = w1
    busy, old = aged(jobs / "PKG-Busy", 9), aged(jobs / "PKG-Old", 9)
    remove = worker.shutil.rmtree

    def rmtree(path, *a, **k):
        if str(path).endswith("PKG-Busy"):
            raise OSError(39, "Directory not empty", str(path))
        return remove(path, *a, **k)
    monkeypatch.setattr(worker.shutil, "rmtree", rmtree)
    lines = []
    assert worker.prune_job_dirs(ctx, lines.append) == ["PKG-Old"]
    assert busy.is_dir() and not old.exists()
    assert lines[1].startswith("1 job folder(s) could not be pruned and stay (PKG-Busy: [Errno 39] Directory not empty")


def test_a_jobs_folder_that_cannot_be_listed_is_one_line_in_the_log(w1, monkeypatch):
    ctx, jobs = w1
    old = aged(jobs / "PKG-Old", 9)

    def scandir(path):
        raise PermissionError(13, "Permission denied", str(path))
    monkeypatch.setattr(worker.os, "scandir", scandir)
    lines = []
    assert worker.prune_job_dirs(ctx, lines.append) == [] and old.is_dir()
    assert lines == [f"job folders not pruned: [Errno 13] Permission denied: '{jobs}'"]
    monkeypatch.undo()
    assert worker.prune_job_dirs(SimpleNamespace(cfg=ctx.cfg, wdir=ctx.wdir.parent / "w2", dry_run=False),
                                 lambda line: pytest.fail(line)) == []     # a worker with no jobs/ yet


def test_a_run_kept_for_a_later_attempt_keeps_its_folder(w1):
    """A submission that stopped part-way is finished by the job's next attempt from
    `jobs/<JOB>/pending-push/` and the lane's records beside it, however long that takes."""
    ctx, jobs = w1
    kept, old = aged(jobs / "GATE-Foo_r2", 30), aged(jobs / "PKG-Old", 30)
    (kept / "pending-push" / "board").mkdir(parents=True)
    touch(kept / "pending-push", 30)
    touch(kept, 30)
    record = ctx.wdir / "pending-push.json"
    for unreadable in ("{", "[]"):                                          # which runs are kept is unknown: nothing goes
        record.write_text(unreadable)
        lines = []
        assert worker.prune_job_dirs(ctx, lines.append) == [] and kept.is_dir() and old.is_dir()
        assert len(lines) == 1 and lines[0].startswith(f"job folders not pruned: {record} cannot be read (")
    record.write_text(json.dumps({"GATE-Foo~2": {"issue": 7, "outstanding": ["push"], "done": []}}))
    assert worker.prune_job_dirs(ctx, lines.append) == ["PKG-Old"] and (kept / "pending-push" / "board").is_dir()
    record.write_text("{}")                                                 # submitted since: an old folder like any other
    assert worker.prune_job_dirs(ctx, lines.append) == ["GATE-Foo_r2"]


def test_nothing_outside_the_workers_own_jobs_goes_and_no_link_is_followed(w1, tmp_path):
    ctx, jobs = w1
    elsewhere = aged(tmp_path / "elsewhere" / "PKG-Elsewhere", 30)
    port, build = aged(ctx.wdir / "port" / "Foo", 30), aged(ctx.wdir / "build" / "0.1.0+aaaa", 30)
    other = aged(ctx.wdir.parent / "w2" / "jobs" / "PKG-Old", 30)          # another worker's
    old = aged(jobs / "PKG-Old", 30)
    (old / "sources").symlink_to(elsewhere, target_is_directory=True)       # a link inside a folder that goes
    touch(old / "sources", 30)
    touch(old, 30)
    (jobs / "PKG-Linked").symlink_to(elsewhere, target_is_directory=True)   # a link in jobs/ itself
    touch(jobs / "PKG-Linked", 30)
    (jobs / "notes.txt").write_text("not a job folder\n")
    touch(jobs / "notes.txt", 30)
    assert worker.prune_job_dirs(ctx, lambda line: None) == ["PKG-Old"]
    assert (elsewhere / "run-1.out").is_file() and (jobs / "PKG-Linked").is_symlink() and (jobs / "notes.txt").is_file()
    assert port.is_dir() and build.is_dir() and other.is_dir()


def test_a_dry_run_removes_nothing(w1):
    ctx, jobs = w1
    old = aged(jobs / "PKG-Old", 30)
    ctx.dry_run = True
    assert worker.prune_job_dirs(ctx, lambda line: pytest.fail(line)) == [] and old.is_dir()


def test_a_round_prunes_before_its_lane_runs_and_keeps_its_own_job(round_ctx, monkeypatch):  # noqa: F811
    ctx, _gh = round_ctx
    own, old = aged(ctx.wdir / "jobs" / JOB, 30), aged(ctx.wdir / "jobs" / "PKG-Old", 30)
    seen = []

    class Looking(Lane):
        def run(self, job, ctx):
            seen.append((own.is_dir(), old.exists()))
            return super().run(job, ctx)
    monkeypatch.setattr(worker, "lane_for", lambda job: Looking(LaneResult(JOB, "limited", "usage limit")))
    lines = []
    assert worker.work_once(ctx, None, log=lines.append).status == "limited"
    assert seen == [(True, False)]
    assert lines[:2] == [f"start {JOB} (Looking, codex m)", "pruned 1 job folder(s) not written to for 3 days: PKG-Old"]


def test_a_worker_prunes_at_its_start_and_keeps_the_job_it_was_started_for(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli.GH, "login", lambda self: "alice")
    monkeypatch.setattr(cli.claims, "open_claims", lambda *a, **k: FakeClaims(owner="alice/alice-1"))
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
    monkeypatch.setattr("tauceti_roadmap_worker.mergeorder._order", lambda ctx: [])
    monkeypatch.setattr(worker, "release_leftovers", lambda ctx, log: [])
    monkeypatch.setattr(worker, "sweep_worktrees", lambda ctx, log: [])
    jobs = example_config().state_dir() / "workers" / "alice-1" / "jobs"
    named, old = aged(jobs / "GATE-Foo_r2", 30), aged(jobs / "PKG-Old", 30)
    seen = []

    def work_once(ctx, only, job_id="", **kw):
        seen.append((job_id, named.is_dir(), old.exists()))
        return LaneResult("", "idle", "no eligible job")
    monkeypatch.setattr(worker, "work_once", work_once)
    try:
        code = cli.main(["--conf", str(cli.paths.HERE / "tauceti-roadmap.toml.example"), "work", "--worker-id", "alice-1",
                         "--job", "GATE-Foo~2"])
    finally:
        prompts.use(None)                 # the worker's snapshot of the briefs must not leak into other tests
        references.use_templates(None)
    assert code == 75 and seen == [("GATE-Foo~2", True, False)]
    assert "pruned 1 job folder(s) not written to for 3 days: PKG-Old" in capsys.readouterr().out
