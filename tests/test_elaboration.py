"""The package, review, fix and gate lanes elaborate `Suggested.lean` whenever `[gates] elaborate` is
set (regression, 2026-10-21: six workers' content checkouts had no `.lake`, so every package shipped
that day skipped its elaboration silently, and a red-team pass on PR #589 found a conjunct that does
not close). The content checkout is wired to the machine's shared Lake packages exactly like a port
worktree; when that cannot deliver, the lane says `Suggested.lean: not elaborated — <reason>`."""

import dataclasses
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tauceti_roadmap_worker import agents, gates
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import base, fix, gate, lake, lane_for, package, pr

from .fakes import FakeGH, FakeTransport, example_config
from .test_checkout import NAME, PKG, live, needs_git  # noqa: F401 - the shared fixture

JOB = f"PKG-{NAME}"


def mark_ready(state: Path, checkout: Path) -> Path:
    shared = lake.shared_packages(state, checkout)
    shared.mkdir(parents=True, exist_ok=True)
    (shared.parent / ".ready").write_text("2026-10-21T09:52:00Z\n")
    return shared


def fake_elaboration(monkeypatch, problems=()):
    """`gates.build_problems` recorded instead of run: (cwd, command) pairs."""
    calls = []

    def build_problems(worktree, command, timeout=3600):
        calls.append((Path(worktree), command))
        return list(problems)
    monkeypatch.setattr(gates, "build_problems", build_problems)
    return calls


# --- the wiring ----------------------------------------------------------------------------------

def test_the_content_checkout_is_wired_to_the_shared_packages_of_its_pin(tmp_path):
    state, content = tmp_path / "state", tmp_path / "content"
    content.mkdir()
    (content / "lake-manifest.json").write_text("{}")
    shared = mark_ready(state, content)
    setup = lake.prepare_lake(content, state, which=lambda name: "/usr/bin/lake")
    link = content / ".lake" / "packages"
    assert setup.ready and setup.packages == shared and setup.note == "shared Lake packages ready"
    assert link.is_symlink() and os.path.realpath(link) == str(shared.resolve())
    # A second round keeps the link; a checkout with packages of its own (the TAUCETI_ROADMAP_LAKE_PACKAGES
    # setup) keeps them: nothing of the checkout's own is removed.
    assert lake.prepare_lake(content, state, which=lambda name: "/usr/bin/lake").packages == shared
    own = tmp_path / "other"
    (own / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    assert lake.link_checkout_packages(own, state) == (own / ".lake" / "packages").resolve()
    assert (own / ".lake" / "packages" / "mathlib").is_dir() and not (own / ".lake" / "packages").is_symlink()


def test_the_first_use_fetches_the_cache_once_under_the_lock(tmp_path):
    state, content = tmp_path / "state", tmp_path / "content"
    content.mkdir()
    calls = []

    def run(cmd, **kw):
        calls.append((tuple(cmd), kw["cwd"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    first = lake.prepare_lake(content, state, run=run, free=lambda p: 500.0, which=lambda n: "/usr/bin/lake")
    assert first.ready and first.note == "shared Lake packages fetched" and calls == [(lake.CACHE_GET, content)]
    assert (first.packages.parent / ".ready").is_file() and (first.packages.parent / ".lock").is_file()
    again = lake.prepare_lake(content, state, run=run, free=lambda p: 500.0, which=lambda n: "/usr/bin/lake")
    assert again.ready and len(calls) == 1                                        # once per pin


def test_what_stops_the_elaboration_is_named(tmp_path):
    state, content = tmp_path / "state", tmp_path / "content"
    content.mkdir()
    def ok(name):
        return "/usr/bin/lake"
    assert "`lake` is not on PATH" in lake.prepare_lake(content, state, which=lambda n: None).reason
    assert "no fetch in this run" in lake.prepare_lake(content, state, fetch=False, which=ok).reason
    low = lake.prepare_lake(content, state, free=lambda p: 12.0, which=ok)
    assert low.reason == "only 12 GB free on the state directory's disk; fetching the shared Lake packages needs 20 GB"
    failed = lake.prepare_lake(content, state, free=lambda p: 500.0, which=ok,
                               run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", ""))
    assert failed.reason == "lake exe cache get exited 1" and not (failed.packages.parent / ".ready").exists()
    # A ready pin costs nothing: neither the disk guard nor a fetch stands in the way.
    mark_ready(state, content)
    assert lake.prepare_lake(content, state, free=lambda p: 1.0, which=ok).ready


# --- the lanes -----------------------------------------------------------------------------------

@needs_git
def test_the_package_lane_elaborates_in_the_wired_checkout(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    job = new_job(JOB)
    lane = lane_for(job)
    monkeypatch.setattr(lake, "lake_on_path", lambda: "/usr/bin/lake")
    shared = mark_ready(ctx.state, ctx.wdir / "content")
    calls = fake_elaboration(monkeypatch)
    ws = lane.prepare(job, ctx)
    assert ws.extra["lake"].ready and (ws.content / ".lake" / "packages").is_symlink()
    assert os.path.realpath(ws.content / ".lake" / "packages") == str(shared.resolve())
    values = lane.common_values(job, ctx, ws)
    assert values["ELABORATE"] == f"`lake env lean <absolute path of Suggested.lean>` run from `{ws.content}`"
    monkeypatch.setattr(package, "package_scan", lambda ctx, ws, name, pkg: [])
    ws.report.write_text("# Report\n")
    assert lane.check(job, ctx, ws) == []
    lean = ws.package(ctx.cfg, job.name) / "Suggested.lean"
    assert calls == [(ws.content, f"lake env lean {lean.resolve()}")]
    assert ws.extra["notes"] == ["Suggested.lean elaborated: sorry the only warning"]
    # What the elaboration finds is a problem of the package, named for what it is.
    calls = fake_elaboration(monkeypatch, ["build exited 1", "error: Suggested.lean:4:0: unknown identifier"])
    assert lane.check(job, ctx, ws) == ["Suggested.lean: elaboration exited 1",
                                        "error: Suggested.lean:4:0: unknown identifier"]
    # A missing object file with a second import is explained: the file, not the machine, is at fault.
    lean.write_text("import Mathlib\nimport TauCeti.Combinatorics.Enumerative.Partition.Conjugate\n")
    fake_elaboration(monkeypatch, ["build exited 1", f"{lean}:1:0: error: object file '/x/Conjugate.olean' of module "
                                   "TauCeti.Combinatorics.Enumerative.Partition.Conjugate does not exist"])
    assert lane.check(job, ctx, ws)[-1] == gates.IMPORT_EXPLANATION


@needs_git
def test_an_impossible_elaboration_is_an_explicit_problem_never_a_skip(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    monkeypatch.setattr(lake, "lake_on_path", lambda: None)
    calls = fake_elaboration(monkeypatch)
    for module in (package, fix):
        monkeypatch.setattr(module, "package_scan", lambda ctx, ws, name, pkg: [])
    for job_id, inputs in ((JOB, {}), (f"FIX-PKG-{NAME}~2", {"review": f"REV-PKG-{NAME}"})):
        job = new_job(job_id, inputs=inputs)
        lane = lane_for(job)
        ws = lane.prepare(job, ctx)
        ws.report.write_text("# Report\n\n## R01 Locator — applied\n\nFixed.\n")
        line = "Suggested.lean: not elaborated — `lake` is not on PATH (install elan; the pinned toolchain follows)"
        problems = lane.check(job, ctx, ws)
        assert line in problems, (job_id, problems)
        assert calls == [] and not ws.extra.get("notes")
        assert lane.common_values(job, ctx, ws)["ELABORATE"] == (
            "not available — `lake` is not on PATH (install elan; the pinned toolchain follows) (say so in the report)")
    # No `[gates] elaborate` is the operator's choice: the brief says so, the checks have nothing to run.
    quiet = dataclasses.replace(ctx, cfg=example_config())
    quiet.cfg.gates.elaborate = ""
    job = new_job(JOB)
    lane = lane_for(job)
    ws = lane.prepare(job, quiet)
    assert "no elaboration command is configured" in lane.common_values(job, quiet, ws)["ELABORATE"]
    assert package.elaboration_problems(quiet, ws, ws.package(quiet.cfg, job.name) / "Suggested.lean") == []


@needs_git
def test_the_problem_reaches_the_repair_pass_and_the_failure(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    monkeypatch.setattr(lake, "lake_on_path", lambda: None)
    briefs = []

    def agent(task, base_env=None):
        briefs.append(task.brief)
        (task.workdir / PKG / "reports").mkdir(parents=True, exist_ok=True)
        (task.workdir / PKG / "reports" / f"{JOB}.md").write_text("# Report\n")
        return agents.AgentResult(exit_code=0, seconds=1.0)
    monkeypatch.setattr(agents, "run_agent", agent)
    monkeypatch.setattr(package, "package_scan", lambda ctx, ws, name, pkg: [])
    job = new_job(JOB)
    job.number = 1
    lane = lane_for(job)
    monkeypatch.setattr(lane, "submission_problems", lambda job, ctx, ws: [])
    monkeypatch.setattr(lane, "push_wip", lambda job, ctx, ws: "")
    result = lane.run(job, ctx)
    assert result.status == "failed" and "Suggested.lean: not elaborated — `lake` is not on PATH" in result.detail
    assert len(briefs) == 2 and "## Repair" in briefs[1] and "- Suggested.lean: not elaborated — `lake`" in briefs[1]
    assert "Elaborate it with `not available — `lake` is not on PATH" in briefs[0]


def test_only_the_lanes_that_elaborate_wire_the_checkout():
    jobs = [new_job(j, inputs=i) for j, i in (
        ("PKG-Foo", {}), ("REV-PKG-Foo", {}), ("FIX-PKG-Foo~2", {}), ("FIX-RT-Foo~1", {"board_pr": 12}),
        ("REV-DESIGN-Foo", {}), ("FIX-DESIGN-Foo~2", {}), ("DESIGN-Foo", {}), ("TARGET-Foo", {}), ("PORT-Foo", {}),
        ("FIX-RT-Foo~1", {"pr": 905}), ("GATE-Foo~1", {"pr": 905}), ("RT-Foo~1a", {"board_pr": 12}))]
    wired = [j.id for j in jobs if lane_for(j).elaborates(j)]
    assert wired == ["PKG-Foo", "REV-PKG-Foo", "FIX-PKG-Foo~2", "FIX-RT-Foo~1"]      # the board one, not the branch one


# --- worktrees: the port lane as before, the gate lane elaborates without a build -----------------

def git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "lake-manifest.json").write_text("{}")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "base"],
                   check=True)
    return path


@needs_git
def test_a_worktree_links_the_shared_packages_and_records_its_setup(tmp_path, monkeypatch):
    state = tmp_path / "state"
    content = git_repo(tmp_path / "w1" / "content")
    ctx = SimpleNamespace(cfg=example_config(), wdir=tmp_path / "w1", state=state, dry_run=False)
    monkeypatch.setattr(lake, "lake_on_path", lambda: "/usr/bin/lake")
    ws = base.Workspace(job_dir=tmp_path, scratch=tmp_path, board=tmp_path, content=content, workdir=tmp_path,
                        report=tmp_path / "r.md", branch="b")
    wt = pr.fresh_worktree(ctx, content, "Alpha", "port", ws)
    shared = lake.shared_packages(state, wt)
    assert os.path.realpath(wt / ".lake" / "packages") == str(shared.resolve())
    assert ws.extra["lake"].reason == "lake exe cache get exited 1"               # conftest: the fetch is `false`
    (shared.parent / ".ready").write_text("now\n")
    # The content checkout wired to the same pin changes nothing for the worktree: the same directory,
    # its `.ready` honoured, no fetch.
    assert lake.prepare_lake(content, state).packages == shared
    wt = pr.fresh_worktree(ctx, content, "Alpha", "port", ws)
    assert os.path.realpath(wt / ".lake" / "packages") == str(shared.resolve()) and ws.extra["lake"].ready


def test_on_a_pull_request_branch_the_build_elaborates_or_the_elaboration_runs(tmp_path, monkeypatch):
    cfg = example_config()
    ctx = SimpleNamespace(cfg=cfg)
    wt = tmp_path / "pr" / "Alpha"
    lean = wt / cfg.roadmap.active_dir / "Alpha" / "Suggested.lean"
    lean.parent.mkdir(parents=True)
    lean.write_text("import Mathlib\n")
    ws = base.Workspace(job_dir=tmp_path, scratch=tmp_path, board=tmp_path, content=tmp_path / "content", workdir=wt,
                        report=tmp_path / "r.md", branch="b", extra={"lake": lake.LakeSetup(packages=tmp_path)})
    calls = fake_elaboration(monkeypatch)
    assert fix.worktree_elaboration_problems(ctx, ws, "Alpha") == [] and calls == []    # the build covers it
    cfg.gates.build = ""
    assert fix.worktree_elaboration_problems(ctx, ws, "Alpha") == []
    assert calls == [(wt, f"lake env lean {lean.resolve()}")]
    ws.extra["lake"] = lake.LakeSetup(reason="lake exe cache get exited 1")
    assert fix.worktree_elaboration_problems(ctx, ws, "Alpha") == [
        "Suggested.lean: not elaborated — lake exe cache get exited 1"]
    assert gate.worktree_elaboration_problems is fix.worktree_elaboration_problems

