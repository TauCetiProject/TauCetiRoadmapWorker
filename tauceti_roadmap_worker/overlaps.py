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
* `dependency_record`: what a pull request waits for at a head, as a structured record: the roadmaps
  it names as suppliers that are not on main, the open revisions whose numbering it cites, and the
  pull requests its description names as dependencies (`declared_dependencies`). `deplabels` keeps the
  dependency label by it, recomputed from the current heads and descriptions on every run.
* `copies`: an open pull request adds a roadmap folder that main has gained since the pull request
  branched (the README is on main, not at the merge base, and the head does not move it there): a
  second copy of a merged roadmap, which no review of its statements can make mergeable.
* `neighbours`: the roadmap folders, at open heads or on main, whose READMEs share the most vocabulary
  with a README (tf-idf cosine over words), where two roadmaps that plan the same objects are
  likeliest to be.
* `subject_neighbours`: the same, by the results a README names (capitalised names: "Hecke",
  "Kervaire-Milnor") and the words of its headings, its supplier section aside. Two plans of one
  subject under different titles share these whatever their prose.

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
from itertools import combinations, pairwise
from pathlib import Path
from urllib.parse import unquote

from . import depgraph
from . import openroadmaps as o

NEIGHBOURS = 5          # closest open roadmaps listed for a README
MIN_SIMILARITY = 0.08   # below this cosine two READMEs share only the house style
COMMON = 0.25           # a word in more than this share of the READMEs carries no signal
MAIN_NEIGHBOURS = 3     # closest roadmaps on main listed for a README
SUBJECT_NEIGHBOURS = 5  # roadmaps listed as closest in named results and headings
MIN_SUBJECT = 0.5       # name cosine + heading cosine (at most 2): below it two READMEs share only a field
SUBJECT_COMMON = 0.2    # a name or heading word in more than this share of the roadmaps carries no signal
SUBJECT_TERMS = 400     # the heaviest names, and heading terms, kept for a README

# A link to a heading of another roadmap's README: `[...](../Name/README.md#anchor)`, at any depth.
LINK = re.compile(r"\]\((?:[^)\s#]*/)?(?P<name>[A-Za-z][A-Za-z0-9]*)/README\.md#(?P<anchor>[^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
FENCE = re.compile(r"^\s*(```|~~~)")
WORD = re.compile(r"[a-zà-ÿ][a-zà-ÿ'\-]{3,}")
NAMED = re.compile(r"\b[A-Z][a-zà-ÿ]{3,}(?:[-–][A-Z][a-zà-ÿ]+)*\b")
SUPPLIER_SECTION = re.compile(r"(?ms)^## (?:Exact )?[Ss]upplier.*?(?=^## |\Z)")
SUBHEADING = re.compile(r"(?m)^#{2,4}\s+(.*)$")
HOUSE_HEADING = re.compile(r"(?i)(?:from |examples|dependencies|scope|conventions|how to read|sources|references)")
# Capitalised words of the house style and of English sentences: no result is named by them.
HOUSE_NAMES = frozenset(NAMED.findall(
    "Roadmap Layer Mathlib Ceti Lean Theorem Lemma Definition Proposition Corollary Section Chapter Examples "
    "Dependencies Sources Scope Conventions From Part Suggested Exact Type Prop This That Then There These Those "
    "When With Under Over Proof Source Statement Target Targets Remark Example Note Notes Here They Both Such "
    "Given Each Every Only Also Where Which What Supplier Suppliers Owned Build Read Check Checks Needs Stage"))


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
        """Main and the merge base define the id and the head neither defines nor maps it (`mapped`): a
        renumbering that says where the old id went keeps its citations valid."""
        return (defines(self.main, c) and defines(self.base, c) and not defines(self.head, c)
                and not mapped(self.head, c))

    def adds(self, c: Citation) -> bool:
        return defines(self.head, c) and not defines(self.base, c) and not defines(self.main, c)


@dataclass(frozen=True)
class Copy:
    pr: dict             # the open pull request, as the index lists it
    name: str            # the roadmap on main it adds again
    folder: str          # where its head has the folder
    base: str            # the merge base of main and the head, which has no such roadmap
    main: str            # the README on main
    head: str            # the README at the head

    def lacks(self, c: Citation) -> bool:
        """Main defines the id and the head neither defines nor maps it."""
        return defines(self.main, c) and not defines(self.head, c) and not mapped(self.head, c)


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


# A renumbering that keeps the old ids readable: a table whose header names the earlier numbering
# ("| Former label | Content | Now |", "| Earlier label | Present location |"), or a sentence saying
# where an old id went ("The earlier separate Layer 1.5 is now §1.3", "Citations of §1.T.1 ... resolve to
# the Tau Ceti tower declarations").
MAP_HEADER = re.compile(r"\b(?:former|formerly|earlier|old|previous|prior)\b", re.IGNORECASE)
MAP_SENTENCE = re.compile(r"\b(?:is|are)\s+now\b|\breads?\s+as\b|\bresolves?\s+(?:to|as)\b|\bformerly\b"
                          r"|\brenumbered\b|\bwas\s+(?:numbered|called|labell?ed)\b", re.IGNORECASE)


def _label(c: Citation) -> re.Pattern:
    if c.kind == "layer":
        return re.compile(r"\bLayers?\s+" + re.escape(c.id) + r"(?!\w|\.\d)")
    return re.compile(r"(?:§\s*|(?<![\w.]))" + re.escape(c.id) + r"(?!\w|\.\d)")


def mapped(text: str, c: Citation) -> bool:
    """Whether a README version maps the old id `c` to where it is now: the first cell of a row of a
    table whose header row names the earlier numbering, or a sentence naming the id with "is now",
    "reads as", "resolves to", "formerly", "renumbered" or "was numbered". A citation by that id stays valid, so the
    renumbering drops nothing (#989 folding SemisimpleAlgebras Layer 1.5 into §1.3, #617's "Former
    layer numbers"). A heading anchor does not follow a mapping: a link to it lands at the top."""
    if c.kind == "anchor" or not text:
        return False
    label = _label(c)
    header = None
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        if header is None:
            header = stripped
            continue
        cells = [x.strip() for x in stripped.strip("|").split("|")]
        if MAP_HEADER.search(header) and cells and label.search(cells[0]):
            return True
    prose = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return any(label.search(s) and MAP_SENTENCE.search(s) for s in re.split(r"(?<=[.;])\s+|\n", prose))


# --- the facts ------------------------------------------------------------------------------------

def folders_of(p: dict, dirs: tuple[str, ...]) -> list[str]:
    """Every roadmap folder an indexed pull request touches, by its full path (`touched_folders` of its
    changed files; the index's name-keyed `folders` for an index without them)."""
    files = [f.get("path", "") for f in p.get("files") or []]
    return o.touched_folders(files, dirs) if files else sorted(set((p.get("folders") or {}).values()))


def rename_source(state: Path, base: str, ref: str, folder: str, files: list[dict], on_main: dict,
                  dirs: tuple[str, ...]) -> str | None:
    """The folder a pull request moves to `folder`, which is new at that path: the folder whose README.md
    the listing (`previous`) or git (`git diff -M` from the merge base to the head) pairs with
    `folder/README.md` as renamed; else the folder main holds under the same name, when the pull request
    removes its README.md and git pairs it with no other. None otherwise: a folder of the same name
    elsewhere, or a rename whose source nobody names, is no evidence of a move."""
    readme = f"{folder}/README.md"
    moves = [f for f in files if f.get("path", "").endswith("README.md") and f.get("change") in ("renamed", "removed")]
    if not base or not moves:
        return None
    for f in moves:
        if f.get("path") == readme and f.get("previous", "").endswith("/README.md"):
            return f["previous"][:-len("/README.md")]
    result = subprocess.run(["git", "-C", str(o.mirror(state)), "diff", "-M", "--name-status", "--diff-filter=R",
                             base, ref, "--", *dirs], capture_output=True, text=True, check=False)
    pairs = {}
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].endswith("/README.md"):
            pairs[parts[1]] = parts[2]
    for old, new in pairs.items():
        if new == readme:
            return old[:-len("/README.md")]
    there = on_main.get(folder.rsplit("/", 1)[-1])
    if (there and there != folder and f"{there}/README.md" not in pairs
            and any(f.get("path") == f"{there}/README.md" and f.get("change") == "removed" for f in files)):
        return there
    return None


def _changes(state: Path, index: dict, *, only: str = "", pr: int = 0) -> tuple[list[Revision], list[Copy]]:
    """(revisions, copies). A revision: a roadmap on main whose README an open pull request changes
    since its merge base with main (`only` and `pr` narrow it to one roadmap or pull request). A head
    the mirror lacks is skipped. A copy: a roadmap main has now and had neither at the head's path nor
    at its own when the pull request branched, which the head adds all the same (it moves no roadmap
    there): main gained it since, from another pull request. The pull request's own listing must name
    the README as added: one opened against the branch of the pull request that merged lists it as
    changed or not at all, and is a revision waiting for a new base, no copy.

    The README is read where the head has it (`folders_of`, by full path): at the merge base there, and
    on main there too, or, when main has moved the roadmap since (no README at that path on main, none
    at main's path at the merge base), where main keeps it now. A folder new at its path is a revision
    only when the pull request moves a roadmap there (`rename_source`), read at the source; another
    folder of the same name is another roadmap."""
    main_ref, on_main = f"refs/heads/{index.get('branch', 'main')}", index.get("main_folders") or {}
    dirs = tuple(index.get("dirs", []))
    found = [(p, folder.rsplit("/", 1)[-1], folder) for p in index.get("prs", []) if not pr or p["pr"] == pr
             for folder in folders_of(p, dirs)
             if folder.rsplit("/", 1)[-1] in on_main and (not only or folder.rsplit("/", 1)[-1] == only)]
    bases = {p["pr"]: _merge_base(state, main_ref, o.pr_ref(p["pr"])) for p, _n, _f in found}
    found = [f for f in found if bases[f[0]["pr"]]]
    specs = [f"{at}:{path}/README.md" for p, name, folder in found
             for at, path in ((bases[p["pr"]], folder), (main_ref, folder), (bases[p["pr"]], on_main[name]),
                              (main_ref, on_main[name]), (o.pr_ref(p["pr"]), folder))]
    texts = _batch(state, specs)
    out, copies = [], []
    for i, (p, name, folder) in enumerate(found):
        base, main, base_there, main_there, head = texts[5 * i:5 * i + 5]
        if base is not None:
            if main is None and base_there is None:
                main = main_there                        # main moved it since the head branched
        else:
            source = rename_source(state, bases[p["pr"]], o.pr_ref(p["pr"]), folder, p.get("files") or [],
                                   on_main, dirs)
            if source:
                base, main = _batch(state, [f"{bases[p['pr']]}:{source}/README.md", f"{main_ref}:{source}/README.md"])
            elif (base_there is None and head and (main or main_there) and head != (main or main_there)
                  and any(f.get("path") == f"{folder}/README.md" and f.get("change") == "added"
                          for f in p.get("files") or [])):
                copies.append(Copy(p, name, folder, bases[p["pr"]], main or main_there, head))
        if main is None or base is None:
            continue                     # the roadmap is not on main, or not yet where the head branched
        if (head or "") != base:
            out.append(Revision(p, name, main, base, head or ""))
    return out, copies


def revisions(state: Path, index: dict, *, only: str = "", pr: int = 0) -> list[Revision]:
    """Each roadmap on main whose README an open pull request changes since its merge base with main
    (`_changes`)."""
    return _changes(state, index, only=only, pr=pr)[0]


def copies(state: Path, index: dict, *, only: str = "", pr: int = 0) -> list[Copy]:
    """Each roadmap main has gained since an open pull request branched that the pull request adds
    again (`_changes`): two texts of one roadmap, of which main's is the merged one."""
    return _changes(state, index, only=only, pr=pr)[1]


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
    defines), the revisions of `pr` itself aside: an id its own revision of the cited roadmap defines
    too is its own, whichever other revision adds it as well."""
    changed = revisions(state, index) if changed is None else changed
    mine = {r.name: r.head for r in changed if pr > 0 and r.pr["pr"] == pr}
    changed = [r for r in changed if r.pr["pr"] != pr]
    cites = citations(readme, {r.name for r in changed}, citer=own, where=f"#{pr}" if pr > 0 else "this README")
    dropped, adopted = [], []
    for c in cites:
        for rev in changed:
            if rev.name == c.cited:
                if rev.drops(c):
                    dropped.append(Removal(rev.pr["pr"], rev.pr.get("author", ""), c))
                elif rev.adds(c) and not (c.cited in mine and defines(mine[c.cited], c)):
                    adopted.append(Removal(rev.pr["pr"], rev.pr.get("author", ""), c))
    return dropped, adopted


def revision_dependencies(state: Path, index: dict, readme: str, own: str, *, pr: int = 0,
                          changed: list[Revision] | None = None) -> list[tuple[int, str, str]]:
    """(pull request, roadmap, author) of each open revision whose numbering `readme` cites: an id only
    its head defines. That revision must merge first."""
    _dropped, adopted = readme_facts(state, index, readme, own, pr=pr, changed=changed)
    return sorted({(r.pr, r.citation.cited, r.author) for r in adopted})


NUMBER = re.compile(r"(?<![\w/&])#(\d{1,6})\b")
# A sentence that says this pull request waits for the pull requests named after the phrase.
# ("merges before #N" is the other direction: #N waits for this one.)
DEPENDS_ON = re.compile(r"\bdepends?\s+on\b|\bmerges?\s+first\b|\bmerges?\s+(?:only\s+)?after\b"
                        r"|\bmerge\s+order:?\**\s*after\b"
                        r"|\bafter\b(?=[^.;]*#\d+[^.;]*\b(?:lands?|merges?|is\s+merged)\b)", re.IGNORECASE)
# A sentence whose subject is a list of pull requests that merge first: "#1125 and #1124 must merge
# first", "#7 merges before this pull request".
SUBJECT_FIRST = re.compile(r"((?:#\d+[\s,]*(?:(?:and|&)\s+)?)+)(?:(?:must|should|will|needs?\s+to|ha(?:ve|s)\s+to)\s+)?"
                           r"merges?\s+(?:first|before\s+(?:this\s+(?:pull\s+request|PR)|it)\b)", re.IGNORECASE)
SELF_SUBJECT = re.compile(r"\b(?:this\s+(?:pull\s+request|PR)|it)\s+(?:(?:must|should|will|can|needs?\s+to)\s+)?$",
                          re.IGNORECASE)
NEGATED = re.compile(r"\b(?:not|never|no\s+longer|n't|no\s+other)\b[^.;]{0,30}$|\bon\s+no\b", re.IGNORECASE)
ABOUT_OTHERS = re.compile(r"^\s*(?:on\s+)?(?:this|the\s+present)\s+(?:pull\s+request|PR)\b|^\s*(?:on\s+)?it\b",
                          re.IGNORECASE)
STACK = re.compile(r"\*\*Stack\.\*\*|^\s*Stack:", re.IGNORECASE)
CHAIN_END = re.compile(r"(?:→|->)\s*\**\s*this\s+(?:pull\s+request|PR)\b", re.IGNORECASE)
BULLET = re.compile(r"^(?:[-*+]|\d+\.)\s")
LIST_HEAD = re.compile(r"\b(?:depends\s+on|dependencies)\s*:\s*$", re.IGNORECASE)
# The structured list a description may carry: `<!-- depends-on -->`, a header, then one checkbox per
# dependency, "- [ ] #1126 AnalyticNumberTheoryPartI" or "- [ ] PlanarTopology (no pull request yet)".
DEPENDS_BLOCK = re.compile(r"<!--\s*depends-on\s*-->(.*?)<!--\s*/depends-on\s*-->", re.DOTALL)
LEAN_IMPORT = re.compile(r"^\s*(?:public\s+)?import\s+TauCetiRoadmap\.([A-Z][A-Za-z0-9]*)", re.MULTILINE)
PATH_SPAN = re.compile(r"`[^`\n]*/[^`\n]*`")     # `ModularForms/LevelOne/TraceFormula/`: a path, no citation
BLOCK_ITEM = re.compile(r"^\s*[-*+]\s*\[(?P<done>[ xX])\]\s*(?:#(?P<pr>\d+)\b)?\s*(?P<name>[A-Z][A-Za-z0-9]+)?")


def _sentence_dependencies(sentence: str) -> set[int]:
    """The `#N` a sentence names as this pull request's prerequisites: those after a positive phrase
    ("depends on", "which merge first:", "merges after", "after ... lands"), not after a negated one
    ("does not depend on", "depends on no other"), and not when the phrase is about this pull request
    ("#22 depends on this pull request", "it merges first"); and those that are the subject of "merge
    first" or "merges before this pull request" ("#1125 and #1124 must merge first"). "Merges before
    #N" names a pull request that waits for this one, and "can merge before or after #N" none."""
    out: set[int] = set()
    for m in DEPENDS_ON.finditer(sentence):
        if NEGATED.search(sentence[:m.end()]) or NEGATED.search(sentence[m.end():m.end() + 12]):
            continue
        rest = sentence[m.end():]
        if ABOUT_OTHERS.match(rest):
            continue
        if re.search(r"first", m.group(0), re.IGNORECASE) and SELF_SUBJECT.search(sentence[:m.start()]):
            continue                     # "this pull request merges first": what follows waits for it
        out |= {int(n) for n in NUMBER.findall(rest)}
    for m in SUBJECT_FIRST.finditer(sentence):
        out |= {int(n) for n in NUMBER.findall(m.group(1))}
    return out


def declared_items(body: str, own: int = 0) -> list[tuple[int | None, str | None]]:
    """(pull request, roadmap) for each dependency a description names: every unchecked item of its
    `<!-- depends-on -->` block (a checked one is met), with its number, its roadmap name or both, and
    (N, None) for every `#N` its prose names (`declared_dependencies`). Never the pull request itself."""
    body = body or ""
    out: set[tuple[int | None, str | None]] = set()
    for block in DEPENDS_BLOCK.findall(body):
        for line in block.split("\n"):
            m = BLOCK_ITEM.match(line)
            if m and m["done"] == " " and (m["pr"] or m["name"]):
                number = int(m["pr"]) if m["pr"] else None
                if number != own:
                    out.add((number, m["name"]))
    out |= {(n, None) for n in declared_dependencies(DEPENDS_BLOCK.sub("", body), own=own)}
    return sorted(out, key=lambda x: (x[0] or 0, x[1] or ""))


def declared_dependencies(body: str, own: int = 0) -> list[int]:
    """The pull requests a description names as this one's dependencies: the `#N` after a phrase that says
    so in a sentence ("Depends on #887", "now depends on the revisions #1222 and #1015", "which must merge
    first: #934", "Merge order: after AnalyticNumberTheoryPartI (#1126) lands", "merges after #12"), those
    of a chain that ends in this pull request ("#1101 → #1807 → this pull request"), the
    bullets under a line ending "Depends on:" or "Dependencies:", and a "Stack." line before "this pull
    request". Never one named before the phrase or about another pull request ("#1808 is independent of
    this pull request", "#22 depends on this pull request"), after a negation ("does not depend on #20"),
    or the pull request itself."""
    out: set[int] = set()
    listing, listed = False, False
    for line in (body or "").split("\n"):
        stripped = line.strip()
        if listing:
            if BULLET.match(stripped):
                out |= {int(n) for n in NUMBER.findall(stripped)}
                listed = True
                continue
            if not stripped and not listed:
                continue
            listing = False
        if STACK.search(line):
            out |= {int(n) for n in NUMBER.findall(line.split("this pull request")[0])}
            continue
        for sentence in re.split(r"(?<=[.;!?])\s+", line):
            out |= _sentence_dependencies(sentence)
            chain = CHAIN_END.search(sentence)          # "#1101 → #1807 → this pull request"
            if chain:
                out |= {int(n) for n in NUMBER.findall(sentence[:chain.start()])}
        if LIST_HEAD.search(stripped):
            listing, listed = True, False
    out.discard(own)
    return sorted(out)


def dependency_record(state: Path, index: dict, readme: str, lean: str, name: str, *, pr: int = 0, body: str = "",
                      changed: list[Revision] | None = None) -> dict:
    """What the pull request `pr` waits for, read from the README `readme` and Suggested.lean `lean` of its
    roadmap `name` and from its description `body`:

    * `suppliers`: the roadmaps it names as suppliers (as the dependency check reads a README, file paths
      in code spans aside, and every `import TauCetiRoadmap.<X>` of its Lean; a `namespace` names none)
      that are not on main, each with the open pull requests that add it and whether the README or the
      Lean names it (`cited`; false for one only its description names). One is met once that roadmap
      is on main, whichever pull request brings it there.
    * `revisions`: the open revisions whose numbering it cites (`readme_facts`), with the ids it cites
      that only each defines ("Lattices §1.4"), met once merged.
    * `declared`: the other dependencies its description names (`declared_items`: its `<!-- depends-on -->`
      block and its prose), as {pr, roadmap}: a bare number, or a block item whose roadmap is on main.
      Whether one is met depends on that pull request (`deplabels.met`): merged; or it was the pull
      request that added the roadmap, which another one has since brought to main. A block item whose
      roadmap is not on main is a supplier instead, met once that roadmap is on main whichever pull
      request brings it (so "- [ ] PlanarTopology (no pull request yet)" waits too).

    The roadmaps the pull request itself carries are never its dependencies."""
    main = set(index.get("main_roadmaps", []))
    own = {name} | next((set(p.get("roadmaps", [])) for p in index.get("prs", []) if p["pr"] == pr), set())
    adders: dict[str, list[int]] = {}
    for p in index.get("prs", []):
        for added in p.get("adds", []):
            adders.setdefault(added, []).append(p["pr"])
    prose = PATH_SPAN.sub(" ", readme)
    suppliers = (set(depgraph.supplier_edges({name: prose}, main | set(adders)).get(name, {}))
                 | set(LEAN_IMPORT.findall(lean))) - main - own
    cited = set(suppliers)
    declared: set[tuple[int, str | None]] = set()
    for number, roadmap in declared_items(body, own=pr):
        if roadmap and roadmap in own:
            continue
        if roadmap and roadmap not in main:          # a new roadmap: met once it is on main
            suppliers.add(roadmap)
            if number and number not in adders.get(roadmap, []):
                adders.setdefault(roadmap, []).append(number)
        elif number:                                 # its pull request decides (`deplabels`)
            declared.add((number, roadmap))
    ids: dict[tuple[int, str], set[str]] = {}
    for got in readme_facts(state, index, readme, name, pr=pr, changed=changed)[1]:
        ids.setdefault((got.pr, got.citation.cited), set()).add(got.citation.label())
    return {"suppliers": [{"roadmap": s, "prs": sorted(n for n in adders.get(s, []) if n != pr), "cited": s in cited}
                          for s in sorted(suppliers)],
            "revisions": [{"roadmap": r, "pr": q, "ids": sorted(i)} for (q, r), i in sorted(ids.items())],
            "declared": [{"pr": n, "roadmap": r} for n, r in sorted(declared, key=lambda x: (x[0], x[1] or ""))]}


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
               k: int = NEIGHBOURS, on_main: bool = False) -> list[tuple[float, str, str]]:
    """The `k` open roadmap folders (where, roadmap) whose README is closest to `readme` in vocabulary,
    as (cosine, where, roadmap), closest first; `texts` as `readmes` gives them. The roadmaps on main
    weigh the words but are not listed, nor is `own` or anything `own_pr` carries. With `on_main` it
    is the converse: the `k` closest roadmaps on main, the open heads weighing the words."""
    counts = [Counter(WORD.findall(text.lower())) for _w, _n, text in texts]
    mine_tf = Counter(WORD.findall(readme.lower()))
    df = Counter(mine_tf.keys())
    for tf in counts:
        df.update(tf.keys())
    n = len(texts) + 1
    mine = _vector(mine_tf, df, n)
    scored = []
    for (where, name, _text), tf in zip(texts, counts):
        if (where == "main") != on_main or name == own or where == f"#{own_pr}":
            continue
        theirs = _vector(tf, df, n)
        small, big = (mine, theirs) if len(mine) < len(theirs) else (theirs, mine)
        score = sum(x * big.get(w, 0.0) for w, x in small.items())
        if score >= MIN_SIMILARITY:
            scored.append((round(score, 3), where, name))
    return sorted(scored, key=lambda s: (-s[0], s[1], s[2]))[:k]


def _subject(text: str) -> tuple[Counter, Counter]:
    """(the results a README names, the words and word pairs of its headings), its supplier section
    aside: two roadmaps that share suppliers do not share a subject for that."""
    text = SUPPLIER_SECTION.sub("", text)
    names = Counter(w for w in NAMED.findall(text) if w not in HOUSE_NAMES)
    heads: Counter = Counter()
    for h in SUBHEADING.findall(text):
        if HOUSE_HEADING.match(h.strip()):
            continue
        words = WORD.findall(h.lower())
        heads.update(words)
        heads.update(f"{a} {b}" for a, b in pairwise(words))
    return names, heads


def _heaviest(tf: Counter, df: Counter, n: int) -> dict[str, float]:
    v = {w: (1 + math.log(k)) * math.log(n / df[w]) for w, k in tf.items() if 2 <= df[w] <= SUBJECT_COMMON * n}
    v = dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[:SUBJECT_TERMS])
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {w: x / norm for w, x in v.items()}


def subject_neighbours(texts: list[tuple[str, str, str]], readme: str, *, own: str = "", own_pr: int = 0,
                       k: int = SUBJECT_NEIGHBOURS) -> list[tuple[float, str, str]]:
    """The `k` roadmaps, on main or at an open head, closest to `readme` in the results they name and
    in their headings, as (name cosine + heading cosine, where, roadmap), closest first. A roadmap read
    at several places (main and its open revisions) counts once in the weights and is listed once, at
    its closest place. Neither `own` nor anything `own_pr` carries is listed."""
    subjects = [_subject(text) for _w, _n, text in texts]
    mine = _subject(readme)
    seen: dict[str, tuple[set, set]] = {own or "": (set(mine[0]), set(mine[1]))}
    for (_where, name, _text), (names, heads) in zip(texts, subjects):
        a, b = seen.setdefault(name, (set(), set()))
        a.update(names)
        b.update(heads)
    n = len(seen)
    df = [Counter(w for sets in seen.values() for w in sets[i]) for i in (0, 1)]
    mine_v = [_heaviest(mine[i], df[i], n) for i in (0, 1)]
    best: dict[str, tuple[float, str]] = {}
    for (where, name, _text), subject in zip(texts, subjects):
        if name == own or where == f"#{own_pr}":
            continue
        score = 0.0
        for i in (0, 1):
            theirs = _heaviest(subject[i], df[i], n)
            small, big = (mine_v[i], theirs) if len(mine_v[i]) < len(theirs) else (theirs, mine_v[i])
            score += sum(x * big.get(w, 0.0) for w, x in small.items())
        score = round(score, 3)
        if score >= MIN_SUBJECT and (name not in best or (score, where == "main") > (best[name][0],
                                                                                    best[name][1] == "main")):
            best[name] = (score, where)
    return sorted(((score, where, name) for name, (score, where) in best.items()),
                  key=lambda s: (-s[0], s[1], s[2]))[:k]


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


def _copy_text(copy: Copy, texts: list[tuple[str, str, str]]) -> list[str]:
    """What a second copy is, in a report: where the two texts are, how much of main's the head has,
    and the ids main defines that the head lacks and other roadmaps cite."""
    main_lines = {line.strip() for line in copy.main.splitlines() if line.strip()}
    head_lines = {line.strip() for line in copy.head.splitlines() if line.strip()}
    kept = round(100 * len(main_lines & head_lines) / max(len(main_lines), 1))
    lines = [(f"- #{copy.pr['pr']} (@{copy.pr.get('author', '?')}) adds `{copy.folder}`, and main has the roadmap "
              f"{copy.name} already: it was not there when this pull request branched (merge base {copy.base[:7]}), "
              "so another pull request merged it since. The README has "
              f"{len(copy.main.splitlines())} lines on main and {len(copy.head.splitlines())} at this head, which "
              f"has {kept}% of main's lines. The two texts cannot both merge: this one becomes a revision of "
              "main's (main merged into the branch, every id main defines kept or mapped), or is closed in its "
              "favour. A head that has nearly all of main's lines was stacked on the pull request that merged, "
              "and merging main into it is the whole repair.")]
    missing: dict[str, list[Removal]] = {}
    for where, citer, text in texts:
        if citer == copy.name:
            continue
        for c in citations(text, {copy.name}, where=where, citer=citer):
            if copy.lacks(c):
                missing.setdefault(c.label(), []).append(Removal(copy.pr["pr"], copy.pr.get("author", ""), c))
    if missing:
        lines.append(f"- Ids main defines that this head lacks and other roadmaps cite ({len(missing)}):")
        lines += [f"  - {label}: cited by {_citers(rs)}" for label, rs in sorted(missing.items())]
    return lines


def report(state: Path, index: dict, name: str, readme: str, *, pr: int = 0,
           texts: list[tuple[str, str, str]] | None = None) -> tuple[str, int]:
    """(Markdown, number of facts) for the roadmap `name` whose README is `readme`, at the head of open
    pull request `pr` when it is one: the other open pull requests that change its folder, the ids it
    cites that an open pull request drops or that only an open revision defines, the ids `pr` drops
    that other roadmaps cite, and its closest neighbours, open and on main. First of all, when `pr`
    adds a roadmap main already has, that it is a second copy."""
    texts = readmes(state, index) if texts is None else texts
    lines, facts = _header(index, f"Overlaps of {name} with the open roadmap pull requests"), 0

    for copy in (copies(state, index, only=name, pr=pr) if pr > 0 else []):
        facts += 1
        lines += ["## A second copy of a roadmap on main", "", *_copy_text(copy, texts), ""]

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
    near = neighbours(texts, readme, own=name, own_pr=pr, k=MAIN_NEIGHBOURS, on_main=True)
    lines += ["", "## Roadmaps on main closest in vocabulary", ""]
    lines += ([f"- {where} {roadmap} ({score:.2f})" for score, where, roadmap in near] or ["None."])
    near = subject_neighbours(texts, readme, own=name, own_pr=pr)
    lines += ["", "## Roadmaps closest in the results they name and in their headings", "",
              ("Open or on main; the score is at most 2, and the higher it is the likelier a second plan of "
               "the same subject."), ""]
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
    lines += ["", "## Open pull requests that add a roadmap main already has", ""]
    again = copies(state, index)
    lines += [f"- #{c.pr['pr']} (@{c.pr.get('author', '?')}) adds `{c.folder}`: main has {c.name} since the pull "
              f"request branched ({len(c.main.splitlines())} lines on main, {len(c.head.splitlines())} at the head)"
              for c in sorted(again, key=lambda c: c.pr["pr"])] or ["None."]
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
