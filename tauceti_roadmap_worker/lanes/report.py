"""The `report` lane: the triage of a roadmap report (`REPORT-<Leaf>~k`, filed by `board sync` from a
Tau Ceti worker's report in the library: reports.py).

Read-only, like the target triage and the errata lane. The worker reads the roadmap on the content
repository's main, finds the reported file and the quoted text in it (a text no longer there, or a
roadmap moved under the completed directory, blocks the job: `board sync` records the first as fixed,
and a human answers the second), looks up every declaration the report names in the pinned Tau Ceti, and
gives the agent the validated report, where the text stands, the reporter's prose quoted as data (from
the job's issue, where the board quoted it), the source links and those lookups. The agent writes one
triage report, `packages/<Leaf>/reports/REPORT-<Leaf>~k.md`, whose first lines are

    Triage (<agent>) of REPORT-<Leaf>~k at <sha7 of main>
    Verdict: confirmed | rejected | needs-human

with `## Evidence` and the section its verdict needs (`## Correction`, `## Reason`, `## Missing`:
`reports.triage_problems`). It is submitted as a board pull request; the intake merges it and closes the
job (`confirmed`, `rejected`) or parks it for a human (`needs-human`), and the next `board sync` records
the verdict in the register. The reporter is an agent of another project, so there is no author to be
independent of: any operator may triage.
"""

from __future__ import annotations

import bisect
import re
import subprocess
from pathlib import Path

from .. import board, gitops, prompts, references, reports
from ..board import Job
from ..gh import GHError
from .base import TEMP_ROOTS, Context, Lane, LaneBlocked, LaneError, Workspace

CONTEXT_LINES = 12      # lines of the file shown around each place the anchor stands
PROSE_LIMIT = 4000      # characters of the reporter's prose a brief quotes
NOTES_LIMIT = 6000      # characters of human comments on the job a brief quotes
LEAN_DECL = r"(theorem|lemma|def|abbrev|structure|class|instance|inductive|axiom|opaque)"


def anchor_spans(text: str, anchor: str) -> list[tuple[int, int]]:
    """The 1-based line ranges where the anchor stands in `text`, whitespace normalised as the harvest
    compares it (`reports.holds`: blank lines and line breaks count as one space)."""
    want = reports.normalise(anchor)
    pieces = [(i, " ".join(line.split())) for i, line in enumerate(text.split("\n"))]
    pieces = [(i, n) for i, n in pieces if n]
    starts, joined = [], ""
    for _i, n in pieces:
        starts.append(len(joined))
        joined += n + " "
    out, pos = [], 0
    while want and (hit := joined.find(want, pos)) >= 0:
        first = bisect.bisect_right(starts, hit) - 1
        last = bisect.bisect_right(starts, hit + len(want) - 1) - 1
        span = (pieces[first][0] + 1, pieces[last][0] + 1)
        if span not in out:
            out.append(span)
        pos = hit + 1
    return out


def excerpt(text: str, spans: list[tuple[int, int]], around: int = CONTEXT_LINES) -> str:
    """The file around each span, with line numbers, the span's lines marked `>`."""
    lines, out = text.split("\n"), []
    for first, last in spans[:5]:
        lo, hi = max(1, first - around), min(len(lines), last + around)
        out.append(f"Lines {first}-{last}:\n")
        out += [f"{'>' if first <= n <= last else ' '} {n:5} | {lines[n - 1]}" for n in range(lo, hi + 1)]
        out.append("")
    return "\n".join(out)


def pinned_library(content: Path) -> Path | None:
    """The pinned Tau Ceti's sources in the content checkout (`.lake/packages`, wired for its pin), or None."""
    root = content / ".lake" / "packages" / "TauCeti" / "TauCeti"
    return root if root.is_dir() else None


def declaration_lines(root: Path | None, names: list[str], timeout: int = 120) -> list[str]:
    """For each declaration the report names, where a declaration of its last component stands in the
    pinned Tau Ceti (`root`), or that none does. A name of a namespace is not checked: the agent opens
    the hit and reads its namespace."""
    out = []
    for name in names:
        last = name.split(".")[-1]
        if root is None:
            out.append(f"- `{name}`: not looked up (the pinned Tau Ceti's sources are not on this host)")
            continue
        pattern = (rf"^\s*(@\[[^]]*\]\s*)?((private|protected|noncomputable|nonrec)\s+)*{LEAN_DECL}\s+"
                   rf"(\S+\.)?{re.escape(last)}(\s|$|:)")
        try:
            found = subprocess.run(["grep", "-rnE", "--include=*.lean", "-m", "3", pattern, str(root)],
                                   capture_output=True, text=True, timeout=timeout,
                                   check=False).stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            found = None
        if found is None:
            out.append(f"- `{name}`: not looked up (the search failed)")
        elif not found:
            out.append(f"- `{name}`: no declaration `{last}` in the pinned Tau Ceti")
        else:
            hits = ", ".join("`TauCeti/" + str(Path(h.split(":", 1)[0]).relative_to(root)) + ":" + h.split(":")[1] + "`"
                             for h in found[:3])
            out.append(f"- `{name}`: a declaration `{last}` at {hits} in the pinned Tau Ceti")
    return out


class ReportLane(Lane):
    kind = "report"
    prompt = "report"

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        """The board and the roadmap repository at main (the base lane's checkouts); the reported text
        located there. A report whose text main no longer holds, or whose roadmap is now under the completed
        directory, blocks the job: `board sync` records the first as fixed and a human answers the second."""
        report = reports.report_of(job)
        if report is None:
            raise LaneError(f"{job.id}: its spec holds no valid roadmap report")
        ws = super().prepare(job, ctx)
        cfg = ctx.cfg
        active, completed = reports.paths(cfg, report)
        head = gitops.git(ws.content, "rev-parse", "HEAD").strip()
        place = next((p for p in (active, completed) if (ws.content / p).is_file()), "")
        text = (ws.content / place).read_text(encoding="utf-8", errors="replace") if place else ""
        spans = anchor_spans(text, report.anchor) if text else []
        if not spans:
            raise LaneBlocked(f"the reported text is no longer in `{active}` on `{cfg.project.branch}` "
                              f"({head[:7]}): `board sync` records the report as fixed and closes this job")
        if place == completed:
            raise LaneBlocked(f"the roadmap is now under `{cfg.roadmap.completed_dir}/`, where no lane works: this "
                              "report needs a human")
        folder = ws.content / Path(place).parent
        anchor_file = ws.scratch / "anchor.md"
        anchor_file.write_text(f"# `{place}` at `{head[:7]}`: where the reported text stands (data)\n\n"
                               + excerpt(text, spans), encoding="utf-8")
        ws.extra.update(head=head, report=report, place=place, folder=folder, spans=spans, anchor_file=anchor_file,
                        readme=folder / "README.md", prose=self.prose(job, ctx), notes=self.human_notes(job, ctx),
                        decls=declaration_lines(pinned_library(ws.content),
                                                list(dict.fromkeys([*report.refutes, *report.replaces]))))
        return ws

    @staticmethod
    def prose(job: Job, ctx: Context) -> str:
        """The reporter's prose, as the board quoted it in the job's issue ('' when it has none or the issue
        cannot be read)."""
        if ctx.gh is None:
            return ""
        try:
            issue = ctx.gh.api(f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}") or {}
        except GHError:
            return ""
        return reports.quoted_prose(issue.get("body") or "")

    @staticmethod
    def human_notes(job: Job, ctx: Context) -> str:
        """What people other than the board wrote on the job's issue (an answer to an earlier `needs-human`
        triage), oldest first, as one text; '' when there is none or the issue cannot be read."""
        bots = {b.lower() for b in ctx.cfg.board.bot_logins}
        try:
            comments = ctx.board.comments(job.number) if job.number else []
        except (GHError, AttributeError):
            return ""
        notes = [f"{(c.get('user') or {}).get('login', '?')} at {c.get('created_at', '?')}:\n{c.get('body') or ''}"
                 for c in comments or [] if ((c.get("user") or {}).get("login") or "").lower() not in bots]
        return "\n\n".join(notes)

    def missing_references(self, job: Job, ctx: Context, ws: Workspace) -> list[references.MissingReference]:
        """None: a triage never waits for a source. What it could not read is in its `## Missing`, with a
        `needs-human` verdict when that decides it."""
        items = references.read_missing(ws.scratch / "needs-references.toml")
        if items:
            self.note(ws, "not requested (a triage records what it could not read under `## Missing`): "
                      + ", ".join(m.key for m in items))
        return []

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        r: reports.Report = ws.extra["report"]
        spans = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in ws.extra["spans"])
        fields = [f"- Roadmap `{r.roadmap}`, file `{r.file}`: `{ws.extra['place']}` (lines {spans} on main, "
                  f"`{ws.extra['head'][:7]}`); the reporter read `{r.roadmap_sha[:12]}`.",
                  f"- Kind: `{r.kind}`; effect: `{r.effect}`.",
                  "- Refutes (as the reporter names them): " + (", ".join(f"`{n}`" for n in r.refutes) or "none"),
                  "- Replaces (as the reporter names them): " + (", ".join(f"`{n}`" for n in r.replaces) or "none"),
                  "- Reported in: " + (", ".join(str(s) for s in job.inputs.get("sources", [])) or "(no link)")]
        prose, notes = ws.extra["prose"], ws.extra["notes"]
        earlier = ws.report.is_file()
        return {"ROADMAP": r.roadmap, "FIELDS": "\n".join(fields), "ANCHOR": prompts.quote_untrusted(r.anchor, 1000),
                "ANCHOR_FILE": str(ws.extra["anchor_file"]), "FILE": str(ws.content / ws.extra["place"]),
                "FOLDER": str(ws.extra["folder"]), "HEAD7": ws.extra["head"][:7], "AGENT": ctx.agent,
                "PROSE": (f"```text\n{prompts.quote_untrusted(prose, PROSE_LIMIT)}\n```" if prose
                          else "(the job's issue quotes none)"),
                "DECLARATIONS": "\n".join(ws.extra["decls"]) or "- none named",
                "PREVIOUS": (f"An earlier triage of this job, which a human was asked to settle, is `{ws.report}`; what "
                             "people wrote on the job's issue since is below." if earlier else "none"),
                "HUMAN_NOTES": (f"```text\n{prompts.quote_untrusted(notes, NOTES_LIMIT)}\n```" if notes else "none")}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        if ws.report.is_file():
            text = ws.report.read_text(encoding="utf-8")
            problems += reports.triage_problems(text, job.id, ws.extra["head"])
            if board.PRIVATE_PATH.search(text) or TEMP_ROOTS.search(text) or str(ctx.state) in text:
                problems.append("report: a machine path (a home directory, a temporary path or the worker's state "
                                "directory): refer to files by repository-relative paths")
        if [p for p in gitops.changed_files(ws.content) if not p.startswith(".lake")]:     # the wired packages
            problems.append("the triage changed the read-only checkout of the roadmap repository")
        return problems

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        verdict = reports.parse_triage(ws.report.read_text(encoding="utf-8"))["verdict"]
        return f"triage of {job.id} at {ws.extra['head'][:7]}: {verdict}"
