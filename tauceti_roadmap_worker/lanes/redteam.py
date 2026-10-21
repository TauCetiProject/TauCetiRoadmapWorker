"""The `redteam` lane: an independent whole-document pass on the draft pull request, read-only.

Port of mk-redteam2.sh / run-redteam.sh: each round has one pass per configured agent (the job
names the agent it needs), on a read-only checkout of the pull request head; the report goes to the
board, and a `fix` lane on the pull request branch merges the round's findings.

With `[redteam] sharded = true`, each red-team job also runs one section pass per top-level README
section that states targets (`## Layer ...`, or the `### Layer ...` subsections when the layers sit
under one `## ` wrapper; adjacent sections are grouped beyond `[redteam] shard_max`), on the
`redteam-section` brief: every statement of the section instantiated
on small examples, which catches pinning gaps a whole-document pass misses. The section findings
are renumbered after the whole-document pass's own, tagged with their section, and merged into the
job's report, so the fix lane reads one report per pass as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import board, gitops, prompts, prreviews
from ..board import Job
from .base import (
    Context,
    Lane,
    LaneError,
    Workspace,
    board_reports,
    checkout_board_pr,
    open_roadmap_prs,
    rev_dir,
    sibling_checkouts,
)
from .review import sample_items

FENCE = re.compile(r"^\s*(```|~~~)")
# `## R01 (P1) — title`, and the variants agents write: a colon or hyphen, brackets, no separator.
FINDING = re.compile(r"^## ([RS])(\d+)\s*[(\[](P[123])[)\]]\s*[:—–-]*\s*(.*)$")
LAYER = re.compile(r"^### Layer\b")
# A section states targets when it has bold-labelled paragraphs or bullets, numbered subsections or Lean.
STATEMENT = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)?\*\*[^*\n]{3,}\*\*|^###\s+\d+(?:\.\d+)*\s|^```lean", re.M)
# Headings of sections that frame a roadmap rather than state it.
FRAMING = re.compile(r"^(?:\d+\.\s*)?(?:scope|(?:\w+ )?conventions|references|provenance|sources|what mathlib|"
                     r"existing|dependencies|ownership|prerequisites|workstreams|ordering|merge order|summary|"
                     r"overview|status|contributing)\b", re.I)


@dataclass(frozen=True)
class Section:
    title: str
    start: int          # 1-based line of its `## ` heading
    end: int            # its last line
    text: str

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def split_sections(readme: str) -> list[Section]:
    """The README's top-level sections: each `## ` heading outside code fences up to the next."""
    lines = readme.split("\n")                 # not splitlines(): editors number lines by "\n" only
    if lines and lines[-1] == "":
        lines.pop()
    heads, fenced = [], False
    for i, line in enumerate(lines, 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            heads.append(i)
    ends = [h - 1 for h in heads[1:]] + [len(lines)] if heads else []
    return [Section(lines[start - 1][3:].strip(), start, end, "\n".join(lines[start - 1:end]))
            for start, end in zip(heads, ends, strict=True)]


def layer_units(sections: list[Section]) -> list[Section]:
    """`## ` sections as they are, except that one holding `### Layer` subsections becomes those layers
    (the text before its first layer stays with the wrapper and is read as context)."""
    out = []
    for section in sections:
        lines = section.text.split("\n")           # keeps a trailing blank line, so line numbers stay exact
        heads, fenced = [], False
        for i, line in enumerate(lines):
            if FENCE.match(line):
                fenced = not fenced
            elif not fenced and LAYER.match(line):
                heads.append(i)
        if not heads:
            out.append(section)
            continue
        for k, i in enumerate(heads):
            j = heads[k + 1] if k + 1 < len(heads) else len(lines)
            out.append(Section(lines[i][4:].strip(), section.start + i, section.start + j - 1, "\n".join(lines[i:j])))
    return out


def statement_sections(sections: list[Section]) -> list[Section]:
    """The sections that state targets: every layer, and any other section with statement-like
    content unless its heading says it only frames the roadmap."""
    return [s for s in sections if re.match(r"layer\b", s.title, re.I)
            or (not FRAMING.match(s.title) and (sample_items(s.text) or STATEMENT.search(s.text)))]


def group_sections(sections: list[Section], max_groups: int) -> list[list[Section]]:
    """At most `max_groups` runs of adjacent sections, as even in length as whole sections allow."""
    if len(sections) <= max_groups:
        return [[s] for s in sections]

    def greedy(cap: int) -> list[list[Section]]:
        groups, current, size = [], [], 0
        for s in sections:
            if current and size + s.size > cap:
                groups.append(current)
                current, size = [], 0
            current.append(s)
            size += s.size
        return groups + ([current] if current else [])

    lo, hi = max(s.size for s in sections), sum(s.size for s in sections)
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if len(greedy(mid)) <= max_groups else (mid + 1, hi)
    return greedy(lo)


def _blocks(text: str) -> tuple[list[tuple[tuple, list[str]]], dict[str, list[str]]]:
    """A report's finding blocks ((prefix, number, severity, title), body lines) and its other `## `
    sections by heading. Lines inside code fences belong to the block they are in."""
    findings, others, current, fenced = [], {}, None, False
    for line in text.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        if fenced or FENCE.match(line):
            if current is not None:
                current.append(line)
            continue
        m = FINDING.match(line)
        if m:
            current = []
            findings.append(((m.group(1), int(m.group(2)), m.group(3), m.group(4).strip()), current))
        elif line.startswith("## "):
            current = others.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return findings, others


def unparsed_findings(text: str) -> bool:
    """A report whose verdict asks for changes but in which no finding heading can be read."""
    verdict = re.search(r"^\*\*Verdict:\*\*\s*(.*)$", text, re.M)
    return bool(verdict and "changes needed" in verdict.group(1).lower() and not _blocks(text)[0])


def merge_section_reports(main: str, shards: list[tuple[str, str | None]], prefix: str = "R") -> str:
    """The whole-document report with every section pass's findings inserted before its
    `## Verified sound`, renumbered to continue its own sequence and tagged with their section; the
    sections' instance tables and the passes that wrote nothing are listed at the end."""
    own = [n for (p, n, _s, _t), _ in _blocks(main)[0] if p == prefix]
    number = max(own, default=0)
    added, summary = [], ["## Section passes", ""]
    for label, text in shards:
        if text is None:
            summary.append(f"- {label}: no report (the pass wrote nothing)")
            continue
        findings, others = _blocks(text)
        summary.append(f"- {label}: {len(findings)} finding(s)")
        for (_p, _n, severity, title), body in findings:
            number += 1
            added.append(f"## {prefix}{number:02d} ({severity}) — [{label}] {title}")
            added += [*body]
        for heading in ("Instances", "Not verifiable"):
            if others.get(heading) and any(line.strip() for line in others[heading]):
                summary += ["", f"### {label}: {heading.lower()}", *[line for line in others[heading] if line.strip()]]
    lines = main.rstrip("\n").splitlines()
    at = next((i for i, line in enumerate(lines) if line.strip() == "## Verified sound"), len(lines))
    merged = lines[:at] + ([*added, ""] if added else []) + lines[at:] + ["", *summary]
    text = "\n".join(merged) + "\n"
    if added:
        text = re.sub(r"^\*\*Verdict:\*\*.*$", "**Verdict:** changes needed", text, count=1, flags=re.M)
    return text


def report_problems(text: str, pr: int, name: str, head7: str, pass_: str) -> list[str]:
    first = text.split("\n", 1)[0].strip()
    want = f"# Red-team review of #{pr} {name} at {head7} (pass {pass_})"
    problems = [] if first == want else [f"report: first line must be {want!r}"]
    if "**Verdict:**" not in text:
        problems.append("report: no **Verdict:** line")
    return problems


class RedTeamLane(Lane):
    kind = "redteam"
    prompt = "redteam"

    transcribes_if_ready = True     # a pass reads the signature transcript when the pin's packages are ready

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        if self.board_pr(job):
            # The package on its board pull request, read-only, before any port.
            checkout_board_pr(ctx, ws, self.board_pr(job))
            prs = open_roadmap_prs(ctx)
            ws.extra.update(prs=prs, siblings=sibling_checkouts(ctx, prs, skip=job.name), pr=self.board_pr(job),
                            folder=ws.package(ctx.cfg, job.name), readme=ws.package(ctx.cfg, job.name) / "README.md")
            ws.extra["previous"] = board_reports(ctx, ws, job.name, job.inputs.get("previous", []) +
                                                 ([job.inputs["repair"]] if job.inputs.get("repair") else []), "previous")
            ws.workdir = ws.board
            return ws
        pr = int(job.inputs.get("pr") or 0)
        if not pr:
            raise LaneError(f"{job.id} names no pull request")
        prs = open_roadmap_prs(ctx)
        ws.extra["prs"] = prs
        ws.extra["siblings"] = sibling_checkouts(ctx, prs, skip=job.name)
        mine = next((p for p in prs if p["pr"] == pr), None)
        if not mine:
            raise LaneError(f"pull request #{pr} is not an open roadmap pull request")
        rev = rev_dir(ctx.state, pr)
        gitops.checkout_pr(ctx.cfg.repositories.content, rev, pr)
        ws.workdir, ws.extra["pr"], ws.extra["head"], ws.extra["branch"] = rev, pr, mine["head"], mine["branch"]
        ws.extra["readme"] = rev / ctx.cfg.roadmap.active_dir / job.name / "README.md"
        ws.extra["folder"] = rev / ctx.cfg.roadmap.active_dir / job.name
        return ws

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        j = board.parse_job_id(job.id)
        pkg_reports = ws.package(ctx.cfg, job.name) / "reports"
        second = ""
        if j.round > 1:
            if ws.extra.get("previous"):
                *earlier, repair_report = ws.extra["previous"]
                previous, repair_path = ", ".join(f"`{p}`" for p in earlier), repair_report
            else:
                previous = ", ".join(f"`{pkg_reports / (p + '.md')}`" for p in job.inputs.get("previous", []))
                repair_path = pkg_reports / (job.inputs.get("repair", "") + ".md")
            second = (f"**This is round {j.round}.** The earlier passes are {previous or 'in the reports folder'} and their "
                      f"repairs are described in `{repair_path}`; the head you review carries those repairs. "
                      "First verify, finding by finding, that each earlier finding is repaired at this head (quote the repaired "
                      "passage; mark it repaired, partly repaired or not repaired, with the witness) and that no repair "
                      "introduced a new error; then run the full pass again, with fresh attention to the classes the earlier "
                      "passes did not find. Number new findings from S01.")
        else:
            second = ("This is the first red-team round of an independently reviewed package: expect what the review "
                      "missed (supplier names that do not match the siblings' current heads, missing Lean mirrors, "
                      "locators the sample did not cover) as well as mathematical defects.")
        if self.board_pr(job):
            outside = f"git -C {ws.workdir} diff --stat origin/main...HEAD"
        else:
            outside = (f"git -C {ws.workdir} diff --stat origin/{ctx.cfg.project.branch}...HEAD -- . "
                       f"':!{ctx.cfg.roadmap.active_dir}/{job.name}'")
        return {"PR": str(ws.extra["pr"]), "HEAD": ws.extra["head"][:7], "PASS": f"{j.pass_} ({ctx.agent})",
                "SECOND_PASS": second, "OUTSIDE_CHECK": outside, "FOLDER": str(ws.extra.get("folder", ""))}

    def briefs(self, job: Job, ctx: Context, ws: Workspace, values: dict) -> list[tuple[str, str]]:
        runs = super().briefs(job, ctx, ws, values)
        if not ctx.cfg.redteam.sharded:
            return runs
        readme = ws.extra.get("readme")
        text = readme.read_text(encoding="utf-8") if readme and readme.is_file() else ""
        groups = group_sections(statement_sections(layer_units(split_sections(text))), ctx.cfg.redteam.shard_max)
        ws.extra["shards"] = []
        for k, group in enumerate(groups, 1):
            report = ws.scratch / "sections" / f"section-{k:02d}.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.unlink(missing_ok=True)           # a report from an earlier attempt must not stand in
            shard = {"index": k, "title": " / ".join(s.title for s in group),
                     "lines": f"{group[0].start}–{group[-1].end}", "report": report}
            ws.extra["shards"].append(shard)
            runs.append((f"section-{k:02d}", prompts.render("redteam-section", {
                **values, "SECTION_INDEX": str(k), "SECTION_COUNT": str(len(groups)), "SECTION_TITLE": shard["title"],
                "SECTION_LINES": shard["lines"], "SECTION_REPORT": str(report)})))
        return runs

    def after_runs(self, job: Job, ctx: Context, ws: Workspace) -> None:
        shards = ws.extra.get("shards") or []
        if not shards or not ws.report.is_file():
            return
        texts = [(f"§{s['index']} {s['title']}", s["report"].read_text(encoding="utf-8") if s["report"].is_file() else None)
                 for s in shards]
        prefix = "S" if board.parse_job_id(job.id).round > 1 else "R"
        ws.report.write_text(merge_section_reports(ws.report.read_text(encoding="utf-8"), texts, prefix), encoding="utf-8")

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        shards = ws.extra.get("shards") or []
        for s in shards:
            head = f"# Section pass {s['index']}/{len(shards)} of #{ws.extra['pr']} {job.name} at {ws.extra['head'][:7]}"
            if not s["report"].is_file():
                problems.append(f"section pass {s['index']} ({s['title']}) wrote no report")
            elif not s["report"].read_text(encoding="utf-8").startswith(head):
                problems.append(f"section pass {s['index']}: first line must start with {head!r}")
            elif unparsed_findings(s["report"].read_text(encoding="utf-8")):
                problems.append(f"section pass {s['index']}: the verdict asks for changes but no finding is written "
                                "as `## R01 (P1) — <title>`")
        if ws.report.is_file():
            j = board.parse_job_id(job.id)
            problems += report_problems(ws.report.read_text(encoding="utf-8"), ws.extra["pr"], job.name,
                                        ws.extra["head"][:7], f"{j.pass_} ({ctx.agent})")
        mine = {str(ws.report.relative_to(ws.board))} if ws.report.is_relative_to(ws.board) else set()
        if any(p not in mine for p in gitops.changed_files(ws.workdir)):
            problems.append("the red-team pass changed the read-only checkout")
        if self.board_pr(job) and not problems and ws.report.is_file():
            problems += self.comment_problems(self.comment_body(job, ctx, ws))
        return problems

    def comment_body(self, job: Job, ctx: Context, ws: Workspace) -> str:
        j = board.parse_job_id(job.id)
        report = ws.report.read_text(encoding="utf-8")
        verdict = "needs_changes" if prreviews.report_findings(report)[0] else "accepted"
        return prreviews.render(prreviews.redteam_head(j.round, j.pass_, ctx.agent, ws.extra["head"]), verdict, report,
                                self.marker(job, ctx, ws))

    def submit_on_board_pr(self, job: Job, ctx: Context, ws: Workspace, summary: str) -> str:
        """The pass as a comment on the board pull request, in the shape the board reads."""
        body = self.comment_body(job, ctx, ws)
        if not ctx.dry_run:
            self.after_board_submission(job, ctx)
        return self.comment_on_board_pr(job, ctx, ws, body)

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        return f"red-team pass {job.id} on #{ws.extra['pr']} at {ws.extra['head'][:7]}"
