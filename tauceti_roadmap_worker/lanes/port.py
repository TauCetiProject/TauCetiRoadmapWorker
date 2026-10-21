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

from .. import depgraph, gates, gitops
from ..board import Job
from ..gh import GHError
from .base import (PR_MARKER, Context, Lane, LaneError, Workspace, dropped_names, ensure_in_order, load_order,
                   open_roadmap_prs, order_path, scan_problems, sibling_checkouts, sibling_readmes)
from .pr import fresh_worktree


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
            f"Check labels missing from the Lean: {c.get('check-labels-missing', 0)}; no `set_option` overrides "
            f"({c.get('set_option', 0)}); sorry'd data: {c.get('sorry-data', 0)}; the dependency gate shows "
            f"{c.get('depcheck', 'no')} problem(s) for this roadmap; the outside-folder diff has {c.get('outside-folder', 0)} path(s).")


def compose_body(name: str, synopsis: str, active_dir: str, deps: str, gate: gates.GateResult, agent: str,
                 model: str, job_id: str) -> str:
    marker = f"<!-- {PR_MARKER} " + json.dumps({"roadmap": name, "job": job_id}) + " -->"
    return "\n\n".join([
        synopsis,
        f"Roadmap folder `{active_dir}/{name}` (README, `Suggested.lean`, `metadata.toml`); the Lean states only "
        "real-bodied definitions and theorems statable against the pinned Mathlib and Tau Ceti; every target is in the README.",
        deps,
        validation_paragraph(gate),
        f"AI assistance: {agent} ({model}) ported the independently reviewed package; author-side red-team passes "
        "by different agents follow on this draft before it is marked ready for human review.",
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
        ws.extra["order_notes"] = ensure_in_order(ctx, ws, job.name, *texts)
        ws.extra["siblings"] = sibling_checkouts(ctx, prs, skip=job.name)
        wt = fresh_worktree(ctx, ws.content, job.name, "port")
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
        order = load_order(order_path(ctx.cfg, ws.board, ws.content))
        opts = gates.GateOptions.from_config(ctx.cfg, order=order or None, dropped=dropped_names(ctx.cfg),
                                             siblings=sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir, ws.extra["prs"]))
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
        deps = depgraph.dependency_paragraph(job.name, readme, {p["name"]: p["pr"] for p in prs}, order)
        body = compose_body(job.name, ws.extra["synopsis"], cfg.roadmap.active_dir, deps, ws.extra["gate"],
                            ctx.agent, ctx.model, job.id)
        title = f"{job.name}: roadmap"
        if ctx.dry_run:
            return f"[dry-run] would open a draft pull request {title!r}"
        fork = gitops.ensure_fork(ctx.gh, cfg.repositories.content, ctx.operator)
        self.push(lambda: gitops.push(ws.workdir, fork, ws.extra["branch"]))
        pr = gitops.open_pull_request(ctx.gh, cfg.repositories.content, f"{ctx.operator}:{ws.extra['branch']}",
                                      cfg.project.branch, title, body,
                                      draft=cfg.roadmap.open_as_draft and not cfg.redteam.ready_after)
        number = int(pr["number"])
        try:
            ctx.gh.write("POST", f"repos/{cfg.repositories.content}/issues/{number}/labels",
                         {"labels": [cfg.roadmap.pr_label, cfg.replies.awaiting_review]})
        except GHError:
            pass
        head7 = gitops.git(ws.workdir, "rev-parse", "--short=7", "HEAD").strip()
        with open(ws.report, "a", encoding="utf-8") as f:
            f.write(f"\n\nPull request: #{number} ({ws.extra['branch']} at {head7})\n")
        return f"ported {job.name}: draft pull request #{number}" + self.write_merge_order(
            ctx, {"pr": number, "name": job.name}, readme, body, prs, order)

    def write_merge_order(self, ctx: Context, new: dict, readme: str, body: str, prs: list[dict],
                          order: list[str]) -> str:
        """The new pull request's "Merge order" paragraph, by the renderer `merge-order` uses (its
        number is known only once it is open); '' when written, else why not."""
        from ..mergeorder import paragraph_for
        para = paragraph_for(ctx, new, readme, prs, order)
        if para is None:
            return f" (the open pull requests could not be read: no merge-order paragraph; run merge-order --pr {new['pr']})"
        if not para:
            return " (not in the merge order: no merge-order paragraph)"
        text = depgraph.apply_paragraph(body, para) or body.rstrip() + "\n\n" + para + "\n"
        try:
            ctx.gh.write("PATCH", f"repos/{ctx.cfg.repositories.content}/pulls/{new['pr']}", {"body": text})
        except GHError as exc:
            return f" (merge-order paragraph not written: {exc}; run merge-order --pr {new['pr']})"
        return ""


def pr_number_from_report(text: str) -> int:
    m = re.search(r"^Pull request: #(\d+)", text or "", re.M)
    return int(m.group(1)) if m else 0
