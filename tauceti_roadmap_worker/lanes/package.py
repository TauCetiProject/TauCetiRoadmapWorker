"""The `package` lane: expand the accepted design into README.md, Suggested.lean, metadata.toml."""

from __future__ import annotations

import json
import re
import shutil
import time
import tomllib
from pathlib import Path

from .. import gates
from ..board import Job
from .base import Context, Lane, Workspace, count, dropped_names, load_order, order_path, scan_problems, sibling_readmes
from .lake import MIN_FREE_GB_BUILD, free_gb, pin_lock
from .design import manifest_problems

PACKAGE_FILES = ("README.md", "Suggested.lean", "metadata.toml", "sources.toml")


def package_scan(ctx: Context, ws: Workspace, name: str, pkg: Path) -> list[str]:
    """Every text gate on a package folder (no git, no build), plus the sources manifest."""
    missing = [f for f in PACKAGE_FILES if not (pkg / f).is_file()]
    if missing:
        return [f"missing {', '.join(missing)}"]
    order = load_order(order_path(ctx.cfg, ws.board, ws.content))
    opts = gates.GateOptions.from_config(ctx.cfg, order=order or None, dropped=dropped_names(ctx.cfg),
                                         siblings=sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir),
                                         tauceti=gates.tauceti_sources(ws.content))
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
ELABORATE_NOTE = "Elaborate Suggested.lean: "
NO_BUILD_FOR_TAUCETI = ("the `TauCeti.*` imports need the targeted build (`[gates] build`), which is not configured; "
                        "`lake env lean` finds no object file for them")
BUILT_TAUCETI = re.compile(r"\bBuilt TauCeti\.")
TARGETED_BUILD_TIMEOUT = gates.BUILD_TIMEOUT


def elaboration_problems(ctx: Context, ws: Workspace, lean: Path, where: Path | None = None,
                         name: str = "") -> list[str]:
    """Elaborate Suggested.lean in the content checkout, wired to the machine's shared Lake packages by
    the lane's prepare step (or in `where`, a worktree wired the same way). A file that imports only
    Mathlib is elaborated with `[gates] elaborate` (`lake env lean`, fast: Mathlib's object files are
    in the shared packages). A file that imports `TauCeti.*` modules goes through the targeted build
    of its module (`[gates] build`): the shared packages hold Tau Ceti's sources without object
    files, and the build makes them, once per machine, for every worker. When the elaboration cannot
    run — no command configured by the operator is the one silent case — the problem says why, so
    it reaches the report and the repair pass instead of being skipped. The way taken is a note:
    `Elaborate Suggested.lean: lake env lean …` or `… targeted build … (built N Tau Ceti modules, M s)`."""
    cfg = ctx.cfg
    if not cfg.gates.elaborate:
        return []
    setup = ws.extra.get("lake")
    how = ""
    if setup is None or setup.reason:
        problems = [NOT_ELABORATED + (setup.reason if setup is not None else "the Lake packages were not wired for this lane")]
    else:
        where = where or ws.content
        try:
            text = lean.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = "import Mathlib\n"        # a missing file is the package scan's finding, not an import's
        modules = gates.tauceti_imports(text)
        if modules and cfg.gates.build:
            raw, how = targeted_build(ctx, ws, lean, where, name or lean.parent.name)
        else:
            command = cfg.gates.elaborate.format(file=str(lean.resolve()))
            raw, how = gates.build_problems(where, command), f"{command} in {where}"
            if modules and any("object file" in p for p in raw):
                raw = raw + [NO_BUILD_FOR_TAUCETI]
        problems = [NOT_ELABORATED + p.removeprefix("build did not run: ") if p.startswith("build did not run: ")
                    else "Suggested.lean: elaboration exited" + p.removeprefix("build exited") if p.startswith("build exited")
                    else p for p in gates.explain_build_problems(text, raw, gates.tauceti_sources(where))]
    notes = ws.extra.setdefault("notes", [])          # the round log shows what happened; a problem shows itself
    if how:
        notes[:] = [n for n in notes if not n.startswith(ELABORATE_NOTE)] + [ELABORATE_NOTE + how]
    if not problems and "Suggested.lean elaborated: sorry the only warning" not in notes:
        notes.append("Suggested.lean elaborated: sorry the only warning")
    return problems


def place_module(lean: Path, target: Path):
    """Put the package's Suggested.lean at the module's path in the checkout (nothing to do when it is
    already that file). Returns the function that undoes it: the folder goes when it was created, the
    earlier file (a roadmap of main by the same name) comes back when it was overwritten."""
    if lean.resolve() == target.resolve():
        return lambda: None
    folder = target.parent
    existed, previous = folder.is_dir(), target.read_bytes() if target.is_file() else None
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(lean, target)

    def undo() -> None:
        if not existed:
            shutil.rmtree(folder, ignore_errors=True)
        elif previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous)
    return undo


def targeted_build(ctx: Context, ws: Workspace, lean: Path, where: Path, name: str) -> tuple[list[str], str]:
    """The targeted build of `<active_dir>.<Name>.Suggested` in `where` (the content checkout, or a
    worktree that already holds the module), with the package's Suggested.lean synced to the module's
    path for the build's duration, under the pin's lock (one build into the shared packages at a
    time), the disk guard and a timeout. Returns (problems, how it went)."""
    cfg = ctx.cfg
    command = cfg.gates.build.format(module=f"{cfg.roadmap.active_dir}.{name}.Suggested")
    free = free_gb(ctx.state)
    if free is not None and free < MIN_FREE_GB_BUILD:
        return [NOT_ELABORATED + f"only {free:.0f} GB free on the state directory's disk; the targeted build that a "
                f"`TauCeti.*` import needs wants {MIN_FREE_GB_BUILD} GB"], f"targeted build `{command}` not started"
    undo = place_module(lean, where / cfg.roadmap.active_dir / name / "Suggested.lean")
    started = time.monotonic()
    try:
        with pin_lock(ws.extra["lake"].packages):
            text, code, failure = gates.build_output(where, command, timeout=TARGETED_BUILD_TIMEOUT)
    finally:
        undo()
    seconds = time.monotonic() - started
    problems = [failure] if failure else gates.build_output_problems(text, code)
    return problems, (f"targeted build `{command}` in {where} (built {count(len(BUILT_TAUCETI.findall(text)), 'Tau Ceti module')}, "
                      f"{seconds:.0f} s)")


def build_tauceti_imports(ctx: Context, ws: Workspace, name: str) -> None:
    """Before an agent that elaborates with `lake env lean` itself (the reviewer): the `TauCeti.*`
    modules the package's Suggested.lean imports get their object files through the targeted build,
    so that the agent's command finds them. What the build finds in the file is the agent's to report."""
    lean = ws.package(ctx.cfg, name) / "Suggested.lean"
    try:
        text = lean.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if ctx.cfg.gates.build and ctx.cfg.gates.elaborate and gates.tauceti_imports(text) and not ctx.dry_run:
        elaboration_problems(ctx, ws, lean, name=name)


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
