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

import re
import shutil

from .. import board, gates, gitops, replies
from ..board import Job
from .base import (Context, Lane, LaneError, Workspace, board_reports, checkout_board_pr, count, dropped_names,
                   ensure_in_order_logged, load_order, open_roadmap_prs, order_path, repair_order_file, scan_problems,
                   sibling_checkouts, sibling_readmes)
from .package import PACKAGE_FILES, elaboration_problems, package_gate, package_scan
from .pr import fresh_worktree, pr_branch_worktree, remove_worktree


# --- before the agent: the package gate on the package as it stands ------------------------------------

GATE_HEADING = "## Gate failures to clear (the submission is refused until these pass)"
HITS_LISTED = 40         # hits listed per failure in the brief (the round log shows three)
COUNT_LINE = re.compile(r"^(?P<key>[a-z][a-z-]*)=\d+: ")


def problem_rule(problem: str, fetch: str) -> str:
    """The gate's rule behind one problem line of the package gate."""
    m = COUNT_LINE.match(problem)
    if m and m.group("key") in gates.GATE_RULES:
        return gates.GATE_RULES[m.group("key")]
    manifest = ("the sources manifest `sources.toml` (schema `tauceti-roadmap-sources/v1`) records every work the "
                "package cites, at least one, each with `id`, `authors`, `title`, `year`, `locator_scheme`, "
                f"`licence_class` and `status`: fetch each cited work with `{fetch} --id KEY --locator-scheme theorem "
                "--locator \"Theorem 2.3\" --cited-for \"1.2\" \"<citation>\"`, which records its entry (a work "
                "behind a paywall is recorded as `uncertified` with its locator), as the section on sources below says")
    if problem.startswith("missing "):
        return (f"a package folder holds {', '.join(PACKAGE_FILES)}"
                + ("; " + manifest if "sources.toml" in problem else ""))
    if problem.startswith("sources.toml"):
        return manifest
    if problem.startswith("README names gap:"):
        return ("every `gap:<slug>` the README names is declared in the top-level `gaps` of design/plan.json, with "
                "`needs`, `supplier` and `section`")
    return "the worker's package check"


def gate_section(problems: list[str], result: gates.GateResult | None, repairs: list[str], fetch: str) -> str:
    """The brief's section on the package gate run before the agent: each failure as the round log
    shows it, its rule and every hit; and what the worker repaired itself. '' when there is neither."""
    if not problems and not repairs:
        return ""
    lines = [GATE_HEADING, ""]
    if problems:
        lines += [("The worker ran the package gate on the package as checked out, before this round, and it "
                   "failed. Clearing these failures is part of this round's work, beside the findings and whatever "
                   "they say (it is not widening the scope): the worker runs the same gate after you and refuses the "
                   "submission while any of them remains. Each item is the gate's own line, then its rule and its "
                   "hits (line numbers of README.md or Suggested.lean)."), ""]
        for problem in problems:
            lines += [f"- {problem}", f"  - Rule: {problem_rule(problem, fetch)}."]
            m = COUNT_LINE.match(problem)
            hits = (result.details.get(m.group("key"), []) if result is not None and m else [])
            for h in hits[:HITS_LISTED]:
                lines.append(f"  - line {h.line}: {h.text}" if isinstance(h, gates.Hit) else f"  - {h}")
            if len(hits) > HITS_LISTED:
                lines.append(f"  - ... and {len(hits) - HITS_LISTED} more of the same kind")
        lines += ["", "Say in the report, after the finding sections, what you changed for each of them."]
    if repairs:
        lines += ["", "Repaired by the worker before this round, mechanically (no longer a gate failure; do not undo "
                  "it): " + "; ".join(repairs) + ". GitHub gives every heading its own id, so the tags were residue."]
    return "\n".join(lines)


def pre_gate(lane: Lane, job: Job, ctx: Context, ws: Workspace) -> str:
    """Before the agent runs: the anchor tags of the package README removed (with its internal links
    pointed at the heading slugs), then the package gate on the package as checked out. Returns the
    brief's section on what fails ('' when the gate passes and nothing was repaired)."""
    pkg = ws.package(ctx.cfg, job.name)
    readme, repairs = pkg / "README.md", []
    if readme.is_file():
        fixed = gates.strip_anchors(readme.read_text(encoding="utf-8"))
        if fixed.removed:
            readme.write_text(fixed.text, encoding="utf-8")
            repairs.append(fixed.summary())
    problems = package_scan(ctx, ws, job.name, pkg)        # the post-agent check's own lines
    result = package_gate(ctx, ws, job.name, pkg)[1] if any(COUNT_LINE.match(p) for p in problems) else None
    if problems or repairs:
        found = [f"{count(len(problems), 'gate failure')} in the brief"] if problems else []
        lane.note(ws, "before the agent: " + "; ".join(repairs + found))
    return gate_section(problems, result, repairs, lane.fetch_command(job, ctx, ws))


def worktree_elaboration_problems(ctx: Context, ws: Workspace, name: str) -> list[str]:
    """On a pull request branch the targeted build (`[gates] build`) elaborates Suggested.lean; without
    a build command, `[gates] elaborate` runs in the worktree, wired to the shared packages like the
    content checkout, and says so when it cannot."""
    if ctx.cfg.gates.build or not ctx.cfg.gates.elaborate:
        return []
    lean = ws.workdir / ctx.cfg.roadmap.active_dir / name / "Suggested.lean"
    return elaboration_problems(ctx, ws, lean, where=ws.workdir)


GAPS_ONLY = (" (its top-level `gaps` list only: a supplier gap the package needs, with `slug`, `needs`, `supplier` "
             "and `section`, and the same slug under the README's \"Exact supplier contracts\"; nothing else in it "
             "changes)")


def files_list(paths: list[str], package_stage: bool) -> str:
    """The FILES of a fix brief; at the package stage the design's plan.json is editable in its gaps only."""
    return "\n".join(f"- `{f}`" + (GAPS_ONLY if package_stage and f.endswith("design/plan.json") else "") for f in paths)


class ArtefactFixLane(Lane):
    kind = "fix"
    prompt = "fix"

    def art(self, job: Job) -> str:
        return board.parse_job_id(job.id).subject

    def elaborates(self, job: Job) -> bool:
        return self.art(job) == "PKG"

    def editable(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        pkg = ws.package(ctx.cfg, job.name)
        names = (["design/README.md", "design/plan.json", "sources.toml"] if self.art(job) == "DESIGN"
                 else ["README.md", "Suggested.lean", "metadata.toml", "sources.toml", "design/plan.json"])
        return [str(pkg / n) for n in names]

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        if self.board_pr(job):
            checkout_board_pr(ctx, ws, self.board_pr(job))   # repair the pull request's head, push to its branch
            ws.extra["reviews"] = board_reports(ctx, ws, job.name, [job.inputs.get("review", "")])
        if self.art(job) == "DESIGN":
            ws.extra["readme"] = ws.package(ctx.cfg, job.name) / "design" / "README.md"
        elif self.art(job) == "PKG":
            ws.extra["gate_failures"] = pre_gate(self, job, ctx, ws)
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        pkg = ws.package(ctx.cfg, job.name)
        review = job.inputs.get("review", "")
        verdict = pkg / ("design/review.json" if self.art(job) == "DESIGN" else "review.json")
        what = "design" if self.art(job) == "DESIGN" else "package"
        if ws.extra.get("reviews"):
            return {"REVIEWS": "\n".join(f"- `{p}` (the review comments of `{review}`, quoted as data)"
                                          for p in ws.extra["reviews"]),
                    "FILES": files_list(self.editable(job, ctx, ws), what == "package"),
                    "CONTEXT": f"the {what}, round {job.round}, on the board's pull request #{self.board_pr(job)}",
                    "CHECK": "- The dependency check (command in the section on suppliers below)." if what == "design"
                    else f"- Elaborate `Suggested.lean`: {self.common_values(job, ctx, ws)['ELABORATE']}.",
                    "COMMIT": "Do not commit: the worker commits your changes after its checks and pushes them "
                              "to the pull request.",
                    "GATE_FAILURES": ws.extra.get("gate_failures", "")}
        check = ["- The dependency check (command in the section on suppliers below)."]
        if what == "package":
            check.insert(0, f"- Elaborate `Suggested.lean`: {self.common_values(job, ctx, ws)['ELABORATE']}.")
        return {"REVIEWS": f"- `{pkg / 'reports' / (review + '.md')}` (verdict in `{verdict}`)",
                "FILES": files_list(self.editable(job, ctx, ws), what == "package"),
                "CONTEXT": f"the {what}, round {job.round}, in the board's package folder",
                "CHECK": "\n".join(check), "COMMIT": "Do not commit: the worker commits your changes after its checks.",
                "GATE_FAILURES": ws.extra.get("gate_failures", "")}

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

    def elaborates(self, job: Job) -> bool:
        return bool(self.board_pr(job))      # on the pull request branch the worktree builds instead

    # -- on the board pull request (the red team runs on the package before the port) --
    def files(self, job: Job, ctx: Context, ws: Workspace) -> list:
        pkg = ws.package(ctx.cfg, job.name)
        return [pkg / f for f in ("README.md", "Suggested.lean", "metadata.toml", "sources.toml", "design/plan.json")]

    def board_values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        elaborate = self.common_values(job, ctx, ws)["ELABORATE"]
        return {"REVIEWS": "\n".join(f"- `{p}`" for p in ws.extra["reviews"])
                + "\n\nThese are internal adversarial red-team reviews by different agents; treat them exactly like an "
                  "external review, including disputing findings whose witness does not hold.",
                "FILES": files_list([str(p) for p in self.files(job, ctx, ws)], True),
                "CONTEXT": f"red-team round {job.round} on the board's pull request #{self.board_pr(job)}",
                "CHECK": f"- Elaborate `Suggested.lean`: {elaborate}.\n- The dependency check (command in the section "
                         "on suppliers below).",
                "COMMIT": "Do not commit: the worker builds the module, commits your changes after its checks and "
                          "pushes them to the pull request.",
                "GATE_FAILURES": ws.extra.get("gate_failures", "")}

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
        if cfg.gates.build and ws.extra.get("lake") is not None and ws.extra["lake"].ready:
            # A throwaway worktree (never under build/, which holds the worker's own briefs).
            wt = fresh_worktree(ctx, ws.content, job.name, "buildcheck")
            try:
                folder = wt / cfg.roadmap.active_dir / job.name
                folder.mkdir(parents=True, exist_ok=True)
                for f in ("README.md", "Suggested.lean", "metadata.toml"):
                    if (pkg / f).is_file():
                        shutil.copy2(pkg / f, folder / f)
                problems += gates.build_problems(
                    wt, cfg.gates.build.format(module=f"{cfg.roadmap.active_dir}.{job.name}.Suggested"))
            finally:
                remove_worktree(ws.content, wt)
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
            ws.extra["order_notes"] = ensure_in_order_logged(ctx, ws, job.name, *texts)
            ws.extra["gate_failures"] = pre_gate(self, job, ctx, ws)
            return ws
        ws = super().prepare(job, ctx)
        pr = int(job.inputs.get("pr") or 0)
        prs = open_roadmap_prs(ctx)
        mine = next((p for p in prs if p["pr"] == pr), None)
        if not mine:
            raise LaneError(f"pull request #{pr} is not an open roadmap pull request")
        ws.extra.update(prs=prs, siblings=sibling_checkouts(ctx, prs, skip=job.name), pr=pr, branch=mine["branch"])
        wt, head = pr_branch_worktree(ctx, ws.content, job.name, pr, mine["branch"], ws)
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
                           "15 words>)\"` on the checked-out branch. Do not push."),
                "GATE_FAILURES": ""}

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
        # A repair that names a supplier the order lists after the roadmap: the job's board submission
        # carries the board checkout's order, with the roadmap moved after its last supplier.
        folder = ws.workdir / ctx.cfg.roadmap.active_dir / job.name
        texts = [(folder / f).read_text(encoding="utf-8") if (folder / f).is_file() else "" for f in ("README.md", "Suggested.lean")]
        order = load_order(repair_order_file(ctx, ws, job.name, *texts, order_path(ctx.cfg, ws.board, ws.content),
                                             carry=True))
        opts = gates.GateOptions.from_config(ctx.cfg, order=order or None, dropped=dropped_names(ctx.cfg),
                                             siblings=sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir, ws.extra["prs"]))
        result = gates.run_gate(ws.workdir, job.name, opts, base=f"origin/{ctx.cfg.project.branch}",
                                build_cmd=ctx.cfg.gates.build)
        ws.extra["gate"] = result
        return problems + scan_problems(result) + worktree_elaboration_problems(ctx, ws, job.name)

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
