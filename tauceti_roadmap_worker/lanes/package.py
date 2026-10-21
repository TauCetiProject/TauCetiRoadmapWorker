"""The `package` lane: expand the accepted design into README.md, Suggested.lean, metadata.toml."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from .. import gates
from ..board import Job
from .base import Context, Lane, Workspace, count, dropped_names, load_order, order_path, scan_problems, sibling_readmes
from .design import manifest_problems

PACKAGE_FILES = ("README.md", "Suggested.lean", "metadata.toml", "sources.toml")


def package_scan(ctx: Context, ws: Workspace, name: str, pkg: Path) -> list[str]:
    """Every text gate on a package folder (no git, no build), plus the sources manifest."""
    missing = [f for f in PACKAGE_FILES if not (pkg / f).is_file()]
    if missing:
        return [f"missing {', '.join(missing)}"]
    order = load_order(order_path(ctx.cfg, ws.board, ws.content))
    opts = gates.GateOptions.from_config(ctx.cfg, order=order or None, dropped=dropped_names(ctx.cfg),
                                         siblings=sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir))
    result = gates.scan_folder(name, (pkg / "README.md").read_text(encoding="utf-8"),
                               (pkg / "Suggested.lean").read_text(encoding="utf-8"), opts)
    # The intake inserts a roadmap into the merge order when its design is accepted, so a package
    # missing from the order is a real problem.
    return (scan_problems(result) + manifest_problems(ctx, pkg / "sources.toml")
            + gap_reference_problems((pkg / "README.md").read_text(encoding="utf-8"), pkg / "design" / "plan.json"))


GAP_REF = re.compile(r"\bgap:([A-Za-z0-9][\w.-]*)")


def gap_reference_problems(readme: str, plan_path: Path) -> list[str]:
    """Every `gap:<slug>` the README names is declared in the plan's `gaps` (when there is a plan)."""
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    declared = {str(g.get("slug") or g.get("id")) for g in plan.get("gaps", []) if isinstance(g, dict)} \
        if isinstance(plan, dict) else set()
    named = {m.rstrip(".") for m in GAP_REF.findall(readme)}
    return [f"README names gap:{slug}, which design/plan.json `gaps` does not declare (add the entry there, with "
            "`needs`, `supplier` and `section`)" for slug in sorted(named - declared)]


NOT_ELABORATED = "Suggested.lean: not elaborated — "


def elaboration_problems(ctx: Context, ws: Workspace, lean: Path, where: Path | None = None) -> list[str]:
    """Elaborate Suggested.lean (`[gates] elaborate`) in the content checkout, wired to the machine's
    shared Lake packages by the lane's prepare step (or in `where`, a worktree wired the same way).
    When the elaboration cannot run — no command configured by the operator is the one silent case —
    the problem says why, so it reaches the report and the repair pass instead of being skipped."""
    cfg = ctx.cfg
    if not cfg.gates.elaborate:
        return []
    setup = ws.extra.get("lake")
    if setup is None or setup.reason:
        problems = [NOT_ELABORATED + (setup.reason if setup is not None else "the Lake packages were not wired for this lane")]
    else:
        raw = gates.build_problems(where or ws.content, cfg.gates.elaborate.format(file=str(lean.resolve())))
        problems = [NOT_ELABORATED + p.removeprefix("build did not run: ") if p.startswith("build did not run: ")
                    else "Suggested.lean: elaboration exited" + p.removeprefix("build exited") if p.startswith("build exited")
                    else p for p in raw]
    if not problems:
        notes = ws.extra.setdefault("notes", [])          # the round log shows that it happened; a problem shows itself
        if "Suggested.lean elaborated: sorry the only warning" not in notes:
            notes.append("Suggested.lean elaborated: sorry the only warning")
    return problems


def package_summary(name: str, pkg: Path, what: str = "package") -> str:
    """`package of Name: 38 targets, 52 Lean declarations, 9 sources`."""
    try:
        readme = (pkg / "README.md").read_text(encoding="utf-8")
        lean = (pkg / "Suggested.lean").read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        manifest = tomllib.loads((pkg / "sources.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        manifest = {}
    return (f"{what} of {name}: {count(len(gates.readme_targets(readme)), 'target')}, "
            f"{count(len(gates.lean_names(lean)), 'Lean declaration')}, {count(len(manifest.get('source', [])), 'source')}")


class PackageLane(Lane):
    kind = "package"
    prompt = "package"
    keeps_wip = True

    def elaborates(self, job: Job) -> bool:
        return True

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        pkg = ws.package(ctx.cfg, job.name)
        problems += package_scan(ctx, ws, job.name, pkg)
        if not problems:
            problems += elaboration_problems(ctx, ws, pkg / "Suggested.lean")
        return problems

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        return package_summary(job.name, ws.package(ctx.cfg, job.name))
