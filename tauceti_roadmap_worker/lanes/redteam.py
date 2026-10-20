"""The `redteam` lane: an independent whole-document pass on the draft pull request, read-only.

Port of mk-redteam2.sh / run-redteam.sh: each round has one pass per configured agent (the job
names the agent it needs), on a read-only checkout of the pull request head; the report goes to the
board, and a `fix` lane on the pull request branch merges the round's findings.
"""

from __future__ import annotations

from .. import board, gitops
from ..board import Job
from .base import Context, Lane, LaneError, Workspace, open_roadmap_prs, rev_dir, sibling_checkouts


def report_problems(text: str, pr: int, name: str, head7: str, pass_: str) -> list[str]:
    first = text.split("\n", 1)[0].strip()
    want = f"# Red-team review of #{pr} {name} at {head7} (pass {pass_})"
    problems = [] if first == want else [f"report: first line must be {want!r}"]
    if "**Verdict:**" not in text:
        problems.append("report: no **Verdict:** line")
    return problems


class RedTeamLane(Lane):
    kind = "redteam"
    prompt = "redteam"

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        pr = int(job.inputs.get("pr") or 0)
        if not pr:
            raise LaneError(f"{job.id} names no pull request")
        prs = open_roadmap_prs(ctx)
        ws.extra["prs"] = prs
        ws.extra["siblings"] = sibling_checkouts(ctx, prs, skip=job.name)
        mine = next((p for p in prs if p["pr"] == pr), None)
        if not mine:
            raise LaneError(f"pull request #{pr} is not an open roadmap pull request")
        rev = rev_dir(ctx.state, pr)
        gitops.checkout_pr(ctx.cfg.repositories.content, rev, pr)
        ws.workdir, ws.extra["pr"], ws.extra["head"], ws.extra["branch"] = rev, pr, mine["head"], mine["branch"]
        ws.extra["readme"] = rev / ctx.cfg.roadmap.active_dir / job.name / "README.md"
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        j = board.parse_job_id(job.id)
        pkg_reports = ws.package(ctx.cfg, job.name) / "reports"
        second = ""
        if j.round > 1:
            previous = ", ".join(f"`{pkg_reports / (p + '.md')}`" for p in job.inputs.get("previous", []))
            repair = job.inputs.get("repair", "")
            second = (f"**This is round {j.round}.** The earlier passes are {previous or 'in the reports folder'} and their "
                      f"repairs are described in `{pkg_reports / (repair + '.md')}`; the head you review carries those repairs. "
                      "First verify, finding by finding, that each earlier finding is repaired at this head (quote the repaired "
                      "passage; mark it repaired, partly repaired or not repaired, with the witness) and that no repair "
                      "introduced a new error; then run the full pass again, with fresh attention to the classes the earlier "
                      "passes did not find. Number new findings from S01.")
        else:
            second = ("This is a freshly ported roadmap: expect port-level defects (supplier names that do not match the "
                      "siblings' current heads, missing Lean mirrors, locators copied from the package) as well as "
                      "mathematical ones.")
        outside = (f"git -C {ws.workdir} diff --stat origin/{ctx.cfg.project.branch}...HEAD -- . "
                   f"':!{ctx.cfg.roadmap.active_dir}/{job.name}'")
        return {"PR": str(ws.extra["pr"]), "HEAD": ws.extra["head"][:7], "PASS": f"{j.pass_} ({ctx.agent})",
                "SECOND_PASS": second, "OUTSIDE_CHECK": outside}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        if ws.report.is_file():
            j = board.parse_job_id(job.id)
            problems += report_problems(ws.report.read_text(encoding="utf-8"), ws.extra["pr"], job.name,
                                        ws.extra["head"][:7], f"{j.pass_} ({ctx.agent})")
        if gitops.git(ws.workdir, "status", "--short").strip():
            problems.append("the red-team pass changed the read-only checkout")
        return problems

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        return f"red-team pass {job.id} on #{ws.extra['pr']} at {ws.extra['head'][:7]}"
