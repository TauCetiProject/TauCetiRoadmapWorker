"""Reviews and replies on the content repository's pull requests (the `gate` lane), as pure functions.

Ported from the maintainer's label-sync.sh, review-batch.sh and mk-reply.py:

* `classify` sorts a pull request's timeline (reviews and issue comments) into reviews and our
  replies. A comment counts as a review when it comes from someone else, or when it is ours but
  names an AI review tool in its first 60 characters (a review we posted on a reviewer's behalf).
  Approvals and "findings addressed" notes are not reviews that need an answer.
* `wanted_label` is the label state machine: an approval by a configured approver at the current
  head clears both labels; a review newer than our last reply means `awaiting-author`; otherwise
  `awaiting-review`.
* `reviews_since_reply` returns EVERY review newer than our last reply. review-batch.sh took only
  the newest one, so a second reviewer's comments posted in between were never answered.
* `guard_reply` keeps the first 40 characters of our reply free of review-tool names: otherwise
  `classify` would read our reply as a review and flip the pull request back to awaiting-author.
* `build_reply` turns a fix report into one bullet per finding. The closing validation sentence
  is computed from the actual gate result: mk-reply.py printed "0 missing identifiers and 0
  problems" unconditionally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHANGES = re.compile(
    r"CHANGES NEEDED|[Cc]hanges needed|correction is needed|needed before approval|substantive (?:change|addition|correction)|"
    r"changes are needed|would not approve|[Rr]equest changes")
SETTLED = re.compile(
    r"[Nn]o further change|ready for manual approval|recommend(?:ing)? manual (?:content )?approval|accepted all|"
    r"would not request another revision|found no issues|[Nn]o remaining blocking|no blocking issue|previous findings closed|"
    r"requested changes are resolved|findings are resolved|nothing further|[Nn]o further repairs|recommend approval|"
    r"(?:I )?would approve|Verdict: APPROVE|comments resolved|found no reason|finding resolved|are resolved|finding addressed|"
    r"comments addressed|are addressed|is addressed|fully addressed|— addressed|- addressed")
APPROVE_VERDICT = re.compile(r"Verdict[^\n]{0,30}APPROVE")
THANKS = re.compile(r"^(?:Thank you|Thanks)")


@dataclass(frozen=True)
class Event:
    t: str          # ISO-8601 UTC timestamp; ISO strings compare chronologically
    user: str
    body: str
    approved: bool = False   # a PR review in state APPROVED
    commit: str = ""         # the head a PR review was submitted on


def _markers_re(markers: list[str]) -> re.Pattern:
    return re.compile("|".join(re.escape(m) for m in markers)) if markers else re.compile(r"(?!)")


def is_review(e: Event, ours: set[str], markers: list[str], ignore: set[str] = frozenset()) -> bool:
    if e.user in ignore:
        return False
    body = ("Verdict: APPROVE " if e.approved else "") + (e.body or "")
    mine = e.user.lower() in ours
    if mine and not (_markers_re(markers).search(body[:60]) and not THANKS.match(body)):
        return False
    if APPROVE_VERDICT.search(body):
        return False
    return bool(CHANGES.search(body)) or not SETTLED.search(body)


def is_reply(e: Event, ours: set[str], markers: list[str], housekeeping: list[str]) -> bool:
    if e.user.lower() not in ours:
        return False
    if _markers_re(markers).search((e.body or "")[:40]):
        return False
    return not any((e.body or "").startswith(p) for p in housekeeping)


def classify(events: list[Event], ours: set[str], markers: list[str], housekeeping: list[str],
             ignore: set[str] = frozenset()) -> tuple[str, str]:
    """(time of the last review, time of our last reply); '' when there is none."""
    ours = {o.lower() for o in ours}
    ordered = sorted(events, key=lambda e: e.t)
    reviews = [e.t for e in ordered if is_review(e, ours, markers, ignore)]
    replies = [e.t for e in ordered if is_reply(e, ours, markers, housekeeping)]
    return (reviews[-1] if reviews else ""), (replies[-1] if replies else "")


def approved_at_head(events: list[Event], approvers: list[str], head: str) -> bool:
    """The latest review by any approver is an approval of exactly this head."""
    approvers_l = {a.lower() for a in approvers}
    theirs = sorted((e for e in events if e.user.lower() in approvers_l and e.commit), key=lambda e: e.t)
    return bool(theirs) and theirs[-1].approved and theirs[-1].commit == head


def wanted_label(events: list[Event], ours: set[str], head: str, *, markers: list[str], housekeeping: list[str],
                 approvers: list[str], ignore: set[str] = frozenset(),
                 awaiting_author: str = "awaiting-author", awaiting_review: str = "awaiting-review") -> tuple[str, list[str]]:
    """(label to add or '', labels to remove)."""
    if approved_at_head(events, approvers, head):
        return "", [awaiting_author, awaiting_review]
    last_review, last_reply = classify(events, ours, markers, housekeeping, ignore)
    if last_review and last_review > last_reply:
        return awaiting_author, [awaiting_review]
    return awaiting_review, [awaiting_author]


def reviews_since_reply(events: list[Event], ours: set[str], markers: list[str], housekeeping: list[str],
                        ignore: set[str] = frozenset()) -> list[Event]:
    """Every review newer than our last reply, oldest first (not only the latest)."""
    ours_l = {o.lower() for o in ours}
    _, last_reply = classify(events, ours_l, markers, housekeeping, ignore)
    return [e for e in sorted(events, key=lambda e: e.t)
            if e.t > last_reply and is_review(e, ours_l, markers, ignore)]


REPLY_PREFIX = "Reply to the review comments above, finding by finding:\n\n"


def reply_is_safe(text: str, markers: list[str]) -> bool:
    return not _markers_re(markers).search(text[:40])


def guard_reply(text: str, markers: list[str]) -> str:
    """The reply, with a neutral opening if a review-tool name would otherwise fall in its first
    40 characters (the prefix alone is longer than 40 characters)."""
    return text if reply_is_safe(text, markers) else REPLY_PREFIX + text


_FINDING_STYLES = (
    re.compile(r"^## (Finding \d+|F\d+|R\d+|S\d+|[A-Z]?\d+)[^\n]*?— ([^\n]*)\n(.*?)(?=\n## |\Z)", re.S | re.M),
    re.compile(r"^\d+\. \*\*(F\d+|R\d+|S\d+|\d+) — ([^*]*?)\.\*\*\s*(.*?)(?=\n\d+\. \*\*|\n\n|\Z)", re.S | re.M),
    re.compile(r"^- \*\*([A-Z]\d+)[^*]*?— ([^*]*?)\.\*\*\s*(.*?)(?=\n- \*\*|\n\n|\Z)", re.S | re.M),
    re.compile(r"^\*\*([A-Z]\d+) — ([^*]*?)\.\*\*\s*(.*?)(?=\n\*\*|\n\n|\Z)", re.S | re.M),
)


def parse_findings(report: str) -> list[tuple[str, str, str]]:
    """(finding id, disposition, explanation) from a fix report, trying the heading styles the fix
    briefs ask for: `## R01 ... — applied`, `1. **R01 — applied.**`, `- **R01 — applied.**`,
    `**R01 — applied.**`."""
    for style in _FINDING_STYLES:
        rows = [(m.group(1), m.group(2).strip().rstrip("."), m.group(3).strip()) for m in style.finditer(report)]
        if rows:
            return rows
    return []


NOT_ACTIONABLE = re.compile(r"^not[ -]actionable\b", re.IGNORECASE)


def not_actionable(report: str) -> dict[str, tuple[str, str]]:
    """Finding id -> (title, reason) for the findings a repair report marked `not actionable in this
    package`, with the reason after the disposition (else the first line of the section)."""
    out = {}
    for fid, how, body in parse_findings(report):
        if not NOT_ACTIONABLE.match(how.strip()):
            continue
        reason = re.sub(r"^not[ -]actionable(?: in this package)?\s*[:.—-]*\s*", "", how.strip(), flags=re.IGNORECASE)
        m = re.search(rf"^(?:##\s+|\d+\.\s+\*\*|-\s+\*\*|\*\*){re.escape(fid)}\b\**\s*(?:[(\[]P[123][)\]])?\s*(.*?)\s*—",
                      report, re.MULTILINE)
        out[fid] = ((m.group(1).strip(" *") if m else ""), (reason or body.split("\n", 1)[0]).strip())
    return out


def build_reply(head7: str, report: str, validation: str, intro: str = "reply to the review",
                markers: list[str] | None = None) -> str:
    lines = [f"Fixed in `{head7}` ({intro}):", ""]
    for fid, how, body in parse_findings(report):
        body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)
        body = " ".join(body.split())
        first_two = " ".join(re.split(r"(?<=[.!?])\s", body)[:2])
        lines.append(f"- **{fid}** ({how}) — {first_two}")
    lines += ["", validation]
    return guard_reply("\n".join(lines) + "\n", markers or [])


def validation_sentence(head7: str, gate) -> str:
    """One sentence stating what the gate actually found at this head."""
    c = gate.counts
    build = "the build was not run" if "build" not in c else (
        "the targeted build succeeded with only `sorry` warnings" if c["build"] == 0 else f"the targeted build reported {c['build']} problem(s)")
    ids = c.get("readme-ids-missing", "?")
    dep = c.get("depcheck", "not run")
    return (f"At `{head7}`: {build}; README identifiers missing from the Lean: {ids}; dependency check problems: {dep}; "
            f"gate {'passed' if gate.ok else 'FAILED'}.")
