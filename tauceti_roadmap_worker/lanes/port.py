"""The `port` lane: copy the accepted package into the content repository on a fork branch, run the
gates, open the draft pull request with the dependency and merge-order paragraphs (the latter by the
renderer `merge-order` uses).

Port of the maintainer's mk-port.sh, mk-syn.sh and open-port-pr.sh: the brief is rendered from
prompts/port.md, the synopsis is taken from the report's "Draft pull request body" section, and the
description is composed here from computed facts (the gate result, the merge chain), so nothing in
it is asserted without having been checked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import depgraph, gates, gitops
from ..board import Job
from ..gh import GHError
from .base import (PR_MARKER, Context, Lane, LaneError, Workspace, count, dropped_names, ensure_in_order_logged,
                   load_order, merged_for, open_roadmap_prs, order_path, repair_order_file, scan_problems, sibling_checkouts,
                   sibling_readmes)
from .pr import fresh_worktree, release_branch


def extract_synopsis(report: str) -> str:
    """The first descriptive paragraph of the report's draft pull-request body (port of mk-syn.sh)."""
    m = (re.search(r"#+\s*Draft pull[- ]request body[^\n]*\n(.*?)(?=\n#+ |\Z)", report, re.S | re.I)
         or re.search(r"#+\s*Draft pull[- ]request[^\n]*\n(.*?)(?=\n#+ |\Z)", report, re.S | re.I))
    body = m.group(1) if m else ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    paras = [p for p in paras if not re.match(r"^(Title:|\*\*Title|Body:|Roadmap folder|Validation|AI )", p)]
    synopsis = paras[0].replace("\n", " ") if paras else ""
    if len(synopsis) <= 80:
        raise LaneError("the port report has no draft pull-request synopsis (over 80 characters)")
    return synopsis


def validation_paragraph(gate: gates.GateResult) -> str:
    c = gate.counts
    build = ("the targeted Lake build succeeds with `sorry` as the only warning" if c.get("build") == 0
             else "the targeted build was not run")
    return (f"Validation: {build}; README identifiers missing from the Lean: {c.get('readme-ids-missing', 0)}; "
            f"no `set_option` overrides ({c.get('set_option', 0)}); sorry'd data: {c.get('sorry-data', 0)}; "
            f"the dependency gate shows "
            f"{c.get('depcheck', 'no')} problem(s) for this roadmap; the outside-folder diff has {c.get('outside-folder', 0)} path(s).")


def model_names(cfg) -> str:
    """`Fable and Astra`: the review and red-team models, by their last name part."""
    models = [cfg.lane_models("review").get(a, "") for a in sorted(cfg.lane_models("review"))]
    models += [p.model for p in sorted(cfg.redteam.passes, key=lambda p: p.agent)]
    names = list(dict.fromkeys(m.rsplit("-", 1)[-1].capitalize() for m in models if m))
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1]


def rounds_on_board(reports: Path, name: str) -> tuple[int, int]:
    """(package review rounds, red-team rounds) whose reports the board holds for the roadmap."""
    def rounds(pattern: str) -> int:
        found = [re.match(pattern, p.name) for p in reports.glob("*.md")] if reports.is_dir() else []
        return max((int(m.group(1) or 1) for m in found if m), default=0)
    return (rounds(rf"^REV-PKG-{re.escape(name)}(?:~(\d+))?\.md$"), rounds(rf"^RT-{re.escape(name)}~(\d+)[a-z]\.md$"))


def assistance_paragraph(agent: str, model: str, red_teamed: tuple[int, int] | None, models: str = "") -> str:
    """Who ported the package and what it went through. `red_teamed` is (review rounds, red-team rounds)
    when the red team ran on the job board before the port, None for a port whose red team runs on
    this pull request (from before the board pull requests)."""
    if red_teamed is None:
        return (f"AI assistance: {agent} ({model}) ported the independently reviewed package; author-side red-team "
                "passes by different agents follow on this draft before it is marked ready for human review.")
    reviews, redteam = red_teamed
    return (f"AI assistance: {agent} ({model}) ported the package after {count(reviews, 'independent review round')} "
            f"and {count(redteam, 'red-team round')} on the job board" + (f" ({models})" if models else "")
            + "; this draft awaits human review.")


def compose_body(name: str, synopsis: str, active_dir: str, deps: str, gate: gates.GateResult, agent: str,
                 model: str, job_id: str, assistance: str = "") -> str:
    marker = f"<!-- {PR_MARKER} " + json.dumps({"roadmap": name, "job": job_id}) + " -->"
    return "\n\n".join([
        synopsis,
        f"Roadmap folder `{active_dir}/{name}` (README, `Suggested.lean`, `metadata.toml`); the Lean states only "
        "real-bodied definitions and theorems statable against the pinned Mathlib and Tau Ceti; every target is in the README.",
        deps,
        validation_paragraph(gate),
        assistance or assistance_paragraph(agent, model, None),
        marker,
    ]) + "\n"


class PortLane(Lane):
    kind = "port"
    prompt = "port"

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        prs = open_roadmap_prs(ctx)
        ws.extra["prs"] = prs
        pkg = ws.package(ctx.cfg, job.name)
        texts = [(pkg / f).read_text(encoding="utf-8") if (pkg / f).is_file() else "" for f in ("README.md", "Suggested.lean")]
        ws.extra["order_notes"] = ensure_in_order_logged(ctx, ws, job.name, *texts)
        ws.extra["siblings"] = sibling_checkouts(ctx, prs, skip=job.name)
        wt = fresh_worktree(ctx, ws.content, job.name, "port", ws)
        release_branch(ws.content, f"roadmap/{gitops.kebab(job.name)}", keep=wt)   # the agent creates it here
        ws.extra["base"] = gitops.git(wt, "rev-parse", "HEAD").strip()
        ws.extra["branch"] = f"roadmap/{gitops.kebab(job.name)}"
        ws.extra["readme"] = wt / ctx.cfg.roadmap.active_dir / job.name / "README.md"
        ws.workdir = wt
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        module = f"{ctx.cfg.roadmap.active_dir}.{job.name}.Suggested"
        build = ctx.cfg.gates.build.format(module=module) if ctx.cfg.gates.build else "(no build configured: skip)"
        return {"BASE": ws.extra["base"][:7], "BRANCH": ws.extra["branch"], "BUILD": build,
                "GATE": ctx.cli("gate", str(ws.workdir), job.name, "--base", f"origin/{ctx.cfg.project.branch}")}

    def gate(self, job: Job, ctx: Context, ws: Workspace) -> gates.GateResult:
        # The ported README may name a supplier the order lists after it: the port's submission
        # carries the board checkout's order, with the roadmap moved after its last supplier.
        folder = ws.workdir / ctx.cfg.roadmap.active_dir / job.name
        texts = [(folder / f).read_text(encoding="utf-8") if (folder / f).is_file() else "" for f in ("README.md", "Suggested.lean")]
        order = load_order(repair_order_file(ctx, ws, job.name, *texts, order_path(ctx.cfg, ws.board, ws.content),
                                             carry=True))
        opts = gates.GateOptions.from_config(ctx.cfg, order=order or None, dropped=dropped_names(ctx.cfg),
                                             siblings=sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir, ws.extra["prs"]),
                                             merged=merged_for(ctx, ws))
        return gates.run_gate(ws.workdir, job.name, opts, base=f"origin/{ctx.cfg.project.branch}",
                              build_cmd=ctx.cfg.gates.build)

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        if gitops.git(ws.workdir, "rev-parse", "HEAD").strip() == ws.extra["base"]:
            problems.append("no commit on the port branch")
        if problems:
            return problems
        result = self.gate(job, ctx, ws)
        ws.extra["gate"] = result
        (ws.job_dir / "gate.json").write_text(json.dumps(result.to_dict(), indent=1) + "\n", encoding="utf-8")
        problems += scan_problems(result)
        try:
            ws.extra["synopsis"] = extract_synopsis(ws.report.read_text(encoding="utf-8"))
        except LaneError as exc:
            problems.append(str(exc))
        return problems

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        cfg, prs = ctx.cfg, ws.extra["prs"]
        readme = (ws.workdir / cfg.roadmap.active_dir / job.name / "README.md").read_text(encoding="utf-8")
        order = load_order(order_path(cfg, ws.board, ws.content))
        from ..mergeorder import facts_for
        found = None if ctx.dry_run else facts_for(ctx, job.name, readme, prs, order)   # the chain, once
        deps = depgraph.dependency_paragraph(None if found is None else found.chain)
        red_teamed = (rounds_on_board(ws.package(cfg, job.name) / "reports", job.name)
                      if any(a.startswith("FIX-RT-") for a in job.after) else None)
        body = compose_body(job.name, ws.extra["synopsis"], cfg.roadmap.active_dir, deps, ws.extra["gate"],
                            ctx.agent, ctx.model, job.id,
                            assistance_paragraph(ctx.agent, ctx.model, red_teamed, model_names(cfg)))
        title = f"{job.name}: roadmap"
        if ctx.dry_run:
            return f"[dry-run] would open a draft pull request {title!r}"
        fork = self.push(lambda: gitops.ensure_fork(ctx.gh, cfg.repositories.content, ctx.operator))
        self.push(lambda: gitops.push(ws.workdir, fork, ws.extra["branch"]))
        pr = self.push(lambda: gitops.open_pull_request(
            ctx.gh, cfg.repositories.content, f"{ctx.operator}:{ws.extra['branch']}", cfg.project.branch, title, body,
            draft=cfg.roadmap.open_as_draft and not cfg.redteam.ready_after))
        number = int(pr["number"])
        # A pull request an earlier attempt opened (a retry, a lost answer) follows the push late.
        self.await_pr_head(ctx, cfg.repositories.content, number,
                           gitops.git(ws.workdir, "rev-parse", "HEAD").strip(),
                           seen=(pr.get("head") or {}).get("sha") or None, then="not labelling it")
        try:
            ctx.gh.write("POST", f"repos/{cfg.repositories.content}/issues/{number}/labels",
                         {"labels": [cfg.roadmap.pr_label, cfg.replies.awaiting_review]})
        except GHError:
            pass
        head7 = gitops.git(ws.workdir, "rev-parse", "--short=7", "HEAD").strip()
        with open(ws.report, "a", encoding="utf-8") as f:
            f.write(f"\n\nPull request: #{number} ({ws.extra['branch']} at {head7})\n")
        return f"ported {job.name}: draft pull request #{number}" + self.write_merge_order(
            ctx, number, job.name, body, found, order)

    def write_merge_order(self, ctx: Context, number: int, name: str, body: str, found, order: list[str]) -> str:
        """The new pull request's "Merge order" paragraph, rendered from the facts its Dependencies
        sentence came from (the renderer `merge-order` uses; the number is known only once the pull
        request is open); '' when written, else why not."""
        if found is None:
            return f" (the open pull requests could not be read: no merge-order paragraph; run merge-order --pr {number})"
        if name not in order:
            return " (not in the merge order: no merge-order paragraph)"
        para = found.paragraph(number)
        text = depgraph.apply_paragraph(body, para) or body.rstrip() + "\n\n" + para + "\n"
        try:
            ctx.gh.write("PATCH", f"repos/{ctx.cfg.repositories.content}/pulls/{number}", {"body": text})
        except GHError as exc:
            return f" (merge-order paragraph not written: {exc}; run merge-order --pr {number})"
        return ""


def pr_number_from_report(text: str) -> int:
    m = re.search(r"^Pull request: #(\d+)", text or "", re.M)
    return int(m.group(1)) if m else 0
