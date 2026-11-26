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
job, whatever the verdict, and the next `board sync` records the verdict in the register. A job id is
triaged once: a job whose triage is on the board's main already is not run again (it waits on that merged
submission, which `reconcile_merged` finishes), and a triage asked again (a comment with a line `triage
again` on the finished job) is the report's next round, filed by `board sync`, whose inputs name the
earlier rounds (`previous`) and the comment that asked (`asked`): the brief quotes that comment first, then
the other comments newest first within its bound, and `history.md` in the scratch holds them all. The
reporter is an agent of another project, so there is no author to be independent of: any operator may
triage.
"""

from __future__ import annotations

import bisect
import re
import subprocess
from pathlib import Path

from .. import board, gitops, prompts, references, reports
from ..board import Job
from ..gh import GHError
from .base import (TEMP_ROOTS, Context, Lane, LaneBlocked, LaneError, LaneResult, Workspace, clear_pending,
                   read_pending)

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


class Triaged(Exception):
    """The job's triage is on the board's main already: the job waits on that merged submission."""


class ReportLane(Lane):
    kind = "report"
    prompt = "report"

    def run(self, job: Job, ctx: Context) -> LaneResult:
        """A job whose triage is merged already (an attempt whose submission merged before its state label, or a
        human's label move) runs nothing: it is `submitted`, the state of a job waiting on its merged
        submission, and `reconcile_merged` finishes it from that submission."""
        pending = read_pending(ctx, job)
        if pending is not None and not (pending.get("brief") or {}).get("evidence"):
            clear_pending(ctx, job)           # kept by older code, with no digest of its brief: triaged again
        try:
            return super().run(job, ctx)
        except Triaged as exc:
            clear_pending(ctx, job)
            if not ctx.dry_run:
                try:
                    ctx.board.set_state(job, "submitted", f"Not run: {exc}")
                except GHError:
                    pass              # the intake's reconciliation finishes the job from any state but done
            return LaneResult(job.id, "submitted", str(exc))

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        """The board and the roadmap repository at main (the base lane's checkouts); the reported text
        located there. A report whose text main no longer holds, or whose roadmap is now under the completed
        directory, blocks the job: `board sync` records the first as fixed and a human answers the second."""
        report = reports.report_of(job)
        if report is None:
            raise LaneError(f"{job.id}: its spec holds no valid roadmap report")
        ws = super().prepare(job, ctx)
        cfg = ctx.cfg
        if ws.report.is_file():
            raise Triaged(f"`{reports.triage_path(cfg, job)}` is on the board's main already: a job is triaged once, "
                          "and the intake finishes it from its merged submission")
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
        people, later = self.notes(job, ctx)
        history = ws.scratch / "history.md"
        history.write_text("# What people wrote on this report's issues, oldest first (data)\n\n"
                           + ("\n\n---\n\n".join(text for _id, text in people) or "(nothing)") + "\n", encoding="utf-8")
        # The digest of the report the brief states: decided here for a run whose agent reads this brief, and kept
        # in the run's record from its first save on (`keep_run`), so a resumed run (a submission that stopped,
        # finished without the agent) is stamped with the digest of the brief its agent read, at any stop.
        # A Suggested.lean report's digest includes the pinned statement as this checkout has it.
        kept = (read_pending(ctx, job) or {}).get("brief") or {}
        statement = reports.statement_digest(text, report.anchor) if report.file == "Suggested.lean" else ""
        if statement is None and not kept.get("evidence"):
            raise LaneBlocked(f"`{report.anchor}` is not declared exactly once in `{place}` on `{cfg.project.branch}` "
                              f"({head[:7]}): the statement it names cannot be located; a human answers")
        ws.extra["brief"] = {"evidence": kept.get("evidence") or reports.evidence_digest({**job.inputs,
                                                                                          "statement": statement})}
        ws.extra.update(head=head, report=report, place=place, folder=folder, spans=spans, anchor_file=anchor_file,
                        readme=folder / "README.md", prose=self.prose(job, ctx), history=history,
                        job_notes=(shown(people, job.inputs.get("asked"), NOTES_LIMIT, history), later),
                        decls=declaration_lines(pinned_library(ws.content),
                                                list(dict.fromkeys([*report.refutes, *report.replaces]))))
        return ws

    @staticmethod
    def prose(job: Job, ctx: Context) -> str:
        """The reporter's prose, as the board quoted it in the job's issue ('' when it has none). An issue that
        cannot be read fails the round (LaneError, tried again): a triage never runs on part of its inputs."""
        if ctx.gh is None:
            return ""
        try:
            issue = ctx.gh.api(f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}")
        except GHError as exc:
            raise LaneError(f"the job's issue #{job.number} could not be read ({str(exc).splitlines()[0][:120]})") from exc
        if not isinstance(issue, dict):
            raise LaneError(f"the job's issue #{job.number} could not be read (no issue in the answer)")
        return reports.quoted_prose(issue.get("body") or "")

    @staticmethod
    def notes(job: Job, ctx: Context) -> tuple[list[tuple[int, str]], list[str]]:
        """(what people other than the board wrote on the issues of the report's earlier rounds and on the job's
        own, oldest first, as (comment id, text): an answer to an earlier triage among them; the board's own
        notes of later sources and revisions, `Reported again in ...` and `Revised in ...`). An issue whose
        comments cannot be read fails the round (LaneError, tried again): a later round's human answer is the
        reason it runs."""
        bots = {b.lower() for b in ctx.cfg.board.bot_logins}
        issues = [(int(p["issue"]), str(p.get("job") or "")) for p in previous(job) if p.get("issue")]
        people, board_notes = [], []
        for number, name in issues + ([(job.number, "")] if job.number else []):
            try:
                comments = ctx.board.comments(number)
            except GHError as exc:
                raise LaneError(f"the comments of #{number} could not be read ({str(exc).splitlines()[0][:120]})") \
                    from exc
            except AttributeError:            # a board without comments (a dry run's stand-in)
                comments = []
            for c in comments or []:
                who, body = ((c.get("user") or {}).get("login") or ""), str(c.get("body") or "")
                if who.lower() not in bots:
                    on = f" on #{number} (`{name}`)" if name else ""
                    people.append((int(c.get("id") or 0), f"{who or '?'} at {c.get('created_at', '?')}{on}:\n{body}"))
                elif body.startswith(reports.LATER_NOTES) and number == job.number:
                    board_notes.append(body.split(reports.ASK_AGAIN.strip(), 1)[0].strip())
        return people, board_notes

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
        prose, (notes, later) = ws.extra["prose"], ws.extra["job_notes"]  # never `notes`: the round log's
        if later:
            fields.append("- Since it was filed (the board's notes; the fields above are the report as its sources "
                          "say it now): " + " ".join(later))
        earlier = [f"`{reports.triage_path(ctx.cfg, board.new_job(p['job']))}` (#{p.get('issue', '?')}, `{p['job']}`)"
                   for p in previous(job) if (ws.board / reports.triage_path(ctx.cfg, board.new_job(p["job"]))).is_file()]
        return {"ROADMAP": r.roadmap, "FIELDS": "\n".join(fields), "ANCHOR": prompts.quote_untrusted(r.anchor, 1000),
                "ANCHOR_FILE": str(ws.extra["anchor_file"]), "FILE": str(ws.content / ws.extra["place"]),
                "FOLDER": str(ws.extra["folder"]), "HEAD7": ws.extra["head"][:7], "AGENT": ctx.agent,
                "PROSE": (f"```text\n{prompts.quote_untrusted(prose, PROSE_LIMIT)}\n```" if prose
                          else "(the job's issue quotes none)"),
                "DECLARATIONS": "\n".join(ws.extra["decls"]) or "- none named",
                "PREVIOUS": ("this round was asked for by a human after the earlier round(s), whose triage(s) are on "
                             "the board's main (read them in the board checkout): " + ", ".join(earlier)
                             + "; what people wrote on their issues is below." if earlier else "none"),
                "HUMAN_NOTES": (f"```text\n{prompts.quote_untrusted(notes, NOTES_LIMIT + 400)}\n```" if notes
                                else "none")}

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
        """The report gets the digest of the report this triage was briefed on (`Evidence: <digest>`, second
        line, written by the worker, never the agent, from `ws.extra["brief"]`, the one place a run's digest is
        decided): the sync records the verdict only for that evidence."""
        lines = [x for x in ws.report.read_text(encoding="utf-8").split("\n") if not x.startswith("Evidence:")]
        lines.insert(1, f"Evidence: {ws.extra['brief']['evidence']}")
        ws.report.write_text("\n".join(lines), encoding="utf-8")
        verdict = reports.parse_triage("\n".join(lines))["verdict"]
        return f"triage of {job.id} at {ws.extra['head'][:7]}: {verdict}"


def previous(job: Job) -> list[dict]:
    """The report's earlier rounds a later round names (`previous`: [{"job", "issue"}], oldest first)."""
    out = []
    for p in job.inputs.get("previous", []) or []:
        try:
            if isinstance(p, dict) and isinstance(p.get("job"), str) and board.new_job(p["job"]).kind == "report":
                out.append(p)
        except board.JobIdError:
            continue
    return out


def shown(people: list[tuple[int, str]], asked, limit: int, history: Path) -> str:
    """What the brief quotes of people's comments ('' for none): the comment that asked for this round first,
    then the others newest first, within `limit` characters, and where the complete history is (`history`)."""
    first = [text for cid, text in people if asked and cid == asked]
    rest = [text for cid, text in reversed(people) if not (asked and cid == asked)]
    out, used, left = [], 0, 0
    for k, text in enumerate(first + rest):
        if out and used + len(text) > limit:
            left = len(first) + len(rest) - k
            break
        out.append(text[:limit])
        used += len(out[-1]) + 2
    if not out:
        return ""
    tail = (f"\n\n({left} older comment(s) not shown here: the complete history, oldest first, is `{history}`)"
            if left else f"\n\n(the complete history, oldest first, is also in `{history}`)")
    return "\n\n".join(out) + tail
