"""The "Merge order" paragraph of every open roadmap pull request (port of merge_order.py and
ext_deps.py).

For each open pull request the port lane opened, the paragraph lists the chain of open roadmap
pull requests of ours that must merge before it (the transitive closure of its downward suppliers,
in merge-order rank), then other authors' open pull requests that add a roadmap it cites. It never
carries a position number. The paragraph sits between `<!-- merge-order -->` markers, so the
refresh is idempotent; a description read that looks like an API error is never overwritten. The
port lane writes the paragraph of the pull request it opens with the same renderer (`paragraph_for`).

Other authors' pull requests are read from the content repository: every open pull request the
port lane did not open that adds a roadmap folder not on main (neither in the active nor in the
completed folder), the ready one before drafts and then the lowest number when several add the
same folder. A roadmap counts when the README cites it as a supplier (the clauses `depgraph.units`
reads). Their changed files are cached by head in `other-prs.json`; pull request numbers in
`dropped-external.txt` never count (the citing roadmap localises what it needs instead). When the
pull requests cannot be read, the paragraph says nothing about other authors.

Inputs that used to be files in /tmp are in the project state directory: `stacks.toml` (child =
parent, for stacked pull requests), `external-deps.json` (dependencies the scan cannot see,
{"<pr>": {"<Roadmap>": [<other pr>, "<author>"]}}, or ext_deps.py's {"<pr>": {"name": ...,
"ext": {...}}}), `mutual.txt` ("<pr> <other pr>" lines: the two cite each other, so there is no
whole-pull-request order between them, and the stage-level graph between them is acyclic) and
`dropped-external.txt`.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from . import depgraph
from .gh import GHError


def _order(ctx) -> list[str]:
    cfg = ctx.cfg
    if cfg.roadmap.merge_order_repo == "local":
        try:
            return depgraph.read_order((ctx.state / cfg.roadmap.merge_order_file).read_text(encoding="utf-8"))
        except OSError:
            return []
    repo = cfg.repositories.packages_repo() if cfg.roadmap.merge_order_repo == "packages" else cfg.repositories.content
    try:
        return depgraph.read_order(ctx.gh.raw_file(repo, cfg.roadmap.merge_order_file))
    except GHError:
        return []


def _stacks(state: Path) -> dict[int, int]:
    try:
        data = tomllib.loads((state / "stacks.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {int(k): int(v) for k, v in data.items() if str(k).isdigit() and str(v).isdigit()}


def _external(state: Path) -> dict[str, dict[str, list]]:
    try:
        data = json.loads((state / "external-deps.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for pr, deps in (data.items() if isinstance(data, dict) else ()):
        deps = deps.get("ext", {}) if isinstance(deps, dict) and isinstance(deps.get("ext"), dict) else deps
        if isinstance(deps, dict):
            out[str(pr)] = {k: list(v) for k, v in deps.items() if isinstance(v, list) and v and str(v[0]).isdigit()}
    return out


def _numbers(path: Path) -> set[int]:
    try:
        return {int(w.lstrip("#")) for w in path.read_text(encoding="utf-8").split() if w.lstrip("#").isdigit()}
    except OSError:
        return set()


def on_main(ctx) -> set[str] | None:
    """The roadmap folders on the content repository's main (active and completed), or None when
    they cannot be read."""
    cfg, names = ctx.cfg, set()
    for folder in (cfg.roadmap.active_dir, cfg.roadmap.completed_dir):
        try:
            items = ctx.gh.api(f"repos/{cfg.repositories.content}/contents/{folder}?ref={cfg.project.branch}")
        except GHError as exc:
            if folder == cfg.roadmap.completed_dir and "404" in str(exc):
                continue
            return None
        names |= {i.get("name", "") for i in items if isinstance(i, dict) and i.get("type") == "dir"}
    return names


def added_folders(files: list[dict], active_dir: str) -> list[str]:
    return sorted({parts[1] for f in files if len(parts := str(f.get("filename", "")).split("/")) >= 3
                   and parts[0] == active_dir and parts[-1] == "README.md"})


def other_roadmap_prs(ctx, ours: set[int]) -> dict[str, tuple[int, str]] | None:
    """Roadmap -> (pull request, author) for the open pull requests not in `ours` that add a roadmap
    folder not on main, or None when the pull requests or main cannot be read."""
    cfg, repo = ctx.cfg, ctx.cfg.repositories.content
    main = on_main(ctx)
    if main is None:
        return None
    try:
        items = ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True)
    except GHError:
        return None
    path = ctx.state / "other-prs.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    dropped, seen, candidates = _numbers(ctx.state / "dropped-external.txt"), {}, {}
    for pr in items:
        number = int(pr["number"])
        if number in ours or number in dropped:
            continue
        head = pr.get("head", {}).get("sha", "")
        entry = cache.get(str(number)) if isinstance(cache, dict) else None
        if not isinstance(entry, dict) or entry.get("head") != head:
            try:
                files = ctx.gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True)
            except GHError:
                return None
            entry = {"head": head, "folders": added_folders(files, cfg.roadmap.active_dir)}
        seen[str(number)] = entry
        for folder in entry["folders"]:
            if folder not in main:
                candidates.setdefault(folder, []).append((bool(pr.get("draft")), number, pr.get("user", {}).get("login", "")))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass
    return {folder: (best[1], best[2]) for folder, found in candidates.items() for best in [min(found)]}


def cited(name: str, readme: str, others: dict[str, tuple[int, str]]) -> dict[str, list]:
    """The other authors' roadmaps a README cites as suppliers: roadmap -> [pull request, author]."""
    edges = depgraph.supplier_edges({name: readme}, set(others))
    return {s: list(others[s]) for s in edges.get(name, {}) if s in others}


def externals(ctx, prs: list[dict], readmes: dict[str, str]) -> dict[str, dict[str, list]]:
    """Other authors' pull requests each of `prs` depends on ({"<pr>": {roadmap: [pr, author]}}): the
    scan of the content repository plus `external-deps.json`. A pull request has no key when its
    dependencies are unknown (the scan failed and the file does not list it, or its README is
    missing)."""
    recorded = _external(ctx.state)
    others = other_roadmap_prs(ctx, {int(p["pr"]) for p in prs})
    if others is None:
        return recorded
    out = dict(recorded)
    for p in prs:
        if p["name"] in readmes:
            out[str(p["pr"])] = {**cited(p["name"], readmes[p["name"]], others), **recorded.get(str(p["pr"]), {})}
    return out


def _mutual(state: Path) -> set[tuple[int, int]]:
    try:
        lines = (state / "mutual.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {(int(a), int(b)) for a, b in (line.split()[:2] for line in lines if len(line.split()) >= 2)
            if a.isdigit() and b.isdigit()}


def plan(prs: list[dict], readmes: dict[str, str], order: list[str], main_extended: set[str],
         stacks: dict[int, int], external: dict, mutual: set[tuple[int, int]]) -> dict[int, str]:
    """The paragraph for each open pull request (pure, given the inputs). A pull request whose
    roadmap is not in the merge order, or whose README was not read, gets none."""
    edges = depgraph.supplier_edges(readmes, set(order), main_extended)
    name2pr = {p["name"]: p["pr"] for p in prs}
    pr2name = {v: k for k, v in name2pr.items()}
    out = {}
    for p in prs:
        if p["name"] not in order or p["name"] not in readmes:
            continue
        chain = [(name2pr[n], n) for n in depgraph.merge_chain(p["name"], edges, order, set(name2pr))]
        parent = stacks.get(p["pr"])
        if parent in pr2name and parent not in [c for c, _ in chain]:
            chain.insert(0, (parent, pr2name[parent]))
        mut = {b for a, b in mutual if a == p["pr"]}
        ext, ext_first, ext_mut = external.get(str(p["pr"])), None, []
        if ext is not None:
            rows = sorted((int(v[0]), k, str(v[1]) if len(v) > 1 else "") for k, v in ext.items() if v)
            ext_first = [r for r in rows if r[0] not in mut]
            ext_mut = [r for r in rows if r[0] in mut]
        out[p["pr"]] = depgraph.merge_order_paragraph(p["pr"], chain, ext_first, ext_mut)
    return out


def paragraph_for(ctx, new: dict, readme: str, prs: list[dict], order: list[str]) -> str:
    """The paragraph of a pull request the port lane just opened (`new` = {pr, name}), by the same
    renderer as `refresh`, or '' when its roadmap is not in the merge order."""
    from .lanes.base import sibling_readmes
    cfg = ctx.cfg
    everyone = [p for p in prs if p["pr"] != new["pr"] and p["name"] != new["name"]] + [new]
    readmes = {**sibling_readmes(ctx.state, cfg.roadmap.active_dir, everyone), new["name"]: readme}
    return plan(everyone, readmes, order, set(cfg.roadmap.main_extended), _stacks(ctx.state),
                externals(ctx, everyone, readmes), _mutual(ctx.state)).get(new["pr"], "")


def refresh(ctx, only: set[int] | None = None, listing: bool = False) -> list[str]:
    from .lanes.base import open_roadmap_prs, sibling_checkouts, sibling_readmes
    cfg, repo = ctx.cfg, ctx.cfg.repositories.content
    prs = open_roadmap_prs(ctx)
    sibling_checkouts(ctx, prs)
    readmes = sibling_readmes(ctx.state, cfg.roadmap.active_dir, prs)
    paragraphs = plan(prs, readmes, _order(ctx), set(cfg.roadmap.main_extended), _stacks(ctx.state),
                      externals(ctx, prs, readmes), _mutual(ctx.state))
    lines = []
    for p in prs:
        if only and p["pr"] not in only:
            continue
        para = paragraphs.get(p["pr"])
        if not para:
            why = "not in the merge order" if p["name"] in readmes else "its README could not be read"
            lines.append(f"#{p['pr']} {p['name']}: {why}; skipped")
            continue
        if listing:
            lines.append(f"#{p['pr']} {p['name']}: {para.splitlines()[1]}")
            continue
        try:
            body = (ctx.gh.api(f"repos/{repo}/pulls/{p['pr']}") or {}).get("body") or ""
        except GHError as exc:
            lines.append(f"#{p['pr']}: read failed ({exc}); skipped")
            continue
        new = depgraph.apply_paragraph(body, para)
        if new is None:
            lines.append(f"#{p['pr']}: the description read looks empty or like an API error; skipped")
        elif new == body:
            lines.append(f"#{p['pr']} {p['name']}: unchanged")
        else:
            ctx.gh.write("PATCH", f"repos/{repo}/pulls/{p['pr']}", {"body": new})
            lines.append(f"#{p['pr']} {p['name']}: merge order updated")
    return lines
