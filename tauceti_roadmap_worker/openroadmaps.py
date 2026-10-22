"""Every open roadmap pull request, searchable, and a resolver for the citations a roadmap makes.

Neither the ownership of an object by another open pull request nor a citation of a section that a
sibling has since renumbered can be seen from main or from the merge order. This module gives
authors and reviewers the open work itself:

* `refresh` lists the content repository's open pull requests that add or change a roadmap folder
  (one GraphQL listing) and fetches their heads into a full bare mirror of the repository shared by
  every worker of the host (`<state>/open-roadmaps/mirror.git`, a few seconds and tens of MB), under a
  lock, at most once per `MAX_AGE`. The index is `<state>/open-roadmaps/index.json`.
* `grep` searches the README and Suggested.lean of every roadmap on main and of the roadmap folders
  each open pull request carries, at its head; `show` prints one of those files.
* `resolve` checks a README's cross-roadmap citations (`Name §1.2`, `Name Layer 3`, `Name A1.2`)
  against the cited roadmap on main and at the heads of the open pull requests that carry it, and its
  backticked Lean names against the pinned libraries' sources (and `TauCetiRoadmap.<Name>.x` against
  that roadmap's Suggested.lean). It reports only what is found nowhere: a citation whose id the cited
  roadmap does not contain at all, a cited roadmap that exists neither on main nor in an open pull
  request, a Lean name whose last component occurs nowhere in the sources. These are hints for the
  agent, never gates: generated Lean names (`to_additive`, `simps`, structure projections) can be
  missing literally.

Everything works from the local mirror: an agent runs `grep`, `show` and `resolve` with no GitHub
token, and only `refresh` reads GitHub.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

from . import gitops


def git(cwd, *args: str, check: bool = True) -> str:
    return gitops.git(cwd, *args, check=check)

MAX_AGE = 1800                 # seconds an index is used before it is rebuilt
FETCH_BATCH = 100              # pull-request refspecs per `git fetch`
TEXT_WIDTH = 240               # characters of a matching line shown
FILES = ("README.md", "Suggested.lean")

QUERY = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title isDraft headRefOid author { login } files(first: 100) { totalCount nodes { path } } }
    }
  }
}"""


def home(state: Path) -> Path:
    return Path(state) / "open-roadmaps"


def mirror(state: Path) -> Path:
    return home(state) / "mirror.git"


def index_path(state: Path) -> Path:
    return home(state) / "index.json"


def pr_ref(number: int) -> str:
    return f"refs/open/{number}"


@contextmanager
def _lock(state: Path):
    home(state).mkdir(parents=True, exist_ok=True)
    with open(home(state) / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


ROADMAP_FILES = ("README.md", "Suggested.lean", "metadata.toml")


def roadmap_folders(paths: list[str], dirs: tuple[str, ...]) -> dict[str, str]:
    """Roadmap name -> folder for the roadmaps a pull request's changed paths touch: the folder of each
    changed README.md, Suggested.lean or metadata.toml under a roadmap directory, at any depth
    (`TauCetiRoadmap/RepresentationTheory/CharacterTheory/README.md` is CharacterTheory)."""
    out: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] in dirs and parts[-1] in ROADMAP_FILES:
            out.setdefault(parts[-2], "/".join(parts[:-1]))
    return out


def inventory(gh, repo: str, dirs: tuple[str, ...]) -> list[dict]:
    """The repository's open pull requests that touch a roadmap folder: number, title, author, head,
    draft, the roadmap folders they touch."""
    owner, name = repo.split("/", 1)
    out, after = [], None
    while True:
        data = gh.graphql(QUERY, owner=owner, name=name, after=after)
        page = ((data.get("repository") or {}).get("pullRequests") or {})
        for pr in page.get("nodes") or []:
            files = pr.get("files") or {}
            paths = [f.get("path", "") for f in files.get("nodes") or []]
            if int(files.get("totalCount") or 0) > len(paths):
                # The listing gives the first 100 files; the rest come from the REST listing (rare).
                paths = [f.get("filename", "") for f in gh.api(f"repos/{repo}/pulls/{pr['number']}/files?per_page=100",
                                                               paginate=True) or []]
            folders = roadmap_folders(paths, dirs)
            if folders:
                out.append({"pr": pr["number"], "title": pr.get("title", ""), "head": pr.get("headRefOid", ""),
                            "author": (pr.get("author") or {}).get("login", ""), "draft": bool(pr.get("isDraft")),
                            "roadmaps": list(folders), "folders": folders})
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return out
        after = info.get("endCursor")


def _main_ref(branch: str) -> str:
    return f"refs/heads/{branch}"


def main_roadmaps(state: Path, branch: str, dirs: tuple[str, ...]) -> dict[str, str]:
    """Roadmap name -> its folder on main, in the mirror: every folder under the roadmap directories
    that holds a README.md, at any depth."""
    out = {}
    listing = git(mirror(state), "ls-tree", "-r", "--name-only", _main_ref(branch), "--", *dirs, check=False)
    for path in listing.splitlines():
        parts = path.split("/")
        if len(parts) >= 3 and parts[-1] == "README.md":
            out.setdefault(parts[-2], "/".join(parts[:-1]))
    return out


def load(state: Path) -> dict:
    try:
        data = json.loads(index_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def age(index: dict, now: dt.datetime | None = None) -> float:
    try:
        built = dt.datetime.fromisoformat(index["at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return float("inf")
    return ((now or dt.datetime.now(dt.UTC)) - built).total_seconds()


def refresh(gh, cfg, *, force: bool = False, now: dt.datetime | None = None) -> dict:
    """The index of open roadmap pull requests, rebuilt when older than MAX_AGE (or `force`): the
    listing from GitHub, every head fetched into the shared mirror, stale heads dropped. Raises on a
    GitHub or git failure; the caller decides what a missing index means."""
    state, repo, branch = cfg.state_dir(), cfg.repositories.content, cfg.project.branch
    dirs = (cfg.roadmap.active_dir, cfg.roadmap.completed_dir)
    with _lock(state):
        current = load(state)
        if not force and current.get("repo") == repo and age(current, now) < MAX_AGE:
            return current
        prs = inventory(gh, repo, dirs)          # GitHub first: nothing is cloned when it cannot be read
        if not (mirror(state) / "HEAD").exists():
            mirror(state).parent.mkdir(parents=True, exist_ok=True)
            git(None, "clone", "-q", "--bare", "--no-tags", gitops.url(repo), str(mirror(state)))
        refspecs = [f"+refs/heads/{branch}:{_main_ref(branch)}"] + \
                   [f"+refs/pull/{p['pr']}/head:{pr_ref(p['pr'])}" for p in prs]
        for i in range(0, len(refspecs), FETCH_BATCH):
            git(mirror(state), "fetch", "-q", "--no-tags", "origin", *refspecs[i:i + FETCH_BATCH])
        keep = {pr_ref(p["pr"]) for p in prs}
        for ref in git(mirror(state), "for-each-ref", "--format=%(refname)", "refs/open/").split():
            if ref not in keep:
                git(mirror(state), "update-ref", "-d", ref, check=False)
        on_main = main_roadmaps(state, branch, dirs)
        for p in prs:
            p["adds"] = [n for n in p["roadmaps"] if n not in on_main]
        index = {"at": (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"), "repo": repo, "branch": branch,
                 "main": git(mirror(state), "rev-parse", _main_ref(branch)).strip(), "dirs": list(dirs),
                 "main_roadmaps": sorted(on_main), "main_folders": on_main, "prs": prs}
        tmp = index_path(state).with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, index_path(state))
        return index


def rows(index: dict) -> list[str]:
    """One line per open roadmap pull request, for `list` and the briefs."""
    out = []
    for p in index.get("prs", []):
        names = ", ".join(f"{n} (new)" if n in p.get("adds", []) else n for n in p["roadmaps"])
        out.append(f"#{p['pr']}  {names}  @{p['author']}  {p['head'][:7]}{'  draft' if p.get('draft') else ''}  "
                   f"{p['title'][:100]}")
    return out


def _paths(index: dict, name: str, files=FILES, pr: dict | None = None) -> list[str]:
    """The files of roadmap `name` in a pull request's tree (its folder there) or on main."""
    folder = ((pr or {}).get("folders") or {}).get(name) or (index.get("main_folders") or {}).get(name)
    folders = [folder] if folder else [f"{d}/{name}" for d in index.get("dirs", [])]
    return [f"{f}/{file}" for f in folders for file in files]


def sources_for(index: dict, name: str) -> list[tuple[str, str]]:
    """(label, tree-ish) of every place the roadmap `name` can be read: main, then each open pull
    request that carries it, at its head."""
    out = []
    if name in index.get("main_roadmaps", []):
        out.append(("main", _main_ref(index.get("branch", "main"))))
    out += [(f"#{p['pr']}", pr_ref(p["pr"])) for p in index.get("prs", []) if name in p["roadmaps"]]
    return out


def read(state: Path, index: dict, treeish: str, name: str, file: str = "README.md") -> str:
    """The text of roadmap `name`'s `file` at a tree-ish of the mirror ('' when absent)."""
    pr = next((p for p in index.get("prs", []) if pr_ref(p["pr"]) == treeish), None)
    for path in _paths(index, name, (file,), pr):
        text = git(mirror(state), "show", f"{treeish}:{path}", check=False)
        if text:
            return text
    return ""


def grep(state: Path, index: dict, pattern: str, *, fixed: bool = False, ignore_case: bool = False,
         lean: bool = True, readme: bool = True, limit: int = 200) -> list[str]:
    """Matching lines of the roadmaps on main and of the folders each open pull request carries, at its
    head: `main: <path>:<line>: <text>` or `#<pr> <Name> (@author): <path>:<line>: <text>`."""
    files = tuple(f for f, on in (("README.md", readme), ("Suggested.lean", lean)) if on)
    if not files:
        return []
    flags = ["-n", "-I", "-F" if fixed else "-E"] + (["-i"] if ignore_case else [])
    targets = []          # open work first: main is in every worker's checkout already
    for p in index.get("prs", []):
        paths = [path for n in p["roadmaps"] for path in _paths(index, n, files, p)]
        targets.append((f"#{p['pr']} {', '.join(p['roadmaps'])} (@{p['author']})", pr_ref(p["pr"]), paths))
    targets.append(("main", _main_ref(index.get("branch", "main")),
                    [f"{d}/*/{f}" for d in index.get("dirs", []) for f in files]))
    out, more = [], 0
    for label, ref, paths in targets:
        result = subprocess.run(["git", "-C", str(mirror(state)), "grep", *flags, "-e", pattern, ref, "--", *paths],
                                capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise ValueError(result.stderr.strip()[:300] or "git grep failed")
        for line in result.stdout.splitlines():
            line = line.removeprefix(ref + ":")
            path, _, rest = line.partition(":")
            number, _, text = rest.partition(":")
            text = " ".join(text.split())
            if len(out) < limit:
                out.append(f"{label}: {path}:{number}: {text[:TEXT_WIDTH]}")
            else:
                more += 1
    if more:
        out.append(f"... {more} more matching lines: narrow the pattern")
    return out


# --- resolving a README's citations ----------------------------------------------------------------

CAMEL = r"[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+"
MIN_NAME = 8      # shorter CamelCase words before a section sign are source keys (`McNinch §2.2`, `TopK T1.5`)
NOT_ROADMAPS = {"TauCeti", "TauCetiRoadmap", "Mathlib", "GitHub", "LeanSearch", "MathOverflow", "ArXiv", "Loogle"}
CITE_TAIL = (r",?\s+(?:§§?\s?(?P<sec>[0-9A-Za-z](?:[\w.]*[0-9A-Za-z])?)|Layers?\s+(?P<layer>\d+)"
             r"|(?P<pre>[A-Z]{1,3}\d+(?:\.[0-9A-Za-z]+)+))")
LEAN_NAME = re.compile(r"`(@?[A-Za-z_][\w'!?₀-₉]*(?:\.[A-Za-z_][\w'!?₀-₉]*)+)`")
TOKEN = re.compile(r"[A-Za-z_][\w'!?₀-₉ₐ-ₜ]*")
ADDITIVE = (("add", "mul"), ("Add", "Mul"), ("zero", "one"), ("Zero", "One"), ("neg", "inv"), ("Neg", "Inv"),
            ("sub", "div"), ("Sub", "Div"), ("nsmul", "pow"), ("zsmul", "zpow"), ("sum", "prod"), ("Sum", "Prod"))


def _labelled(text: str, token: str) -> bool:
    """`token` appears in a heading or at the start of a bold label or bullet of `text`."""
    tok = re.compile(r"(?<![\w.])§?\s?" + re.escape(token) + r"(?![\w])")
    for line in text.split("\n"):
        head = line.lstrip()
        if head.startswith(("#", "**", "- **", "* **", "<a ")) and tok.search(line):
            return True
    return False


def _present(text: str, token: str, layer: bool = False) -> bool:
    """The cited id occurs in the roadmap: literally, or as item N of a numbered list under the heading
    or label of its parent id (`B1.4` is the fourth item under `## B1. Principal bundles`)."""
    if layer:
        return re.search(r"\bLayers?\s+" + re.escape(token) + r"\b", text) is not None
    if re.search(r"(?<![\w.])" + re.escape(token) + r"(?![\w])", text):
        return True
    parent, _, last = token.rpartition(".")
    return bool(parent and last.isdigit()) and _enumerated(text, parent, int(last))


def _enumerated(text: str, parent: str, n: int) -> bool:
    """Whether the section headed by `parent` (a heading or bold label naming it) has at least n
    numbered items before the next heading."""
    lines = text.split("\n")
    tok = re.compile(r"(?<![\w.])§?\s?" + re.escape(parent) + r"(?![\w])")
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("#", "**")) and tok.search(line):
            items = 0
            for following in lines[i + 1:]:
                if following.startswith("#"):
                    break
                if re.match(r"\s*\d+\.\s", following):
                    items += 1
            if items >= n:
                return True
    return False


def citation_problems(state: Path, index: dict, readme: str, own: str = "") -> list[str]:
    """`<line>: <citation>: <why>` for each cross-roadmap citation of `readme` found nowhere."""
    known = set(index.get("main_roadmaps", [])) | {n for p in index.get("prs", []) for n in p["roadmaps"]}
    names = sorted(known, key=len, reverse=True)
    cite = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b" + CITE_TAIL) if names else None
    unknown = re.compile(r"(?<![\w./])(" + CAMEL + r")" + CITE_TAIL)
    texts: dict[str, list[tuple[str, str]]] = {}
    out, seen = [], set()
    for number, line in enumerate(readme.split("\n"), 1):
        for m in unknown.finditer(line):
            name = m.group(1)
            if (name not in known and name != own and name not in NOT_ROADMAPS and len(name) >= MIN_NAME
                    and (name, "?") not in seen):
                seen.add((name, "?"))
                out.append(f"{number}: {m.group(0).strip()}: no roadmap {name} on main or in an open pull request")
        for m in cite.finditer(line) if cite else ():
            name = m.group(1)
            if name == own:
                continue
            token = (m.group("layer") or m.group("sec") or m.group("pre")).rstrip(".")
            if (name, token) in seen:
                continue
            seen.add((name, token))
            if name not in texts:
                texts[name] = [(label, read(state, index, ref, name)) for label, ref in sources_for(index, name)]
            where = texts[name]
            if any(_present(text, token, layer=bool(m.group("layer"))) for _label, text in where):
                continue
            checked = ", ".join(label for label, _t in where) or "nowhere"
            out.append(f"{number}: {m.group(0).strip()}: {name} has no {'Layer ' if m.group('layer') else ''}{token} "
                       f"(checked {checked})")
    return out


def packages_dir(content: Path | None = None) -> Path | None:
    """The pinned Lake packages to resolve Lean names against: the content checkout's, else the
    host's shared ones (`TAUCETI_ROADMAP_LAKE_PACKAGES`)."""
    for candidate in ([Path(content) / ".lake" / "packages"] if content else []) + \
            ([Path(os.environ["TAUCETI_ROADMAP_LAKE_PACKAGES"])] if os.environ.get("TAUCETI_ROADMAP_LAKE_PACKAGES") else []):
        if (candidate / "mathlib").is_dir():
            return candidate
    return None


def library_tokens(state: Path, packages: Path) -> set[str]:
    """Every identifier token of the pinned Mathlib and Tau Ceti sources, cached per pin under the
    state directory (the first build reads every source file once)."""
    roots = [packages / "mathlib" / "Mathlib", packages / "TauCeti" / "TauCeti"]
    revs = [git(r.parent, "rev-parse", "HEAD", check=False).strip() or str(r.stat().st_mtime if r.exists() else 0)
            for r in roots]
    cache = home(state) / f"tokens-{hashlib.sha256('|'.join(revs).encode()).hexdigest()[:16]}.txt"
    if cache.is_file():
        return set(cache.read_text(encoding="utf-8").split("\n"))
    tokens: set[str] = set()
    for root in roots:
        for path in root.rglob("*.lean") if root.is_dir() else ():
            try:
                tokens.update(TOKEN.findall(path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text("\n".join(sorted(tokens)), encoding="utf-8")
    os.replace(tmp, cache)
    return tokens


def _multiplicative(name: str) -> str:
    for add, mul in ADDITIVE:
        name = name.replace(add, mul)
    return name


def name_problems(state: Path, index: dict, readme: str, lean: str, tokens: set[str] | None,
                  own: str = "") -> list[str]:
    """`<where>: <name>: <why>` for each backticked Lean name of the README (and each name Suggested.lean
    uses after `TauCetiRoadmap.`) that occurs nowhere: `TauCetiRoadmap.<Name>.x` in that roadmap's
    Suggested.lean on main or at an open head; any other qualified name in the pinned sources."""
    out, seen = [], set()
    roadmap_lean: dict[str, str] = {}
    first: dict[str, int] = {}           # each name at its first line, in document order
    for n, line in enumerate(readme.split("\n"), 1):
        for m in LEAN_NAME.finditer(line):
            first.setdefault(m.group(1).lstrip("@"), n)
    for full, n in sorted(first.items(), key=lambda item: (item[0], item[1])):
        where = f"README {n}"
        if full in seen:
            continue
        seen.add(full)
        parts = full.split(".")
        last = parts[-1]
        if parts[0] == "TauCetiRoadmap" and len(parts) >= 3:
            target = parts[1]
            if target not in roadmap_lean:
                roadmap_lean[target] = lean if target == own else "\n".join(
                    read(state, index, ref, target, "Suggested.lean") for _l, ref in sources_for(index, target))
            text = roadmap_lean[target]
            if text and not _present(text, last):
                out.append(f"{where}: {full}: not in {target}'s Suggested.lean on main or at an open head")
            continue
        if tokens is None or parts[0] in ("TauCetiRoadmap",):
            continue
        if last not in tokens and _multiplicative(last) not in tokens:
            out.append(f"{where}: {full}: `{last}` occurs nowhere in the pinned Mathlib and Tau Ceti sources "
                       "(a generated name? check it with #check)")
    return out


def resolve(state: Path, index: dict, readme: str, *, own: str = "", lean: str = "",
            packages: Path | None = None) -> tuple[list[str], list[str], str]:
    """(citation problems, name problems, a note on what could not be checked)."""
    note = ""
    tokens = None
    if packages is not None:
        try:
            tokens = library_tokens(state, packages)
        except OSError as exc:
            note = f"Lean names were not checked: the pinned sources could not be read ({exc})"
    else:
        note = "Lean names outside TauCetiRoadmap were not checked: no pinned Lake packages on this host"
    return citation_problems(state, index, readme, own), name_problems(state, index, readme, lean, tokens, own), note


def report(citations: list[str], names: list[str], note: str, index: dict, what: str) -> str:
    """The resolver's result as Markdown, for a job's scratch folder and the command line."""
    lines = [f"# Citations of {what}", "",
             f"Checked against main ({index.get('main', '')[:7]}) and {len(index.get('prs', []))} open roadmap pull "
             f"requests at their heads (index of {index.get('at', '?')}). Only what is found nowhere is listed; "
             "these are leads to check, not verdicts.", ""]
    lines += ["## Cross-roadmap citations found nowhere", ""] + ([f"- {c}" for c in citations] or ["None."]) + [""]
    lines += ["## Lean names found nowhere", ""] + ([f"- {n}" for n in names] or ["None."])
    if note:
        lines += ["", note + "."]
    return "\n".join(lines) + "\n"
