"""Reviews and replies on the content repository's pull requests (the `gate` lane), as pure functions.

Ported from the maintainer's label-sync.sh, review-batch.sh and mk-reply.py:

* `classify` sorts a pull request's timeline (reviews and issue comments) into reviews and our
  replies. A comment counts as a review when it comes from someone else, or when it is ours but
  names an AI review tool in its first 60 characters (a review we posted on a reviewer's behalf).
  Approvals and "findings addressed" notes are not reviews that need an answer.
* `is_maintainer_review`: the maintainer may act as the same login as the workers, so a worker's
  pull request can carry the maintainer's own review under the owner's login. A review or comment
  by a configured maintainer login that opens with the maintainer-review prefix is read as another
  reviewer's comment; every other comment of the owner stays ours.
* `wanted_label` is the label state machine: an approval by a configured approver at the current
  head clears both labels; an unanswered review means `awaiting-author`; otherwise
  `awaiting-review`.
* `unanswered` returns EVERY review and comment no reply acknowledges. review-batch.sh took only
  the newest one, so a second reviewer's comments posted in between were never answered. A gate
  reply records the exact version of every review, comment and inline comment its agent read
  (`answered_marker`), and acknowledges those only: feedback that arrives or is edited while the
  agent works stays unanswered. A reply without the record (posted before it existed) acknowledges
  everything older than itself, as every reply used to; once a pull request has a recorded reply,
  later replies without one acknowledge nothing.
* `guard_reply` keeps the first 40 characters of our reply free of review-tool names: otherwise
  `classify` would read our reply as a review and flip the pull request back to awaiting-author.
* `build_reply` turns a fix report into one bullet per finding. The closing validation sentence
  is computed from the actual gate result: mk-reply.py printed "0 missing identifiers and 0
  problems" unconditionally.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from . import prreviews

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
MAINTAINER_REVIEW_PREFIX = "Maintainer review"
SHORTCODE = re.compile(r":[a-z0-9_+-]+:")
# Leading characters skipped before the prefix: emoji and other symbols, skin tones, variation
# selectors, keycaps and joiners (besides whitespace).
DECORATION = frozenset({"So", "Sk", "Mn", "Me", "Cf"})


@dataclass(frozen=True)
class Event:
    t: str          # ISO-8601 UTC timestamp; ISO strings compare chronologically
    user: str
    body: str
    approved: bool = False   # a PR review in state APPROVED
    commit: str = ""         # the head a PR review was submitted on
    review_id: int = 0       # the PR review this is (a review) or belongs to (an inline comment)
    ref: str = ""            # the record on GitHub: review-<id>, comment-<id> or inline-<id>
    version: str = ""        # `text_version` of its text as GitHub holds it


def text_version(body: str) -> str:
    """A short digest of a review's or comment's text: a new version exactly when the text is edited
    (the REST API gives reviews no update time, and an inline comment's line moves with every push)."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:12]


def _markers_re(markers: list[str]) -> re.Pattern:
    return re.compile("|".join(re.escape(m) for m in markers)) if markers else re.compile(r"(?!)")


def undecorated(body: str) -> str:
    """The text after any leading whitespace and emoji (characters or `:shortcode:`)."""
    i = 0
    while i < len(body):
        m = SHORTCODE.match(body, i)
        if m:
            i = m.end()
        elif body[i].isspace() or unicodedata.category(body[i]) in DECORATION:
            i += 1
        else:
            break
    return body[i:]


def is_maintainer_review(e: Event, maintainers: set[str], prefix: str = MAINTAINER_REVIEW_PREFIX) -> bool:
    """A review or comment by a maintainer login whose text opens with `prefix` (case-insensitive,
    after optional emoji and whitespace). Exact at the start: a reply that only mentions the prefix
    later on, or quotes it after other text, is not one. An empty prefix matches nothing."""
    if not prefix or e.user.lower() not in {m.lower() for m in maintainers}:
        return False
    return undecorated(e.body or "")[:len(prefix)].lower() == prefix.lower()


def is_review(e: Event, ours: set[str], markers: list[str], ignore: set[str] = frozenset(),
              maintainers: set[str] = frozenset(), maintainer_prefix: str = MAINTAINER_REVIEW_PREFIX) -> bool:
    if e.user in ignore:
        return False
    body = ("Verdict: APPROVE " if e.approved else "") + (e.body or "")
    mine = e.user.lower() in ours and not is_maintainer_review(e, maintainers, maintainer_prefix)
    if mine and not (_markers_re(markers).search(body[:60]) and not THANKS.match(body)):
        return False
    if APPROVE_VERDICT.search(body):
        return False
    return bool(CHANGES.search(body)) or not SETTLED.search(body)


def is_reply(e: Event, ours: set[str], markers: list[str], housekeeping: list[str],
             maintainers: set[str] = frozenset(), maintainer_prefix: str = MAINTAINER_REVIEW_PREFIX) -> bool:
    if e.user.lower() not in ours or is_maintainer_review(e, maintainers, maintainer_prefix):
        return False
    if _markers_re(markers).search((e.body or "")[:40]):
        return False
    return not any((e.body or "").startswith(p) for p in housekeeping)


def classify(events: list[Event], ours: set[str], markers: list[str], housekeeping: list[str],
             ignore: set[str] = frozenset(), maintainers: set[str] = frozenset(),
             maintainer_prefix: str = MAINTAINER_REVIEW_PREFIX) -> tuple[str, str]:
    """(time of the last review, time of our last reply); '' when there is none."""
    ours = {o.lower() for o in ours}
    ordered = sorted(events, key=lambda e: e.t)
    reviews = [e.t for e in ordered if is_review(e, ours, markers, ignore, maintainers, maintainer_prefix)]
    replies = [e.t for e in ordered if is_reply(e, ours, markers, housekeeping, maintainers, maintainer_prefix)]
    return (reviews[-1] if reviews else ""), (replies[-1] if replies else "")


def approved_at_head(events: list[Event], approvers: list[str], head: str) -> bool:
    """The latest review by any approver is an approval of exactly this head."""
    approvers_l = {a.lower() for a in approvers}
    theirs = sorted((e for e in events if e.user.lower() in approvers_l and e.commit), key=lambda e: e.t)
    return bool(theirs) and theirs[-1].approved and theirs[-1].commit == head


def wanted_label(events: list[Event], ours: set[str], head: str, *, markers: list[str], housekeeping: list[str],
                 approvers: list[str], ignore: set[str] = frozenset(),
                 awaiting_author: str = "awaiting-author", awaiting_review: str = "awaiting-review",
                 maintainers: set[str] = frozenset(),
                 maintainer_prefix: str = MAINTAINER_REVIEW_PREFIX) -> tuple[str, list[str]]:
    """(label to add or '', labels to remove)."""
    if approved_at_head(events, approvers, head):
        return "", [awaiting_author, awaiting_review]
    if unanswered(events, ours, markers, housekeeping, ignore, maintainers, maintainer_prefix):
        return awaiting_author, [awaiting_review]
    return awaiting_review, [awaiting_author]


ANSWERED_MARK = "tauceti-roadmap-answered"
ANSWERED_RE = re.compile(r"<!--\s*" + ANSWERED_MARK + r"\b(.*?)-->", re.DOTALL)


def versions(events: list[Event]) -> dict[str, str]:
    """ref -> version of the events that have a record on GitHub."""
    return {e.ref: e.version for e in events if e.ref}


def answered_marker(job: str, answered: dict[str, str]) -> str:
    """The record a reply carries of the exact reviews and comments it answers (`versions`)."""
    return f"<!-- {ANSWERED_MARK} {json.dumps({'job': job, 'answered': answered}, sort_keys=True)} -->"


def answered_by(body: str) -> dict[str, str] | None:
    """ref -> version from a reply's record (the last one: the worker appends it after the agent's text),
    {} for a record that does not parse, None for a reply without one."""
    found = ANSWERED_RE.findall(body or "")
    if not found:
        return None
    try:
        answered = json.loads(found[-1].strip()).get("answered")
    except (ValueError, AttributeError):
        return {}
    return {str(k): str(v) for k, v in answered.items()} if isinstance(answered, dict) else {}


def acknowledged(events: list[Event], ours: set[str], markers: list[str], housekeeping: list[str],
                 maintainers: set[str] = frozenset(),
                 maintainer_prefix: str = MAINTAINER_REVIEW_PREFIX) -> tuple[set[tuple[str, str]], str]:
    """What our replies acknowledge: the (ref, version) pairs their records name, and the time up to
    which the replies without a record acknowledge everything older. Only those older than the first
    recorded reply count so, since they were posted before the records existed. An inline comment is
    never a reply."""
    ours = {o.lower() for o in ours}
    seen: set[tuple[str, str]] = set()
    before, recorded = "", False
    for e in sorted(events, key=lambda e: e.t):
        if e.ref.startswith("inline-") or not is_reply(e, ours, markers, housekeeping, maintainers, maintainer_prefix):
            continue
        answered = answered_by(e.body)
        if answered is not None:
            recorded = True
            seen |= set(answered.items())
        elif not recorded:
            before = e.t
    return seen, before


def unanswered(events: list[Event], ours: set[str], markers: list[str], housekeeping: list[str],
               ignore: set[str] = frozenset(), maintainers: set[str] = frozenset(),
               maintainer_prefix: str = MAINTAINER_REVIEW_PREFIX) -> list[Event]:
    """Every review and comment no reply acknowledges, oldest first (not only the latest), a
    maintainer's review under the owner's login included. An inline comment counts when the review
    it belongs to does, or on its own terms. An acknowledged review edited since is unanswered again."""
    ours_l = {o.lower() for o in ours}
    seen, before = acknowledged(events, ours_l, markers, housekeeping, maintainers, maintainer_prefix)

    def review(e: Event) -> bool:
        return is_review(e, ours_l, markers, ignore, maintainers, maintainer_prefix)
    reviewing = {e.review_id for e in events if e.ref.startswith("review-") and review(e)}
    return [e for e in sorted(events, key=lambda e: e.t)
            if (review(e) or (e.ref.startswith("inline-") and e.review_id in reviewing))
            and (e.ref, e.version) not in seen and e.t > before]


REPLY_PREFIX = "Reply to the review comments above, finding by finding:\n\n"


def reply_is_safe(text: str, markers: list[str]) -> bool:
    return not _markers_re(markers).search(text[:40])


def guard_reply(text: str, markers: list[str]) -> str:
    """The reply, with a neutral opening if a review-tool name would otherwise fall in its first
    40 characters (the prefix alone is longer than 40 characters)."""
    return text if reply_is_safe(text, markers) else REPLY_PREFIX + text


def _ids(bare: str) -> str:
    """A section heading's finding ids: one, or several joined by `,` or `and`, each possibly qualified."""
    one = prreviews.QUALIFIER + f"(?:{bare})"
    return rf"({one}(?:(?:[ \t]*,[ \t]*|[ \t]+and[ \t]+){one})*)"


_FINDING_STYLES = (
    re.compile(r"^## " + _ids(r"Finding \d+|F\d+|R\d+|S\d+|[A-Z]?\d+") + r"[^\n]*?— ([^\n]*)\n(.*?)(?=\n## |\Z)",
               re.S | re.M),
    re.compile(r"^\d+\. \*\*" + _ids(r"F\d+|R\d+|S\d+|\d+") + r" — ([^*]*?)\.\*\*\s*(.*?)(?=\n\d+\. \*\*|\n\n|\Z)",
               re.S | re.M),
    re.compile(r"^- \*\*" + _ids(r"[A-Z]\d+") + r"[^*]*?— ([^*]*?)\.\*\*\s*(.*?)(?=\n- \*\*|\n\n|\Z)", re.S | re.M),
    re.compile(r"^\*\*" + _ids(r"[A-Z]\d+") + r" — ([^*]*?)\.\*\*\s*(.*?)(?=\n\*\*|\n\n|\Z)", re.S | re.M),
)
FINDING_REF = re.compile(prreviews.QUALIFIER + r"(?:Finding \d+|[A-Z]?\d+)")


def parse_findings(report: str) -> list[tuple[str, str, str]]:
    """(finding id, disposition, explanation) from a fix report, trying the heading styles the fix
    briefs ask for: `## R01 ... — applied`, `1. **R01 — applied.**`, `- **R01 — applied.**`,
    `**R01 — applied.**`. The id is the heading's, qualified (`RT-Foo~2a/F01`) or naming several
    findings (`RT-Foo~2a/F01, RT-Foo~2b/F03`) as written: `finding_ids` splits it."""
    for style in _FINDING_STYLES:
        rows = [(m.group(1), m.group(2).strip().rstrip("."), m.group(3).strip()) for m in style.finditer(report)]
        if rows:
            return rows
    return []


def finding_ids(heading: str) -> list[str]:
    """The finding ids a section's heading names (`parse_findings`)."""
    return FINDING_REF.findall(heading)


def dispositions(report: str) -> list[tuple[str, str]]:
    """(finding id, disposition) for every finding a repair report's sections name; a section that
    settles several findings counts for each."""
    return [(one, how) for fid, how, _body in parse_findings(report) for one in finding_ids(fid)]


def _within(part: list[str], whole: list[str]) -> bool:
    rest = iter(whole)
    return all(p in rest for p in part)


def names(written: str, ids: list[str]) -> list[str]:
    """The ids among `ids` that a written finding id names: the same finding, the qualification of one
    within the other's (`F01` and `RT-Foo~2a/c7/F01` both name `RT-Foo~2a/F01`; `RT-Foo~2b/F01` does not)."""
    w = written.split("/")
    return [i for i in ids if (p := i.split("/"))[-1] == w[-1] and (_within(w[:-1], p[:-1]) or _within(p[:-1], w[:-1]))]


def coverage_problems(reviews: list[tuple[str, str]], report: str) -> list[str]:
    """The findings of the reviews in scope, each review given as (job id, text), that no section of the
    repair report settles: one section per finding, every finding settled (applied, applied differently,
    disputed, or not actionable with the reason). A section that settles several findings names each.
    An id settles the one finding it names, so where several reviews or comments number their findings
    alike (a red-team round's passes, a review of several comments), only the qualified id
    (`prreviews.review_findings`) does: a bare `F01` names none of them."""
    wanted = list(dict.fromkeys(fid for fid, _sev, _title, _body in prreviews.review_findings(reviews)))
    covered, ambiguous = set(), {}
    for written, _how in dispositions(report):
        named = names(written, wanted)
        if len(named) == 1:
            covered.add(named[0])
        elif named:
            ambiguous[written] = named
    missing = [fid for fid in wanted if fid not in covered]
    if not missing:
        return []
    out = [f"report: no section for the review's finding{'s' if len(missing) > 1 else ''} {', '.join(missing)}: "
           "write one section per finding, headed `## <finding id> (<severity>) — <applied | applied differently | "
           "disputed | not actionable in this package: <reason>>`"]
    return out + [f"report: `{written}` names {len(named)} findings ({', '.join(named)}): head the section with the "
                  "qualified id of each finding it settles" for written, named in ambiguous.items()
                  if set(named) & set(missing)]


NOT_ACTIONABLE = re.compile(r"^not[ -]actionable\b", re.IGNORECASE)


def not_actionable(report: str) -> dict[str, tuple[str, str]]:
    """Finding id -> (title, reason) for the findings a repair report marked `not actionable in this
    package`, with the reason after the disposition (else the first line of the section); a section
    naming several findings gives each the same entry."""
    out = {}
    for fid, how, body in parse_findings(report):
        if not NOT_ACTIONABLE.match(how.strip()):
            continue
        reason = re.sub(r"^not[ -]actionable(?: in this package)?\s*[:.—-]*\s*", "", how.strip(), flags=re.IGNORECASE)
        m = re.search(rf"^(?:##\s+|\d+\.\s+\*\*|-\s+\*\*|\*\*){re.escape(fid)}\b\**\s*(?:[(\[]P[123][)\]])?\s*(.*?)\s*—",
                      report, re.MULTILINE)
        for one in finding_ids(fid):
            out[one] = ((m.group(1).strip(" *") if m else ""), (reason or body.split("\n", 1)[0]).strip())
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
