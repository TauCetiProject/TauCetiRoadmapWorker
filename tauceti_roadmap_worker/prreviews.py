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
CLAIM_HEAD = re.compile(r"^reviewing(?:\s*\(([^)\n]{1,40})\))?\s*$", re.IGNORECASE)
VERDICT = re.compile(r"^\W*verdict\W*(accepted|needs[_ ]changes|changes needed|rejected)\b",
                     re.IGNORECASE | re.MULTILINE)
FINDING = re.compile(r"^\s*\d+\.\s+\*\*(F\d+)\*\*\s*(?:\((P[123])\))?", re.MULTILINE)
REPORT_FINDING = re.compile(r"^## ([RS])(\d+)\s*[(\[](P[123])[)\]]\s*[:—–-]*\s*(.*)$")


@dataclass
class Parsed:
    kind: str                       # review | redteam | claim
    agent: str = ""
    sha: str = ""
    verdict: str = ""               # accepted | needs_changes | rejected
    findings: list[tuple[str, str]] = field(default_factory=list)   # (F01, P1)
    round: int = 0
    pass_: str = ""
    worker: dict = field(default_factory=dict)                      # the posting worker's marker, if any

    @property
    def blocking(self) -> int:
        return sum(1 for _f, severity in self.findings if severity in ("P1", "P2"))


def normal_verdict(text: str) -> str:
    text = text.lower().replace(" ", "_")
    return "needs_changes" if text in ("needs_changes", "changes_needed") else text


def parse(body: str) -> Parsed | None:
    """The review, red-team pass or claim a comment states, or None (anything else, or a review
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
    else:
        return None
    verdict = VERDICT.search(body)
    if not verdict:
        return None
    parsed.verdict = normal_verdict(verdict.group(1))
    parsed.findings = FINDING.findall(body)
    parsed.worker = worker if isinstance(worker, dict) else {}
    if parsed.verdict == "needs_changes" and not parsed.findings:
        return None
    return parsed


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


def render(head: str, verdict: str, report: str, marker: dict) -> str:
    """A worker's report in the comment shape: `head` (the first line), the verdict, the findings
    numbered F01, F02, ... with their severity, the report's other sections, and the worker's
    marker."""
    findings, rest = report_findings(report)
    lines = [head, "", f"Verdict: {verdict}", ""]
    for k, (severity, title, body) in enumerate(findings, 1):
        lines.append(f"{k}. **F{k:02d}** ({severity}) {title}")
        lines += [("   " + line) if line.strip() else "" for line in body]
    text = "\n".join(lines).rstrip() + "\n\n" + "\n".join(rest).strip() + "\n"
    return text + "\n" + f"<!-- {MARK} {json.dumps(marker, sort_keys=True)} -->\n"


def review_head(agent: str, sha: str) -> str:
    return f"Review ({agent}) of {sha[:7]}"


def redteam_head(round_: int, pass_: str, agent: str, sha: str) -> str:
    return f"Red team round {round_} pass {pass_} ({agent}) of {sha[:7]}"
