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
The AI review of a roadmap pull request on the content repository (`render_pr_review`) has the
posted layout (`render_posted`): first line `🤖 AI review (<agent>, <model>) of <sha40>` (an incremental
re-review adds ` (incremental re-review since <sha7>)`), its verdict `APPROVE`, `CHANGES NEEDED` or
`REJECT`, the head and folder reviewed, a `**Summary.**`, the findings (F01…), small corrections
(C01…) and format notes (N01…) apart, the questions, one collapsed block of what was checked, and
its own marker (`tauceti-roadmap-prreview`). Only findings decide a verdict; nothing reads a small
correction or a format note as one. Comments posted before this layout are read too (`read_posted`).
A comment whose first line is `reviewing` (optionally `reviewing (<agent>)`) claims the open review.
Only the shapes are read; everything else in a comment is data. This module is the pure part:
parsing, the contract a review or pass is held to whichever route it came by (`contract_problems`),
and rendering a worker's report into the shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import claims, gates
from .board import Job

MARK = "tauceti-roadmap-review"
MARK_RE = re.compile(r"<!--\s*" + MARK + r"\s*(\{.*?\})\s*-->", re.DOTALL)
PRREVIEW_MARK = "tauceti-roadmap-prreview"
PRREVIEW_MARK_RE = re.compile(r"<!--\s*" + PRREVIEW_MARK + r"\s*(\{.*?\})\s*-->", re.DOTALL)
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
# A finding id's qualification by its review's job id and its comment (`review_findings`).
QUALIFIER = r"(?:[A-Z]+-[\w~-]*/)?(?:c\d+/)?"
FROM_LINE = re.compile(r"^<!-- from @?[^,\n]*, (\S*) -->$", re.MULTILINE)


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
    evidence: int = 0                                               # rows of its adversarial tables

    @property
    def blocking(self) -> int:
        return sum(1 for _f, severity in self.findings if severity in ("P1", "P2"))

    @property
    def provenance(self) -> tuple[str, str]:
        """(agent, model) behind the comment: its worker's marker when it carries one, else the agent
        its first line names and no model."""
        if self.worker:
            return str(self.worker.get("agent", self.agent)), str(self.worker.get("model", ""))
        return self.agent, ""


def normal_verdict(text: str) -> str:
    text = text.lower().replace(" ", "_")
    return "needs_changes" if text in ("needs_changes", "changes_needed") else text


def worker_marker(body: str, pattern: re.Pattern = MARK_RE) -> dict:
    """The worker's marker a comment carries ({job, worker, agent, model, head}), or {}."""
    found = pattern.findall(body or "")
    try:
        marker = json.loads(found[-1]) if found else {}
    except ValueError:
        return {}
    return marker if isinstance(marker, dict) else {}


def parse(body: str) -> Parsed | None:
    """The review, red-team pass, errata or claim a comment states, or None (anything else, or a review
    without a verdict, or one that asks for changes without a numbered finding)."""
    lines = (body or "").strip().splitlines()
    if not lines:
        return None
    first = lines[0].strip().strip("*").strip()
    worker = worker_marker(body)
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
    parsed.evidence = adversarial_rows(body)
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


def report_comments(text: str) -> list[tuple[str, str]]:
    """The comments a review's report holds, as (key, text). The intake joins the comments a review
    counted under a `<!-- from login, url -->` line each (`@login` in older reports); the key is `c` and the comment's id from the
    url (its position when the url has none). A report without such lines is one comment, keyed ''."""
    text = text or ""
    heads = list(FROM_LINE.finditer(text))
    if not heads:
        return [("", text)]
    out = []
    for k, m in enumerate(heads):
        cid = re.search(r"#issuecomment-(\d+)$", m.group(1))
        end = heads[k + 1].start() if k + 1 < len(heads) else len(text)
        out.append((f"c{cid.group(1) if cid else k + 1}", text[m.end():end]))
    return out


def review_findings(reviews: list[tuple[str, str]]) -> list[tuple[str, str, str, str]]:
    """(id, severity, title, body) of every finding of the reviews in scope, each review given as (job
    id, text). Every review, and every comment of one, numbers its findings from F01: when more than one
    of them has findings, each id is qualified by its review (`RT-Foo~2a/F01`), and by its comment too
    when that review holds several with findings (`REV-PKG-Foo~2/c123/F01`); else the ids are the
    review's own."""
    parts = [(job, key, found) for job, text in reviews for key, part in report_comments(text)
             if (found := findings_with_bodies(part))]
    jobs = [job for job, _key, _found in parts]
    out = []
    for job, key, found in parts:
        prefix = "" if len(parts) == 1 else f"{job}/" + (f"{key}/" if jobs.count(job) > 1 else "")
        out += [(prefix + fid, severity, title, body) for fid, severity, title, body in found]
    return out


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


def contract_problems(parsed: Parsed, job: Job, declared: set[str] | None = None) -> list[str]:
    """Why a review or red-team pass does not count for `job` ([] when it does): the contract the
    review and red-team lanes hold a worker's report to, which the board applies to every comment,
    whichever route it came by, and a lane to the comment it is about to post.

    Every finding names its severity, so that the verdict record's counts are the findings'; an
    acceptance has no P1 or P2 finding; an accepted review, and a pass with no finding, carry the
    adversarial table (`adversarial_rows`); an acceptance is conditional only on gaps in `declared`,
    the slugs the plan at the reviewed head declares (None: not checked).

    A pass job that requires an agent counts a pass by that agent's family of models only
    (`claims.family`), and one that requires a model, a pass whose worker marker names that model.
    A comment without a marker is trusted only from a listed login or the board's bot
    (`boardpr.trusted`): the name it signs with (`chatgpt`, `claude`, ...) is that login's
    attestation of the family that ran, and its model is not checked."""
    problems = []
    unrated = [f for f, severity in parsed.findings if not severity]
    if unrated:
        problems.append(f"finding {', '.join(unrated)} names no severity (P1, P2 or P3)")
    listed = [f"{f} ({severity})" for f, severity in parsed.findings]
    if parsed.verdict == "accepted" and listed:
        problems.append(f"it accepts with findings ({', '.join(listed)}): an acceptance lists no finding; a repair, "
                        "whatever its priority, asks for changes")
    if not parsed.evidence and parsed.kind == "review" and parsed.verdict == "accepted":
        problems.append(EVIDENCE)
    elif not parsed.evidence and parsed.kind == "redteam" and not parsed.findings:
        problems.append(EVIDENCE.replace("an acceptance", "a pass with no finding"))
    if parsed.verdict == "accepted" and declared is not None and set(parsed.conditional) - declared:
        problems.append("it is conditional on gaps the plan at this head does not declare: "
                        + ", ".join(f"`{g}`" for g in sorted(set(parsed.conditional) - declared)))
    if parsed.kind == "redteam" and job.requires_agent:
        agent, model = parsed.provenance
        if claims.family(agent) != claims.family(job.requires_agent):
            problems.append(f"it is signed `{agent}`, but this pass is for the family of `{job.requires_agent}` (sign "
                            + ", ".join(f"`{n}`" for n in claims.family_names(job.requires_agent)) + ")")
        elif job.requires_model and parsed.worker and model != job.requires_model:
            problems.append(f"its worker ran `{model or '?'}`, but this pass requires the model `{job.requires_model}`")
    return problems


def reraised(title: str, body: str) -> set[str]:
    """The earlier finding ids a finding says it re-raises (`Re-raised: F01`), qualified as the repair
    report wrote them (`Re-raised: REV-PKG-Foo~2/c123/F01`)."""
    return {fid for m in RERAISED.findall(title + "\n" + body)
            for fid in re.findall(r"\b" + QUALIFIER + r"[RSF]\d+\b", m)}


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
        if line.startswith("<details") or line.lstrip().startswith("<!--"):
            inside = False             # (a comment's collapsed block, or its marker, ends the section)
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
    findings, rest = _report_blocks(report)
    return [(severity, title, body) for _fid, severity, title, body in findings], rest


def report_finding_ids(report: str) -> list[str]:
    """The ids (`R01`, `S02`, ...) of the findings `report_findings` reads, in order."""
    return [fid for fid, _severity, _title, _body in _report_blocks(report)[0]]


def _report_blocks(report: str) -> tuple[list[tuple[str, str, str, list[str]]], list[str]]:
    findings, rest, current, fenced = [], [], None, False
    for line in (report or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        m = None if fenced else REPORT_FINDING.match(line)
        if m:
            current = []
            findings.append((m.group(1) + m.group(2), m.group(3), m.group(4).strip(), current))
        elif not fenced and line.startswith("## "):
            current = None
            rest.append(line)
        elif current is not None:
            current.append(line)
        elif not line.startswith("# ") and not re.match(r"^\**Verdict", line):
            rest.append(line)
    return findings, rest


HUMAN_SECTION = "For the human reviewers"
HUMAN_HEADING = re.compile(r"^##\s+" + HUMAN_SECTION + r"\s*$", re.IGNORECASE)
NO_FLAG = re.compile(r"^\W*(none|nothing)\W*$", re.IGNORECASE)


def human_flags(report: str) -> list[str] | None:
    """The bullets of a report's `## For the human reviewers` section: the questions a human decides
    (whether the subject is mainstream research mathematics, a licence), which are not findings.
    `[]` when the section says `- none`; `None` when the report has no such section or it lists
    nothing, which is a report that did not answer."""
    bullets, inside, fenced = [], False, False
    for line in (report or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        if fenced:
            continue
        if line.startswith("## ") or (inside and (line.startswith("<details") or line.lstrip().startswith("<!--"))):
            if inside:
                break
            inside = bool(HUMAN_HEADING.match(line))
        elif inside and re.match(r"^\s*[-*]\s+\S", line):
            bullets.append(re.sub(r"^\s*[-*]\s+", "", line).strip())
        elif inside and bullets and line.strip():
            bullets[-1] += " " + line.strip()
    if not bullets:
        return None
    return [b for b in bullets if not NO_FLAG.match(b)]


def human_notice(report: str) -> str:
    """The line a comment carries under its verdict when its report flags something for the humans
    who approve: the flags are in the report's last section, where few would look for them."""
    flags = human_flags(report) or []
    if not flags:
        return ""
    return (f"**For the human reviewers:** this review raises {len(flags)} question{'s' if len(flags) != 1 else ''} "
            "that a human decides (the scope of the roadmap, or a licence). "
            f"{'They are' if len(flags) != 1 else 'It is'} in the section of that name at the end and "
            f"{'do' if len(flags) != 1 else 'does'} not change the verdict.")


PROVENANCE_LABELS = ("Source independence", "Research link")
PROVENANCE_LINE = re.compile(r"^[\s>*_-]*(" + "|".join(PROVENANCE_LABELS) + r")\**\s*:\**\s*(.*)$")


def provenance_lines(report: str) -> list[str]:
    """A report's `Source independence:` and `Research link:` statements, each as one line
    `**Label:** text` (the first of each, with the lines that continue it up to a blank line, a list
    item, a heading or the other statement), in that order: what a comment shows under its verdict,
    because the report states them among its checks, where the humans who approve seldom read."""
    found: dict[str, list[str]] = {}
    current, fenced = None, False
    for line in (report or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced, current = not fenced, None
        if fenced:
            continue
        m = PROVENANCE_LINE.match(line)
        if m:
            current = found.setdefault(m.group(1), []) if m.group(1) not in found else None
            if current is not None:
                current.append(m.group(2).strip())
        elif current is not None and line.strip() and not re.match(r"^\s*([-*+]\s|\d+\.\s|#|\|)", line):
            current.append(line.strip())
        else:
            current = None
    return [f"**{label}:** " + " ".join(x for x in found[label] if x) for label in PROVENANCE_LABELS
            if " ".join(found.get(label, [])).strip()]


def render(head: str, verdict: str, report: str, marker: dict, conditional: list[str] | None = None, *,
           preamble: list[str] = (), mark: str = MARK) -> str:
    """A worker's report in the comment shape: `head` (the first line), the verdict (and the gaps an
    acceptance is conditional on), the `preamble` lines, the findings numbered F01, F02, ... with
    their severity, the report's other sections, and the worker's marker (`mark`). A report that flags
    something for the human reviewers says so before its findings (`human_notice`), after the
    report's statements on its sources and on research (`provenance_lines`)."""
    findings, rest = report_findings(report)
    lines = [head, "", f"Verdict: {verdict}", ""]
    if conditional:
        lines[-1:] = ["Conditional on the announced gaps: " + ", ".join(f"`{g}`" for g in conditional), ""]
    if preamble:
        lines += [*preamble, ""]
    for stated in provenance_lines(report):
        lines += [stated, ""]
    notice = human_notice(report)
    if notice:
        lines += [notice, ""]
    for k, (severity, title, body) in enumerate(findings, 1):
        lines.append(f"{k}. **F{k:02d}** ({severity}) {title}")
        lines += [("   " + line) if line.strip() else "" for line in body]
    text = "\n".join(lines).rstrip() + "\n\n" + "\n".join(rest).strip() + "\n"
    return text + "\n" + f"<!-- {mark} {json.dumps(marker, sort_keys=True)} -->\n"


def review_head(agent: str, sha: str) -> str:
    return f"Review ({agent}) of {sha[:7]}"


def redteam_head(round_: int, pass_: str, agent: str, sha: str) -> str:
    return f"Red team round {round_} pass {pass_} ({agent}) of {sha[:7]}"


def errata_head(agent: str, sha: str) -> str:
    return f"Errata ({agent}) of {sha[:7]}"


# --- the AI review of a roadmap pull request ---------------------------------------------------------

# The verdict line of a pull-request review: an acceptance reads `Verdict: APPROVE`, which the gate
# lane's reading of the timeline (`replies.APPROVE_VERDICT`) never takes for a review to answer; the
# others read as a review that asks for changes.
PR_VERDICTS = {"accepted": "APPROVE", "needs_changes": "CHANGES NEEDED", "rejected": "REJECT"}
PR_STATUS_LINES = {
    "accepted": ("The AI review accepts this head: the pull request is ready for a human's final approval. The AI "
                 "does not approve or merge; a human does."),
    "needs_changes": ("Changes needed before a human's final approval: the findings below block it at this head "
                      "(every finding, P3 included, is a change to make)."),
    "rejected": ("Changes needed: the AI review rejects the roadmap as it stands at this head (its scope or plan, "
                 "see the findings); a human decides how to continue."),
}


def pr_review_head(agent: str, model: str, sha: str, since: str = "") -> str:
    """The first line of a pull-request review: `🤖 AI review (<agent>, <model>) of <sha40>`, and for an
    incremental re-review of the change since the head `since` the previous review read,
    ` (incremental re-review since <since7>)`."""
    return f"\U0001f916 AI review ({agent}, {model}) of {sha}" + (f" (incremental re-review since {since[:7]})"
                                                                  if since else "")


# An acceptance of one roadmap folder of a pull request that adds or changes several.
PR_ACCEPTED_ONE_OF = ("The AI review accepts this roadmap folder at this head. The pull request also adds or changes "
                      "{others} other roadmap folder{s}, each reviewed on its own: it is ready for a human's final "
                      "approval once every one is accepted. The AI does not approve or merge; a human does.")


def render_pr_review(head: str, status: str, report: str, marker: dict, reviewed: str,
                     validation: str = "", conditional: list[str] | None = None, *, folder: str = "",
                     others: int = 0, pass_note: str = "") -> str:
    """A pull-request review's comment, in the posted layout (`render_posted`): `head`
    (`pr_review_head`), the verdict line (and the gaps an acceptance is conditional on), the head
    reviewed (`Reviewed head: <sha>`), the roadmap folder (`folder`), `pass_note` for a pass before the
    final review, then the report's `**Summary.**` (composed by `posted_summary` when the report has
    none: with `others` other roadmap folders an acceptance covers this one only), its findings
    (renumbered F01, …), small corrections (C01, …), format notes (N01, …), questions for the author,
    questions for the human reviewers and source issues; then one collapsed block of what was
    checked: the sentence on the worker's checks (`validation`), the earlier findings, what could not
    be verified, the checks set aside as false positives; then the marker `tauceti-roadmap-prreview`
    ({job, head, verdict, agent, model, worker, mode}: `incremental`, `change` or `full`). `## Verified sound`,
    `Source independence:` and `Research link:` stay in the report and are not posted."""
    p = posted_from_report(report)
    p.head = head
    p.verdict_line = f"Verdict: {PR_VERDICTS.get(status, PR_VERDICTS['needs_changes'])}"
    if status == "accepted" and conditional:
        p.conditional_line = "Conditional on the announced gaps: " + ", ".join(f"`{g}`" for g in conditional)
    p.reviewed = f"Reviewed head: {reviewed}"
    p.folder = f"Roadmap folder: `{folder}/`" if folder else ""
    p.pass_note, p.others, p.gate = pass_note, others, validation
    if p.summary and status == "accepted" and others and "other roadmap folder" not in p.summary:
        p.summary += " " + others_sentence(others).replace("Accepted for this roadmap folder. ", "")
    p.markers = [f"<!-- {PRREVIEW_MARK} {json.dumps(marker, sort_keys=True)} -->"]
    return render_posted(p)


def prreview_marker(body: str) -> dict:
    """The marker of a pull-request review comment ({job, head, verdict, agent, model, worker, mode}), or {}."""
    return worker_marker(body, PRREVIEW_MARK_RE)


# --- the posted layout of a pull-request review, and the reading of both its forms -------------------
#
# The comment a pull-request review posts reads, top to bottom: the header lines tools read (the
# `🤖 AI review` line, `Verdict:`, `Reviewed head:`, `Roadmap folder:`), a `**Summary.**` of two to four
# sentences, then `## Findings`, `## Small corrections (non-blocking)`, `## Format notes (non-blocking)`,
# `## Questions for the author`, `## For the human reviewers` and `## Source issues`, each only when it
# has something, then one collapsed block of what was checked, then the marker. Comments posted before
# this layout (the status sentence and the gate's sentence under the header, the `Source independence:`
# and `Research link:` statements, the findings straight after them, then `## Previous findings`,
# `## Verified sound`, `## Not verifiable` and the rest) are read by the same parser (`read_posted`).

POSTED_HEAD = re.compile(r"^\W*AI review \((?P<agent>[^,)\n]+),\s*(?P<model>[^)\n]*)\) of (?P<sha>[0-9a-f]{7,40})"
                         r"(?: \(incremental re-review since (?P<since>[0-9a-f]{7,40})\))?\s*$")
REMARK = re.compile(r"^(?P<n>\d+)\.\s+\*\*(?P<id>[FCN]\d+)\*\*\s*(?:\((?P<sev>P[123])\))?\s*(?P<title>.*)$")
FINDINGS_HEADING = "Findings"
CORRECTIONS_HEADING = "Small corrections (non-blocking)"
NOTES_HEADING = "Format notes (non-blocking)"
QUESTIONS_HEADING = "Questions for the author"
SOURCE_HEADING = "Source issues"
# The visible sections after the remarks, in this order; everything else goes in the collapsed block.
VISIBLE = (QUESTIONS_HEADING, HUMAN_SECTION, SOURCE_HEADING)
DROPPED = ("Verified sound",)
DETAILS_OPEN = "<details><summary>What was checked</summary>"
DETAILS_CLOSE = "</details>"
SUMMARY_LEAD = "**Summary.**"
PASS_LEAD = "This is a pass before the final review."
ONE_OF = re.compile(r"also adds or changes (\d+) other roadmap folders?")
GATE_SENTENCE = re.compile(r"^At `[0-9a-f]{7}`: ")
PIN_MARK = "tauceti-roadmap-text-version"
PIN_RE = re.compile(r"<!--\s*" + PIN_MARK + r"\s*(\{.*?\})\s*-->", re.DOTALL)
FENCE_LINE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Remark:
    id: str                 # F01, C01, N01
    severity: str           # P1, P2, P3 for a finding; '' otherwise
    title: str
    body: list[str]         # its lines as posted (indented under the item)

    def lines(self, number: int) -> list[str]:
        return [f"{number}. **{self.id}**" + (f" ({self.severity})" if self.severity else "") + f" {self.title}".rstrip(),
                *self.body]


@dataclass
class Posted:
    """A pull-request review comment, read into its parts (`read_posted`)."""
    head: str                                                   # the first line, as posted
    agent: str = ""
    model: str = ""
    sha: str = ""
    since: str = ""
    verdict_line: str = ""
    conditional_line: str = ""
    reviewed: str = ""                                          # the `Reviewed head:` line
    folder: str = ""                                            # the `Roadmap folder:` line
    pass_note: str = ""
    others: int = 0                                             # other roadmap folders of the pull request
    summary: str = ""                                           # the `**Summary.**` paragraph (new form)
    gate: str = ""                                              # the sentence on the worker's gate
    findings: list[Remark] = field(default_factory=list)
    corrections: list[Remark] = field(default_factory=list)
    notes: list[Remark] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)   # (heading, text) of the other `##` sections
    loose: list[str] = field(default_factory=list)              # paragraphs under no heading, kept in the block
    details: str = ""                                           # the collapsed block's text (new form)
    markers: list[str] = field(default_factory=list)            # the marker comments at the end, verbatim
    new_form: bool = False

    @property
    def verdict(self) -> str:
        m = re.search(r"(APPROVE|CHANGES NEEDED|REJECT)", self.verdict_line)
        return {"APPROVE": "accepted", "CHANGES NEEDED": "needs_changes", "REJECT": "rejected"}.get(
            m.group(1) if m else "", "")

    def section(self, heading: str) -> str:
        return next((text for h, text in self.sections if h == heading), "")


def _paragraphs(lines: list[str]) -> list[list[str]]:
    out, cur = [], []
    for line in lines:
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _items(lines: list[str]) -> tuple[list[Remark], list[str]]:
    """The numbered remarks at the start of `lines` (each with its indented body), and what follows them."""
    out: list[Remark] = []
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines) and (m := REMARK.match(lines[i])):
        body, i = [], i + 1
        while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")) and not REMARK.match(lines[i]):
            body.append(lines[i])
            i += 1
        while body and not body[-1].strip():
            body.pop()
        out.append(Remark(m.group("id"), m.group("sev") or "", m.group("title").strip(), body))
        while i < len(lines) and not lines[i].strip():
            i += 1
    return out, lines[i:]


def read_posted(body: str) -> Posted | None:
    """A pull-request review comment in either layout, read into its parts; None for a comment that is
    not one (its first line is not a review's, or it carries no `tauceti-roadmap-prreview` marker)."""
    lines = (body or "").rstrip("\n").split("\n")
    if not lines or not (m := POSTED_HEAD.match(lines[0].strip())) or not PRREVIEW_MARK_RE.search(body or ""):
        return None
    p = Posted(lines[0].rstrip(), agent=m["agent"].strip(), model=m["model"].strip(), sha=m["sha"], since=m["since"] or "")
    # The marker comments close the comment; whatever comes after them is kept with them.
    end = len(lines)
    while end > 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("<!--")):
        end -= 1
    p.markers = [x for x in lines[end:] if x.strip()]
    rest = lines[1:end]
    # Split off the `##` sections and the collapsed block (outside code fences).
    top, sections, details, current, fenced, in_details = [], [], [], None, False, False
    for line in rest:
        if FENCE_LINE.match(line):
            fenced = not fenced
        if not fenced and line.strip() == DETAILS_OPEN:
            in_details, current = True, None
            p.new_form = True
            continue
        if in_details:
            if not fenced and line.strip() == DETAILS_CLOSE:
                in_details = False
            else:
                details.append(line)
            continue
        if not fenced and line.startswith("## "):
            current = [line[3:].strip(), []]
            sections.append(current)
        elif current is not None:
            current[1].append(line)
        else:
            top.append(line)
    p.details = "\n".join(details).strip("\n")
    # The header lines and the paragraphs before the first remark or section.
    first = next((k for k, line in enumerate(top) if REMARK.match(line)), len(top))
    for para in _paragraphs(top[:first]):
        lead = para[0].strip()
        text = "\n".join(para)
        if re.match(r"^\W*verdict\W", lead, re.IGNORECASE) and not p.verdict_line:
            p.verdict_line = para[0]
            p.conditional_line = next((x for x in para[1:] if x.startswith("Conditional on")), "")
        elif lead.startswith("Reviewed head:") and not p.reviewed:
            p.reviewed = para[0]
        elif lead.startswith("Roadmap folder:") and not p.folder:
            p.folder = para[0]
        elif lead.startswith(PASS_LEAD):
            p.pass_note = text
        elif lead.startswith(SUMMARY_LEAD):
            p.summary = text
        elif lead in PR_STATUS_LINES.values() or lead.startswith("The AI review accepts this roadmap folder"):
            if mm := ONE_OF.search(text):
                p.others = int(mm.group(1))
        elif GATE_SENTENCE.match(lead) and not p.gate:
            p.gate = text
        elif PROVENANCE_LINE.match(lead) or lead.startswith("**For the human reviewers:** this review raises"):
            continue                       # restated elsewhere, or not posted any more
        else:
            p.loose.append(text)
    p.findings, after = _items(top[first:])
    p.loose += ["\n".join(x) for x in _paragraphs(after)]
    for heading, text in sections:
        if heading in (FINDINGS_HEADING, CORRECTIONS_HEADING, NOTES_HEADING):
            found, after = _items(text)
            {FINDINGS_HEADING: p.findings, CORRECTIONS_HEADING: p.corrections, NOTES_HEADING: p.notes}[heading].extend(found)
            if any(x.strip() for x in after):
                p.sections.append((f"{heading} (continued)", "\n".join(after).strip("\n")))
        else:
            p.sections.append((heading, "\n".join(text).strip("\n")))
    return p


def _bullets(text: str) -> list[str]:
    return [x for x in text.split("\n") if re.match(r"^\s*[-*]\s+\S", x) and not NO_FLAG.match(re.sub(r"^\s*[-*]\s+", "", x))]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _clip(title: str, limit: int = 160) -> str:
    title = " ".join(title.split()).rstrip(".")
    return title if len(title) <= limit else title[:limit - 1].rstrip() + "…"


def posted_summary(p: Posted) -> str:
    """The `**Summary.**` a review comment opens with, composed from its parts: the verdict in words,
    the main findings by their own titles (P1 and P2 first), the counts of findings, small corrections
    and format notes, and what is left for a human to decide. Two to four sentences."""
    questions = len(_bullets(p.section(HUMAN_SECTION)))
    asked = len(_bullets(p.section(QUESTIONS_HEADING)))
    minor = []
    if p.corrections:
        minor.append(_plural(len(p.corrections), "small correction"))
    if p.notes:
        minor.append(_plural(len(p.notes), "format note"))
    if p.verdict == "accepted":
        sentences = ["Accepted: this roadmap folder is ready for a human's final approval (the AI does not approve or "
                     "merge)." if not p.others else others_sentence(p.others)]
        if minor:
            sentences.append(" and ".join(minor).capitalize() + ", none blocking.")
    else:
        counts = [f"{sum(1 for f in p.findings if f.severity == s)} {s}" for s in ("P1", "P2", "P3")
                  if any(f.severity == s for f in p.findings)]
        what = (f"{_plural(len(p.findings), 'finding')} block{'s' if len(p.findings) == 1 else ''} this head"
                + (f" ({', '.join(counts)})" if counts else ""))
        sentences = [("Changes needed: the review rejects the roadmap as it stands; " if p.verdict == "rejected"
                      else "Changes needed: ") + what + "."]
        main = [f for f in p.findings if f.severity in ("P1", "P2")][:2] or p.findings[:1]
        if main:
            sentences.append(("The main one" if len(main) == 1 else "The main ones") + ": "
                             + "; ".join(f"{f.id}, {_clip(f.title)}" for f in main) + ".")
        if minor:
            sentences.append(" and ".join(minor).capitalize() + " besides, none blocking.")
    left = []
    if asked:
        left.append(f"{_plural(asked, 'question')} for the author")
    if questions:
        left.append(f"{_plural(questions, 'question')} for the human reviewers, which do not change the verdict")
    sentences.append("It leaves " + " and ".join(left) + "." if left else "Nothing is left for a human to decide.")
    return SUMMARY_LEAD + " " + " ".join(sentences[:4])


def others_sentence(others: int) -> str:
    """What an acceptance of one roadmap folder means when the pull request has `others` more."""
    return (f"Accepted for this roadmap folder. The pull request also adds or changes {_plural(others, 'other roadmap folder')}, "
            "each reviewed on its own: it is ready for a human's final approval once every one is accepted (the AI "
            "does not approve or merge).")


DETAIL_LABELS = {"Previous findings": "Earlier findings", "Not verifiable": "Not verified",
                 "Deferred findings": "Deferred findings"}


def posted_details(p: Posted) -> str:
    """The collapsed block of an old-form comment: the gate's sentence, the paragraphs under no heading,
    and the sections that are not posted in the open (earlier findings, what could not be verified, the
    deferred findings, any other), each under a bold label; `## Verified sound` is not posted."""
    if p.new_form:
        return p.details
    parts = [x for x in [p.gate, *p.loose] if x.strip()]
    for heading, text in p.sections:
        if heading in VISIBLE or heading in DROPPED or not text.strip():
            continue
        base = heading.split(" (", 1)[0]
        label = DETAIL_LABELS.get(base, base) + heading[len(base):]
        parts.append(f"**{label}.**\n\n{text.strip()}")
    return "\n\n".join(parts)


def render_posted(p: Posted, pin: str = "") -> str:
    """The comment of `p` in the posted layout, with the text-version pin `pin` when given (`pin_marker`)."""
    out = [p.head, "", p.verdict_line] + ([p.conditional_line] if p.conditional_line else []) + [""]
    out += [x for line in (p.reviewed, p.folder, p.pass_note) if line for x in (line, "")]
    out += [p.summary or posted_summary(p), ""]
    for heading, remarks in ((FINDINGS_HEADING, p.findings), (CORRECTIONS_HEADING, p.corrections),
                             (NOTES_HEADING, p.notes)):
        if remarks:
            out += [f"## {heading}", ""]
            for k, remark in enumerate(remarks, 1):
                out += [*remark.lines(k), ""]
        if heading == FINDINGS_HEADING and p.section(f"{heading} (continued)"):
            out += [p.section(f"{heading} (continued)"), ""]
    for heading in VISIBLE:
        text = p.section(heading)
        if not any(x.strip() and not NO_FLAG.match(re.sub(r"^\s*[-*]\s+", "", x)) for x in text.split("\n")):
            continue                     # (nothing, or `- none`: the section is left out)
        out += [f"## {heading}", "", text.strip(), ""]
    details = posted_details(p)
    if details.strip():
        out += [DETAILS_OPEN, "", details.strip(), "", DETAILS_CLOSE, ""]
    markers = [m for m in p.markers if not PIN_RE.search(m)]
    if pin:
        markers.append(pin_marker(pin))
    return "\n".join(out).rstrip("\n") + "\n\n" + "\n".join(markers) + "\n"


def pin_marker(version: str) -> str:
    """The comment that pins a reformatted review's text version to that of the text it replaced."""
    return f"<!-- {PIN_MARK} {json.dumps({'version': version})} -->"


def pinned_version(body: str) -> str:
    """The text version a review comment pins (`pin_marker`), honoured only on a comment that carries a
    pull-request review's marker; '' when there is none."""
    if not PRREVIEW_MARK_RE.search(body or ""):
        return ""
    found = PIN_RE.findall(body or "")
    try:
        version = json.loads(found[-1]).get("version") if found else ""
    except (ValueError, AttributeError):
        return ""
    return version if isinstance(version, str) and re.fullmatch(r"[0-9a-f]{12}", version) else ""


def posted_identity(body: str) -> tuple:
    """What the pipeline reads from a review comment, which a reformatting must leave as it was: the
    first line's agent, model, head and incremental base, the verdict and the gaps it is conditional on,
    the reviewed head and the folder, the findings' ids, severities and titles (`findings_with_bodies`),
    the marker, the questions for the human reviewers and the source issues."""
    p = read_posted(body)
    if p is None:
        return ()
    gaps = gap_slugs(p.conditional_line.split(":", 1)[1]) if ":" in p.conditional_line else []
    return (p.agent, p.model, p.sha, p.since, p.verdict, tuple(gaps), p.reviewed.strip(), p.folder.strip(),
            tuple((f, s, t) for f, s, t, _b in findings_with_bodies(body)),
            json.dumps(prreview_marker(body), sort_keys=True), tuple(human_flags(body) or []),
            json.dumps(source_issues(body), sort_keys=True))


def reformat_posted(body: str, version: str) -> str | None:
    """A review comment rebuilt in the posted layout (`render_posted`), pinned to `version` (the text
    version of the comment as first posted: a pin already there is kept), or None when it is not a
    review comment or the rebuild would change what the pipeline reads from it (`posted_identity`)."""
    p = read_posted(body)
    if p is None:
        return None
    if not pinned_version(body) and render_posted(p).rstrip("\n") == (body or "").rstrip("\n"):
        return body              # posted in this layout already (by a worker of this build): nothing to pin
    new = render_posted(p, pinned_version(body) or version)
    return new if posted_identity(new) == posted_identity(body) else None


# --- a pull-request review's report, read into the posted layout ----------------------------------------

REPORT_REMARK = re.compile(r"^## ([CN])(\d+)\s*(?:[(\[]P[123][)\]])?\s*[:—–-]*\s*(.*)$")
FALSE_POSITIVES = re.compile(r"^\W*Automatic checks set aside as false positives\b", re.IGNORECASE)


def report_sections(report: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(the lines before the first `## ` heading, [(heading text, its lines)]) of a report, outside code
    fences."""
    top: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fenced = False
    for line in (report or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            top.append(line)
    return top, sections


def report_remarks(report: str) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    """(the small corrections, the format notes) of a report: its `## C01 — title` and `## N01 — title`
    blocks, each as (title, body lines)."""
    corrections, notes = [], []
    for heading, lines in report_sections(report)[1]:
        m = REPORT_REMARK.match("## " + heading)
        if m:
            body = list(lines)
            while body and not body[-1].strip():
                body.pop()
            while body and not body[0].strip():
                body.pop(0)
            (corrections if m.group(1) == "C" else notes).append((m.group(3).strip(), body))
    return corrections, notes


def report_summary(report: str) -> str:
    """The report's `**Summary.**` paragraph ('' when it has none)."""
    top, _sections = report_sections(report)
    for para in _paragraphs(top):
        if para[0].strip().startswith(SUMMARY_LEAD):
            return "\n".join(x.strip() for x in para)
    return ""


def _indented(lines: list[str]) -> list[str]:
    return [("   " + line) if line.strip() else "" for line in lines]


def posted_from_report(report: str) -> Posted:
    """A worker's report as the parts of a posted comment (`render_posted`): its summary, its findings
    (`report_findings`, renumbered F01…), small corrections and format notes (`report_remarks`), the
    sections posted in the open (questions for the author, for the human reviewers, source issues),
    and, for the collapsed block, the earlier findings, what could not be verified, the deferred
    findings and the checks set aside as false positives. The verified-sound evidence, the
    provenance statements and anything else stay in the report."""
    p = Posted("")
    p.summary = report_summary(report)
    for k, (severity, title, body) in enumerate(report_findings(report)[0], 1):
        lines = _indented(body)
        while lines and not lines[-1].strip():
            lines.pop()
        p.findings.append(Remark(f"F{k:02d}", severity, title, lines))
    corrections, notes = report_remarks(report)
    p.corrections = [Remark(f"C{k:02d}", "", title, ["", *_indented(body)] if body else [])
                     for k, (title, body) in enumerate(corrections, 1)]
    p.notes = [Remark(f"N{k:02d}", "", title, ["", *_indented(body)] if body else [])
               for k, (title, body) in enumerate(notes, 1)]
    _top, sections = report_sections(report)
    folded = []
    for heading, lines in sections:
        text = "\n".join(lines).strip("\n")
        base = heading.split(" (", 1)[0]
        if REPORT_FINDING.match("## " + heading) or REPORT_REMARK.match("## " + heading):
            continue
        if heading in VISIBLE:
            p.sections.append((heading, text))
            continue
        skipped = [x for x in lines if FALSE_POSITIVES.match(x)]
        if skipped:
            p.loose.extend(x.strip() for x in skipped)
            text = "\n".join(x for x in lines if not FALSE_POSITIVES.match(x)).strip("\n")
        if base in DETAIL_LABELS and text.strip():
            folded.append((heading, text))
    p.sections += folded
    return p


# --- plain language: what a posted remark may not contain -------------------------------------------------

def _rule_ids() -> re.Pattern:
    keys = [k for k in gates.RULE_KINDS if k != "set_option"]
    hyphenated = [k for k in keys if "-" in k] + ["prnum", "depcheck"]
    return re.compile(r"`(?:" + "|".join(map(re.escape, keys)) + r")`"
                      r"|(?<![\w-])(?:" + "|".join(map(re.escape, hyphenated)) + r")(?![\w-])"
                      r"|\bgate\s*:\s*\w|\bgate\s+(?:rule|check|failure|report|result|scanner|label)s?\b"
                      r"|\bthe\s+(?:worker's\s+)?gate\b", re.IGNORECASE)


PRIVATE_FILE = re.compile(
    r"(?<![\w.-])(?:gate\.md|gate\.json|changes\.md|pull-request\.md|overlaps\.md|library-matches\.md|reviews\.md|"
    r"answered\.json|repair-report\.md|needs-references\.toml|brief(?:-[\w-]+)?\.md)(?!\w)"
    r"|\bscratch/|\bjob folder\b|\bthe\s+(?:worker's\s+)?brief\b|/home/|/tmp/|/Users/", re.IGNORECASE)
JOB_ID = re.compile(r"\b(?:PRREVIEW|GATE|REV|RT|FIX|PORT|PKG|DESIGN|TARGET|ERRATA)-[A-Z][\w~-]*")


def plain_language_problems(report: str) -> list[str]:
    """What a pull-request review's report would post that names a check's label (`prnum`,
    `readme-ids-missing`, "gate: residue"), a file or folder of the worker (its check report, a
    scratch file, the brief, the job folder), a job id or a local path: each a problem asking for the
    sentence instead. Only what is posted is read (`posted_from_report`); the verified-sound evidence
    stays in the report and may name what it likes."""
    p = posted_from_report(report)
    parts = [("the summary", p.summary)]
    report_ids = report_finding_ids(report)
    parts += [(report_ids[k] if k < len(report_ids) else f.id, "\n".join([f.title, *f.body]))
              for k, f in enumerate(p.findings)]
    parts += [(r.id, "\n".join([r.title, *r.body])) for r in (*p.corrections, *p.notes)]
    parts += [(f"`## {h}`", text) for h, text in p.sections] + [("the line on checks set aside", x) for x in p.loose]
    rule_id, out = _rule_ids(), []
    for label, text in parts:
        for pattern, what in ((rule_id, "the label of an automatic check"),
                              (PRIVATE_FILE, "a file or folder of the worker, or a local path"), (JOB_ID, "a job id")):
            m = pattern.search(text or "")
            if m:
                out.append(f"report: {label} names {what} ({m.group(0).strip()!r}): say in a sentence what the rule "
                           "is and why it matters here (the plain words are beside each check in the list of "
                           "automatic checks), never a check's label, a worker's file, a job id or a path")
    return out
