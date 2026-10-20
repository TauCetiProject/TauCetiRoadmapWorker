"""Refresh the "Merge order" paragraph of every open roadmap pull request (port of merge_order.py).

For each open pull request the port lane opened, the paragraph lists the chain of open roadmap
pull requests that must merge before it (the transitive closure of its downward suppliers, in
merge-order rank), then other authors' roadmap pull requests it depends on. It never carries a
position number. The paragraph sits between `<!-- merge-order -->` markers, so the refresh is
idempotent; a description read that looks like an API error is never overwritten.

Inputs that used to be files in /tmp are now in the project state directory:
`stacks.toml` (child = parent, for stacked pull requests), `external-deps.json`
({"<pr>": {"<Roadmap>": [<other pr>, "<author>"]}}) and `mutual.txt` ("<pr> <other pr>" lines).
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


def _external(state: Path) -> dict:
    try:
        return json.loads((state / "external-deps.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _mutual(state: Path) -> set[tuple[int, int]]:
    try:
        lines = (state / "mutual.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {(int(a), int(b)) for a, b in (line.split()[:2] for line in lines if len(line.split()) >= 2)
            if a.isdigit() and b.isdigit()}


def plan(prs: list[dict], readmes: dict[str, str], order: list[str], main_extended: set[str],
         stacks: dict[int, int], external: dict, mutual: set[tuple[int, int]]) -> dict[int, str]:
    """The paragraph for each open pull request (pure, given the inputs)."""
    edges = depgraph.supplier_edges(readmes, set(order), main_extended)
    name2pr = {p["name"]: p["pr"] for p in prs}
    pr2name = {v: k for k, v in name2pr.items()}
    out = {}
    for p in prs:
        chain = [(name2pr[n], n) for n in depgraph.merge_chain(p["name"], edges, order, set(name2pr))]
        parent = stacks.get(p["pr"])
        if parent in pr2name and parent not in [c for c, _ in chain]:
            chain.insert(0, (parent, pr2name[parent]))
        ext = external.get(str(p["pr"]), {})
        mut = {b for a, b in mutual if a == p["pr"]}
        ext_first = sorted(((v[0], k) for k, v in ext.items() if v and v[0] not in mut))
        ext_mut = sorted(((v[0], k) for k, v in ext.items() if v and v[0] in mut))
        out[p["pr"]] = depgraph.merge_order_paragraph(p["pr"], chain, ext_first, ext_mut)
    return out


def refresh(ctx, only: set[int] | None = None, listing: bool = False) -> list[str]:
    from .lanes.base import open_roadmap_prs, sibling_checkouts, sibling_readmes
    cfg, repo = ctx.cfg, ctx.cfg.repositories.content
    prs = open_roadmap_prs(ctx)
    sibling_checkouts(ctx, prs)
    readmes = sibling_readmes(ctx.state, cfg.roadmap.active_dir, prs)
    paragraphs = plan(prs, readmes, _order(ctx), set(cfg.roadmap.main_extended), _stacks(ctx.state),
                      _external(ctx.state), _mutual(ctx.state))
    lines = []
    for p in prs:
        if only and p["pr"] not in only:
            continue
        para = paragraphs[p["pr"]]
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
