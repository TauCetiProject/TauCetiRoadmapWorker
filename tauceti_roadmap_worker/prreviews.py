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
The AI review of a roadmap pull request on the content repository (`render_pr_review`) is a comment
in the same shape, first line `🤖 AI review (<agent>, <model>) of <sha40>` (an incremental re-review
adds ` (incremental re-review since <sha7>)`), its verdict `APPROVE`,
`CHANGES NEEDED` or `REJECT`, and its own marker (`tauceti-roadmap-prreview`).
A comment whose first line is `reviewing` (optionally `reviewing (<agent>)`) claims the open review.
Only the shapes are read; everything else in a comment is data. This module is the pure part:
parsing, the contract a review or pass is held to whichever route it came by (`contract_problems`),
and rendering a worker's report into the shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import claims
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
    blocking = [f"{f} ({severity})" for f, severity in parsed.findings if severity in ("P1", "P2")]
    if parsed.verdict == "accepted" and blocking:
        problems.append(f"it accepts with blocking findings ({', '.join(blocking)}): an acceptance leaves P3 notes only")
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


def render(head: str, verdict: str, report: str, marker: dict, conditional: list[str] | None = None, *,
           preamble: list[str] = (), mark: str = MARK) -> str:
    """A worker's report in the comment shape: `head` (the first line), the verdict (and the gaps an
    acceptance is conditional on), the `preamble` lines, the findings numbered F01, F02, ... with
    their severity, the report's other sections, and the worker's marker (`mark`)."""
    findings, rest = report_findings(report)
    lines = [head, "", f"Verdict: {verdict}", ""]
    if conditional:
        lines[-1:] = ["Conditional on the announced gaps: " + ", ".join(f"`{g}`" for g in conditional), ""]
    if preamble:
        lines += [*preamble, ""]
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
    "needs_changes": ("Changes needed before a human's final approval: the findings below (P1 and P2) block it at "
                      "this head."),
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
                     others: int = 0) -> str:
    """A pull-request review's comment: `head` (`pr_review_head`), the verdict line, the head reviewed
    (`Reviewed head: <sha>`), the roadmap folder reviewed (`folder`), what the verdict means for the
    human who approves (with `others` other roadmap folders in the pull request, an acceptance covers
    this one only), the worker's gate at that head (`validation`), then the report's findings and
    sections (`render`) and the marker `tauceti-roadmap-prreview` ({job, head, verdict, agent, model,
    worker, mode}: `incremental` or `full`)."""
    meaning = PR_STATUS_LINES.get(status, PR_STATUS_LINES["needs_changes"])
    if status == "accepted" and others:
        meaning = PR_ACCEPTED_ONE_OF.format(others=others, s="s" if others != 1 else "")
    preamble = [f"Reviewed head: {reviewed}", "", *([f"Roadmap folder: `{folder}/`", ""] if folder else []), meaning]
    if validation:
        preamble += ["", validation]
    return render(head, PR_VERDICTS.get(status, PR_VERDICTS["needs_changes"]), report, marker,
                  conditional if status == "accepted" else None, preamble=preamble, mark=PRREVIEW_MARK)


def prreview_marker(body: str) -> dict:
    """The marker of a pull-request review comment ({job, head, verdict, agent, model, worker, mode}), or {}."""
    return worker_marker(body, PRREVIEW_MARK_RE)
