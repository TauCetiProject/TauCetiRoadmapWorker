"""Reviews and red-team passes as comments on a board pull request.

A design or package submission is not merged while it is reviewed: its pull request on the board
stays open, labelled `needs-review`, through the review rounds, their fixes and (for a package) the
red-team rounds; every review or pass is a comment on it, from a worker or from a browser model a
maintainer drives, and the fix lanes push to its branch. A review comment reads

    Review (<agent>) of <sha7>

    Verdict: accepted | needs_changes | rejected

    1. **F01** (P1) <short title>
       Location: ... Claim: ... Witness: ... Repair: ...
    2. **F02** (P3) ...

    ## Verified sound
    ...

and a red-team pass the same, with the first line `Red team round <N> pass <x> (<agent>) of <sha7>`.
The errata of a roadmap's sources are a comment on the errata job's own issue, first line
`Errata (<agent>) of <sha7>`, then `Verdict: recorded`, then its `## Source issues`, `## Checked`
and `## Not verifiable` sections.
A comment whose first line is `reviewing` (optionally `reviewing (<agent>)`) claims the open review.
Only the shapes are read; everything else in a comment is data. This module is the pure part:
parsing, and rendering a worker's report into the shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MARK = "tauceti-roadmap-review"
MARK_RE = re.compile(r"<!--\s*" + MARK + r"\s*(\{.*?\})\s*-->", re.DOTALL)
REVIEW_HEAD = re.compile(r"^Review \(([^)\n]{1,40})\) of ([0-9a-f]{7,40})\s*$", re.IGNORECASE)
REDTEAM_HEAD = re.compile(r"^Red team round (\d+) pass ([a-z]) \(([^)\n]{1,40})\) of ([0-9a-f]{7,40})\s*$",
                          re.IGNORECASE)
ERRATA_HEAD = re.compile(r"^Errata \((?P<agent>[^)]+)\) of (?P<sha>[0-9a-f]{7,40})")
CLAIM_HEAD = re.compile(r"^reviewing(?:\s*\(([^)\n]{1,40})\))?\s*$", re.IGNORECASE)
VERDICT = re.compile(r"^\W*verdict\W*(accepted|needs[_ ]changes|changes needed|rejected)\b",
                     re.IGNORECASE | re.MULTILINE)
ERRATA_VERDICT = re.compile(r"^\W*verdict\W*(recorded)\b", re.IGNORECASE | re.MULTILINE)
FINDING = re.compile(r"^\s*\d+\.\s+\*\*(F\d+)\*\*\s*(?:\((P[123])\))?", re.MULTILINE)
REPORT_FINDING = re.compile(r"^## ([RS])(\d+)\s*[(\[](P[123])[)\]]\s*[:—–-]*\s*(.*)$")
CONDITIONAL = re.compile(r"^\W*conditional on the (?:announced|declared) gaps\W*(.+)$", re.IGNORECASE | re.MULTILINE)
ANY_FINDING = re.compile(r"^(?:##\s+([RSF]\d+)\s*[(\[](P[123])[)\]]\s*[:—–-]*\s*(.*)"
                         r"|\s*\d+\.\s+\*\*([RSF]\d+)\*\*\s*(?:\((P[123])\))?\s*(.*))$")
RERAISED = re.compile(r"\bre-?rais(?:ed|es)\b[^:\n]*:\s*([^\n]+)", re.IGNORECASE)


@dataclass
class Parsed:
    kind: str                       # review | redteam | errata | claim
    agent: str = ""
    sha: str = ""
    verdict: str = ""               # accepted | needs_changes | rejected; errata: recorded
    findings: list[tuple[str, str]] = field(default_factory=list)   # (F01, P1)
    round: int = 0
    pass_: str = ""
    worker: dict = field(default_factory=dict)                      # the posting worker's marker, if any
    conditional: list[str] = field(default_factory=list)            # gaps an acceptance is conditional on

    @property
    def blocking(self) -> int:
        return sum(1 for _f, severity in self.findings if severity in ("P1", "P2"))


def normal_verdict(text: str) -> str:
    text = text.lower().replace(" ", "_")
    return "needs_changes" if text in ("needs_changes", "changes_needed") else text


def parse(body: str) -> Parsed | None:
    """The review, red-team pass, errata or claim a comment states, or None (anything else, or a review
    without a verdict, or one that asks for changes without a numbered finding)."""
    lines = (body or "").strip().splitlines()
    if not lines:
        return None
    first = lines[0].strip().strip("*").strip()
    worker = {}
    found = MARK_RE.findall(body or "")
    if found:
        try:
            worker = json.loads(found[-1])
        except ValueError:
            worker = {}
    if (m := CLAIM_HEAD.match(first)) and len(lines) <= 3:
        return Parsed("claim", agent=(m.group(1) or "").strip())
    if m := REVIEW_HEAD.match(first):
        parsed = Parsed("review", agent=m.group(1).strip(), sha=m.group(2).lower())
    elif m := REDTEAM_HEAD.match(first):
        parsed = Parsed("redteam", agent=m.group(3).strip(), sha=m.group(4).lower(), round=int(m.group(1)),
                        pass_=m.group(2).lower())
    elif m := ERRATA_HEAD.match(first):
        parsed = Parsed("errata", agent=m["agent"].strip(), sha=m["sha"].lower())
    else:
        return None
    verdict = (ERRATA_VERDICT if parsed.kind == "errata" else VERDICT).search(body)
    if not verdict:
        return None
    parsed.verdict = normal_verdict(verdict.group(1))
    parsed.findings = FINDING.findall(body)
    if m := CONDITIONAL.search(body):
        parsed.conditional = gap_slugs(m.group(1))
    parsed.worker = {k: v for k, v in worker.items() if isinstance(v, str)} if isinstance(worker, dict) else {}
    if parsed.verdict == "needs_changes" and not parsed.findings:
        return None
    return parsed


def gap_slugs(text: str) -> list[str]:
    """`gap:a, b` or "`a`, `b`" -> ["a", "b"]."""
    return [t for t in (x.strip(" `.;").removeprefix("gap:") for x in re.split(r"[,;]|\band\b", text)) if t
            and re.fullmatch(r"[A-Za-z0-9][\w.-]*", t)]


def findings_with_bodies(text: str) -> list[tuple[str, str, str, str]]:
    """(id, severity, title, body) for every finding of a review, red-team pass or repair report, in
    either shape (`## R01 (P1) — title` or `1. **F01** (P1) title`), outside code fences."""
    out, current, fenced = [], None, False
    for line in (text or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        m = None if fenced else ANY_FINDING.match(line)
        if m:
            fid, sev, title = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), m.group(6))
            current = [fid, sev or "", title.strip(), []]
            out.append(current)
        elif not fenced and line.startswith("## "):
            current = None
        elif current is not None:
            current[3].append(line)
    return [(f, s, t, "\n".join(b).strip()) for f, s, t, b in out]


TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
EVIDENCE_HEADER = re.compile(r"instance|tried|computed", re.I)
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")


def adversarial_rows(report: str) -> int:
    """Rows of the report's adversarial tables: Markdown tables whose header names the instances tried or
    what was computed (`| Statement | Instances tried | Values | Result | Change |`), outside code fences. A row counts
    when it carries a computed value (a digit, `=`, `≠`, `<`, `>`) beyond its first cell."""
    rows, lines, fenced, i = 0, (report or "").split("\n"), False, 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        elif (not fenced and TABLE_ROW.match(line) and EVIDENCE_HEADER.search(line) and i + 1 < len(lines)
              and TABLE_RULE.match(lines[i + 1])):
            i += 2
            while i < len(lines) and TABLE_ROW.match(lines[i]):
                cells = lines[i].strip().strip("|").split("|")
                if re.search(r"[0-9=≠<>≤≥]", "|".join(cells[1:])):
                    rows += 1
                i += 1
            continue
        i += 1
    return rows


EVIDENCE = ("the report has no adversarial table with computed values: an acceptance carries its evidence, a "
            "Markdown table whose header names the instances tried (statement, instances tried, the values computed "
            "on each side, result, change), one row per statement, with the values")


def reraised(title: str, body: str) -> set[str]:
    """The earlier finding ids a finding says it re-raises (`Re-raised: F01`)."""
    return {fid for m in RERAISED.findall(title + "\n" + body) for fid in re.findall(r"\b[RSF]\d+\b", m)}


SOURCE_ISSUE = re.compile(r"^\s*[-*]\s*\*\*(Misprint|Error|Gap)\*\*\s+in\s+(.+?)\s+at\s+(.+?):\s*(.+?);\s*"
                          r"it should\s+(?:be|read|say|state)?\s*(.+?);\s*affects\s+(nothing|a stated result|the proof)\b\.?(.*)$",
                          re.I | re.S)
SAYS_PREFIX = re.compile(r"^the source\s+(?:says|states|gives|writes|prints|claims|reads|asserts|has|defines|takes|places|uses)"
                         r"\s+(?:that\s+)?", re.I)


def source_issues(body: str) -> list[dict]:
    """The `## Source issues` bullets of a report or comment (mistakes found in published sources):
    [{kind, source, locator, says, should, affects, note}], the shape the prompts require. Bullets
    the regex does not match are kept with kind `unparsed` and the line as `says`, so a reviewer's
    finding is never dropped silently."""
    out, inside, current = [], False, []
    for line in (body or "").split("\n"):
        if line.startswith("## "):
            inside = line.strip().lower().startswith("## source issues")
            continue
        if not inside:
            continue
        if re.match(r"^\s*[-*]\s", line):
            if current:
                out.append("\n".join(current))
            current = [line]
        elif current and line.strip():
            current.append(line)
    if current:
        out.append("\n".join(current))
    issues = []
    for text in out:
        flat = " ".join(text.split())
        if re.match(r"^[-*]\s*none\b", flat, re.I):
            continue
        m = SOURCE_ISSUE.match(flat)
        if m:
            note = m.group(7).strip(" ;.")
            issues.append({"kind": m.group(1).lower(), "source": m.group(2).strip("` "), "locator": m.group(3).strip(),
                           "says": SAYS_PREFIX.sub("", m.group(4).strip()), "should": m.group(5).strip(),
                           "affects": m.group(6).lower(),
                           "note": note})
        else:
            issues.append({"kind": "unparsed", "source": "", "locator": "", "says": flat.lstrip("-* ").strip(),
                           "should": "", "affects": "", "note": ""})
    return issues


def report_findings(report: str) -> tuple[list[tuple[str, str, list[str]]], list[str]]:
    """A worker report's `## R01 (P1) — title` blocks as (severity, title, body lines), and the
    rest (other sections), outside code fences."""
    findings, rest, current, fenced = [], [], None, False
    for line in (report or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        m = None if fenced else REPORT_FINDING.match(line)
        if m:
            current = []
            findings.append((m.group(3), m.group(4).strip(), current))
        elif not fenced and line.startswith("## "):
            current = None
            rest.append(line)
        elif current is not None:
            current.append(line)
        elif not line.startswith("# ") and not re.match(r"^\**Verdict", line):
            rest.append(line)
    return findings, rest


def render(head: str, verdict: str, report: str, marker: dict, conditional: list[str] | None = None) -> str:
    """A worker's report in the comment shape: `head` (the first line), the verdict (and the gaps an
    acceptance is conditional on), the findings numbered F01, F02, ... with their severity, the
    report's other sections, and the worker's marker."""
    findings, rest = report_findings(report)
    lines = [head, "", f"Verdict: {verdict}", ""]
    if conditional:
        lines[-1:] = ["Conditional on the announced gaps: " + ", ".join(f"`{g}`" for g in conditional), ""]
    for k, (severity, title, body) in enumerate(findings, 1):
        lines.append(f"{k}. **F{k:02d}** ({severity}) {title}")
        lines += [("   " + line) if line.strip() else "" for line in body]
    text = "\n".join(lines).rstrip() + "\n\n" + "\n".join(rest).strip() + "\n"
    return text + "\n" + f"<!-- {MARK} {json.dumps(marker, sort_keys=True)} -->\n"


def review_head(agent: str, sha: str) -> str:
    return f"Review ({agent}) of {sha[:7]}"


def redteam_head(round_: int, pass_: str, agent: str, sha: str) -> str:
    return f"Red team round {round_} pass {pass_} ({agent}) of {sha[:7]}"


def errata_head(agent: str, sha: str) -> str:
    return f"Errata ({agent}) of {sha[:7]}"
