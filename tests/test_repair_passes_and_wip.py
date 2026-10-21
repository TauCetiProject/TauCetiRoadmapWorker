"""A package run is not thrown away for problems its repairs are still reducing, and a retry anywhere
starts from what the failed run wrote (regressions: Cohomology Comparisons Part II failed on 74
`readme-ids-missing`, K3SurfacesAndSymplecticBoundedness on 3, each after one repair pass, and each
restarted elsewhere from scratch)."""

import dataclasses
import subprocess

from tauceti_roadmap_worker import agents, gitops, prompts
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for

from .fakes import FakeGH, FakeTransport
from .test_checkout import NAME, PKG, live, needs_git, run  # noqa: F401 - the shared fixture

JOB = f"PKG-{NAME}"


def scripted(monkeypatch, lane, problem_counts):
    """The checks report these numbers of problems, run after run; returns the briefs the agent got."""
    briefs, counts = [], iter(problem_counts)

    def agent(task, base_env=None):
        briefs.append(task.brief)
        return agents.AgentResult(exit_code=0, seconds=1.0)
    monkeypatch.setattr(agents, "run_agent", agent)
    monkeypatch.setattr(lane, "validate", lambda job, ctx, ws: [f"readme-ids-missing: id{k}" for k in range(next(counts))])
    monkeypatch.setattr(lane, "submit", lambda job, ctx, ws, summary: "https://example.invalid/1")
    monkeypatch.setattr(lane, "push_wip", lambda job, ctx, ws: "")
    return briefs


@needs_git
def test_repairs_go_on_while_the_problems_fall(live, monkeypatch):  # noqa: F811
    ctx, _board = live
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(JOB)
    job.number = 1
    lane = lane_for(job)
    briefs = scripted(monkeypatch, lane, [74, 20, 3, 0])
    assert lane.run(job, ctx).status == "submitted" and len(briefs) == 4         # the run and three repairs
    # Each repair gets only the problems that remain (a brief lists at most sixty of them).
    assert "id59" in briefs[1] and "id19" in briefs[2] and "id20" not in briefs[2]
    assert "id2" in briefs[3] and "id3" not in briefs[3]
    lane = lane_for(job)
    briefs = scripted(monkeypatch, lane, [3, 3])
    result = lane.run(job, ctx)
    assert result.status == "failed" and len(briefs) == 2                       # no progress: no second repair
    lane = lane_for(job)
    briefs = scripted(monkeypatch, lane, [9, 5, 4, 2])
    assert lane.run(job, ctx).status == "failed" and len(briefs) == 4           # three repairs at most


def test_the_package_brief_states_the_identifier_rule():
    text = prompts.template("package")
    assert "must be declared in `Suggested.lean` (a sorried statement is fine) or be written as prose" in text
    assert "`congruenceKernel.inv_mem`, `congruenceKernel.mem_iff` and `congruenceKernel.mul_mem`" in text
    assert text.index("The gate's rule on identifiers") < text.index("## Inputs")


@needs_git
def test_a_failed_runs_deliverables_reach_the_next_attempt_on_any_worker(live, tmp_path, monkeypatch):  # noqa: F811
    ctx, _board_origin = live
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "init", "-q", "--bare", str(fork)], check=True)
    fork_repo = "alice/" + ctx.cfg.repositories.packages_repo().split("/")[1]
    origin_url = gitops.url
    monkeypatch.setattr(gitops, "url", lambda repo: fork.as_uri() if repo == fork_repo else origin_url(repo))
    monkeypatch.setattr(gitops, "ensure_fork", lambda gh, repo, login: f"{login}/{repo.split('/')[1]}")
    ctx = dataclasses.replace(ctx, gh=FakeGH(), transport=FakeTransport())
    job = new_job(JOB)
    job.number = 1
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    (ws.board / PKG / "README.md").write_text("# QSeries\\n\\nAPI: `congruenceKernel.inv_mem`.\\n")
    (ws.board / PKG / "Suggested.lean").write_text("import Mathlib\\n")
    note = lane.push_wip(job, ctx, ws)
    assert "kept on" in note and job.inputs["wip"]["branch"] == f"wip/{JOB}"
    assert sorted(job.inputs["wip"]["files"]) == [f"{PKG}/README.md", f"{PKG}/Suggested.lean"]
    assert "congruenceKernel" in subprocess.run(["git", "--git-dir", str(fork), "show", f"wip/{JOB}:{PKG}/README.md"],
                                                capture_output=True, text=True, check=True).stdout
    # Another worker's attempt starts from them.
    other = dataclasses.replace(ctx, worker_id="w2")
    lane2 = lane_for(job)
    ws2 = lane2.prepare(job, other)
    assert not (ws2.board / PKG / "README.md").exists()                      # the board's main has no package yet
    assert sorted(lane2.restore_wip(job, other, ws2)) == [f"{PKG}/README.md", f"{PKG}/Suggested.lean"]
    assert "congruenceKernel" in (ws2.board / PKG / "README.md").read_text()
    assert "Its deliverables are back in place" in lane2.earlier_attempt(ws2)
    # A submission removes the branch and the record.
    lane2.drop_wip(job, other, ws2)
    assert "wip" not in job.inputs
    listed = subprocess.run(["git", "--git-dir", str(fork), "branch", "--list", f"wip/{JOB}"], capture_output=True,
                            text=True, check=True)
    assert listed.stdout.strip() == ""
