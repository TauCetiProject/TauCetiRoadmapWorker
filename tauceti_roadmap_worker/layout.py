"""The order of a roadmap README's top-level sections, and the pure reordering that brings an older
README into the order the README-shape brief asks for.

A README is read as its opening (the `# Roadmap:` title and the overview before any `##` heading)
and its top-level sections, each a `##` heading with everything up to the next one (its `###`
subsections included); a `##` inside a fenced code block is not a heading. Each part is its text
(`body`) and the blank lines that follow it (`gap`).

`reorder` moves, whole, `## Scope and ownership` and `## Exact supplier contracts` to just before
`## References` (to the end when there is none), and `## How to read the build` to directly after
the opening summary. Nothing else moves and no byte of any section changes; the blank lines between
sections stay where they were, so a README already in that order comes back byte for byte (the
reordering is idempotent). `section_permutation` is the check the intake runs on two heads' READMEs:
the same opening and the same sections, in any order.

Only the three headings the brief names move, matched exactly (whatever their case): a heading that
mixes them with other matter ("Scope and conventions") stays where its author put it, since moving it
would move the conventions too.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

H2 = re.compile(r"^ {0,3}##(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*$")
H1 = re.compile(r"^ {0,3}#(?:[ \t]+.*)?$")
FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")

SCOPE = "scope and ownership"
SUPPLIERS = "exact supplier contracts"
HOW_TO_READ = "how to read the build"
REFERENCES = "references"
# A heading that opens the overview in some READMEs (`## Summary`): the guide to the layers follows it.
OVERVIEW = ("summary", "overview")
AT_THE_END = (SCOPE, SUPPLIERS)


class LayoutError(ValueError):
    """A README whose sections cannot be reordered safely (the reason is the message)."""


@dataclass(frozen=True)
class Part:
    title: str          # the `##` heading's text, normalised (`title_key`); '' for the opening
    body: str           # its text, from the heading line to its last non-blank line
    gap: str            # the blank lines after it


def title_key(text: str) -> str:
    """A heading's text as the reordering matches it: lower case, emphasis and a trailing colon or
    full stop dropped, spaces folded."""
    text = re.sub(r"[*_`]", "", text or "").strip().rstrip(":.").strip()
    return " ".join(text.lower().split())


def parts(text: str) -> list[Part]:
    """The opening and the top-level sections of a README, in order. Raises LayoutError for a `#`
    heading after the first `##` one (a README in another shape)."""
    lines = (text or "").splitlines(keepends=True)
    chunks: list[tuple[str, list[str]]] = [("", [])]
    fence = ""
    for line in lines:
        bare = line.rstrip("\r\n")
        m = FENCE.match(bare)
        if fence:
            if m and m.group("fence")[0] == fence[0] and len(m.group("fence")) >= len(fence) \
                    and not bare.strip()[len(m.group("fence")):].strip():
                fence = ""
        elif m:
            fence = m.group("fence")
        elif (h := H2.match(bare)) is not None:
            chunks.append((title_key(h.group("text") or ""), [line]))
            continue
        elif len(chunks) > 1 and H1.match(bare):
            raise LayoutError(f"a level-one heading after the first section: {bare.strip()[:60]!r}")
        chunks[-1][1].append(line)
    out = []
    for title, chunk in chunks:
        end = len(chunk)
        while end and not chunk[end - 1].strip():
            end -= 1
        out.append(Part(title, "".join(chunk[:end]), "".join(chunk[end:])))
    return out


def join(sections: list[Part], gaps: list[str]) -> str:
    """The README of `sections` in that order, each followed by the gap of its position."""
    return "".join(part.body + gap for part, gap in zip(sections, gaps))


def target_order(sections: list[Part]) -> list[Part]:
    """The sections after the opening in the brief's order: the guide to the layers first (after a
    `## Summary` or `## Overview` section when the README has one), scope and ownership and the
    supplier contracts just before the references, everything else as it stands."""
    guide = [s for s in sections if s.title == HOW_TO_READ]
    tail = [s for s in sections if s.title == SCOPE] + [s for s in sections if s.title == SUPPLIERS]
    rest = [s for s in sections if s.title not in (HOW_TO_READ, *AT_THE_END)]
    at = 1 if rest and rest[0].title in OVERVIEW else 0
    rest[at:at] = guide
    refs = next((k for k, s in enumerate(rest) if s.title == REFERENCES), len(rest))
    rest[refs:refs] = tail
    return rest


def reorder(text: str) -> str:
    """The README with its sections in the brief's order (`target_order`), every section's text and
    every gap unchanged; `text` itself when it is in that order already. Raises LayoutError when the
    result would not be a permutation of the sections (it never should be: the check is the
    contract, not a formality)."""
    found = parts(text)
    if len(found) < 2:
        return text
    opening, sections = found[0], found[1:]
    ordered = target_order(sections)
    if [id(s) for s in ordered] == [id(s) for s in sections]:
        return text
    # A last line without its newline gets one while it is read, so that every section ends with one
    # wherever it goes, and the result loses it again: the file ends as it did.
    bare = not text.endswith("\n")
    if bare:
        found = parts(text + "\n")
        opening, sections = found[0], found[1:]
        ordered = target_order(sections)
    out = join([opening, *ordered], [p.gap for p in found])
    if bare and out.endswith("\n"):
        out = out[:-1]
    if not section_permutation(text, out):
        raise LayoutError("the reordered README is not a permutation of the original's sections")
    return out


def moved(text: str) -> list[str]:
    """The headings `reorder` moves in `text`, as written in the README (empty when it is in order)."""
    found = parts(text)
    if len(found) < 2:
        return []
    sections = found[1:]
    ordered = target_order(sections)
    return [s.body.split("\n", 1)[0].strip() for k, s in enumerate(sections) if ordered[k] is not s
            and s.title in (HOW_TO_READ, *AT_THE_END)]


def section_permutation(old: str, new: str) -> bool:
    """Whether `new` holds exactly the opening and the sections of `old`, in any order of the
    sections: the same opening, and the same sections each byte for byte, only the blank lines
    between them aside. False when either cannot be read as sections."""
    try:
        a, b = parts(old), parts(new)
    except LayoutError:
        return False
    def body(part: Part) -> str:          # (the last section of a file without a final newline lacks it)
        return part.body.removesuffix("\n").removesuffix("\r")
    if body(a[0]) != body(b[0]):
        return False
    return Counter(map(body, a[1:])) == Counter(map(body, b[1:]))


def in_order(text: str) -> bool:
    """Whether the README's sections already stand in the brief's order."""
    try:
        return reorder(text) == text
    except LayoutError:
        return False
