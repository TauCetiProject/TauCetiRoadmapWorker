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


def elaboration_problems(ctx: Context, ws: Workspace, lean: Path) -> list[str]:
    """Elaborate Suggested.lean in the content checkout's existing build, if there is one."""
    if not ctx.cfg.gates.elaborate or not (ws.content / ".lake").is_dir():
        return []
    return gates.build_problems(ws.content, ctx.cfg.gates.elaborate.format(file=str(lean.resolve())))


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

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        pkg = ws.package(ctx.cfg, job.name)
        problems += package_scan(ctx, ws, job.name, pkg)
        if not problems:
            problems += elaboration_problems(ctx, ws, pkg / "Suggested.lean")
        return problems

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        return package_summary(job.name, ws.package(ctx.cfg, job.name))
