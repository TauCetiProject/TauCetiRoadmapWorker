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

import shutil

from .. import board, gates, gitops, replies
from ..board import Job
from .base import (Context, Lane, LaneError, Workspace, board_reports, checkout_board_pr, count, dropped_names,
                   ensure_in_order, load_order, open_roadmap_prs, order_path, scan_problems, sibling_checkouts,
                   sibling_readmes)
from .package import elaboration_problems, package_scan
from .pr import fresh_worktree, pr_branch_worktree


class ArtefactFixLane(Lane):
    kind = "fix"
    prompt = "fix"

    def art(self, job: Job) -> str:
        return board.parse_job_id(job.id).subject

    def editable(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        pkg = ws.package(ctx.cfg, job.name)
        names = (["design/README.md", "design/plan.json", "sources.toml"] if self.art(job) == "DESIGN"
                 else ["README.md", "Suggested.lean", "metadata.toml", "sources.toml"])
        return [str(pkg / n) for n in names]

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        if self.board_pr(job):
            checkout_board_pr(ctx, ws, self.board_pr(job))   # repair the pull request's head, push to its branch
            ws.extra["reviews"] = board_reports(ctx, ws, job.name, [job.inputs.get("review", "")])
        if self.art(job) == "DESIGN":
            ws.extra["readme"] = ws.package(ctx.cfg, job.name) / "design" / "README.md"
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        pkg = ws.package(ctx.cfg, job.name)
        review = job.inputs.get("review", "")
        verdict = pkg / ("design/review.json" if self.art(job) == "DESIGN" else "review.json")
        what = "design" if self.art(job) == "DESIGN" else "package"
        if ws.extra.get("reviews"):
            return {"REVIEWS": "\n".join(f"- `{p}` (the review comments of `{review}`, quoted as data)"
                                          for p in ws.extra["reviews"]),
                    "FILES": "\n".join(f"- `{f}`" for f in self.editable(job, ctx, ws)),
                    "CONTEXT": f"the {what}, round {job.round}, on the board's pull request #{self.board_pr(job)}",
                    "CHECK": "- The dependency check (command in the section on suppliers below)." if what == "design"
                    else f"- Elaborate `Suggested.lean`: {self.common_values(job, ctx, ws)['ELABORATE']}.",
                    "COMMIT": "Do not commit: the worker commits your changes after its checks and pushes them "
                              "to the pull request."}
        check = ["- The dependency check (command in the section on suppliers below)."]
        if what == "package":
            check.insert(0, f"- Elaborate `Suggested.lean`: {self.common_values(job, ctx, ws)['ELABORATE']}.")
        return {"REVIEWS": f"- `{pkg / 'reports' / (review + '.md')}` (verdict in `{verdict}`)",
                "FILES": "\n".join(f"- `{f}`" for f in self.editable(job, ctx, ws)),
                "CONTEXT": f"the {what}, round {job.round}, in the board's package folder",
                "CHECK": "\n".join(check), "COMMIT": "Do not commit: the worker commits your changes after its checks."}

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """`round 2 of the Name design repaired: 4 of 5 findings applied`."""
        try:
            rows = replies.parse_findings(ws.report.read_text(encoding="utf-8"))
        except OSError:
            rows = []
        applied = sum(1 for _f, how, _b in rows if how.lower().startswith("applied"))
        settled = sum(1 for _f, how, _b in rows if replies.NOT_ACTIONABLE.match(how.strip()))
        what = "design" if self.art(job) == "DESIGN" else "package"
        return (f"round {job.round} of the {job.name} {what} repaired: {applied} of {count(len(rows), 'finding')} applied"
                + (f", {settled} not actionable in this package" if settled else ""))

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

    # -- on the board pull request (the red team runs on the package before the port) --
    def files(self, job: Job, ctx: Context, ws: Workspace) -> list:
        pkg = ws.package(ctx.cfg, job.name)
        return [pkg / f for f in ("README.md", "Suggested.lean", "metadata.toml", "sources.toml")]

    def board_values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        elaborate = self.common_values(job, ctx, ws)["ELABORATE"]
        return {"REVIEWS": "\n".join(f"- `{p}`" for p in ws.extra["reviews"])
                + "\n\nThese are internal adversarial red-team reviews by different agents; treat them exactly like an "
                  "external review, including disputing findings whose witness does not hold.",
                "FILES": "\n".join(f"- `{p}`" for p in self.files(job, ctx, ws)),
                "CONTEXT": f"red-team round {job.round} on the board's pull request #{self.board_pr(job)}",
                "CHECK": f"- Elaborate `Suggested.lean`: {elaborate}.\n- The dependency check (command in the section "
                         "on suppliers below).",
                "COMMIT": "Do not commit: the worker builds the module, commits your changes after its checks and "
                          "pushes them to the pull request."}

    def board_check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        """The package's text gates, its elaboration, and the targeted build of the module in a fresh
        checkout of the roadmap repository with the package copied into its folder."""
        problems = Lane.check(self, job, ctx, ws)
        if ws.report.is_file() and not replies.parse_findings(ws.report.read_text(encoding="utf-8")):
            problems.append("report: no finding sections in the required heading style")
        pkg = ws.package(ctx.cfg, job.name)
        problems += package_scan(ctx, ws, job.name, pkg)
        if problems:
            return problems
        problems += elaboration_problems(ctx, ws, pkg / "Suggested.lean")
        cfg = ctx.cfg
        if cfg.gates.build and (ws.content / ".lake").is_dir():
            wt = fresh_worktree(ctx, ws.content, job.name, "build")
            folder = wt / cfg.roadmap.active_dir / job.name
            folder.mkdir(parents=True, exist_ok=True)
            for f in ("README.md", "Suggested.lean", "metadata.toml"):
                if (pkg / f).is_file():
                    shutil.copy2(pkg / f, folder / f)
            problems += gates.build_problems(wt, cfg.gates.build.format(module=f"{cfg.roadmap.active_dir}.{job.name}.Suggested"))
        return problems

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        if self.board_pr(job):
            ws = Lane.prepare(self, job, ctx)
            checkout_board_pr(ctx, ws, self.board_pr(job))
            prs = open_roadmap_prs(ctx)
            ws.extra.update(prs=prs, siblings=sibling_checkouts(ctx, prs, skip=job.name),
                            reviews=board_reports(ctx, ws, job.name, job.inputs.get("reviews", [])))
            pkg = ws.package(ctx.cfg, job.name)
            ws.extra["readme"] = pkg / "README.md"
            texts = [(pkg / f).read_text(encoding="utf-8") if (pkg / f).is_file() else "" for f in ("README.md", "Suggested.lean")]
            ws.extra["order_notes"] = ensure_in_order(ctx, ws, job.name, *texts)
            return ws
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
        if self.board_pr(job):
            return self.board_values(job, ctx, ws)
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
        if self.board_pr(job):
            return self.board_check(job, ctx, ws)
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
        if self.board_pr(job):
            return f"red-team round {job.round} repaired on #{self.board_pr(job)}"
        new = gitops.git(ws.workdir, "rev-parse", "HEAD").strip()
        if new == ws.extra["head"]:
            raise LaneError("the head did not move; nothing to push")
        if ctx.dry_run:
            return f"[dry-run] would push {new[:7]} to {ws.extra['branch']}"
        fork = gitops.ensure_fork(ctx.gh, ctx.cfg.repositories.content, ctx.operator)
        self.push(lambda: gitops.push(ws.workdir, fork, ws.extra["branch"],
                                      expected=ws.extra.get("push_base") or ws.extra["head"]))
        rows = replies.parse_findings(ws.report.read_text(encoding="utf-8"))
        applied = sum(1 for _, how, _ in rows if how.startswith("applied"))
        ctx.gh.write("POST", f"repos/{ctx.cfg.repositories.content}/issues/{ws.extra['pr']}/comments",
                     {"body": f"Red-team repairs at `{new[:7]}`: {applied} of {len(rows)} internal findings applied, "
                              f"the rest disputed with evidence. {replies.validation_sentence(new[:7], ws.extra['gate'])}"})
        return f"red-team round {job.round} repaired on #{ws.extra['pr']} at {new[:7]}"
