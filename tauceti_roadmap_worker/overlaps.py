"""Where open roadmap pull requests overlap one another, read from the host's mirror of their heads.

Each open pull request is reviewed on its own, against main, so what two of them do to each other is
seen by neither review: two rewrites of one roadmap that cannot both merge, or a revision that
renumbers the sections other roadmaps cite. This module states the mechanical part, from the mirror
`openroadmaps` keeps (no GitHub read):

* `shared`: two or more open pull requests change the same roadmap folder; `git merge-tree` says
  whether each pair of heads merges, with the conflicted files (or git's message when it names none).
* `removals`: an open pull request changes a roadmap that is on main and drops an id another roadmap
  cites. The ids are the citations `roadmaps resolve` reads (`Name §1.2`, `Name Layer 3`, `Name A1.2`)
  and links to a heading (`../Name/README.md#anchor`). One is dropped when the README defines it on
  main and at the merge base of main and that head, and no longer at the head
  (`openroadmaps.defined`: a heading, label or numbered item, not a mention; a heading anchor by
  GitHub's slug). The merge base keeps an id main gained after the branch from counting as dropped,
  and a head without the README (deleted or emptied) defines nothing. A citing roadmap is read on
  main and at every open head that carries it.
* `readme_facts`: for one README, the ids it cites that an open revision drops, and the converse, the
  ids it cites that only an open revision defines (at its head, not at its merge base, not on main).
  Citing such an id makes that revision a dependency, which `mergeorder` writes into the merge-order
  paragraph (`revision_dependencies`).
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
from functools import lru_cache
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
class Merge:
    clean: bool
    paths: tuple[str, ...] = ()          # the conflicted files
    messages: tuple[str, ...] = ()       # git's CONFLICT lines, for a conflict that names no file


@dataclass(frozen=True)
class Shared:
    folder: str
    roadmap: str
    prs: tuple[int, ...]
    conflicts: dict      # (a, b) -> Merge, or None when not checked


@dataclass(frozen=True)
class Revision:
    pr: dict             # the open pull request, as the index lists it
    name: str            # the roadmap on main it changes
    main: str            # its README on main
    base: str            # at the merge base of main and the head
    head: str            # at the head ('' when the head deletes or empties it)

    def drops(self, c: Citation) -> bool:
        return defines(self.main, c) and defines(self.base, c) and not defines(self.head, c)

    def adds(self, c: Citation) -> bool:
        return defines(self.head, c) and not defines(self.base, c) and not defines(self.main, c)


# --- reading the mirror ---------------------------------------------------------------------------

def _batch(state: Path, specs: list[str]) -> list[str | None]:
    """The text of each `<tree-ish>:<path>` of the mirror, None when absent: one `git cat-file --batch`."""
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
            texts.append(None)
    return texts


def _merge_base(state: Path, a: str, b: str) -> str:
    result = subprocess.run(["git", "-C", str(o.mirror(state)), "merge-base", a, b], capture_output=True, text=True,
                            check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def readmes(state: Path, index: dict) -> list[tuple[str, str, str]]:
    """(where, roadmap, README text) of every roadmap on main and of every roadmap folder an open pull
    request carries, at its head: what a removal can break."""
    places = [("main", f"refs/heads/{index.get('branch', 'main')}", name, folder)
              for name, folder in sorted((index.get("main_folders") or {}).items())]
    places += [(f"#{p['pr']}", o.pr_ref(p["pr"]), name, folder)
               for p in index.get("prs", []) for name, folder in sorted((p.get("folders") or {}).items())]
    texts = _batch(state, [f"{ref}:{folder}/README.md" for _w, ref, _n, folder in places])
    return [(where, name, text) for (where, _r, name, _f), text in zip(places, texts, strict=True) if text]


# --- citations and anchors ------------------------------------------------------------------------

def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped (letters, digits, `_` and `-`
    kept, in any script), spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


@lru_cache(maxsize=512)
def anchors(text: str) -> frozenset[str]:
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
    return frozenset(out | set(HTML_ANCHOR.findall(text)))


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


def defines(text: str, citation: Citation) -> bool:
    """Whether a version of the cited README defines the cited id (a heading anchor: has the heading)."""
    if citation.kind == "anchor":
        return citation.id in anchors(text)
    return o.defined(text, citation.id, layer=citation.kind == "layer")


# --- the facts ------------------------------------------------------------------------------------

def revisions(state: Path, index: dict, *, only: str = "", pr: int = 0) -> list[Revision]:
    """Each roadmap on main whose README an open pull request changes since its merge base with main
    (`only` and `pr` narrow it to one roadmap or pull request). A head the mirror lacks is skipped."""
    main_ref, on_main = f"refs/heads/{index.get('branch', 'main')}", index.get("main_folders") or {}
    found = [(p, name, folder, on_main[name]) for p in index.get("prs", []) if not pr or p["pr"] == pr
             for name, folder in sorted((p.get("folders") or {}).items())
             if name in on_main and name not in p.get("adds", []) and (not only or name == only)]
    bases = {p["pr"]: _merge_base(state, main_ref, o.pr_ref(p["pr"])) for p, _n, _f, _m in found}
    found = [f for f in found if bases[f[0]["pr"]]]
    specs = [s for p, _n, folder, main in found
             for s in (f"{main_ref}:{main}/README.md", f"{bases[p['pr']]}:{main}/README.md",
                       f"{o.pr_ref(p['pr'])}:{folder}/README.md")]
    texts = _batch(state, specs)
    out = []
    for i, (p, name, _f, _m) in enumerate(found):
        main, base, head = texts[3 * i:3 * i + 3]
        if main is None or base is None:
            continue                     # the roadmap is not on main, or not yet where the head branched
        if (head or "") != base:
            out.append(Revision(p, name, main, base, head or ""))
    return out


def removals(state: Path, index: dict, *, texts: list[tuple[str, str, str]] | None = None, only: str = "",
             pr: int = 0) -> list[Removal]:
    """Every citation, on main or at an open head, of an id that an open pull request's head drops from
    a roadmap on main (`only`, `pr`: of that roadmap, by that pull request). `texts` as `readmes`
    gives them, read when not given."""
    changed = revisions(state, index, only=only, pr=pr)
    if not changed:
        return []
    names = {r.name for r in changed}
    texts = readmes(state, index) if texts is None else texts
    by_name: dict[str, list[Citation]] = {}
    for where, citer, text in texts:
        for c in citations(text, names, where=where, citer=citer):
            by_name.setdefault(c.cited, []).append(c)
    out = []
    for rev in changed:
        verdict: dict[tuple[str, str], bool] = {}
        for c in by_name.get(rev.name, []):
            key = (c.kind, c.id)
            if key not in verdict:
                verdict[key] = rev.drops(c)
            if verdict[key]:
                out.append(Removal(rev.pr["pr"], rev.pr.get("author", ""), c))
    return out


def readme_facts(state: Path, index: dict, readme: str, own: str, *, pr: int = 0,
                 changed: list[Revision] | None = None) -> tuple[list[Removal], list[Removal]]:
    """(the ids `readme` cites that an open revision drops, those it cites that only an open revision
    defines), the revisions of `pr` itself aside."""
    changed = revisions(state, index) if changed is None else changed
    changed = [r for r in changed if r.pr["pr"] != pr]
    cites = citations(readme, {r.name for r in changed}, citer=own, where=f"#{pr}" if pr > 0 else "this README")
    dropped, adopted = [], []
    for c in cites:
        for rev in changed:
            if rev.name == c.cited:
                if rev.drops(c):
                    dropped.append(Removal(rev.pr["pr"], rev.pr.get("author", ""), c))
                elif rev.adds(c):
                    adopted.append(Removal(rev.pr["pr"], rev.pr.get("author", ""), c))
    return dropped, adopted


def revision_dependencies(state: Path, index: dict, readme: str, own: str, *, pr: int = 0,
                          changed: list[Revision] | None = None) -> list[tuple[int, str, str]]:
    """(pull request, roadmap, author) of each open revision whose numbering `readme` cites: an id only
    its head defines. That revision must merge first."""
    _dropped, adopted = readme_facts(state, index, readme, own, pr=pr, changed=changed)
    return sorted({(r.pr, r.citation.cited, r.author) for r in adopted})


def merge_conflicts(state: Path, a: int, b: int) -> Merge | None:
    """Whether the heads of open pull requests `a` and `b` merge, with the conflicted files, and git's
    CONFLICT lines when it names no file (a directory rename split); None when git cannot say (a git
    older than 2.38, a head not in the mirror)."""
    result = subprocess.run(["git", "-C", str(o.mirror(state)), "merge-tree", "--write-tree", "--name-only",
                             o.pr_ref(a), o.pr_ref(b)], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return Merge(True)
    if result.returncode != 1:
        return None
    files, _, messages = result.stdout.partition("\n\n")
    paths = tuple(sorted({line for line in files.splitlines()[1:] if line.strip()}))
    said = tuple(line.strip() for line in messages.splitlines() if line.startswith("CONFLICT"))
    return Merge(False, paths, () if paths else (said or ("a conflict git names no file for",)))


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


def _conflict_text(pair: tuple[int, int], merge: Merge | None) -> str:
    a, b = pair
    if merge is None:
        return f"#{a} and #{b}: not checked (git could not merge the heads)"
    if merge.clean:
        return f"#{a} and #{b} merge cleanly"
    if merge.paths:
        return f"#{a} and #{b} conflict in " + ", ".join(f"`{p}`" for p in merge.paths)
    return f"#{a} and #{b} conflict: " + "; ".join(merge.messages)


def _citers(found: list[Removal]) -> str:
    places: dict[str, list[str]] = {}
    for r in found:
        places.setdefault(r.citation.citer, []).append(f"{r.citation.where} line {r.citation.line}")
    return "; ".join(f"{citer} ({', '.join(sorted(set(p)))})" for citer, p in sorted(places.items()))


def report(state: Path, index: dict, name: str, readme: str, *, pr: int = 0,
           texts: list[tuple[str, str, str]] | None = None) -> tuple[str, int]:
    """(Markdown, number of facts) for the roadmap `name` whose README is `readme`, at the head of open
    pull request `pr` when it is one: the other open pull requests that change its folder, the ids it
    cites that an open pull request drops or that only an open revision defines, the ids `pr` drops
    that other roadmaps cite, and its closest open neighbours."""
    texts = readmes(state, index) if texts is None else texts
    lines, facts = _header(index, f"Overlaps of {name} with the open roadmap pull requests"), 0

    others = [s for s in shared(state, index, only=name) if not pr or pr in s.prs]
    carried = [p for p in index.get("prs", []) if name in p.get("roadmaps", []) and p["pr"] != pr]
    lines += ["## Other open pull requests that change this roadmap", ""]
    if carried:
        facts += len(carried)
        for p in carried:
            pairs = [(pair, merge) for s in others for pair, merge in s.conflicts.items() if p["pr"] in pair
                     and (not pr or pr in pair)]
            merged = "; ".join(_conflict_text(pair, merge) for pair, merge in pairs)
            lines.append(f"- #{p['pr']} (@{p.get('author', '?')}) {p.get('title', '')[:90]}"
                         + (f": {merged}" if merged else ""))
    else:
        lines.append("None.")
    lines.append("")

    changed = revisions(state, index)
    broken, adopted = readme_facts(state, index, readme, name, pr=pr, changed=changed)
    sections = (("## Ids this README cites that an open pull request drops", broken,
                 "changes {cited} and no longer defines it at its head (main and its merge base do)"),
                ("## Ids this README cites that only an open revision defines", adopted,
                 "defines it in {cited} at its head (main does not): that pull request must merge first"))
    for title, found, what in sections:
        lines += [title, ""]
        grouped: dict[tuple[int, str], list[Removal]] = {}
        for r in found:
            grouped.setdefault((r.pr, r.citation.label()), []).append(r)
        facts += len(grouped)
        for (q, label), rs in sorted(grouped.items()):
            at = ", ".join(str(n) for n in sorted({r.citation.line for r in rs}))
            lines.append(f"- {label} (line{'s' if ',' in at else ''} {at}): #{q} (@{rs[0].author}) "
                         + what.format(cited=rs[0].citation.cited))
        if not grouped:
            lines.append("None.")
        lines.append("")

    if pr > 0 and any(r.pr["pr"] == pr and r.name == name for r in changed):
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
        lines += [f"  - {_conflict_text(pair, merge)}" for pair, merge in sorted(s.conflicts.items())]
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
