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
from .base import Context, Lane, Workspace, load_order, order_path, sibling_readmes

PREREQ = re.compile(r"^(?:mathlib|tauceti):[A-Za-z_][\w.'!?]*$|^roadmap:[A-Z][A-Za-z0-9]*(?:\s.*)?$|^target:[\w.]+$")
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
    gaps_for = {n for g in data.get("gaps", []) if isinstance(g, dict) for n in g.get("neededBy", [])}
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
                    problems.append(f"plan.json: target {tid}: prerequisite {p!r} is not mathlib:/tauceti:/roadmap:/target:")
    for layer in layers:
        for t in layer.get("targets", []) if isinstance(layer, dict) else []:
            for p in t.get("prerequisites", []):
                if str(p).startswith("target:") and str(p)[7:] not in ids:
                    problems.append(f"plan.json: target {t.get('id')}: unknown {p}")
    return problems


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
        target = job.inputs.get("target", "")
        name = board.parse_job_id(target).name if target else job.name
        return {"TARGET_FILE": str(ws.package(ctx.cfg, name) / "target.md")}

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
                          ("prnum", gates.scan_pr_numbers(text)), ("private-paths", gates.scan_private_paths(text))):
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
