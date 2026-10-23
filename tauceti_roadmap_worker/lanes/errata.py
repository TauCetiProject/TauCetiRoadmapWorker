"""The `errata` lane: the mistakes in a roadmap's published sources, read at the locators it cites.

One job per roadmap (`ERRATA-<Name>`), on the roadmap as it stands on the content repository's main
(`inputs.ref = "main"`) or at the head of the open pull request that adds it (`inputs.pr`), read-only.
The agent reads the README in full, opens every cited source it can read legally at every cited
locator, and records each misprint, error or gap of the SOURCE in the `## Source issues` shape the
reviews use (the roadmap's own mistakes are review findings, not errata), with what it checked and
what it could not read. The worker posts the report as a comment on the job's own issue; `board sync`
collects it into the errata register (`errata.py`) and marks the job done. No pull request, no push,
and no needs-references request: a source that cannot be read is recorded as not verifiable.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .. import board, gitops, prreviews, references
from ..board import Job
from ..gh import GHError
from .base import TEMP_ROOTS, Context, Lane, LaneBlocked, LaneError, Workspace, rev_dir

QUOTE_LIMIT = 40        # words a quoted span may run to (the reviews' rule: describe, never copy)
QUOTED = re.compile(r"“([^”]*)”|\"([^\"\n]*)\"")


def section_bodies(text: str) -> dict[str, list[str]]:
    """The report's `## ` sections, by lower-cased heading, with their lines (outside code fences)."""
    out: dict[str, list[str]] = {}
    current, fenced = None, False
    for line in text.split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = out.setdefault(line[3:].strip().lower(), [])
        elif current is not None:
            current.append(line)
    return out


def long_quotes(text: str, limit: int = QUOTE_LIMIT) -> list[tuple[int, int]]:
    """(line, words) of every quoted span longer than `limit` words: a run of `>` lines, or text
    between quotation marks. A source is described in one's own words, never copied."""
    out, start, words = [], 0, 0
    lines = text.split("\n")
    for i, line in enumerate([*lines, ""], 1):
        if line.lstrip().startswith(">"):
            start, words = (start or i), words + len(line.lstrip()[1:].split())
            continue
        if start and words > limit:
            out.append((start, words))
        start, words = 0, 0
    for m in QUOTED.finditer(text):
        n = len((m.group(1) or m.group(2) or "").split())
        if n > limit:
            out.append((text.count("\n", 0, m.start()) + 1, n))
    return sorted(out)


def report_problems(text: str, head: str, agent: str, state: str = "") -> list[str]:
    """Why an errata report cannot be posted: its first line and verdict, its `## Source issues`
    (every bullet in the register's shape, or `- none`), a `## Checked` section with counts, no
    machine path and no quoted passage."""
    problems = []
    first = text.split("\n", 1)[0].strip().strip("*").strip()
    m = prreviews.ERRATA_HEAD.match(first)
    if not m or not head.startswith(m["sha"]):
        problems.append(f"report: first line must be {prreviews.errata_head(agent, head)!r}")
    if not prreviews.ERRATA_VERDICT.search(text):
        problems.append("report: no `Verdict: recorded` line after the first line")
    sections = section_bodies(text)
    issues = next((body for heading, body in sections.items() if heading.startswith("source issues")), None)
    if issues is None:
        problems.append("report: no `## Source issues` section (write `- none` when the sources have no mistake)")
    else:
        found = prreviews.source_issues(text)
        for issue in [i for i in found if i["kind"] == "unparsed"][:3]:
            problems.append("report: a source issue not in the required shape (`- **Misprint** | **Error** | **Gap** "
                            "in <source id> at <locator>: the source says ...; it should be ...; affects ...`): "
                            + issue["says"][:160])
        if not found and not any(re.match(r"^\s*[-*]\s*none\b", line, re.IGNORECASE) for line in issues):
            problems.append("report: `## Source issues` lists nothing: write `- none` when the sources have no mistake")
    checked = next((body for heading, body in sections.items() if heading.startswith("checked")), None)
    if checked is None:
        problems.append("report: no `## Checked` section (sources opened, locators read, statements verified)")
    elif not re.search(r"\d", "\n".join(checked)):
        problems.append("report: `## Checked` gives no count (sources opened, locators read, statements verified)")
    if board.PRIVATE_PATH.search(text) or TEMP_ROOTS.search(text) or (state and state in text):
        problems.append("report: a machine path (a home directory, a temporary path or the worker's state "
                        "directory): refer to files by repository-relative paths and to sources by their id")
    for line, words in long_quotes(text)[:3]:
        problems.append(f"report line {line}: a quoted span of {words} words (at most {QUOTE_LIMIT}): describe the "
                        "source in your own words")
    return problems


def pr_state(ctx: Context, number: int) -> str:
    """`open`, `merged` or `closed` for a pull request of the content repository (`open` when it
    cannot be read: the checkout of its head then decides)."""
    if ctx.gh is None:
        return "open"
    try:
        data = ctx.gh.api(f"repos/{ctx.cfg.repositories.content}/pulls/{number}") or {}
    except GHError:
        return "open"
    if data.get("merged_at"):
        return "merged"
    return "closed" if data.get("state") == "closed" else "open"


def roadmap_folder(ctx: Context, job: Job, checkout: Path) -> str:
    """The roadmap's folder in the checkout, relative: the job's `folder`, else its place under the
    active or the completed directory (a roadmap completed since the job was created)."""
    given = str(job.inputs.get("folder") or "").strip("/")
    if given and (Path(given).is_absolute() or ".." in Path(given).parts):
        raise LaneError(f"{job.id}: folder {given!r} is not a folder of the repository")
    cfg = ctx.cfg.roadmap
    places = (given, f"{cfg.active_dir}/{job.name}", f"{cfg.completed_dir}/{job.name}")
    for rel in dict.fromkeys(x for x in places if x):
        if (checkout / rel / "README.md").is_file():
            return rel
    raise LaneError(f"{job.id}: no README.md at {given or cfg.active_dir + '/' + job.name} in the checkout (the "
                    "roadmap was moved or removed)")


class ErrataLane(Lane):
    kind = "errata"
    prompt = "errata"

    @staticmethod
    def pull_request(job: Job) -> int:
        try:
            return int(job.inputs.get("pr") or 0)
        except (TypeError, ValueError):
            return 0

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        """The roadmap read-only: the content checkout at main, or the head of its pull request in the
        host's shared checkout of that pull request (as a red-team pass on a roadmap pull request
        reads it). A pull request merged since the job was created is read on main."""
        ws = super().prepare(job, ctx)
        cfg = ctx.cfg
        pr = self.pull_request(job)
        state = pr_state(ctx, pr) if pr else ""
        if state == "closed":
            raise LaneBlocked(f"pull request #{pr} was closed without merging: its roadmap is on no branch this job "
                              "can read (close the job, or point it at main)")
        if state == "open":
            checkout = rev_dir(ctx.state, pr)
            head = gitops.checkout_pr(cfg.repositories.content, checkout, pr)
            where = f"the head of pull request #{pr}"
        else:
            ref = str(job.inputs.get("ref") or "main")
            if ref not in ("main", cfg.project.branch):
                raise LaneError(f"{job.id}: ref {ref!r}: an errata job reads main or an open pull request")
            if state == "merged":
                self.note(ws, f"#{pr} is merged: the roadmap is read on {cfg.project.branch}")
            checkout, pr = ws.content, 0
            head = gitops.git(checkout, "rev-parse", "HEAD").strip()
            where = f"`{cfg.project.branch}`"
        rel = roadmap_folder(ctx, job, checkout)
        ws.workdir = checkout
        ws.extra.update(head=head, pr=pr, where=where, rel_folder=rel, folder=checkout / rel,
                        readme=checkout / rel / "README.md")
        return ws

    def prepare_tools(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """Nothing: the open roadmap pull requests and the citation resolver serve ownership and
        citation checks, which an errata job does not make."""

    def tools_text(self, ctx: Context, ws: Workspace) -> str:
        return "- The sources service above is the one tool this job needs."

    def manifest_file(self, job: Job, ctx: Context, ws: Workspace) -> Path:
        """The roadmap's own sources manifest at the head read, else its package's on the board."""
        own = Path(ws.extra["folder"]) / "sources.toml"
        path = own if own.is_file() else super().manifest_file(job, ctx, ws)
        if path.is_file() and path == own:
            ws.extra["manifest"] = f"`{ws.extra['rel_folder']}/sources.toml` at `{ws.extra['head'][:7]}`"
        elif path.is_file():
            ws.extra["manifest"] = f"`{board.manifest_path(ctx.cfg.board.packages_dir, job.name)}` on the board"
        return path

    def sources_text(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """The roadmap's sources for the brief: the manifest and the legal copies the worker holds
        (what the review lanes list), or, with no manifest, where the README lists its sources."""
        texts = ws.extra.get("source_texts", "none recorded yet")
        held = texts if texts.startswith("\n") else ""
        fetch = (" A cited source with no copy here: fetch it with the sources service (the command under Sources "
                 "below) and read it from the path it prints.")
        if ws.extra.get("manifest"):
            return (f"The roadmap's sources manifest is {ws.extra['manifest']}." + fetch
                    + ("\n\nThe worker holds these copies (and the citations settled without a text):" + held if held
                       else f"\n\nThe worker holds no copy of its texts yet ({texts})."))
        folder, rel = Path(ws.extra["folder"]), ws.extra["rel_folder"]
        readme = Path(ws.extra["readme"]).read_text(encoding="utf-8", errors="replace")
        heading = re.search(r"^#{2,3}\s+References\b", readme, re.MULTILINE)
        places = ["the README's `## References` section"] if heading else []
        places += [f"`{rel}/references.md`"] if (folder / "references.md").is_file() else []
        places += [f"the notes in `{rel}/references/`"] if (folder / "references").is_dir() else []
        return ("The roadmap has no sources manifest: its sources are the works cited in "
                + (" and ".join(places) or "the README") + "." + fetch
                + ("\n\nThe worker holds:" + held if held else ""))

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        j = board.parse_job_id(job.id)
        earlier = [board.format_job_id(board.JobId("ERRATA", job.name, r)) for r in range(1, j.round)]
        return {"README": str(ws.extra["readme"]), "FOLDER": ws.extra["rel_folder"], "HEAD7": ws.extra["head"][:7],
                "WHERE": ws.extra["where"], "AGENT": ctx.agent, "SOURCES": self.sources_text(job, ctx, ws),
                "REPORT": str(ws.report),
                "PREVIOUS": ", ".join(f"`{e}` (its comment on its own issue)" for e in earlier) or "none"}

    def missing_references(self, job: Job, ctx: Context, ws: Workspace) -> list[references.MissingReference]:
        """None: an errata job never waits for a source. One it could not read is in its report under
        `## Not verifiable`; a request the agent or the sources service wrote is noted, not posted."""
        items = references.read_missing(ws.scratch / "needs-references.toml")
        if items:
            self.note(ws, "not requested (an errata job records an unreadable source as not verifiable): "
                      + ", ".join(m.key for m in items))
        return []

    def comment_body(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """The report in the comment shape the register reads: the errata head, `Verdict: recorded`,
        its sections, and the worker's marker with the head read."""
        report = ws.report.read_text(encoding="utf-8")
        first, _, rest = report.partition("\n")
        if prreviews.ERRATA_HEAD.match(first.strip().strip("*").strip()):
            report = rest
        return prreviews.render(prreviews.errata_head(ctx.agent, ws.extra["head"]), "recorded", report,
                                self.marker(job, ctx, ws))

    def comment_problems(self, body: str) -> list[str]:
        parsed = prreviews.parse(body)
        if parsed is None or parsed.kind != "errata":
            return [("report: the board cannot read it as errata: first line `Errata (<agent>) of <head>`, then "
                     "`Verdict: recorded`")]
        return []

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        if ws.report.is_file():
            problems += report_problems(ws.report.read_text(encoding="utf-8"), ws.extra["head"], ctx.agent,
                                        str(ctx.state))
        mine = {str(ws.report.relative_to(ws.workdir))} if ws.report.is_relative_to(ws.workdir) else set()
        if any(p not in mine for p in gitops.changed_files(ws.workdir)):
            problems.append("the errata pass changed the read-only checkout of the roadmap")
        if not problems:
            problems += self.comment_problems(self.comment_body(job, ctx, ws))
        return problems

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        issues = prreviews.source_issues(ws.report.read_text(encoding="utf-8")) if ws.report.is_file() else []
        kinds = Counter(i["kind"] for i in issues)
        return (f"errata of {job.name} at {ws.extra['head'][:7]}: {len(issues)} source issues ({kinds['misprint']} "
                f"misprints, {kinds['error']} errors, {kinds['gap']} gaps)")

    def submit(self, job: Job, ctx: Context, ws: Workspace, summary: str) -> str:
        """The errata as a comment on the job's own issue, after the job is recorded as this worker's
        and marked submitted (so the board can never collect it and then see it flipped back)."""
        body = self.comment_body(job, ctx, ws)
        if ctx.dry_run:
            return f"[dry-run] would comment on #{job.number}"
        self.after_board_submission(job, ctx)
        self.push(lambda: ctx.board.comment(job.number, body))
        return f"https://github.com/{ctx.cfg.repositories.board_repo()}/issues/{job.number}"
