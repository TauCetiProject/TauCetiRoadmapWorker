"""Where open roadmap pull requests overlap one another, read from the host's mirror of their heads.

Each open pull request is reviewed on its own, against main, so what two of them do to each other is
seen by neither review: two rewrites of one roadmap that cannot both merge, or a revision that
renumbers the sections other roadmaps cite. This module states the mechanical part, from the mirror
`openroadmaps` keeps (no GitHub read):

* `shared`: two or more open pull requests change the same roadmap folder; `git merge-tree` says
  whether each pair of heads merges, and lists the conflicted files when it does not.
* `removals`: an open pull request changes a roadmap that is on main and drops an id another roadmap
  cites. The ids are the citations `roadmaps resolve` reads (`Name §1.2`, `Name Layer 3`, `Name A1.2`)
  and links to a heading (`../Name/README.md#anchor`); one is dropped when the roadmap has it on
  main and no longer has it at that head (`openroadmaps.present`; a heading anchor by GitHub's
  slug). A citing roadmap is read on main and at every open head that carries it.
* `neighbours`: the open roadmap folders whose READMEs share the most vocabulary with a README
  (tf-idf cosine over words), where two roadmaps that plan the same objects are likeliest to be.

Whether two roadmaps both own an object, or each hand it to the other, is mathematics, for the
review; these facts say where to look. `report` writes them for one roadmap (a brief's
`scratch/overlaps.md`), `report_all` for every open pull request (`roadmaps overlaps`).
"""

from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from urllib.parse import unquote

from . import openroadmaps as o

NEIGHBOURS = 5          # closest open roadmaps listed for a README
MIN_SIMILARITY = 0.08   # below this cosine two READMEs share only the house style
COMMON = 0.25           # a word in more than this share of the READMEs carries no signal

# A link to a heading of another roadmap's README: `[...](../Name/README.md#anchor)`, at any depth.
LINK = re.compile(r"\]\((?:[^)\s#]*/)?(?P<name>[A-Za-z][A-Za-z0-9]*)/README\.md#(?P<anchor>[^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
FENCE = re.compile(r"^\s*(```|~~~)")
WORD = re.compile(r"[a-zà-ÿ][a-zà-ÿ'\-]{3,}")


@dataclass(frozen=True)
class Citation:
    where: str       # where the citing README was read: "main" or "#<pr>"
    citer: str       # the citing roadmap
    line: int
    cited: str       # the cited roadmap
    id: str          # "1.2", "3" (a layer), "A1.2", or the anchor
    kind: str        # "section", "layer" or "anchor"
    text: str        # the citation as written

    def label(self) -> str:
        if self.kind == "anchor":
            return f"{self.cited} #{self.id}"
        return f"{self.cited} {'Layer ' if self.kind == 'layer' else '§'}{self.id}"


@dataclass(frozen=True)
class Removal:
    pr: int          # the open pull request whose head drops the id
    author: str
    citation: Citation


@dataclass(frozen=True)
class Shared:
    folder: str
    roadmap: str
    prs: tuple[int, ...]
    conflicts: dict      # (a, b) -> conflicted paths ([] merges cleanly; None not checked)


# --- reading the mirror ---------------------------------------------------------------------------

def _batch(state: Path, specs: list[str]) -> list[str]:
    """The text of each `<tree-ish>:<path>` of the mirror, '' when absent: one `git cat-file --batch`."""
    if not specs:
        return []
    out = subprocess.run(["git", "-C", str(o.mirror(state)), "cat-file", "--batch"],
                         input="\n".join(specs).encode() + b"\n", capture_output=True, check=True).stdout
    texts, i = [], 0
    for _spec in specs:
        end = out.index(b"\n", i)
        header = out[i:end].split()
        i = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            texts.append(out[i:i + size].decode("utf-8", errors="replace"))
            i += size + 1
        else:
            texts.append("")
    return texts


def readmes(state: Path, index: dict) -> list[tuple[str, str, str]]:
    """(where, roadmap, README text) of every roadmap on main and of every roadmap folder an open pull
    request carries, at its head: what a removal can break."""
    places = [("main", f"refs/heads/{index.get('branch', 'main')}", name, folder)
              for name, folder in sorted((index.get("main_folders") or {}).items())]
    places += [(f"#{p['pr']}", o.pr_ref(p["pr"]), name, folder)
               for p in index.get("prs", []) for name, folder in sorted((p.get("folders") or {}).items())]
    texts = _batch(state, [f"{ref}:{folder}/README.md" for _w, ref, _n, folder in places])
    return [(where, name, text) for (where, _r, name, _f), text in zip(places, texts) if text]


# --- citations and anchors ------------------------------------------------------------------------

def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped (letters, digits, `_` and `-`
    kept, in any script), spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """The heading anchors of a README (a repeated heading's `-1`, `-2`, ...), outside code fences,
    and its explicit `<a id=...>` or `<a name=...>`."""
    out, seen, fenced = set(), Counter(), False
    for line in text.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if m:
            s = slug(m.group(1))
            out.add(s if not seen[s] else f"{s}-{seen[s]}")
            seen[s] += 1
    return out | set(HTML_ANCHOR.findall(text))


def citations(text: str, names, *, where: str = "", citer: str = "") -> list[Citation]:
    """The citations a README makes of the roadmaps `names`, its own excepted: ids and heading links."""
    pattern = o.cite_pattern(n for n in names if n != citer)
    out = []
    for number, line in enumerate(text.split("\n"), 1):
        for m in pattern.finditer(line) if pattern else ():
            token, layer = o.cited_id(m)
            out.append(Citation(where, citer, number, m.group(1), token, "layer" if layer else "section",
                                m.group(0).strip()))
        for m in LINK.finditer(line):
            if m.group("name") in names and m.group("name") != citer:
                out.append(Citation(where, citer, number, m.group("name"), unquote(m.group("anchor")).lower(),
                                    "anchor", m.group(0)))
    return out


def has(text: str, citation: Citation) -> bool:
    if citation.kind == "anchor":
        return citation.id in anchors(text)
    return o.present(text, citation.id, layer=citation.kind == "layer")


# --- the facts ------------------------------------------------------------------------------------

def revisions(state: Path, index: dict, *, only: str = "", pr: int = 0) -> list[tuple[dict, str, str, str]]:
    """(pull request, roadmap, text on main, text at its head) for each roadmap on main an open pull
    request changes (its README differs); `only` and `pr` narrow it to one roadmap or pull request."""
    branch, on_main = index.get("branch", "main"), index.get("main_folders") or {}
    found = [(p, name, folder, on_main[name]) for p in index.get("prs", []) if not pr or p["pr"] == pr
             for name, folder in sorted((p.get("folders") or {}).items())
             if name in on_main and name not in p.get("adds", []) and (not only or name == only)]
    specs = [s for p, _n, folder, main in found
             for s in (f"refs/heads/{branch}:{main}/README.md", f"{o.pr_ref(p['pr'])}:{folder}/README.md")]
    texts = _batch(state, specs)
    out = []
    for i, (p, name, _f, _m) in enumerate(found):
        main, head = texts[2 * i], texts[2 * i + 1]
        if main and head and main != head:
            out.append((p, name, main, head))
    return out


def removals(state: Path, index: dict, *, texts: list[tuple[str, str, str]] | None = None, only: str = "",
             pr: int = 0) -> list[Removal]:
    """Every citation, on main or at an open head, of an id that an open pull request's head drops from
    a roadmap on main (`only`, `pr`: of that roadmap, by that pull request). `texts` as `readmes`
    gives them, read when not given."""
    changed = revisions(state, index, only=only, pr=pr)
    if not changed:
        return []
    names = {name for _p, name, _m, _h in changed}
    texts = readmes(state, index) if texts is None else texts
    by_name: dict[str, list[Citation]] = {}
    for where, citer, text in texts:
        for c in citations(text, names, where=where, citer=citer):
            by_name.setdefault(c.cited, []).append(c)
    out = []
    for p, name, main, head in changed:
        verdict: dict[tuple[str, str], bool] = {}
        for c in by_name.get(name, []):
            key = (c.kind, c.id)
            if key not in verdict:
                verdict[key] = has(main, c) and not has(head, c)
            if verdict[key]:
                out.append(Removal(p["pr"], p.get("author", ""), c))
    return out


def merge_conflicts(state: Path, a: int, b: int) -> list[str] | None:
    """The files in conflict when the heads of open pull requests `a` and `b` merge ([] when they merge
    cleanly), or None when git cannot say (a git older than 2.38, a head not in the mirror)."""
    result = subprocess.run(["git", "-C", str(o.mirror(state)), "merge-tree", "--write-tree", "--name-only",
                             "--no-messages", o.pr_ref(a), o.pr_ref(b)], capture_output=True, text=True,
                            check=False)
    if result.returncode == 0:
        return []
    if result.returncode != 1:
        return None
    return sorted({line for line in result.stdout.splitlines()[1:] if line.strip()})


def shared(state: Path, index: dict, *, merge: bool = True, only: str = "") -> list[Shared]:
    """The roadmap folders two or more open pull requests change (`only`: that roadmap's), with the
    merge result of each pair of their heads."""
    by_folder: dict[str, tuple[str, list[int]]] = {}
    for p in index.get("prs", []):
        for name, folder in (p.get("folders") or {}).items():
            if not only or name == only:
                by_folder.setdefault(folder, (name, []))[1].append(p["pr"])
    out = []
    for folder, (name, prs) in sorted(by_folder.items()):
        if len(prs) < 2:
            continue
        pairs = list(combinations(sorted(prs), 2))
        out.append(Shared(folder, name, tuple(sorted(prs)),
                          {pair: merge_conflicts(state, *pair) if merge else None for pair in pairs}))
    return out


def _vector(tf: Counter, df: Counter, n: int) -> dict[str, float]:
    v = {w: (1 + math.log(k)) * math.log(n / df[w]) for w, k in tf.items() if 2 <= df[w] <= COMMON * n}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {w: x / norm for w, x in v.items()}


def neighbours(texts: list[tuple[str, str, str]], readme: str, *, own: str = "", own_pr: int = 0,
               k: int = NEIGHBOURS) -> list[tuple[float, str, str]]:
    """The `k` open roadmap folders (where, roadmap) whose README is closest to `readme` in vocabulary,
    as (cosine, where, roadmap), closest first; `texts` as `readmes` gives them. The roadmaps on main
    weigh the words but are not listed, nor is `own` or anything `own_pr` carries."""
    counts = [Counter(WORD.findall(text.lower())) for _w, _n, text in texts]
    mine_tf = Counter(WORD.findall(readme.lower()))
    df = Counter(mine_tf.keys())
    for tf in counts:
        df.update(tf.keys())
    n = len(texts) + 1
    mine = _vector(mine_tf, df, n)
    scored = []
    for (where, name, _text), tf in zip(texts, counts):
        if where == "main" or name == own or where == f"#{own_pr}":
            continue
        theirs = _vector(tf, df, n)
        small, big = (mine, theirs) if len(mine) < len(theirs) else (theirs, mine)
        score = sum(x * big.get(w, 0.0) for w, x in small.items())
        if score >= MIN_SIMILARITY:
            scored.append((round(score, 3), where, name))
    return sorted(scored, key=lambda s: (-s[0], s[1], s[2]))[:k]


# --- reports --------------------------------------------------------------------------------------

def _header(index: dict, title: str) -> list[str]:
    return [f"# {title}", "",
            (f"Computed from main ({index.get('main', '')[:7]}) and the heads of {len(index.get('prs', []))} open "
             f"roadmap pull requests (index of {index.get('at', '?')}), not from what the pull requests say. "
             "These are leads to check, not verdicts."), ""]


def _conflict_text(pair: tuple[int, int], paths: list[str] | None) -> str:
    a, b = pair
    if paths is None:
        return f"#{a} and #{b}: not checked (git could not merge the heads)"
    if not paths:
        return f"#{a} and #{b} merge cleanly"
    return f"#{a} and #{b} conflict in " + ", ".join(f"`{p}`" for p in paths)


def _citers(found: list[Removal]) -> str:
    places: dict[str, list[str]] = {}
    for r in found:
        places.setdefault(r.citation.citer, []).append(f"{r.citation.where} line {r.citation.line}")
    return "; ".join(f"{citer} ({', '.join(sorted(set(p)))})" for citer, p in sorted(places.items()))


def report(state: Path, index: dict, name: str, readme: str, *, pr: int = 0,
           texts: list[tuple[str, str, str]] | None = None) -> tuple[str, int]:
    """(Markdown, number of facts) for the roadmap `name` whose README is `readme`, at the head of open
    pull request `pr` when it is one: the other open pull requests that change its folder, the ids it
    cites that an open pull request drops, the ids `pr` drops that other roadmaps cite, and its
    closest open neighbours."""
    texts = readmes(state, index) if texts is None else texts
    lines, facts = _header(index, f"Overlaps of {name} with the open roadmap pull requests"), 0
    prs = {p["pr"]: p for p in index.get("prs", [])}

    others = [s for s in shared(state, index, only=name) if not pr or pr in s.prs]
    carried = [p for p in index.get("prs", []) if name in p.get("roadmaps", []) and p["pr"] != pr]
    lines += ["## Other open pull requests that change this roadmap", ""]
    if carried:
        facts += len(carried)
        for p in carried:
            pairs = [(pair, paths) for s in others for pair, paths in s.conflicts.items() if p["pr"] in pair
                     and (not pr or pr in pair)]
            merged = "; ".join(_conflict_text(pair, paths) for pair, paths in pairs)
            lines.append(f"- #{p['pr']} (@{p.get('author', '?')}) {p.get('title', '')[:90]}"
                         + (f": {merged}" if merged else ""))
    else:
        lines.append("None.")
    lines.append("")

    changed = {(p["pr"], n): (m, h) for p, n, m, h in revisions(state, index)}
    mine = citations(readme, {n for _p, n in changed}, citer=name, where=f"#{pr}" if pr else "this README")
    broken = []
    for c in mine:
        for (q, cited), (main, head) in changed.items():
            if cited == c.cited and q != pr and has(main, c) and not has(head, c):
                broken.append(Removal(q, prs[q].get("author", ""), c))
    lines += ["## Ids this README cites that an open pull request drops", ""]
    grouped: dict[tuple[int, str], list[Removal]] = {}
    for r in broken:
        grouped.setdefault((r.pr, r.citation.label()), []).append(r)
    facts += len(grouped)
    for (q, label), rs in sorted(grouped.items()):
        at = ", ".join(str(n) for n in sorted({r.citation.line for r in rs}))
        lines.append(f"- {label} (line{'s' if ',' in at else ''} {at}): #{q} (@{rs[0].author}) changes "
                     f"{rs[0].citation.cited} and has no such {'heading' if rs[0].citation.kind == 'anchor' else 'id'} "
                     "at its head (main has it)")
    if not grouped:
        lines.append("None.")
    lines.append("")

    if pr and (pr, name) in changed:
        dropped = removals(state, index, texts=texts, only=name, pr=pr)
        lines += [f"## Ids of {name} that this pull request drops and other roadmaps cite", ""]
        if dropped:
            groups: dict[str, list[Removal]] = {}
            for r in dropped:
                groups.setdefault(r.citation.label(), []).append(r)
            facts += len(groups)
            lines += [f"- {label}: cited by {_citers(rs)}" for label, rs in sorted(groups.items())]
        else:
            lines.append("None.")
        lines.append("")

    near = neighbours(texts, readme, own=name, own_pr=pr)
    lines += ["## Open roadmaps closest in vocabulary", ""]
    lines += ([f"- {where} {roadmap} ({score:.2f})" for score, where, roadmap in near] or ["None."])
    return "\n".join(lines) + "\n", facts


def report_all(state: Path, index: dict, *, merge: bool = True) -> str:
    """Every shared roadmap folder and every dropped id cited elsewhere, across the open pull requests."""
    lines = _header(index, "Overlaps between the open roadmap pull requests")
    folders = shared(state, index, merge=merge)
    lines += ["## Roadmaps that two or more open pull requests change", ""]
    for s in folders:
        lines.append(f"- {s.roadmap} (`{s.folder}`): " + ", ".join(f"#{n}" for n in s.prs))
        lines += [f"  - {_conflict_text(pair, paths)}" for pair, paths in sorted(s.conflicts.items())]
    if not folders:
        lines.append("None.")
    lines += ["", "## Ids an open pull request drops that other roadmaps cite", ""]
    found = removals(state, index)
    by_pr: dict[int, dict[str, list[Removal]]] = {}
    for r in found:
        by_pr.setdefault(r.pr, {}).setdefault(r.citation.label(), []).append(r)
    for number in sorted(by_pr):
        author = next(iter(by_pr[number].values()))[0].author
        lines.append(f"- #{number} (@{author}):")
        lines += [f"  - {label}: cited by {_citers(rs)}" for label, rs in sorted(by_pr[number].items())]
    if not by_pr:
        lines.append("None.")
    return "\n".join(lines) + "\n"
