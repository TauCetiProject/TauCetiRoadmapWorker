"""The `fix` lane: resolve a review's findings.

* `FIX-DESIGN-Name~r` / `FIX-PKG-Name~r`: round r of a design or package, edited in the board's
  package folder only, after the review of round r-1 did not accept it. A fresh independent review
  of round r follows.
* `FIX-RT-Name~r`: the repairs after red-team round r, on the pull request branch (only the branch
  owner can push there, so the job names its operator). Port of mk-redfix.sh / mk-redfix2.sh /
  mk-gfix.sh: the brief is the same, the reviews are the round's pass reports, and after the
  repairs the gate must pass before the branch is pushed. A housekeeping comment records the new
  head; it is not a reply to a human review.
"""

from __future__ import annotations

from .. import board, gates, gitops, replies
from ..board import Job
from .base import (Context, Lane, LaneError, Workspace, dropped_names, load_order, open_roadmap_prs, order_path,
                   scan_problems, sibling_checkouts, sibling_readmes)
from .package import elaboration_problems, package_scan
from .pr import pr_branch_worktree


class ArtefactFixLane(Lane):
    kind = "fix"
    prompt = "fix"
    edits_manifest = True

    def art(self, job: Job) -> str:
        return board.parse_job_id(job.id).subject

    def editable(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        pkg = ws.package(ctx.cfg, job.name)
        names = (["design/README.md", "design/plan.json", "sources.toml"] if self.art(job) == "DESIGN"
                 else ["README.md", "Suggested.lean", "metadata.toml", "sources.toml"])
        return [str(pkg / n) for n in names]

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        if self.art(job) == "DESIGN":
            ws.extra["readme"] = ws.package(ctx.cfg, job.name) / "design" / "README.md"
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        pkg = ws.package(ctx.cfg, job.name)
        review = job.inputs.get("review", "")
        verdict = pkg / ("design/review.json" if self.art(job) == "DESIGN" else "review.json")
        what = "design" if self.art(job) == "DESIGN" else "package"
        check = ["- The dependency check (command in the section on suppliers below)."]
        if what == "package":
            check.insert(0, f"- Elaborate `Suggested.lean`: {self.common_values(job, ctx, ws)['ELABORATE']}.")
        return {"REVIEWS": f"- `{pkg / 'reports' / (review + '.md')}` (verdict in `{verdict}`)",
                "FILES": "\n".join(f"- `{f}`" for f in self.editable(job, ctx, ws)),
                "CONTEXT": f"the {what}, round {job.round}, in the board's package folder",
                "CHECK": "\n".join(check), "COMMIT": "Do not commit: the worker commits your changes after its checks."}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        if ws.report.is_file() and not replies.parse_findings(ws.report.read_text(encoding="utf-8")):
            problems.append("report: no finding sections in the required heading style")
        if self.art(job) == "PKG":
            pkg = ws.package(ctx.cfg, job.name)
            problems += package_scan(ctx, ws, job.name, pkg)
            if not problems:
                problems += elaboration_problems(ctx, ws, pkg / "Suggested.lean")
        return problems


class RedTeamFixLane(Lane):
    kind = "fix"
    prompt = "fix"

    def records_author(self, job: Job) -> bool:
        return False

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        pr = int(job.inputs.get("pr") or 0)
        prs = open_roadmap_prs(ctx)
        mine = next((p for p in prs if p["pr"] == pr), None)
        if not mine:
            raise LaneError(f"pull request #{pr} is not an open roadmap pull request")
        ws.extra.update(prs=prs, siblings=sibling_checkouts(ctx, prs, skip=job.name), pr=pr, branch=mine["branch"])
        wt, head = pr_branch_worktree(ctx, ws.content, job.name, pr, mine["branch"])
        ws.workdir, ws.extra["head"] = wt, head
        ws.extra["readme"] = wt / ctx.cfg.roadmap.active_dir / job.name / "README.md"
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        cfg, folder = ctx.cfg, f"{ctx.cfg.roadmap.active_dir}/{job.name}"
        reports = ws.package(cfg, job.name) / "reports"
        reviews = "\n".join(f"- `{reports / (r + '.md')}`" for r in job.inputs.get("reviews", []))
        build = cfg.gates.build.format(module=f"{cfg.roadmap.active_dir}.{job.name}.Suggested") if cfg.gates.build else ""
        gate = ctx.cli("gate", str(ws.workdir), job.name, "--base", f"origin/{cfg.project.branch}")
        return {"REVIEWS": reviews + "\n\nThese are internal adversarial red-team reviews by different agents; treat "
                "them exactly like an external review, including disputing findings whose witness does not hold.",
                "FILES": f"- `{ws.workdir / folder / 'README.md'}`\n- `{ws.workdir / folder / 'Suggested.lean'}`",
                "CONTEXT": f"red-team round {job.round} on pull request #{ws.extra['pr']}, branch `{ws.extra['branch']}`",
                "CHECK": (f"    {build}\n\n" if build else "") + f"    {gate}",
                "COMMIT": (f"Then `git add {folder} && git commit -m \"{job.name}: red-team repairs (<your summary, at most "
                           "15 words>)\"` on the checked-out branch. Do not push.")}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        if ws.report.is_file() and not replies.parse_findings(ws.report.read_text(encoding="utf-8")):
            problems.append("report: no finding sections in the required heading style")
        if gitops.git(ws.workdir, "rev-parse", "HEAD").strip() == ws.extra["head"]:
            problems.append("no repair commit on the pull request branch")
        if gitops.git(ws.workdir, "status", "--short").strip():
            problems.append("uncommitted changes left in the worktree")
        if problems:
            return problems
        order = load_order(order_path(ctx.cfg, ws.board, ws.content))
        opts = gates.GateOptions.from_config(ctx.cfg, order=order or None, dropped=dropped_names(ctx.cfg),
                                             siblings=sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir, ws.extra["prs"]))
        result = gates.run_gate(ws.workdir, job.name, opts, base=f"origin/{ctx.cfg.project.branch}",
                                build_cmd=ctx.cfg.gates.build)
        ws.extra["gate"] = result
        return problems + scan_problems(result)

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        new = gitops.git(ws.workdir, "rev-parse", "HEAD").strip()
        if new == ws.extra["head"]:
            raise LaneError("the head did not move; nothing to push")
        if ctx.dry_run:
            return f"[dry-run] would push {new[:7]} to {ws.extra['branch']}"
        fork = gitops.ensure_fork(ctx.gh, ctx.cfg.repositories.content, ctx.operator)
        gitops.push(ws.workdir, fork, ws.extra["branch"])
        rows = replies.parse_findings(ws.report.read_text(encoding="utf-8"))
        applied = sum(1 for _, how, _ in rows if how.startswith("applied"))
        ctx.gh.write("POST", f"repos/{ctx.cfg.repositories.content}/issues/{ws.extra['pr']}/comments",
                     {"body": f"Red-team repairs at `{new[:7]}`: {applied} of {len(rows)} internal findings applied, "
                              f"the rest disputed with evidence. {replies.validation_sentence(new[:7], ws.extra['gate'])}"})
        return f"red-team round {job.round} repaired on #{ws.extra['pr']} at {new[:7]}"
