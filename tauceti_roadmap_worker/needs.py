"""The `*Needs:*` lines of a roadmap README against the sections each target's text cites.

A target's `*Needs:*` line lists what its statement and its route need, item by item (the README-shape
brief). A reviewer reads a target against its source and passes a line that lists two sections while
the text under it uses four: the comparison is clerical, and three full reviews of one roadmap raised
no remark on it where a referee later found four such lines. `report` does the clerical half: for each
numbered subsection of a layer it lists the sections of this README its text cites and its `*Needs:*`
line does not, saying which are cited only in a Check and which stand later in the README, and the
later sections a `*Needs:*` line itself lists. They are leads: a section named for contrast is no need.
The annotation is read where it stands, on a line of its own or after the statement in one paragraph.

A reference counts as this README's when no roadmap name stands before it: "SobolevMapsBetweenManifolds
§4.2, §4.3" cites another roadmap twice, "§1.2, §2.1" this one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING = re.compile(r"^(#{2,3})\s+(.*?)\s*$", re.M)
ID = r"(?:[A-Z]{1,3}\.?)?\d+(?:\.\d+)+[a-z]?"       # 2.1, 2.3a, P1.2, R3.1.2, RD.1.2
NUMBERED = re.compile(r"^(?:§\s*)?(" + ID + r")(?=[\s.:]|$)")
ITEM = r"(?:§+\s*)?" + ID + r"(?:\([a-z0-9]+\))?"
# One citation: "§1.1", "§1.1–§1.3", "§§4.1–4.2, 1.2 and 3.3". A number with no sign of its own is part
# of it after a dash, or anywhere in a chain opened with "§§".
CHAIN = re.compile(r"§+\s*" + ID + r"(?:\([a-z0-9]+\))?(?:\s*(?:,|–|-|\band\b|\bto\b|\bor\b)\s*" + ITEM + r")*")
LINK = re.compile(r"(,|–|-|\band\b|\bto\b|\bor\b)?\s*(§+)?\s*(" + ID + r")(?:\([a-z0-9]+\))?")
# A roadmap, a library or a source named before a section sign: a word with two capitals (`PDE`,
# `TeichmullerGeometry`), a backticked name, or a bracketed source key. The chain of references that
# follows it (", §4.3", "–§6.3", "and §5.1", and after "§§" the bare "–4.6", ", 4.8") belongs to it.
FOREIGN = re.compile(
    r"(?:\b[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|`[^`\n]+`|\[[^\]\n]+\]|\bTau Ceti|\bMathlib)(?:\s+README)?[\s,]*"
    r"§+\s*" + ID + r"(?:\([a-z0-9]+\))?(?:\s*(?:,|–|-|and|to|or)\s*(?:§+\s*)?" + ID + r"(?:\([a-z0-9]+\))?)*")
# To the end of its paragraph. A backtick before it is prose about the annotation, not one.
NEEDS = re.compile(r"(?<!`)\*Needs:\*.*?(?=\n\s*\n|\Z)", re.S)
# A Check block runs from its label to the next labelled paragraph or a heading (`*Needs:*` is cut first).
CHECK = re.compile(r"^\*{1,2}Checks?[:.]?\*{1,2}[:.]?.*?(?=^\*\*[A-Z]|^#|\Z)", re.M | re.S)


@dataclass
class Target:
    id: str
    title: str
    has_needs: bool = False
    unlisted: list[str] = field(default_factory=list)       # cited in the statement or the route, not listed
    check_only: list[str] = field(default_factory=list)     # cited in a Check only, not listed
    later_listed: list[str] = field(default_factory=list)   # listed, and standing later in the README
    unknown: list[str] = field(default_factory=list)        # cited, and no subsection of this README


def _own(text: str, ids: dict[str, int]) -> tuple[set[str], set[str]]:
    """(the subsections of this README `text` cites, the section numbers it cites that this README has
    not), references to other roadmaps removed and ranges within one layer filled in."""
    text = FOREIGN.sub(" ", text)
    found, unknown = set(), set()
    for chain in CHAIN.finditer(text):
        plural, last = chain.group(0).startswith("§§"), ""
        for sep, sign, sid in LINK.findall(chain.group(0)):
            dash = sep in ("–", "-")
            if last and not (sign or plural or dash):
                break                                   # "§1.1 and 2.5 times": the chain ended at the sign
            layer = sid.rsplit(".", 1)[0]
            if dash and last in ids and sid in ids and last.rsplit(".", 1)[0] == layer:
                found.update(k for k, i in ids.items() if ids[last] <= i <= ids[sid] and k.rsplit(".", 1)[0] == layer)
            (found if sid in ids else unknown).add(sid)
            last = sid
    return found, unknown


def targets(readme: str) -> list[Target]:
    """One entry for each numbered `###` subsection of the README, in its order."""
    heads = [(m.start(), m.end(), len(m.group(1)), m.group(2)) for m in HEADING.finditer(readme)]
    sections = []
    for k, (start, end, level, title) in enumerate(heads):
        number = NUMBERED.match(title)
        if level == 3 and number:
            stop = heads[k + 1][0] if k + 1 < len(heads) else len(readme)
            sections.append((number.group(1), title, readme[end:stop]))
    ids = {sid: i for i, (sid, _t, _b) in enumerate(sections)}
    out = []
    for sid, title, body in sections:
        needs = "\n".join(m.group(0) for m in NEEDS.finditer(body))
        plain = NEEDS.sub("\n\n", body)
        checks = "\n".join(m.group(0) for m in CHECK.finditer(plain))
        rest = CHECK.sub(" ", plain)
        listed, unknown_n = _own(needs, ids)
        used, unknown = _own(rest, ids)
        in_checks, unknown_c = _own(checks, ids)
        used.discard(sid)
        in_checks.discard(sid)
        key = ids.get
        out.append(Target(
            sid, title, bool(needs),
            unlisted=sorted(used - listed, key=key),
            check_only=sorted(in_checks - used - listed, key=key),
            later_listed=sorted((x for x in listed if ids[x] > ids[sid]), key=key),
            unknown=sorted(unknown | unknown_c | unknown_n)))
    return out


def report(readme: str, path: str = "README.md") -> tuple[str, int]:
    """(the text of `scratch/needs.md`, how many subsections it has a lead for)."""
    found = targets(readme)
    ids = {t.id: i for i, t in enumerate(found)}
    lines = [f"# `*Needs:*` lines of `{path}` against the sections each target cites", "",
             "Computed from the text. Leads, not verdicts: a section the text names for contrast or as a "
             "pointer is not a need, and a need stated in words (\"the definition of the class\") is not seen.", ""]
    count = 0
    for t in found:
        notes = []
        if not t.has_needs and (t.unlisted or t.check_only):
            notes.append("no `*Needs:*` line; its text cites " + ", ".join(f"§{x}" for x in t.unlisted + t.check_only))
        elif t.unlisted or t.check_only:
            later = [x for x in t.unlisted if ids[x] > ids[t.id]]
            earlier = [x for x in t.unlisted if ids[x] < ids[t.id]]
            if earlier:
                notes.append("cited in its text, not in its `*Needs:*` line: " + ", ".join(f"§{x}" for x in earlier))
            if later:
                notes.append("cited in its text, later in the README, not in its `*Needs:*` line: "
                             + ", ".join(f"§{x}" for x in later))
            if t.check_only:
                notes.append("cited in a Check only: " + ", ".join(
                    f"§{x}" + (" (later in the README)" if ids[x] > ids[t.id] else "") for x in t.check_only))
        if t.later_listed:
            notes.append("its `*Needs:*` line lists a later section: " + ", ".join(f"§{x}" for x in t.later_listed))
        if t.unknown:
            notes.append("cites a section this README does not have: " + ", ".join(f"§{x}" for x in t.unknown))
        if notes:
            count += 1
            lines.append(f"- **§{t.id}** ({t.title}): " + "; ".join(notes) + ".")
    if not count:
        lines.append(f"Nothing found in {len(found)} numbered subsections." if found
                     else "The README has no numbered `###` subsections: nothing was compared.")
    return "\n".join(lines) + "\n", count
