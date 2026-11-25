"""What the library's main branch holds past a roadmap's pin, under the proper names the roadmap uses.

A roadmap is checked against Mathlib and Tau Ceti at its pins, which is right for what `Suggested.lean`
may use and blind to what the library gained since: a pull request pinned two days before the library
received the planar Beltrami coefficient kept taking it from a roadmap that was still a pull request.
`report` lists, for the reviewer, the directories that hold Lean files on the library's main branch and
not in the pinned tree whose path carries a proper name the README uses, a new development under that
name first. They are leads: the reviewer opens the likely ones and says under `## For the human
reviewers` what main now provides, since the pin decides what the Lean file can state. Two reads of
the Git tree API a round (`git/trees/<sha>?recursive=1`), each kept in the worker's state by commit."""

from __future__ import annotations

import json
import re
from pathlib import Path

RARE = 80             # a path word counts when at most this many files of the main tree carry it
MIN_LEN = 5
LISTED = 30
WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
# The README's proper names: a capitalised word that does not open a sentence (Beltrami, Wirtinger,
# Sobolev). Ordinary words match thousands of paths; the names a subject is known by match its files.
NAME = re.compile(r"(?<=[a-zà-ÿ,;:(–-] )([A-Z][a-zà-ÿ]{%d,})\b" % (MIN_LEN - 1))
COMMON = {"layer", "layers", "theorem", "lemma", "check", "checks", "definition", "proposition", "corollary",
          "mathlib", "needs", "route", "prove", "define", "state", "section", "chapter", "springer", "cambridge",
          "oxford", "press", "university", "annals", "journal", "roadmap", "suggested", "identity", "total"}


def pinned_rev(content: Path, package: str) -> str:
    """The commit `lake-manifest.json` of the checkout pins `package` at ('' when it does not say)."""
    try:
        manifest = json.loads((content / "lake-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    return next((str(p.get("rev") or "") for p in (packages if isinstance(packages, list) else [])
                 if isinstance(p, dict) and str(p.get("name") or "").lower() == package.lower()), "")


def lean_files(gh, repo: str, sha: str, cache: Path | None) -> tuple[set[str], bool]:
    """(the `.lean` paths of the tree of `sha`, whether GitHub truncated the listing)."""
    kept = cache / f"{sha}.json" if cache is not None else None
    if kept is not None and kept.is_file():
        data = json.loads(kept.read_text(encoding="utf-8"))
        return set(data["files"]), bool(data.get("truncated"))
    tree = gh.api(f"repos/{repo}/git/trees/{sha}?recursive=1") or {}
    files = sorted(str(e.get("path")) for e in tree.get("tree") or []
                   if e.get("type") == "blob" and str(e.get("path") or "").endswith(".lean"))
    if kept is not None:
        kept.parent.mkdir(parents=True, exist_ok=True)
        for old in sorted(kept.parent.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-6]:
            old.unlink(missing_ok=True)
        kept.write_text(json.dumps({"files": files, "truncated": bool(tree.get("truncated"))}), encoding="utf-8")
    return set(files), bool(tree.get("truncated"))


def path_words(path: str) -> set[str]:
    return {w.lower() for part in path[:-len(".lean")].split("/") for w in WORD.findall(part) if len(w) >= MIN_LEN}


def names(text: str) -> set[str]:
    return {m.group(1).lower() for m in NAME.finditer(text)} - COMMON


def matches(new: set[str], main: set[str], text: str) -> list[tuple[str, int, list[str]]]:
    """The directories that hold files of `new` whose path has a proper name of `text` that few paths of
    `main` carry: (directory, how many such files, the names). First the directories that hold the largest
    share of all the files carrying their rarest name: a new development under that name."""
    counts: dict[str, int] = {}
    for path in main:
        for w in path_words(path):
            counts[w] = counts.get(w, 0) + 1
    used = names(text)
    found: dict[str, tuple[int, set[str]]] = {}
    for path in new:
        words = {w for w in path_words(path) if w in used and counts.get(w, 0) <= RARE}
        if words:
            where = path.rsplit("/", 1)[0]
            n, seen = found.get(where, (0, set()))
            found[where] = (n + 1, seen | words)
    return sorted(((d, n, sorted(ws, key=lambda w: (counts[w], w))) for d, (n, ws) in found.items()),
                  key=lambda x: (-x[1] / min(counts[w] for w in x[2]), x[0]))


def report(gh, repo: str, pin: str, branch: str, text: str, cache: Path | None = None) -> tuple[str, int]:
    """(the text of `scratch/library-main.md`, how many directories it found). `text` is the roadmap's README
    and Lean file."""
    head = str(gh.api(f"repos/{repo}/commits/{branch}", jq=".sha") or "").strip()
    lines = [f"# `{repo}` on `{branch}` (`{head[:10]}`) against the pin `{pin[:10]}`", ""]
    if not head or head == pin:
        return "\n".join(lines + ["The pin is the head of the branch: nothing to compare."]) + "\n", 0
    at_pin, cut_a = lean_files(gh, repo, pin, cache)
    at_main, cut_b = lean_files(gh, repo, head, cache)
    new = at_main - at_pin
    found = matches(new, at_main, text)
    lines += [f"{len(new)} Lean files are on `{branch}` and not in the pinned tree. {len(found)} directories hold some "
              "whose path carries a proper name this roadmap's README uses and few other files carry, a new "
              f"development under that name first. Leads, not verdicts: open them at `{head[:10]}` (the pinned checkout lacks them) and "
              "compare statements.", ""]
    if cut_a or cut_b:
        lines += ["GitHub truncated a tree listing: the list may be incomplete.", ""]
    lines += [f"- `{where}/` ({n} new file{'s' if n != 1 else ''}; {', '.join(words)})" for where, n, words in found[:LISTED]]
    if len(found) > LISTED:
        lines.append(f"- … and {len(found) - LISTED} more")
    return "\n".join(lines) + "\n", len(found)
