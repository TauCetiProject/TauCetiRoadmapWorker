"""The "Merge order" paragraph of every open roadmap pull request (port of merge_order.py and
ext_deps.py).

For each open pull request the port lane opened, the paragraph lists the chain of open roadmap
pull requests of ours that must merge before it (the transitive closure of its downward suppliers,
in merge-order rank), then other authors' open pull requests that add a roadmap it cites. It never
carries a position number. The paragraph sits between `<!-- merge-order -->` markers, so the
refresh is idempotent; a description read that looks like an API error is never overwritten. The
port lane writes the paragraph of the pull request it opens with the same renderer (`paragraph_for`).

A pull request is ours when the port lane opened it (its marker), or when the merge order names a
roadmap folder it adds and one of the project's operators opened it: a login in `[board] operators`
(default: the running login) or one the claims namespace records. Ours are the chain's links (with
their READMEs, read at their heads, for the transitive closure); only the port lane's get a paragraph
written by this module. Every other open pull request that adds a roadmap folder not on main
(neither in the active nor in the completed folder) is another author's, the ready one before
drafts and then the lowest number when several add the same folder, and counts when the README
cites that roadmap as a supplier (the clauses `depgraph.units` reads). Changed files and READMEs are
cached by head in `other-prs.json`; pull request numbers in `dropped-external.txt` never count (the
citing roadmap localises what it needs instead). When the open pull requests cannot be read, no
paragraph is written: neither the chain nor the other authors' clause could be stated.

Inputs that used to be files in /tmp are in the project state directory: `stacks.toml` (child =
parent, for stacked pull requests), `external-deps.json` (dependencies the scan cannot see,
{"<pr>": {"<Roadmap>": [<other pr>, "<author>"]}}, or ext_deps.py's {"<pr>": {"name": ...,
"ext": {...}}}), `mutual.txt` ("<pr> <other pr>" lines: the two cite each other, so there is no
whole-pull-request order between them, and the stage-level graph between them is acyclic) and
`dropped-external.txt`.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass
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


def operators(ctx) -> set[str]:
    """The project's operators, lower-cased: `[board] operators` (default: the running login) and
    every login the claims namespace records (when it can be read)."""
    from . import claims
    cfg = ctx.cfg
    names = {x.lower() for x in (cfg.board.operators or [getattr(ctx, "operator", "")]) if x}
    if cfg.repositories.claims:
        shared = claims.Claims(repo=cfg.repositories.claims,
                               gitdir=ctx.state / "claims" / (cfg.repositories.claims.replace("/", "__") + ".git"),
                               owner="merge-order", operator="", project=cfg.project.name)
        try:
            names |= {x.lower() for x in shared.operators()}
        except (OSError, subprocess.SubprocessError):
            pass
    return names


@dataclass
class Survey:
    """The open pull requests besides the port lane's: ours (operator-opened, in the merge order)
    as {pr, name, names}, with their READMEs, and other authors' roadmaps -> (pr, author)."""
    members: list[dict]
    readmes: dict[str, str]
    others: dict[str, tuple[int, str]]


def survey(ctx, ours: set[int], order: list[str], logins: set[str] | None = None) -> Survey | None:
    """Read the open pull requests not in `ours` (the port lane's), or None when they, their files
    or main cannot be read."""
    cfg, repo = ctx.cfg, ctx.cfg.repositories.content
    main = on_main(ctx)
    if main is None:
        return None
    try:
        items = ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True)
    except GHError:
        return None
    logins = operators(ctx) if logins is None else logins
    rank = {n: i for i, n in enumerate(order)}
    path = ctx.state / "other-prs.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    cache = cache if isinstance(cache, dict) else {}
    dropped, seen, candidates, members, readmes = _numbers(ctx.state / "dropped-external.txt"), {}, {}, [], {}
    for pr in items:
        number = int(pr["number"])
        if number in ours or number in dropped:
            continue
        head, login = pr.get("head", {}).get("sha", ""), pr.get("user", {}).get("login", "")
        entry = cache.get(str(number))
        if not isinstance(entry, dict) or entry.get("head") != head:
            try:
                files = ctx.gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True)
            except GHError:
                return None
            entry = {"head": head, "folders": added_folders(files, cfg.roadmap.active_dir), "readmes": {}}
        new = sorted((f for f in entry["folders"] if f not in main), key=lambda f: (rank.get(f, len(rank)), f))
        mine = [f for f in new if f in rank]
        if mine and login.lower() in logins:
            texts = entry.setdefault("readmes", {})
            for name in mine:
                if name not in texts:
                    try:
                        texts[name] = ctx.gh.raw_file(repo, f"{cfg.roadmap.active_dir}/{name}/README.md", ref=head)
                    except GHError:
                        return None
                readmes[name] = texts[name]
            members.append({"pr": number, "name": mine[0], "names": mine, "owner": login})
        else:
            for folder in new:
                candidates.setdefault(folder, []).append((bool(pr.get("draft")), number, login))
        seen[str(number)] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass
    others = {folder: (best[1], best[2]) for folder, found in candidates.items() for best in [min(found)]}
    return Survey(members, readmes, others)


def cited(name: str, readme: str, others: dict[str, tuple[int, str]]) -> dict[str, list]:
    """The other authors' roadmaps a README cites as suppliers: roadmap -> [pull request, author]."""
    edges = depgraph.supplier_edges({name: readme}, set(others))
    return {s: list(others[s]) for s in edges.get(name, {}) if s in others}


def externals(state: Path, prs: list[dict], readmes: dict[str, str],
              others: dict[str, tuple[int, str]]) -> dict[str, dict[str, list]]:
    """Other authors' pull requests each of `prs` depends on ({"<pr>": {roadmap: [pr, author]}}): what
    the README cites among `others`, plus `external-deps.json`. A pull request whose README is
    missing has no key (unknown), unless the file lists it."""
    recorded = _external(state)
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


@dataclass
class OrderFacts:
    """What a pull request's "Merge order" paragraph states: the chain of ours that must merge
    first, and other authors' pull requests (None: not read) and those that cite it back."""
    chain: list[tuple[int, str]]
    external: list[tuple] | None
    mutual: list[tuple]

    def paragraph(self, pr: int) -> str:
        return depgraph.merge_order_paragraph(pr, self.chain, self.external, self.mutual)


def facts(prs: list[dict], readmes: dict[str, str], order: list[str], main_extended: set[str],
          stacks: dict[int, int], external: dict, mutual: set[tuple[int, int]],
          members: list[dict] | tuple = ()) -> dict[int, OrderFacts]:
    """The merge-order facts of each of `prs` (pure, given the inputs). `members` are ours too (links
    of the chain, one per pull request even when it adds several roadmaps) but get no facts. A pull
    request whose roadmap is not in the merge order, or whose README was not read, gets none."""
    edges = depgraph.supplier_edges(readmes, set(order), main_extended)
    name2pr = {p["name"]: p["pr"] for p in prs}
    pr2name = {v: k for k, v in name2pr.items()}
    for m in members:
        pr2name.setdefault(m["pr"], m["name"])
        for n in m.get("names") or [m["name"]]:
            name2pr.setdefault(n, m["pr"])
    out = {}
    for p in prs:
        if p["name"] not in order or p["name"] not in readmes:
            continue
        chain: list[tuple[int, str]] = []
        for n in depgraph.merge_chain(p["name"], edges, order, set(name2pr)):
            q = name2pr[n]
            if q != p["pr"] and q not in [c for c, _ in chain]:
                chain.append((q, pr2name[q]))
        parent = stacks.get(p["pr"])
        if parent in pr2name and parent not in [c for c, _ in chain]:
            chain.insert(0, (parent, pr2name[parent]))
        mut = {b for a, b in mutual if a == p["pr"]}
        ext, ext_first, ext_mut = external.get(str(p["pr"])), None, []
        if ext is not None:
            rows = sorted((int(v[0]), k, str(v[1]) if len(v) > 1 else "") for k, v in ext.items() if v)
            ext_first = [r for r in rows if r[0] not in mut]
            ext_mut = [r for r in rows if r[0] in mut]
        out[p["pr"]] = OrderFacts(chain, ext_first, ext_mut)
    return out


def plan(prs: list[dict], readmes: dict[str, str], order: list[str], main_extended: set[str],
         stacks: dict[int, int], external: dict, mutual: set[tuple[int, int]],
         members: list[dict] | tuple = ()) -> dict[int, str]:
    """The paragraph for each of `prs` (pure, given the inputs): `facts`, rendered."""
    return {pr: f.paragraph(pr) for pr, f in facts(prs, readmes, order, main_extended, stacks, external, mutual,
                                                   members).items()}


def all_facts(ctx, prs: list[dict], readmes: dict[str, str], order: list[str]) -> dict[int, OrderFacts] | None:
    """The merge-order facts of each of the port lane's `prs` (whose READMEs are `readmes`), or None
    when the other open pull requests cannot be read."""
    found = survey(ctx, {int(p["pr"]) for p in prs}, order)
    if found is None:
        return None
    every = {**found.readmes, **readmes}
    return facts(prs, every, order, set(ctx.cfg.roadmap.main_extended), _stacks(ctx.state),
                 externals(ctx.state, prs, readmes, found.others), _mutual(ctx.state), found.members)


def paragraphs(ctx, prs: list[dict], readmes: dict[str, str], order: list[str]) -> dict[int, str] | None:
    """The paragraph of each of the port lane's `prs`, or None when the open pull requests cannot be read."""
    found = all_facts(ctx, prs, readmes, order)
    return None if found is None else {pr: f.paragraph(pr) for pr, f in found.items()}


NEW = -1      # the pull request the port lane is about to open, before it has a number


def facts_for(ctx, name: str, readme: str, prs: list[dict], order: list[str], number: int = NEW) -> OrderFacts | None:
    """The merge-order facts of a port lane pull request (`number`, or one about to be opened): the
    Dependencies sentence and the Merge order paragraph both come from them. None when the open pull
    requests cannot be read; empty facts when the roadmap is not in the merge order."""
    from .lanes.base import sibling_readmes
    new = {"pr": number, "name": name}
    everyone = [p for p in prs if p["pr"] != number and p["name"] != name] + [new]
    readmes = {**sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir, everyone), name: readme}
    found = all_facts(ctx, everyone, readmes, order)
    if found is None:
        return None
    return found.get(number, OrderFacts([], None, []))


def paragraph_for(ctx, new: dict, readme: str, prs: list[dict], order: list[str]) -> str | None:
    """The paragraph of a pull request the port lane just opened (`new` = {pr, name}), by the same
    renderer as `refresh`: '' when its roadmap is not in the merge order, None when the open pull
    requests cannot be read."""
    found = facts_for(ctx, new["name"], readme, prs, order, new["pr"])
    if found is None:
        return None
    return found.paragraph(new["pr"]) if new["name"] in order else ""


def refresh(ctx, only: set[int] | None = None, listing: bool = False) -> list[str]:
    from .lanes.base import open_roadmap_prs, sibling_checkouts, sibling_readmes
    cfg, repo = ctx.cfg, ctx.cfg.repositories.content
    prs = open_roadmap_prs(ctx)
    sibling_checkouts(ctx, prs)
    readmes = sibling_readmes(ctx.state, cfg.roadmap.active_dir, prs)
    written = paragraphs(ctx, prs, readmes, _order(ctx))
    if written is None:
        return ["the open pull requests could not be read; no merge-order paragraph written"]
    lines = []
    for p in prs:
        if only and p["pr"] not in only:
            continue
        para = written.get(p["pr"])
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
