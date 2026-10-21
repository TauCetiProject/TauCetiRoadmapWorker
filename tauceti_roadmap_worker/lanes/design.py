"""The `design` lane: one agent writes the roadmap plan (design README, plan.json, sources.toml).

The design lane is the only expensive one, so it is capped per operator per day
(`[design] daily_cap`), counted in a ledger in the project state directory shared by every worker
of the operator on this host.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tomllib
from pathlib import Path

from .. import board, depgraph, gates, sources
from ..board import Job
from .base import Context, Lane, Workspace, count, load_order, order_path, sibling_readmes

PREREQ = re.compile(r"^(?:mathlib|tauceti):[A-Za-z_][\w.'!?]*$|^roadmap:[A-Z][A-Za-z0-9]*(?:\s.*)?$|^target:[\w.]+$"
                    r"|^gap:[A-Za-z0-9][\w.-]*$")
DEF_KINDS = ("definition", "construction")


def validate_plan(data: dict, name: str) -> list[str]:
    """Mechanical checks of plan.json; the mathematics is the reviewer's."""
    problems = []
    if data.get("roadmap") != name:
        problems.append(f"plan.json: roadmap must be {name!r}")
    layers = data.get("layers")
    if not isinstance(layers, list) or not layers:
        return problems + ["plan.json: no layers"]
    ids = set()
    gaps = [g for g in data.get("gaps", []) if isinstance(g, dict)]
    gaps_for = {n for g in gaps for n in g.get("neededBy", [])}
    by_slug = {str(g.get("slug") or g.get("id") or ""): g for g in gaps}
    named = set()
    for layer in layers:
        for t in layer.get("targets", []) if isinstance(layer, dict) else []:
            tid = t.get("id", "?")
            if tid in ids:
                problems.append(f"plan.json: duplicate target id {tid}")
            ids.add(tid)
            for key in ("kind", "statement"):
                if not t.get(key):
                    problems.append(f"plan.json: target {tid} has no {key}")
            if t.get("kind") in DEF_KINDS and len(t.get("tests", [])) < 3:
                problems.append(f"plan.json: definition {tid} has fewer than three tests")
            loc = t.get("locator") or {}
            if not (loc.get("source") and loc.get("where")) and tid not in gaps_for:
                problems.append(f"plan.json: target {tid} has no locator and no recorded gap")
            for p in t.get("prerequisites", []):
                if not PREREQ.match(str(p)):
                    problems.append(f"plan.json: target {tid}: prerequisite {p!r} is not mathlib:/tauceti:/roadmap:/"
                                    "target:/gap:")
                elif str(p).startswith("gap:"):
                    if str(p)[4:] not in by_slug:
                        problems.append(f"plan.json: target {tid}: {p} names no entry of `gaps`")
                    named.add(str(p)[4:])
    for layer in layers:
        for t in layer.get("targets", []) if isinstance(layer, dict) else []:
            for p in t.get("prerequisites", []):
                if str(p).startswith("target:") and str(p)[7:] not in ids:
                    problems.append(f"plan.json: target {t.get('id')}: unknown {p}")
    for slug in sorted(named & set(by_slug)):
        problems += [f"plan.json: gap {slug}: {p}" for p in supplier_gap_problems(by_slug[slug])]
    return problems


def supplier_gap_problems(gap: dict) -> list[str]:
    """A gap a prerequisite names (`gap:<slug>`) states what is missing: `needs` (one sentence),
    `supplier` (the roadmap that must provide it, or "none") and, for a supplier, `section` (its
    section or target)."""
    problems = []
    if not str(gap.get("needs", "")).strip():
        problems.append("`needs` (one sentence: what is missing) is required")
    supplier = str(gap.get("supplier", "")).strip()
    if not supplier or (supplier != "none" and not re.fullmatch(board.NAME_RE, supplier)):
        problems.append("`supplier` must name a roadmap (CamelCase) or be \"none\"")
    elif supplier != "none" and not str(gap.get("section") or gap.get("target") or "").strip():
        problems.append("`section` (the supplier's section or target that must provide it) is required")
    return problems


def supplier_gaps(plan: dict) -> list[dict]:
    """The gaps of a plan that a named supplier must close."""
    return [g for g in plan.get("gaps", []) if isinstance(g, dict) and str(g.get("supplier", "none")) not in ("", "none")
            and (g.get("slug") or g.get("id"))]


def design_summary(name: str, plan: dict, readme: str = "") -> str:
    """`design of Name: 6 layers, 41 targets, 27 tests, 5 gaps (5 for suppliers)`: targets counted in
    the design README as the package lane counts them (`gates.readme_targets`), else in the plan."""
    layers = [layer for layer in plan.get("layers", []) if isinstance(layer, dict)]
    targets = [t for layer in layers for t in layer.get("targets", []) if isinstance(t, dict)]
    tests = sum(len(t.get("tests") or []) for t in targets)
    gaps = [g for g in plan.get("gaps", []) if isinstance(g, dict)]
    owed = len(supplier_gaps(plan))
    found = len(gates.readme_targets(readme)) or len(targets)
    return (f"design of {name}: {count(len(layers), 'layer')}, {count(found, 'target')}, "
            f"{count(tests, 'test')}, {count(len(gaps), 'gap')}" + (f" ({owed} for suppliers)" if owed else ""))


def gaps_listing(plan: dict) -> str:
    """The gaps a plan declares, as the review briefs list them: slug, supplier and section, needs."""
    rows = []
    for g in plan.get("gaps", []):
        if not isinstance(g, dict):
            continue
        slug, supplier = str(g.get("slug") or g.get("id") or "?"), str(g.get("supplier") or "none")
        where = str(g.get("section") or g.get("target") or "")
        rows.append(f"`gap:{slug}`: supplier `{supplier}`" + (f" {where}" if where and supplier != "none" else "")
                    + f"; needs: {' '.join(str(g.get('needs') or g.get('detail') or '(not stated)').split())}")
    return "\n" + "\n".join(f"- {r}" for r in rows) if rows else "none declared"


def ledger_path(state: Path) -> Path:
    return state / "design-ledger.jsonl"


def designs_today(state: Path, operator: str, today: str | None = None) -> int:
    today = today or dt.date.today().isoformat()
    try:
        lines = ledger_path(state).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        count += entry.get("operator", "").lower() == operator.lower() and entry.get("date") == today
    return count


def record_design(state: Path, operator: str, job_id: str) -> None:
    ledger_path(state).parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path(state), "a", encoding="utf-8") as f:
        f.write(json.dumps({"operator": operator, "job": job_id, "date": dt.date.today().isoformat()}) + "\n")


class DesignLane(Lane):
    kind = "design"
    prompt = "design"

    def eligible(self, job: Job, ctx: Context) -> str:
        reason = super().eligible(job, ctx)
        if reason:
            return reason
        cap = ctx.cfg.design.daily_cap
        if designs_today(ctx.state, ctx.operator) >= cap:
            return f"daily design cap reached ({cap} per operator per day)"
        return ""

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        ws.extra["readme"] = ws.package(ctx.cfg, job.name) / "design" / "README.md"
        # A triage that renamed the roadmap left its sources (and the answers to its references
        # requests) in the target's folder: the design starts from them and submits them as its own.
        target = job.inputs.get("target", "")
        origin = board.parse_job_id(target).name if target else job.name
        mine, theirs = ws.package(ctx.cfg, job.name) / "sources.toml", ws.package(ctx.cfg, origin) / "sources.toml"
        if origin != job.name and theirs.is_file() and not mine.exists():
            mine.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(theirs, mine)
        if not ctx.dry_run:
            record_design(ctx.state, ctx.operator, job.id)
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        from .target import absorbed_text
        target = job.inputs.get("target", "")
        name = board.parse_job_id(target).name if target else job.name
        return {"TARGET_FILE": str(ws.package(ctx.cfg, name) / "target.md"),
                "ABSORBED": absorbed_text(job, ws.board, ctx.cfg.board.packages_dir)}

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        design = ws.package(ctx.cfg, job.name) / "design"
        try:
            plan = json.loads((design / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        readme = (design / "README.md").read_text(encoding="utf-8") if (design / "README.md").is_file() else ""
        return design_summary(job.name, plan, readme) if isinstance(plan, dict) else ""

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        pkg = ws.package(ctx.cfg, job.name)
        readme, plan, manifest = pkg / "design" / "README.md", pkg / "design" / "plan.json", pkg / "sources.toml"
        for path in (readme, plan, manifest):
            if not path.is_file():
                problems.append(f"missing {path.relative_to(ws.board)}")
        if problems:
            return problems
        try:
            problems += validate_plan(json.loads(plan.read_text(encoding="utf-8")), job.name)
        except ValueError as exc:
            problems.append(f"plan.json: {exc}")
        problems += manifest_problems(ctx, manifest)
        text = readme.read_text(encoding="utf-8")
        for key, hits in (("residue", gates.scan_process_residue(text)), ("latex", gates.scan_latex(text)),
                          ("prnum", gates.scan_pr_numbers(text)), ("private-paths", gates.scan_private_paths(text)),
                          ("layer-ids", gates.scan_layer_ids(text))):
            problems += [f"design README {key}: line {h.line}: {h.text}" for h in hits[:5]]
        order = load_order(order_path(ctx.cfg, ws.board, ws.content))
        if order and ctx.cfg.gates.require_depcheck:
            readmes = {**sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir), job.name: text}
            edges = depgraph.supplier_edges(readmes, set(order), set(ctx.cfg.roadmap.main_extended))
            problems += [p for p in depgraph.check_order(edges, order, only={job.name}) if not p.startswith("NOT IN ORDER")]
        return problems


def manifest_problems(ctx: Context, manifest: Path) -> list[str]:
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [f"sources.toml: {exc}"]
    s = ctx.cfg.sources
    return [f"sources.toml: {p}" for p in sources.validate_manifest(data, s.allow, s.deny, s.uncertified_ok)]
