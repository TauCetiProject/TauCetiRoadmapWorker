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

# --- what a README lacks of the layout ------------------------------------------------------------------
#
# The project's README ("Laying out the README") asks for an overview that begins `**Summary.**` under
# the title, then `## Conventions`, the `## Layer k: ...` sections, then `## Scope and ownership`,
# `## Exact supplier contracts` and `## References` last. `lacks` says which of the three a README is
# short of, in words an author can act on; `layoutask` asks for them.

NEEDS = ("overview", "sections", "order")
CONVENTIONS = "conventions"
SECTION_NAMES = {CONVENTIONS: "## Conventions", SCOPE: "## Scope and ownership", SUPPLIERS: "## Exact supplier contracts",
                 REFERENCES: "## References"}
#: The sections the project's README puts before the scope, besides the layers.
SPECIFICATION = (CONVENTIONS, "worked examples", "downstream consumers")


def follows_layout(titles: list[str]) -> bool:
    """Whether sections with these titles (`title_key` form, the opening left out) stand in an order
    the project's README allows: `## How to read the build`, when there is one, first (after a summary
    or overview section), and `## Scope and ownership`, then `## Exact supplier contracts`, after the
    conventions, every layer, the worked examples and the downstream consumers. The README places no
    other section. One it does not name (`## Beyond this roadmap`, `## Prior formalization`) may stand
    before the scope, where `reorder` leaves it, or after the supplier contracts, where an author who
    reads "then the scope and the supplier contracts" puts it: both have the layout, and a request
    that held out for the first asked an author who had done the second to reorder again."""
    if HOW_TO_READ in titles and titles.index(HOW_TO_READ) != (1 if titles[0] in OVERVIEW else 0):
        return False
    body = [k for k, title in enumerate(titles) if title in SPECIFICATION or title.startswith("layer")]
    tail = [titles.index(title) for title in AT_THE_END if title in titles]
    return tail == sorted(tail) and not (body and tail and tail[0] < body[-1])


def lacks(text: str) -> dict[str, str]:
    """What a README lacks of the README layout, by kind ({} when nothing): "overview", the text between
    the title and the first `##` heading does not begin with `**Summary.**`; "sections", which of
    `## Conventions`, a `## Layer ...` section, `## Scope and ownership`, `## Exact supplier contracts`
    and `## References` it has none of; "order", its sections are not in an order the layout allows
    (`follows_layout`) or `## References` is not the last. A README that does not read as sections lacks
    all three."""
    try:
        titles = [p.title for p in parts(text)[1:]]
        opening = split_opening(text)[1]
    except LayoutError:
        return {"overview": "an overview that begins `**Summary.**` under the title",
                "sections": "its `##` sections (the README does not read as a title followed by sections)",
                "order": "the order of the sections"}
    out = {}
    if not opening.lstrip().startswith("**Summary.**"):
        out["overview"] = "an overview that begins `**Summary.**`, between the title and the first `##` heading"
    missing = [SECTION_NAMES[key] for key in (CONVENTIONS, SCOPE, SUPPLIERS, REFERENCES) if key not in titles]
    if not any(t.startswith("layer") for t in titles):
        missing.insert(1 if CONVENTIONS not in titles else 0, "## Layer k: …")
    if missing:
        out["sections"] = ("the section" + ("s " if len(missing) > 1 else " ")
                           + ", ".join(f"`{m}`" for m in missing[:-1]) + (" and " if len(missing) > 1 else "")
                           + f"`{missing[-1]}`")
    if not follows_layout(titles) or (REFERENCES in titles and titles[-1] != REFERENCES):
        out["order"] = ("the order of the sections (the conventions and the layers first; the scope, the supplier "
                        "contracts and the references at the end)")
    return out


# --- an overview pass: the opening rewritten, the sections reordered ------------------------------------
#
# `overview` rewrites a README's opening and reorders its sections in one pass, correcting inside the
# sections that move the direction words the move made wrong. `rewrite_problems` is the check that the
# pass changed nothing else; the intake runs it on the two heads' READMEs as it runs
# `section_permutation`.

MOVED = (HOW_TO_READ, SCOPE, SUPPLIERS)
SWAPS = {"below": "above", "above": "below", "following": "preceding", "preceding": "following",
         "later": "earlier", "earlier": "later", "after": "before", "before": "after"}
TOKEN = re.compile(r"(\w+)")


def split_opening(text: str) -> tuple[str, str]:
    """(the `# Roadmap:` title line, the opening after it) of a README: the opening is everything up to
    the first `##` heading, without its surrounding blank lines."""
    found = parts(text)
    lines = found[0].body.split("\n")
    title = lines[0] if lines and lines[0].startswith("# ") else ""
    rest = lines[1:] if title else lines
    return title, "\n".join(rest).strip("\n")


def swaps(a: str, b: str) -> int | None:
    """How many direction words (`SWAPS`) turn `a` into `b`; None when they differ in anything else."""
    ta, tb = TOKEN.split(a), TOKEN.split(b)
    if len(ta) != len(tb):
        return None
    n = 0
    for x, y in zip(ta, tb):
        if x == y:
            continue
        if SWAPS.get(x.lower()) == y.lower() and x[:1].isupper() == y[:1].isupper():
            n += 1
            continue
        return None
    return n


def rewrite_problems(old: str, new: str) -> list[str]:
    """Why `new` is not `old` with only the changes an overview pass may make ([] when it is): (a) the
    opening, (b) the order of the top-level sections, (c) a `## How to read the build` section gone
    whole (folded into the opening), (d) direction-word swaps (`SWAPS`) inside the sections that move
    (`MOVED`). The title stays, the opening opens with `**Summary.**`, and every other section is the
    same byte for byte (the blank lines between sections aside)."""
    try:
        a, b = parts(old), parts(new)
    except LayoutError as exc:
        return [f"not readable as sections: {exc}"]
    problems = []
    if split_opening(old)[0] != split_opening(new)[0]:
        problems.append("the title changed")
    if not split_opening(new)[1].startswith("**Summary.**"):
        problems.append("the opening does not open with `**Summary.**`")

    def body(p: Part) -> str:
        return p.body.removesuffix("\n").removesuffix("\r")
    common = Counter(map(body, a[1:])) & Counter(map(body, b[1:]))
    gone, came, left = [], [], dict(common)
    for p in a[1:]:
        if left.get(body(p), 0):
            left[body(p)] -= 1
        else:
            gone.append(p)
    left = dict(common)
    for p in b[1:]:
        if left.get(body(p), 0):
            left[body(p)] -= 1
        else:
            came.append(p)
    for p in came:
        match = next((q for q in gone if q.title == p.title and p.title in MOVED and swaps(body(q), body(p))), None)
        if match is None:
            problems.append(f"the section `## {p.body.split(chr(10), 1)[0][3:].strip()}` changed (only the opening, "
                            "the order of the sections, a folded guide and direction words in the sections that move "
                            "may change)")
        else:
            gone.remove(match)
    folded = [p for p in gone if p.title == HOW_TO_READ]
    for p in gone:
        if p.title != HOW_TO_READ or len(folded) > 1:
            problems.append(f"the section `## {p.body.split(chr(10), 1)[0][3:].strip()}` is gone")
    return problems
