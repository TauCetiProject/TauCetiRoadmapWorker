"""What every lane shares: the run context, the workspace, the brief, the agent run, the
`needs-references` hand-off and the submission to the board.

A lane run is: eligible? -> clean checkouts of the board's main -> make the manifest's texts
available in the sources cache -> render the brief (or briefs) -> run the agent on each -> parked
on missing references? -> mechanical checks -> lane-specific finish (push, open a PR, reply) ->
submit the report and deliverables to the board as a pull request that names the job. Anything a
run writes in the board checkout is either submitted or reproducible from the board: the next
round starts from the board's main again.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import shutil
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .. import (agents, board, claims, depgraph, fetcher, gates, gitops, openroadmaps, prompts, prreviews, references,
               signatures, sources, usage)
from ..board import Board, Job
from ..config import Config
from ..depgraph import read_order
from ..gh import GH, GHError, transient
from ..paths import sanitize_worker_id
from .lake import MIN_FREE_GB_BUILD, MIN_FREE_GB_WARN, LakeSetup, free_gb, prepare_lake  # noqa: F401 (re-exports)

PR_MARKER = "tauceti-roadmap-pr"


@dataclass
class Context:
    cfg: Config
    gh: GH
    board: Board
    claims: claims.Claims | None
    worker_id: str
    operator: str
    agent: str
    model: str
    conf: str = ""
    dry_run: bool = False
    agent_fixed: bool = True   # False: each job runs on its lane's configured agent (or the one it requires)
    model_fixed: bool = False  # True: --model pinned; jobs whose lane pins another model are skipped
    transport: object = None   # the sources service's HTTP transport (None: urllib); tests pass a fake

    def sources_fetcher(self) -> fetcher.Fetcher:
        return fetcher.Fetcher(fetcher.client_from_config(self.cfg, self.transport), sources.cache_root(self.state),
                               self.cfg.sources)

    @property
    def state(self) -> Path:
        return self.cfg.state_dir()

    @property
    def wdir(self) -> Path:
        return self.state / "workers" / sanitize_worker_id(self.worker_id)

    def cli(self, *args: str) -> str:
        """A command line agents can run to call this harness (gate, depcheck)."""
        conf = f"--conf {shlex.quote(self.conf)} " if self.conf else ""
        return f"tauceti-roadmap {conf}" + " ".join(shlex.quote(a) for a in args)


@dataclass
class Workspace:
    job_dir: Path
    scratch: Path
    board: Path
    content: Path
    workdir: Path
    report: Path
    branch: str
    extra: dict = field(default_factory=dict)
    job_id: str = ""

    def package(self, cfg: Config, name: str) -> Path:
        return self.board / board.package_dir(cfg.board.packages_dir, name)


@dataclass
class LaneResult:
    job: str
    status: str            # submitted | failed | limited | needs-references | skipped | dry-run
    detail: str = ""
    url: str = ""
    retry_at: dt.datetime | None = None
    agent: str = ""        # the family the round ran (a usage limit pauses that family only)
    worked: float = 0.0    # seconds the agent had run in this round when a usage limit stopped it

    def describe(self) -> str:
        """`status: detail` for the log; a usage limit hit after work says how much was lost."""
        if self.status == "limited" and self.worked >= LIMIT_WORK_NOTED:
            return f"limited (after {round(self.worked / 60)} min of work): {self.detail}"
        return f"{self.status}: {self.detail}"


LIMIT_WORK_NOTED = 60      # seconds of agent work before a usage limit that the log counts as wasted


def limited(job: Job, result: agents.AgentResult, where: str, worked: float) -> LaneResult:
    """The round's result for an agent run that hit a usage limit: the message's first line, the
    reset time the message gave, and the agent time spent in the round up to and including the run."""
    return LaneResult(job.id, "limited", (result.message or "usage limit") + where, retry_at=result.reset_at,
                      worked=worked)


# GitHub refuses an issue comment above 65,536 characters; the review is one comment.
COMMENT_LIMIT = 60_000

class LaneError(Exception):
    pass


class LaneBlocked(LaneError):
    """The job cannot go on without a human (a merge-order cycle): it is set to `state:blocked`."""


# --- shared lookups -------------------------------------------------------------------------------

def pr_answerers(cfg, owner: str) -> set[str]:
    """Who may answer for a pull request: its author, plus every pool member when the author is in the pool."""
    pool = set(cfg.roadmap.pool)
    return {owner} | pool if owner in pool else {owner}


def pr_operator(cfg, owner: str) -> str:
    """The operator a gate round of this pull request requires: "" (any operator) for a pool author."""
    return "" if owner in set(cfg.roadmap.pool) else owner


def open_roadmap_prs(ctx: Context) -> list[dict]:
    """Open pull requests the port lane opened (label `roadmap.pr_label`, marker in the body):
    [{pr, name, head, branch, owner}], cached in the state directory for the gate CLI."""
    repo = ctx.cfg.repositories.content
    items = ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True)
    out = []
    for pr in items:
        if ctx.cfg.roadmap.pr_label not in {label["name"] for label in pr.get("labels", [])}:
            continue
        marker = re.search(r"<!--\s*" + PR_MARKER + r"\s*(\{.*?\})\s*-->", pr.get("body") or "", re.S)
        try:
            data = json.loads(marker.group(1)) if marker else {}
        except ValueError:
            data = {}
        if data.get("roadmap"):
            owner = pr["user"]["login"]
            out.append({"pr": pr["number"], "name": data["roadmap"], "head": pr["head"]["sha"],
                        "branch": pr["head"]["ref"], "owner": owner,
                        "head_repo": ((pr.get("head") or {}).get("repo") or {}).get("full_name") or "",
                        "operator": pr_operator(ctx.cfg, owner)})
    (ctx.state / "prs.json").parent.mkdir(parents=True, exist_ok=True)
    (ctx.state / "prs.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return out


def board_package_index(ctx: Context, ws: "Workspace") -> Path:
    """A file in the job's scratch directory listing every package under review on the board (name,
    board pull request, branch, head) with the command that fetches its README. The board checkout
    sits on ONE pull request's branch, forked from the board's main when that package was imported,
    so packages imported later are not under its `packages/`; a reviewer that looked only there
    reported a real package as existing nowhere (2026-10-23)."""
    path = ws.scratch / "board-packages-index.txt"
    repo = ctx.cfg.repositories.packages_repo()
    rows: list[str] = []
    try:
        pulls = ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True) if ctx.gh else []
    except GHError:
        pulls = None
    for pr in pulls or []:
        head = pr.get("head") or {}
        m = re.match(r"Import ([A-Za-z0-9]+)", pr.get("title") or "")
        name = m.group(1) if m else ""
        if name and (head.get("ref") or "").startswith("import/"):
            rows.append(f"{name}\t#{pr['number']}\t{head.get('ref', '')}\t{(head.get('sha') or '')[:7]}")
    lines = ["# Packages under review on the board: name, board pull request, branch, head.",
             "# Such a package's README lives at packages/<Name>/README.md on ITS pull request's head, not in the",
             "# board checkout of another pull request's branch. Read one with:",
             f"#   gh api repos/{repo}/contents/packages/<Name>/README.md?ref=<head> --jq .content | base64 -d",
             "# Before claiming that a roadmap or package exists nowhere, check this list, the merge order,",
             "# the roadmap repository's main and the open roadmap pull requests."]
    if pulls is None:
        lines.append("# (the board could not be read when this brief was written: check the board's open pull requests yourself)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines + sorted(rows)) + "\n", encoding="utf-8")
    return path


def cached_prs(state: Path) -> list[dict]:
    try:
        return json.loads((state / "prs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def rev_dir(state: Path, pr: int) -> Path:
    return state / "rev" / str(pr)


def sibling_checkouts(ctx: Context, prs: list[dict], skip: str = "") -> list[tuple[int, str, Path]]:
    out = []
    for p in prs:
        if p["name"] == skip:
            continue
        dest = rev_dir(ctx.state, p["pr"])
        try:
            gitops.checkout_pr(ctx.cfg.repositories.content, dest, p["pr"])
        except gitops.GitError:
            continue
        out.append((p["pr"], p["name"], dest / ctx.cfg.roadmap.active_dir / p["name"]))
    return out


def sibling_readmes(state: Path, active_dir: str, prs: list[dict] | None = None) -> dict[str, str]:
    out = {}
    for p in prs if prs is not None else cached_prs(state):
        path = rev_dir(state, p["pr"]) / active_dir / p["name"] / "README.md"
        if path.is_file():
            out[p["name"]] = path.read_text(encoding="utf-8", errors="replace")
    return out


def order_path(cfg: Config, ws_board: Path, ws_content: Path) -> Path:
    base = {"packages": ws_board, "content": ws_content, "local": cfg.state_dir()}[cfg.roadmap.merge_order_repo]
    return base / cfg.roadmap.merge_order_file


def load_order(path: Path) -> list[str]:
    try:
        return read_order(path.read_text(encoding="utf-8"))
    except OSError:
        return []


MAIN_ORDER_COPY = "merge-order-main.txt"
MOVED_ORDER_COPY = "merge-order-moved.txt"


def main_order_text(cfg: Config, ws: Workspace) -> str:
    """The merge-order file as the board's main has it ('' when it cannot be read)."""
    try:
        return gitops.git(ws.board, "show", f"refs/remotes/origin/main:{cfg.roadmap.merge_order_file}",
                          check=False) or ""
    except (gitops.GitError, OSError):
        return ""


def gate_order_file(cfg: Config, ws: Workspace, name: str) -> Path:
    """The merge-order file the package gate and the agents' dependency check read: the board
    checkout's, or, when the roadmap is missing there and the board's main lists it, a copy of main's
    in the job's scratch folder. The head of a board pull request opened before the roadmap entered
    the merge order (an explorer import) carries the older file. Once the worker has moved the
    roadmap after its last supplier in a copy (`repair_order_file`), that copy."""
    moved = ws.extra.get("order_file")
    if moved and Path(moved).is_file():
        return Path(moved)
    path = order_path(cfg, ws.board, ws.content)
    if cfg.roadmap.merge_order_repo != "packages" or name in load_order(path):
        return path
    text = main_order_text(cfg, ws)
    if name not in read_order(text):
        return path
    copy = ws.scratch / MAIN_ORDER_COPY
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(text, encoding="utf-8")
    return copy


def dropped_names(cfg: Config) -> list[str]:
    if not cfg.gates.dropped_names_file:
        return []
    try:
        return read_order((cfg.state_dir() / cfg.gates.dropped_names_file).read_text(encoding="utf-8"))
    except OSError:
        return []


UNSUBMITTED = "unsubmitted"
REPAIR_PASSES = 3       # repair runs after the checks, while the number of problems strictly falls
MAX_REQUESTS = 3        # needs-references requests one job may post before it is blocked for a human
UNREADABLE_NOTE = ("the text was fetched, but it is a scan this worker could not read: a worker with OCR "
                   "([sources] ocr = true and the `ocr` extra) reads it; or name another source, a local text, or "
                   "waive it")
# Machine paths in the files that hand work on (reports, the triage, verdicts, the manifest) are
# rewritten before the gate; README and Lean content is never rewritten (a path there is a defect).
SCRUBBED = ("target.md", "review.json", "sources.toml")
TEMP_ROOTS = re.compile(board.TEMP_ROOT)


def scrub_paths(text: str, state: Path, home: Path) -> str:
    """Machine paths out of a hand-off: the project state directory becomes `<state>`, the home
    directory `~`, and a temporary path `<scratch>/`."""
    def prefix(root: str, label: str, text: str) -> str:
        root = root.rstrip("/\\")
        return re.sub(re.escape(root) + r"(?![\w.-])", lambda m: label, text) if len(root) > 1 else text
    for root in sorted({str(state), str(Path(state).resolve())}, key=len, reverse=True):
        text = prefix(root, "<state>", text)
    for root in sorted({str(home), str(Path(home).resolve())}, key=len, reverse=True):
        text = prefix(root, "~", text)
    return TEMP_ROOTS.sub("<scratch>/", text)


def set_aside(checkout: Path, jobs: Path, worker: str, base: str = "main") -> Path | None:
    """Copy what the last round left in the board checkout beyond the board's main (uncommitted
    files, and files a submission committed but may not have pushed) to `jobs/<JOB>/unsubmitted/`
    of the job whose branch (`<worker>/<JOB>`) is checked out, before the checkout is reset. A round
    that left nothing keeps the copy of an earlier one. Returns the folder when this round left
    files, else None. Best effort: a checkout git cannot read is reset all the same."""
    try:
        branch = gitops.current_branch(checkout)
        if not branch.startswith(worker + "/"):
            return None
        try:
            changed = gitops.changed_files(checkout, f"refs/remotes/origin/{base}")
        except gitops.GitError:
            changed = gitops.changed_files(checkout)
        files = [p for p in changed if (checkout / p).is_file()]
    except gitops.GitError:
        return None
    if not files:
        return None
    dest = jobs / branch.split("/", 1)[1] / UNSUBMITTED
    shutil.rmtree(dest, ignore_errors=True)
    for rel in files:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checkout / rel, dest / rel)
    return dest


def roadmap_readmes(ctx: Context, ws: Workspace) -> dict[str, str]:
    """The READMEs the merge-order placement reads for consumers: the roadmaps on the content
    repository's main and in its open roadmap pull requests."""
    active = ws.content / ctx.cfg.roadmap.active_dir
    readmes = {p.parent.name: p.read_text(encoding="utf-8", errors="replace")
               for p in active.glob("*/README.md")} if active.is_dir() else {}
    readmes.update(sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir, ws.extra.get("prs")))
    return readmes


def move_in_order(ctx: Context, ws: Workspace, name: str, readme: str, lean: str, text: str) -> tuple[str, list[str]]:
    """The repair of an UPWARD edge of the roadmap being submitted: the merge-order file `text`, which
    lists `name`, with `name` moved right after its last supplier (its README's, as the dependency check
    reads them, and every `TauCetiRoadmap.<X>` its Lean names) when one of them comes after it, and the
    notes. A move that would put one of its consumers before it is not made: `text` comes back as it is,
    with a note saying why, and the dependency check reports the UPWARD edge as before."""
    order = read_order(text)
    pos = {n: i for i, n in enumerate(order)}
    suppliers = depgraph.package_suppliers(name, readme, lean, set(order))
    if name not in pos or not any(pos[s] > pos[name] for s in suppliers if s in pos):
        return text, []                 # nothing to move: the other READMEs are not read
    readmes = roadmap_readmes(ctx, ws)
    edges = depgraph.supplier_edges({**readmes, name: readme}, set(order) | set(readmes) | {name})
    consumers = {c for c, sups in edges.items() if name in sups and c != name}
    try:
        new, note = depgraph.move_after_suppliers(text, name, suppliers, consumers)
    except depgraph.OrderCycle as exc:
        return text, [f"merge order: {name} not moved after its last supplier: {exc}"]
    return new, [note] if note else []


def note_line(ws: Workspace, line: str) -> None:
    """A line for the round's log (printed, kept in the job folder's requests.log and in the result)."""
    print(f"{ws.job_dir.name}: {line}")
    ws.extra.setdefault("notes", []).append(line)
    try:
        with open(ws.job_dir / "requests.log", "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ} {line}\n")
    except OSError:
        pass


def repair_order_file(ctx: Context, ws: Workspace, name: str, readme: str, lean: str, path: Path,
                      carry: bool = False) -> Path:
    """The merge-order file a gate checks the roadmap against, once its UPWARD edges are repaired
    (`move_in_order`; each move or refusal logged once). `carry`: `path` is the board checkout's file
    and the job's submission carries it to the board's main (the port, a red-team fix of a roadmap pull
    request), so it is edited in place; else the moved order is written to a copy in the job's scratch
    folder, which the gate and the agents' dependency check read from then on, and the intake moves
    the roadmap on the board's main (`board intake`, and the merge of its board pull request)."""
    if ctx.cfg.roadmap.merge_order_repo != "packages" or not path.is_file():
        return path
    text = path.read_text(encoding="utf-8")
    if name not in read_order(text):
        return path
    new, notes = move_in_order(ctx, ws, name, readme, lean, text)
    for line in notes:
        if line not in ws.extra.get("notes", []):
            note_line(ws, line + ("" if new == text or carry else
                                  " (in the copy the gate reads; the intake moves it on the board's main)"))
    if new == text:
        return path
    if carry:
        path.write_text(new, encoding="utf-8")
        return path
    copy = ws.scratch / MOVED_ORDER_COPY
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(new, encoding="utf-8")
    ws.extra["order_file"] = str(copy)
    return copy


def ensure_in_order(ctx: Context, ws: Workspace, name: str, readme: str, lean: str) -> list[str]:
    """Place the roadmap in the board's merge-order file (in the board checkout, so the submission
    carries the change) right after its last supplier, when it is not listed yet; returns notes.
    A placement that would put a supplier after a consumer raises LaneBlocked. A roadmap listed above
    one of its suppliers is moved right after the last of them (`move_in_order`): in the board
    checkout's file, or, on a board pull request whose roadmap the board's main lists, in a copy of
    main's that the gate reads (the intake moves it on main)."""
    path = order_path(ctx.cfg, ws.board, ws.content)
    if ctx.cfg.roadmap.merge_order_repo != "packages" or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    order = read_order(text)
    if ws.extra.get("board_pr"):
        main = main_order_text(ctx.cfg, ws)
        if name in read_order(main):
            # The head of a board pull request older than the roadmap's insertion on main: placing (or
            # moving) it on the branch as well would list it twice, or conflict, when the pull request merges.
            new, notes = move_in_order(ctx, ws, name, readme, lean, main)
            if new != main:
                copy = ws.scratch / MOVED_ORDER_COPY
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_text(new, encoding="utf-8")
                ws.extra["order_file"] = str(copy)
                notes = [n + " (in the copy the gate reads; the intake moves it on the board's main)" for n in notes]
            return ([] if name in order else [f"{name} is in the merge order on the board's main"]) + notes
    if name in order:
        new, notes = move_in_order(ctx, ws, name, readme, lean, text)
        if new != text:
            path.write_text(new, encoding="utf-8")
        return notes
    readmes = roadmap_readmes(ctx, ws)
    known = set(order) | set(readmes)
    suppliers = depgraph.package_suppliers(name, readme, lean, known)
    edges = depgraph.supplier_edges({**readmes, name: readme}, known | {name})
    consumers = {c for c, sups in edges.items() if name in sups and c != name}
    try:
        new, notes = depgraph.place_in_order(text, name, suppliers, consumers)
    except depgraph.OrderCycle as exc:
        raise LaneBlocked(f"merge order: {exc}") from exc
    path.write_text(new, encoding="utf-8")
    placed = read_order(new)
    at = placed.index(name)
    where = f"after {placed[at - 1]}" if at else "first"
    return [f"{name} placed in the merge order {where}"] + notes


def ensure_in_order_logged(ctx: Context, ws: Workspace, name: str, readme: str, lean: str) -> list[str]:
    """`ensure_in_order`, its notes (a placement, a move, why a move was not made) in the round's log."""
    notes = ensure_in_order(ctx, ws, name, readme, lean)
    for line in notes:
        note_line(ws, line)
    return notes


def checkout_board_pr(ctx: Context, ws: Workspace, number: int) -> None:
    """Check the head of the board pull request a job works on out in the board checkout, on the
    job's branch, and note where its branch lives (only its owner pushes to it)."""
    pr = {}
    if ctx.gh is not None:
        try:
            pr = ctx.gh.api(f"repos/{ctx.cfg.repositories.packages_repo()}/pulls/{number}") or {}
        except GHError as exc:
            raise LaneError(f"board pull request #{number} cannot be read ({exc})") from exc
        if not ((pr.get("head") or {}).get("ref") and ((pr.get("head") or {}).get("repo") or {}).get("full_name")):
            raise LaneError(f"board pull request #{number}: its branch is unknown (deleted fork?)")
        if pr.get("state") != "open":
            raise LaneError(f"board pull request #{number} is {pr.get('state') or 'not open'}")
    gitops.git(ws.board, "fetch", "-q", "origin", f"pull/{number}/head")
    gitops.git(ws.board, "checkout", "-q", "--force", "-B", ws.branch, "FETCH_HEAD")
    head = pr.get("head") or {}
    ws.extra.update(board_pr=number, head=gitops.git(ws.board, "rev-parse", "HEAD").strip(),
                    pr_ref=head.get("ref", ""), pr_repo=(head.get("repo") or {}).get("full_name", ""),
                    pr_owner=(pr.get("user") or {}).get("login", ""))


def board_reports(ctx: Context, ws: Workspace, name: str, job_ids: list[str], folder: str = "reviews") -> list[Path]:
    """The reports the intake copied to the board's main for these jobs (reviews and passes on the
    pull request), fetched into the scratch folder for the agent."""
    out = []
    pkg = board.package_dir(ctx.cfg.board.packages_dir, name)
    for jid in job_ids:
        path = ws.scratch / folder / f"{claims.ref_safe(jid)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A fix commits its report on the pull request's branch; reviews and passes are copied to main.
        local = ws.board / pkg / "reports" / f"{jid}.md"
        from_branch = jid.startswith("FIX-") and local.is_file()
        try:
            text = local.read_text(encoding="utf-8") if from_branch else \
                (ctx.gh.raw_file(ctx.cfg.repositories.packages_repo(), f"{pkg}/reports/{jid}.md") if ctx.gh else "")
        except GHError:
            text = ""
        path.write_text(text or f"(no report of {jid} on the board yet)\n", encoding="utf-8")
        out.append(path)
    return out


def library_description(content: Path) -> str:
    m, t = content / ".lake" / "packages" / "mathlib" / "Mathlib", content / ".lake" / "packages" / "TauCeti" / "TauCeti"
    if m.is_dir() and t.is_dir():
        return f"Mathlib at `{m}` and Tau Ceti at `{t}` (the pins of `{content}/lake-manifest.json`)"
    return (f"Mathlib and Tau Ceti on GitHub at the commits pinned in `{content}/lake-manifest.json` (search with "
            "Loogle, LeanSearch, the documentation or GitHub code search, then read the file at the pinned commit)")


# --- the lane -------------------------------------------------------------------------------------

class Lane:
    kind = ""
    prompt = ""
    resumes_checkpoint = False  # carries on from what its last run left unsubmitted (no lane does yet)
    manifest_add_only = False   # its fetches add to the manifest, never rewrite an entry (reviews)

    # -- hooks --
    def eligible(self, job: Job, ctx: Context) -> str:
        """'' when this worker may take the job, else the reason it may not."""
        if job.requires_agent and job.requires_agent != ctx.agent:
            return f"needs agent {job.requires_agent}"
        if job.requires_model and job.requires_model != ctx.model:
            return f"needs model {job.requires_model}"
        if job.requires_operator and job.requires_operator.lower() != ctx.operator.lower():
            pool = {p.lower() for p in ctx.cfg.roadmap.pool}
            if not (job.requires_operator.lower() in pool and ctx.operator.lower() in pool):
                return f"needs the branch owner {job.requires_operator}"
        if job.independent_of and ctx.claims:
            problem = claims.separation_problem(job.independent_of, ctx.operator,
                                                ctx.claims.recorded_authors(job.independent_of),
                                                ctx.cfg.review.separation, ctx.agent, ctx.model)
            if problem:
                return problem
        if builds(job):
            free = free_gb(ctx.state)
            if free is not None and free < MIN_FREE_GB_BUILD:
                return (f"only {free:.0f} GB free on the state directory's disk; a lane that builds the roadmap "
                        f"needs {MIN_FREE_GB_BUILD} GB")
        # A `local:` answer: the text never travels, so only an operator who holds it resumes the job.
        index = sources.cache_root(ctx.state) / "local-index.json"
        for need in references.local_needs(job):
            if not fetcher.local_source(ctx.cfg.sources.local_dirs, need["name"], need.get("sha256", ""), index):
                return f"needs local source {need['name']}"
        return ""

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        return {}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        return [] if ws.report.is_file() else [f"no report at {ws.report}"]

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """After the checks passed: the lane's last steps, and the one-line summary of what the
        round did (the round log's `submitted:` line, the commit subject and the submission's
        description); '' falls back to `<kind> of <Name>`."""
        return ""

    def records_author(self, job: Job) -> bool:
        return bool(board.artefact_of(job.id)) and job.kind != "review"

    @staticmethod
    def board_pr(job: Job) -> int:
        """The board pull request the job works on (reviews, red-team passes and fixes in the
        comment flow), or 0. A design or package job itself always opens its own pull request."""
        if job.kind not in ("review", "redteam", "fix"):
            return 0
        try:
            return int(job.inputs.get("board_pr") or 0)
        except (TypeError, ValueError):
            return 0

    def edits_manifest(self, job: Job, ctx: Context) -> bool:
        """Whether the job's deliverables include the roadmap's sources.toml (the board decides)."""
        return board.manifest_path(ctx.cfg.board.packages_dir, job.name) in board.allowed_paths(
            job, ctx.cfg.board.packages_dir)

    def elaborates(self, job: Job) -> bool:
        """Whether the lane (its agent, its checks) elaborates the package's Suggested.lean in the
        content checkout: the package lane, package reviews and the fixes of a package."""
        return False

    # -- shared steps --
    def prepare(self, job: Job, ctx: Context) -> Workspace:
        """Every round starts from clean checkouts of the board's and the content repository's
        main. What the last round left unsubmitted in the board checkout is copied to its job's
        folder first (`unsubmitted/`) and discarded: the board's copy wins. Only a lane that
        resumes a checkpoint gets it back, and only into the same job's package folder."""
        cfg = ctx.cfg
        worker = sanitize_worker_id(ctx.worker_id)
        job_dir = ctx.wdir / "jobs" / claims.ref_safe(job.id)
        scratch = job_dir / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        board_dir, content_dir = ctx.wdir / "board", ctx.wdir / "content"
        pkg = board.package_dir(cfg.board.packages_dir, job.name)
        kept = set_aside(board_dir, ctx.wdir / "jobs", worker)
        gitops.sync_checkout(cfg.repositories.packages_repo(), board_dir, "main", scrub=(pkg,))
        gitops.sync_checkout(cfg.repositories.content, content_dir, cfg.project.branch)
        for checkout in (board_dir, content_dir):      # worktrees of the content checkout share its identity
            gitops.set_identity(checkout, ctx.operator)
        shared = os.environ.get("TAUCETI_ROADMAP_LAKE_PACKAGES")
        if shared and Path(shared).is_dir() and not (content_dir / ".lake" / "packages").exists():
            # One built Mathlib/Tau Ceti for every worker of the host (never build it per worker).
            (content_dir / ".lake").mkdir(parents=True, exist_ok=True)
            os.symlink(Path(shared).resolve(), content_dir / ".lake" / "packages")
        branch = f"{worker}/{claims.ref_safe(job.id)}"
        gitops.git(board_dir, "checkout", "-q", "-B", branch)
        if self.resumes_checkpoint and kept == job_dir / UNSUBMITTED and (kept / pkg).is_dir():
            shutil.copytree(kept / pkg, board_dir / pkg, dirs_exist_ok=True)
        report = board_dir / pkg / "reports" / f"{job.id}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        ws = Workspace(job_dir=job_dir, scratch=scratch, board=board_dir, content=content_dir,
                       workdir=board_dir, report=report, branch=branch, job_id=job.id)
        if (cfg.gates.elaborate or cfg.gates.build) and self.elaborates(job):
            # The content checkout elaborates with the machine's shared Lake packages for its pin (fetched
            # once per pin, under the lock and the disk guard), exactly as a port worktree builds with them.
            ws.extra["lake"] = prepare_lake(content_dir, ctx.state, fetch=not ctx.dry_run, free=free_gb)
        return ws

    def common_values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        cfg = ctx.cfg
        order_file = gate_order_file(cfg, ws, job.name)
        order = load_order(order_file)
        prs = ws.extra.get("prs") or cached_prs(ctx.state)
        sibs = ws.extra.get("siblings") or []
        earlier = order[:order.index(job.name)] if job.name in order else order
        dropped = dropped_names(cfg)
        extra_rules = ""
        if cfg.prompts.extra_rules_file:
            path = ws.board / cfg.prompts.extra_rules_file
            if path.is_file():
                extra_rules = "## Project rules\n\n" + path.read_text(encoding="utf-8").strip()
        return {
            "JOB": job.id, "ROADMAP": job.name, "ROUND": str(job.round), "WORKDIR": str(ws.workdir),
            "SCRATCH": str(ws.scratch), "REPORT": str(ws.report), "CONTENT_REPO": cfg.repositories.content,
            "CONTENT": str(ws.content), "ACTIVE_DIR": cfg.roadmap.active_dir, "COMPLETED_DIR": cfg.roadmap.completed_dir,
            "PACKAGES": str(ws.board / cfg.board.packages_dir), "PACKAGE": str(ws.package(cfg, job.name)),
            "PACKAGE_INDEX": str(board_package_index(ctx, ws)),
            "ORDER": str(order_file), "SUPPLIERS": ", ".join(earlier) or "none yet",
            "DROPPED": ", ".join(dropped) or "none",
            "DEPCHECK": self.depcheck_command(job, ctx, ws, order_file),
            "GAPS": declared_gaps(ws.package(cfg, job.name)),
            "OCR": ocr_line(cfg),
            "LIBRARY": library_description(ws.content),
            "SIBLINGS": "; ".join(f"{name} at `{path}`" for _pr, name, path in sibs) or
                        ("; ".join(p["name"] for p in prs) or "none"),
            "MAX_LEAN_LINES": str(cfg.gates.max_lean_lines), "ELABORATE": elaborate_line(cfg, ws),
            "EXTRA_RULES": extra_rules,
            "SOURCE_TEXTS": ws.extra.get("source_texts", "none recorded yet"),
            "FETCH": self.fetch_command(job, ctx, ws),
            "TOOLS": self.tools_text(ctx, ws),
        }

    transcribes_if_ready = False    # a lane that does not elaborate still gets the signature transcript

    def citation_target(self, job: Job, ctx: Context, ws: Workspace) -> Path | None:
        """The README whose citations the worker resolves before the agent runs: the one the lane
        works on (`readme`), else the package's, else the design's; None when there is none yet."""
        pkg = ws.package(ctx.cfg, job.name)
        for path in (ws.extra.get("readme"), pkg / "README.md", pkg / "design" / "README.md"):
            if path and Path(path).is_file():
                return Path(path)
        return None

    def prepare_tools(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """The index of open roadmap pull requests (refreshed at most every half hour, shared by the
        host's workers), and the citations of the roadmap's README that are found nowhere, written to
        `scratch/citations.md` for the agent. Best effort: a failure is a line in the round log and in
        the brief, never a failed round."""
        index: dict = {}
        try:
            index = (openroadmaps.refresh(ctx.gh, ctx.cfg) if ctx.gh is not None and not ctx.dry_run
                     else openroadmaps.load(ctx.state))
        except Exception as exc:  # noqa: BLE001 - a tool for the agent: its failure must never fail the round
            index = openroadmaps.load(ctx.state)
            print(f"{ws.job_dir.name}: the index of open roadmap pull requests was not refreshed "
                  f"({type(exc).__name__}: {str(exc)[:160]})" + ("; using the last one" if index else ""))
        ws.extra["open_index"] = index
        readme = self.citation_target(job, ctx, ws)
        if not index or readme is None:
            return
        lean = readme.parent / "Suggested.lean"
        try:
            citations, names, why = openroadmaps.resolve(
                ctx.state, index, readme.read_text(encoding="utf-8"), own=job.name,
                lean=lean.read_text(encoding="utf-8") if lean.is_file() else "",
                packages=openroadmaps.packages_dir(ws.content))
        except Exception as exc:  # noqa: BLE001 - as above
            print(f"{ws.job_dir.name}: the citations were not resolved ({type(exc).__name__}: {str(exc)[:160]})")
            return
        path = ws.scratch / "citations.md"
        rel = readme.relative_to(ws.board) if readme.is_relative_to(ws.board) else readme.name
        path.write_text(openroadmaps.report(citations, names, why, index, str(rel)), encoding="utf-8")
        ws.extra["citations"] = (path, len(citations) + len(names))

    def prepare_signatures(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """The elaborated type of every declaration of the roadmap's Suggested.lean, with the section
        binders it looks to have lost, written to `scratch/signatures.md` (lanes that elaborate, with
        the shared Lake packages ready). Best effort, like the other tools."""
        setup, readme = ws.extra.get("lake"), self.citation_target(job, ctx, ws)
        lean = readme.parent / "Suggested.lean" if readme is not None else None
        if ctx.dry_run or not ctx.cfg.gates.elaborate or not (lean and lean.is_file()):
            return
        if setup is None and self.transcribes_if_ready:
            # A lane that does not elaborate (a red-team pass) uses the pin's packages only when they are
            # already fetched on this host: it never starts a fetch for a transcript.
            setup = prepare_lake(ws.content, ctx.state, fetch=False, free=free_gb)
        if setup is None or not setup.ready:
            return
        # The project's forbidden patterns are read before anything is elaborated (`signatures.elaborate`
        # itself refuses a file that runs code at elaboration).
        forbidden = gates.scan_forbidden(lean.read_text(encoding="utf-8", errors="replace"),
                                         list(getattr(ctx.cfg.gates, "forbidden_patterns", []) or []))
        if forbidden:
            found = signatures.Transcript([], [], f"the file matches the project's forbidden patterns (line "
                                                  f"{forbidden[0].line}), so the worker does not elaborate it")
            path = ws.scratch / "signatures.md"
            path.write_text(signatures.report(found, "Suggested.lean"), encoding="utf-8")
            ws.extra["signatures"] = (path, 0, 0, found.reason)
            return
        try:
            found = signatures.elaborate(ctx.cfg.gates.elaborate, lean, ws.scratch / "signatures", ws.content)
        except Exception as exc:  # noqa: BLE001 - a tool for the agent: its failure must never fail the round
            print(f"{ws.job_dir.name}: the signatures were not elaborated ({type(exc).__name__}: {str(exc)[:160]})")
            return
        path = ws.scratch / "signatures.md"
        path.write_text(signatures.report(found, "Suggested.lean"), encoding="utf-8")
        ws.extra["signatures"] = (path, len(found.entries), found.flagged, found.reason)

    def tools_text(self, ctx: Context, ws: Workspace) -> str:
        """The brief's tools section: the open roadmap pull requests, the citation resolver and the
        elaborated signatures, each listed when the worker could prepare it."""
        index = ws.extra.get("open_index") or {}
        lines = []
        if not index:
            lines.append("- The index of open roadmap pull requests is not available this round: use the sibling list "
                         "above, and say in your report that ownership against other open pull requests was not "
                         "checked.")
        else:
            prs = index.get("prs", [])
            new = sum(len(p.get("adds", [])) for p in prs)
            lines.append(
                f"- **Open roadmap pull requests** ({len(prs)}, {new} of them new roadmaps; "
                f"index of {index.get('at')}): "
                f"`{ctx.cli('roadmaps', 'list')}` lists them; `{ctx.cli('roadmaps', 'grep', '<regex>')}` searches the "
                "README and Suggested.lean of the roadmap folders they carry, at their heads, then every roadmap on "
                "main (`--fixed`, `-i`, `--readme-only`, `--lean-only`); "
                f"`{ctx.cli('roadmaps', 'show', '<Name>')}` prints one (`--file Suggested.lean`, `--pr N`). Use them "
                "for every ownership and duplication check, and to read a roadmap that is still a pull request.")
            citation = (f"- **Citations**: `{ctx.cli('roadmaps', 'resolve', '<README.md>')}` lists the cross-roadmap "
                        "citations and backticked Lean names of a README that are found nowhere (on main, at the open "
                        "heads, in the pinned sources). They are leads, not verdicts: check each before you rely on it "
                        "or report it.")
            found = ws.extra.get("citations")
            if found:
                citation += (f" For this roadmap's README the worker found {found[1]} (`{found[0]}`)." if found[1]
                             else " For this roadmap's README the worker found none.")
            lines.append(citation)
        sigs = ws.extra.get("signatures")
        if sigs:
            path, total, flagged, reason = sigs
            lines.append(f"- **Elaborated signatures**: `{path}` " + (f"(not available: {reason})." if reason else
                         f"has the elaborated type of each of the {total} declarations of Suggested.lean as it stands "
                         f"now; {flagged} of them look to have lost a section binder (a hypothesis about objects they "
                         "use). Compare each with its README statement; after you change the file, check again with "
                         "`#check @name`."))
        return "\n".join(lines)

    def depcheck_command(self, job: Job, ctx: Context, ws: Workspace, order_file: Path) -> str:
        """The dependency check an agent runs. A roadmap at the design stage (its target, design,
        design review or design fix) is a candidate: it enters the merge order when its design is
        accepted, so until then the check places it after its last supplier."""
        readme = str(ws.extra.get("readme") or ws.package(ctx.cfg, job.name) / "README.md")
        if job.kind in ("target", "design") or board.artefact_of(job.id) == "DESIGN":
            return ctx.cli("gate", "--candidate", readme, "--only", job.name, "--order", str(order_file))
        return ctx.cli("gate", "--depcheck", readme, "--name", job.name, "--order", str(order_file), "--movable")

    def fetch_command(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """The command line an agent runs to fetch a source through the service."""
        args = ["sources", "fetch"]
        if self.edits_manifest(job, ctx):
            args += ["--manifest", str(ws.package(ctx.cfg, job.name) / "sources.toml")]
            if self.manifest_add_only:
                args.append("--add-only")
        return ctx.cli(*args, "--missing-out", str(ws.scratch / "needs-references.toml"))

    def manifest_file(self, job: Job, ctx: Context, ws: Workspace) -> Path:
        """The roadmap's sources manifest the run reads: its package's on the board."""
        return ws.package(ctx.cfg, job.name) / "sources.toml"

    def prepare_sources(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """Bring every text the roadmap's manifest records into the sources cache, list them for the
        brief, and turn texts that are gone for good into a needs-references request. A request left
        by an earlier run of this job is archived first: its answers are in the manifest now."""
        request = ws.scratch / "needs-references.toml"
        if request.is_file():
            request.replace(ws.scratch / "needs-references.previous.toml")
        manifest_path = self.manifest_file(job, ctx, ws)
        try:
            own = tomllib.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
        except tomllib.TOMLDecodeError as exc:
            ws.extra["source_texts"] = f"none: the manifest does not parse ({exc})"
            return
        manifest = self.with_answers(job, ctx, ws, own)
        if manifest is None:
            return
        available = ctx.sources_fetcher().materialize(manifest, offline=ctx.dry_run)
        if own is not None:
            self.record_local_hashes(job, ctx, manifest_path, own, available)
        lines = []
        for a in available:
            if a.text:
                lines.append(f"- `{a.id}` ({a.label}): `{a.text}`")
            else:
                lines.append(f"- `{a.id}` ({a.label}): unavailable: {a.problem}")
        for s in manifest.get("source", []):
            status = references.settled_status(s, ctx.cfg.sources.uncertified_ok) if isinstance(s, dict) else ""
            if status and status != "local":
                label = f"{s.get('authors', '')}, {s.get('title', '')}, {s.get('year', '')}".strip(", ")
                lines.append(f"- `{s.get('id', '?')}` ({label}): settled as {status}: cite it by its locator under "
                             "this key; never request it again")
        ws.extra["source_texts"] = "\n" + "\n".join(lines) if lines else "none recorded yet"
        for a in available:
            if a.text or not a.permanent:
                continue
            entry = sources.find_source(manifest, id=a.id) or {}
            item = references.MissingReference(
                authors=str(entry.get("authors", "")), title=str(entry.get("title", a.id)), year=str(entry.get("year", "")),
                needed_for=", ".join(entry.get("cited_for", [])) or f"the roadmap's citations of {a.id}",
                tried=[a.problem], key=a.id, doi=str(entry.get("doi", "")), url=str(entry.get("url", "")),
                locator_scheme=str(entry.get("locator_scheme", "")), sha256=str(entry.get("sha256", "")),
                note=UNREADABLE_NOTE if a.problem == sources.UNREADABLE else "")
            references.add_missing(request, item)
            ws.extra.setdefault("auto_missing", []).append(item)

    def with_answers(self, job: Job, ctx: Context, ws: Workspace, own: dict | None) -> dict | None:
        """The manifest the run reads: the job's own (on a board pull request, at its head) with the
        answers the intake recorded on the board's main (entries with a `resolution`) in place of its
        entries for the same keys, so a `local:`, `source:` or `waive:` answer reaches a job whose
        package lives on a pull request. None when there is neither."""
        rel = board.manifest_path(ctx.cfg.board.packages_dir, job.name)
        answered = []
        try:
            text = gitops.git(ws.board, "show", f"refs/remotes/origin/main:{rel}", check=False)
            answered = [e for e in (tomllib.loads(text).get("source", []) if text.strip() else [])
                        if isinstance(e, dict) and e.get("resolution")]
        except (gitops.GitError, OSError, tomllib.TOMLDecodeError):
            pass
        if own is None and not answered:
            return None
        merged = copy.deepcopy(own) if own is not None else sources.new_manifest(job.name)
        merged.setdefault("source", [])
        for entry in answered:
            mine = sources.find_source(merged, id=str(entry.get("id", "")))
            if mine is None:
                merged["source"].append(dict(entry))
            elif mine != entry:
                mine.clear()
                mine.update(entry)
        return merged

    def record_local_hashes(self, job: Job, ctx: Context, path: Path, manifest: dict, available: list) -> None:
        """A `local` text read here gets its hash recorded, as `certified`, in the manifest this job
        submits (a review leaves the author's entries as they are)."""
        if not self.edits_manifest(job, ctx) or self.manifest_add_only:
            return
        found, changed = {a.id: a.sha256 for a in available if a.sha256}, False
        for entry in manifest.get("source", []):
            sha = found.get(str(entry.get("id", "")))
            if sha and entry.get("status") == "local":
                entry.update(status="certified", sha256=sha, accessed=dt.date.today().isoformat())
                changed = True
        if changed:
            sources.save_manifest(path, manifest)

    def scrub_outputs(self, job: Job, ctx: Context, ws: Workspace) -> list[Path]:
        """Rewrite machine paths in the job's hand-off files (its report, the triage, verdicts, the
        manifest) before any check reads them; returns the files changed. The privacy gate stays as
        the backstop, and README and Lean content is never rewritten."""
        pkg = ws.package(ctx.cfg, job.name)
        files = [ws.report] + [p for name in SCRUBBED for p in pkg.rglob(name) if "reports" not in p.parts]
        changed = []
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            clean = scrub_paths(text, ctx.state, Path.home())
            if clean != text:
                path.write_text(clean, encoding="utf-8")
                changed.append(path)
        return changed

    def manifests(self, job: Job, ctx: Context, ws: Workspace) -> list[dict]:
        """The roadmap's sources manifest as the job sees it, and as the board's main records it
        (where the intake writes the answers to requests)."""
        rel = board.manifest_path(ctx.cfg.board.packages_dir, job.name)
        texts = [(ws.board / rel).read_text(encoding="utf-8") if (ws.board / rel).is_file() else ""]
        try:
            texts.append(gitops.git(ws.board, "show", f"refs/remotes/origin/main:{rel}", check=False))
        except (gitops.GitError, OSError):
            pass
        out = []
        for text in texts:
            try:
                out.append(tomllib.loads(text) if text.strip() else {})
            except tomllib.TOMLDecodeError:
                continue
        return out

    def missing_references(self, job: Job, ctx: Context, ws: Workspace) -> list[references.MissingReference]:
        """The request this run ends with: the agent's items minus every citation the manifest has
        settled (waived, or uncertified where the project accepts it), and, when there is still a
        request, completed by every manifest text the worker found unreadable here, so one request
        lists them all."""
        items = references.read_missing(ws.scratch / "needs-references.toml")
        manifests = self.manifests(job, ctx, ws)
        kept, dropped = references.drop_settled(items, manifests, ctx.cfg.sources.uncertified_ok)
        if dropped:
            self.note(ws, "dropped from the request, settled in the manifest" + references.settle_line(dropped))
        if kept:
            have = {m.key.lower() for m in kept}
            extra = [m for m in ws.extra.get("auto_missing", []) if m.key.lower() not in have]
            extra, _ = references.drop_settled(extra, manifests, ctx.cfg.sources.uncertified_ok)
            if extra:
                self.note(ws, "added to the request, unreadable here: " + ", ".join(m.key for m in extra))
                kept += extra
        if kept:
            # A text a human already answered is asked for again at most once more: the next request
            # for it (under whatever key) is settled with the earlier answer instead of being posted.
            kept, capped = references.drop_capped(kept, manifests, self.earlier_requests(job, ctx))
            if capped:
                self.note(ws, "not requested again, answered in the manifest" + references.settle_line(capped)
                          + "; settled with the earlier answer")
        return kept

    def earlier_requests(self, job: Job, ctx: Context) -> list[list[references.MissingReference]]:
        """The items of the needs-references requests this job posted on its issue (none when the
        issue cannot be read)."""
        try:
            return references.earlier_requests(ctx.board.comments(job.number), job.id,
                                               ctx.cfg.sources.request_associations,
                                               ctx.cfg.board.bot_logins + [ctx.operator])
        except (GHError, AttributeError):
            return []

    def note(self, ws: Workspace, line: str) -> None:
        """A line for the round's log (printed, kept in the job folder's requests.log and in the result)."""
        note_line(ws, line)

    def briefs(self, job: Job, ctx: Context, ws: Workspace, values: dict) -> list[tuple[str, str]]:
        """(label, brief) for each agent run of the job, in order; '' labels the main brief."""
        return [("", prompts.render(self.prompt, values))]

    def after_runs(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """Combine the outputs of several runs (the red-team lane merges its section passes)."""

    def run_agent(self, ctx: Context, ws: Workspace, brief: str, label: str = "") -> agents.AgentResult:
        attempt = len(list(ws.job_dir.glob("run-*.out"))) + 1
        stem = f"run-{attempt}" + (f"-{label}" if label else "")
        result = agents.run_agent(agents.AgentRun(
            agent=ctx.agent, model=ctx.model, workdir=ws.workdir, brief=brief,
            log_out=ws.job_dir / f"{stem}.out", log_err=ws.job_dir / f"{stem}.err",
            timeout=ctx.cfg.agents.timeout_minutes * 60, sandbox_profile=ctx.cfg.sandbox.profile,
            proxy_url=ctx.cfg.sandbox.proxy_url, netjail=ctx.cfg.sandbox.netjail,
            claude_hook=ctx.cfg.sandbox.claude_hook, settings_dir=ctx.wdir / "settings"))
        usage.record(ctx.wdir, ws.job_dir, usage.entry(
            job=ws.job_id or ws.job_dir.name, kind=self.kind, lane=type(self).__name__, run=stem, label=label,
            agent=ctx.agent, model=ctx.model, worker=ctx.worker_id, result=result, brief_chars=len(brief)))
        return result

    def park(self, job: Job, ctx: Context, missing: list[references.MissingReference]) -> LaneResult:
        """Post the request (from the template, with its machine-readable marker, machine paths
        rewritten: the comment is public) and park the job. One request per run; a job that has
        already asked MAX_REQUESTS times is blocked for a human instead."""
        keys = ", ".join(m.key for m in missing)
        if self.parked:
            return LaneResult(job.id, "failed", f"a second needs-references request in one run is refused ({keys})")
        self.parked = True
        try:
            asked = references.request_count(ctx.board.comments(job.number), job.id, ctx.cfg.sources.request_associations,
                                             ctx.cfg.board.bot_logins + [ctx.operator])
        except (GHError, AttributeError):     # an issue that cannot be read: count none
            asked = 0
        if asked >= MAX_REQUESTS:
            return LaneResult(job.id, "blocked", f"this job has asked for references {asked} times; instead of a "
                              f"further request a human decides on {keys} (answer, waive, or narrow the job).")

        def clean(value):
            if isinstance(value, list):
                return [clean(v) for v in value]
            return scrub_paths(value, ctx.state, Path.home()) if isinstance(value, str) else value
        missing = [dataclasses.replace(m, **{k: clean(v) for k, v in dataclasses.asdict(m).items()}) for m in missing]
        ctx.board.comment(job.number, references.request_comment(job.id, job.name, missing))
        try:
            ctx.board.set_state(job, "needs-references")
        except GHError:
            pass  # operators without write access on the board: `board sync` mirrors the state
        return LaneResult(job.id, "needs-references", f"{len(missing)} source(s) missing: "
                          + ", ".join(m.key for m in missing))

    def validate(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        """Everything that would stop the submission: the lane's checks, then the board's own rules
        on the files it would submit (deliverables only, no local path, valid data, no source
        material)."""
        return self.check(job, ctx, ws) + self.submission_problems(job, ctx, ws)

    def submission_problems(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        allowed = board.allowed_paths(job, ctx.cfg.board.packages_dir, board.order_file(ctx.cfg))
        changed = gitops.changed_files(ws.board)
        outside = [p for p in changed if p not in allowed]
        if outside:
            return ["the agent changed files outside the job's deliverables: " + ", ".join(outside[:10])
                    + " (restore them; only the job's deliverables may change)"]
        problems = []
        for path in changed:
            data = (ws.board / path).read_bytes() if (ws.board / path).is_file() else b""
            problems += board.file_problems(path, data.decode("utf-8", errors="replace"), allowed, data)
            if path.endswith("/design/plan.json") and board.gaps_only(job):
                before = gitops.git(ws.board, "show", f"HEAD:{path}", check=False)
                problems += board.plan_change_problems(path, before, data.decode("utf-8", errors="replace"))
        return problems

    def earlier_attempt(self, ws: Workspace) -> str:
        """For a retry: where the failed attempt's deliverables and problems are, so the agent repairs
        them rather than starting over (on this worker its kept folder and problems file; from any
        worker, the deliverables its wip branch held, now in place)."""
        problems = ws.job_dir / "problems.txt"
        restored = ws.extra.get("wip_restored")
        if restored:
            wip, files = restored
            return ("## An earlier attempt\n\nAn earlier run of this job (by @" + str(wip.get("by", "?")) + ") failed the "
                    "worker's checks after its repairs. Its deliverables are back in place: " + ", ".join(f"`{f}`" for f in files)
                    + ". Start from them: keep what is sound and repair what the checks refuse, rather than starting over.")
        if not problems.is_file():
            return ""
        previous = ws.job_dir / "problems.previous.txt"
        problems.replace(previous)
        kept = ws.job_dir / UNSUBMITTED
        return ("## An earlier attempt\n\nAn earlier run of this job on this worker failed the worker's checks; the "
                f"problems are listed in `{previous}`." + (
                    f" What it wrote is kept in `{kept}` (laid out like the repository). Start from it: put what is sound "
                    "back in place and repair the listed problems, rather than starting over." if kept.is_dir() else ""))

    def comment_on_board_pr(self, job: Job, ctx: Context, ws: Workspace, body: str) -> str:
        if ctx.dry_run:
            return f"[dry-run] would comment on #{self.board_pr(job)}"
        made = self.push(lambda: ctx.gh.write(
            "POST", f"repos/{ctx.cfg.repositories.packages_repo()}/issues/{self.board_pr(job)}/comments",
            {"body": body})) or {}
        return made.get("html_url", "")

    def marker(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        return {"job": job.id, "worker": ctx.worker_id, "agent": ctx.agent, "model": ctx.model,
                "head": ws.extra.get("head", "")}

    def submit_on_board_pr(self, job: Job, ctx: Context, ws: Workspace, summary: str) -> str:
        """A fix on the board pull request: commit the job's files on top of its head, push them to
        its branch (the job's operator owns it), and say so on the pull request with the worker's
        marker; the intake opens the next round."""
        changed = gitops.changed_files(ws.board)
        if not changed:
            raise LaneError("nothing to push")
        if ctx.dry_run:
            return f"[dry-run] would push {', '.join(changed)} to #{self.board_pr(job)}"
        gitops.commit_paths(ws.board, changed, f"{job.id}: {summary}\n\nAI-assisted: {ctx.agent} ({ctx.model}), "
                                               f"worker {ctx.worker_id}.", ctx.operator)
        fork = gitops.ensure_fork(ctx.gh, ctx.cfg.repositories.packages_repo(), ctx.operator)
        if ws.extra.get("pr_repo") and ws.extra["pr_repo"].lower() != fork.lower():
            raise LaneError(f"the pull request's branch is in {ws.extra['pr_repo']}; only its owner pushes to it")
        self.push(lambda: gitops.push_branch(ws.board, fork, ws.extra.get("pr_ref") or ws.branch,
                                             expected=ws.extra.get("push_base") or ws.extra.get("head")))
        head = gitops.git(ws.board, "rev-parse", "HEAD").strip()
        # The intake reads the pull request's head once it sees this comment (a head still at the
        # fix's base releases the fix as not pushed): the comment waits for the head.
        self.await_pr_head(ctx, ctx.cfg.repositories.packages_repo(), self.board_pr(job), head,
                           then="not commenting")
        marker = {**self.marker(job, ctx, ws), "head": head}
        url = self.comment_on_board_pr(job, ctx, ws, f"Fix `{job.id}` pushed `{head[:7]}` by `{ctx.operator}` "
                                                     f"({ctx.agent}, {ctx.model}): {summary}\n\n"
                                                     f"<!-- tauceti-roadmap-review {json.dumps(marker, sort_keys=True)} -->\n")
        self.after_board_submission(job, ctx)
        return url

    def after_board_submission(self, job: Job, ctx: Context) -> None:
        """Record who did the job (the board trusts a worker's comment by this record) and mark it
        submitted; for a review or a pass this comes before its comment, so the board can never
        conclude the job and then see it flipped back."""
        if ctx.claims:
            ctx.claims.record_author(job.id, ctx.agent, ctx.model)
        try:
            ctx.board.set_state(job, "submitted")
        except GHError:
            pass

    def comment_problems(self, body: str) -> list[str]:
        """A comment the board would not read (no verdict, or changes asked for without a numbered
        finding) or that GitHub refuses (longer than a comment may be) is caught before it is posted."""
        if prreviews.parse(body) is None:
            return [("report: the board cannot read it as a review: give the verdict, and write every finding as "
                     "`## R01 (P1) — <short title>`")]
        if len(body) > COMMENT_LIMIT:
            return [(f"report: too long to post as a pull-request comment ({len(body):,} characters; the limit is "
                     f"{COMMENT_LIMIT:,}): keep every finding and the sampled rows of the adversarial table, and cut "
                     "the per-target ledgers and the restated package content")]
        return []

    def submit(self, job: Job, ctx: Context, ws: Workspace, summary: str) -> str:
        """Commit the job's own files on the worker's board branch, push it to the operator's fork
        and open the submission pull request that names the job (or, for a job on a board pull
        request, push to it or comment on it)."""
        if self.board_pr(job):
            return self.submit_on_board_pr(job, ctx, ws, summary)
        cfg = ctx.cfg
        problems = self.submission_problems(job, ctx, ws)
        if problems:
            raise LaneError("; ".join(problems))
        changed = gitops.changed_files(ws.board)
        if not changed:
            raise LaneError("nothing to submit")
        message = (f"{job.id}: {summary}\n\nAI-assisted: {ctx.agent} ({ctx.model}), worker {ctx.worker_id}.")
        if ctx.dry_run:
            return f"[dry-run] would submit {', '.join(changed)}"
        gitops.commit_paths(ws.board, changed, message, ctx.operator)
        repo = cfg.repositories.packages_repo()
        fork = self.push(lambda: gitops.ensure_fork(ctx.gh, repo, ctx.operator))
        self.push(lambda: gitops.push(ws.board, fork, ws.branch))
        body = (f"Refs #{job.number}\n\n{summary}\n\nAgent: {ctx.agent} ({ctx.model}); operator `{ctx.operator}`; "
                f"worker `{ctx.worker_id}`. Files: " + ", ".join(f"`{p}`" for p in changed) + "\n\n"
                + board.encode_submission(job.id, ctx.worker_id, ctx.operator, ctx.agent, ctx.model))
        pr = self.push(lambda: gitops.open_pull_request(ctx.gh, repo, f"{ctx.operator}:{ws.branch}", "main",
                                                        f"{board.title_for(job)}: submission", body, draft=False))
        if self.records_author(job) and ctx.claims:
            ctx.claims.record_author(job.id, ctx.agent, ctx.model)
        try:
            ctx.board.set_state(job, "submitted")
        except GHError:
            pass  # operators without write access on the board: `board sync` mirrors the state
        return (pr or {}).get("html_url", "")

    agent_started = False   # set once the first agent run begins: a failure before it is the operator's side
    parked = False          # set by the run's one needs-references request

    def run(self, job: Job, ctx: Context) -> LaneResult:
        reason = self.eligible(job, ctx)
        if reason:
            return LaneResult(job.id, "skipped", reason)
        pending = read_pending(ctx, job)
        if pending:
            return self.resume_push(job, ctx, pending)
        ws = self.prepare(job, ctx)
        self.restore_wip(job, ctx, ws)
        self.prepare_sources(job, ctx, ws)
        self.prepare_tools(job, ctx, ws)
        self.prepare_signatures(job, ctx, ws)
        values = {**self.common_values(job, ctx, ws), **self.values(job, ctx, ws)}
        runs = self.briefs(job, ctx, ws, values)
        earlier = self.earlier_attempt(ws)
        if earlier and runs:
            runs[0] = (runs[0][0], runs[0][1].rstrip() + "\n\n" + earlier)
        for label, brief in runs:
            (ws.job_dir / (f"brief-{label}.md" if label else "brief.md")).write_text(brief, encoding="utf-8")
        if ctx.dry_run:
            return LaneResult(job.id, "dry-run", f"{len(runs)} brief(s) written to {ws.job_dir}")
        self.agent_started = True
        worked = 0.0        # agent seconds in this round: what a usage limit wastes
        for label, brief in runs:
            result = self.run_agent(ctx, ws, brief, label)
            worked += result.seconds
            where = f" ({label})" if label else ""
            if result.limited:  # before the failure path: a limit exits non-zero too, and consumes no attempt
                return limited(job, result, where, worked)
            if result.exit_code != 0:
                return LaneResult(job.id, "failed", f"agent exited {result.exit_code}{where}"
                                  + (" (timeout)" if result.timed_out else ""))
        self.after_runs(job, ctx, ws)
        self.scrub_outputs(job, ctx, ws)
        missing = self.missing_references(job, ctx, ws)
        if missing:
            return with_notes(self.park(job, ctx, missing), ws)
        problems = self.validate(job, ctx, ws)
        # Repair runs in the same workspace before the job fails (still one attempt): each fed only the
        # problems that remain, up to REPAIR_PASSES while their number strictly falls.
        for attempt in range(1, REPAIR_PASSES + 1):
            if not problems:
                break
            (ws.job_dir / "problems.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
            result = self.run_agent(ctx, ws, repair_brief(runs[0][1], problems), "repair" if attempt == 1
                                    else f"repair{attempt}")
            worked += result.seconds
            if result.limited:
                return limited(job, result, " (repair)", worked)
            self.scrub_outputs(job, ctx, ws)
            if result.exit_code != 0:
                problems = problems + [f"the repair run exited {result.exit_code}"]
                break
            before, problems = len(problems), self.validate(job, ctx, ws)
            if problems and len(problems) >= before:
                break            # the repair did not reduce the problems: another pass would not either
        if problems:
            (ws.job_dir / "problems.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
            wip = self.push_wip(job, ctx, ws)
            return with_notes(LaneResult(job.id, "failed", "; ".join(problems[:5]) + wip), ws)
        (ws.job_dir / "problems.txt").unlink(missing_ok=True)
        result = self.deliver(job, ctx, ws)
        if result.status == "submitted":
            self.drop_wip(job, ctx, ws)
        return with_notes(result, ws)

    keeps_wip = False       # a failed run leaves its deliverables on a wip/<JOB> branch for the next attempt

    def wip_branch(self, job: Job) -> str:
        return f"wip/{claims.ref_safe(job.id)}"

    def push_wip(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """After a run that failed its checks: its deliverables (only those that are safe to publish: no
        source material, no local path) committed and pushed to `wip/<JOB>` on the operator's fork, and
        recorded on the job, so the next attempt, on any worker, starts from them. Returns a note."""
        if not self.keeps_wip or ctx.dry_run:
            return ""
        allowed = board.allowed_paths(job, ctx.cfg.board.packages_dir, board.order_file(ctx.cfg))
        try:
            changed = [p for p in gitops.changed_files(ws.board) if p in allowed and (ws.board / p).is_file()]
            safe = [p for p in changed if not gates.source_material_problems(p, (ws.board / p).read_bytes())
                    and not board.PRIVATE_PATH.search((ws.board / p).read_text(encoding="utf-8", errors="replace"))]
            if not safe:
                return ""
            gitops.commit_paths(ws.board, safe, f"{job.id}: unsubmitted deliverables of a run that failed its checks "
                                                "(work in progress)", ctx.operator)
            fork = gitops.ensure_fork(ctx.gh, ctx.cfg.repositories.packages_repo(), ctx.operator)
            gitops.push(ws.board, fork, self.wip_branch(job))
            head = gitops.git(ws.board, "rev-parse", "HEAD").strip()
        except (gitops.GitError, GHError, OSError) as exc:
            return f" (its deliverables could not be kept on {self.wip_branch(job)}: {str(exc).splitlines()[0][:120]})"
        job.inputs["wip"] = {"repo": fork, "branch": self.wip_branch(job), "head": head, "by": ctx.operator,
                             "files": safe}
        try:
            ctx.board.update_spec(job)
        except (GHError, AttributeError):
            pass
        return f" (its deliverables are kept on {fork} {self.wip_branch(job)} for the next attempt)"

    def restore_wip(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        """At the start of an attempt: the deliverables a failed attempt left on its wip branch put in
        place (any worker can read the fork). Returns the files restored."""
        wip = job.inputs.get("wip") if self.keeps_wip else None
        if not isinstance(wip, dict) or not wip.get("repo") or not wip.get("branch"):
            return []
        try:
            gitops.git(ws.board, "fetch", "-q", gitops.url(wip["repo"]), f"refs/heads/{wip['branch']}")
        except gitops.GitError:
            return []
        allowed = board.allowed_paths(job, ctx.cfg.board.packages_dir, board.order_file(ctx.cfg))
        restored = []
        for rel in wip.get("files", []):
            if rel not in allowed:
                continue
            text = gitops.git(ws.board, "show", f"FETCH_HEAD:{rel}", check=False)
            if text:
                (ws.board / rel).parent.mkdir(parents=True, exist_ok=True)
                (ws.board / rel).write_text(text, encoding="utf-8")
                restored.append(rel)
        if restored:
            ws.extra["wip_restored"] = (wip, restored)
        return restored

    def drop_wip(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """After a submission: the wip branch goes (when it is on this operator's fork) and the job forgets it."""
        wip = job.inputs.pop("wip", None)
        if not isinstance(wip, dict):
            return
        if str(wip.get("by", "")).lower() == (ctx.operator or "").lower():
            gitops.git(ws.board, "push", "-q", gitops.url(wip["repo"]), f":refs/heads/{wip['branch']}", check=False)
        try:
            ctx.board.update_spec(job)
        except (GHError, AttributeError):
            pass

    def deliver(self, job: Job, ctx: Context, ws: Workspace) -> LaneResult:
        """The lane's last steps and the submission, once the checks passed. A branch that moved
        while the job ran blocks the job for a human; a push that keeps failing keeps the run
        (`pending-push/`), and the next attempt on this worker pushes it without running the agent."""
        try:
            note = self.finish(job, ctx, ws) or f"{board.TITLE[job.kind].lower()} of {job.name}"
            url = self.submit(job, ctx, ws, note)
        except gitops.BranchMoved as exc:
            return LaneResult(job.id, "blocked", str(exc))
        except (gitops.PushError, GHError) as exc:
            if isinstance(exc, GHError) and not transient(exc):
                return LaneResult(job.id, "failed", str(exc))
            tries = save_pending(ctx, job, ws, str(exc))
            if tries >= PUSH_ATTEMPTS:
                clear_pending(ctx, job)
                return LaneResult(job.id, "failed", f"the push failed {tries} times; the run is given up: {exc}")
            return LaneResult(job.id, "failed", f"push failed ({exc}); the run is kept in "
                              f"{ws.job_dir / PENDING_DIR} and the next attempt pushes it without running the agent "
                              f"(try {tries} of {PUSH_ATTEMPTS})")
        except (LaneError, gitops.GitError) as exc:
            return LaneResult(job.id, "failed", str(exc))
        clear_pending(ctx, job)
        self.drop_worktree(ctx, ws)
        return LaneResult(job.id, "submitted", note, url)

    def drop_worktree(self, ctx: Context, ws: Workspace) -> None:
        """After a submission, the lane's roadmap worktree (pr/<Name>, port/<Name>) goes: the next
        round that needs it checks it out again, its Lake packages a link to the shared copy."""
        if ws.workdir != ws.board and ws.workdir.parent.name in ("pr", "port") and ws.workdir.is_dir():
            from .pr import remove_worktree
            remove_worktree(ws.content, ws.workdir)

    def push(self, do):
        """A push or a GitHub write of the submission step, tried again PUSH_RETRIES times
        PUSH_BACKOFF seconds apart before its error stands: a refused push, a GitHub 5xx or timeout.
        A moved branch, or any other error, is never retried."""
        for attempt in range(PUSH_RETRIES + 1):
            try:
                return do()
            except (gitops.PushError, GHError) as exc:
                if attempt == PUSH_RETRIES or (isinstance(exc, GHError) and not transient(exc)):
                    raise
                self.sleep(PUSH_BACKOFF)

    def await_pr_head(self, ctx: Context, repo: str, number: int, sha: str, *, seen: str | None = None,
                      then: str = "", wait: int | None = None, poll: int | None = None) -> None:
        """After a push, before anything that relies on the pull request's head: GitHub's pull-request
        record follows a pushed branch only after a few seconds, so its head is read again every
        `poll` seconds until it is the pushed `sha`. `seen` is a head already in hand (the answer
        that opened the pull request), counted as the first read. A transient read error counts as
        a read that did not show it. LaneError (ending with `then`) once `wait` seconds (HEAD_WAIT)
        have passed without it."""
        wait = HEAD_WAIT if wait is None else wait
        poll = max(1, HEAD_POLL if poll is None else poll)
        live = seen if seen is not None else pr_head(ctx, repo, number)
        waited = 0
        while live != sha and waited < wait:
            self.sleep(poll)
            waited += poll
            live = pr_head(ctx, repo, number)
        if live != sha:
            raise LaneError(f"#{number}: the pull request head did not update to the pushed {sha[:7]} within "
                            f"{wait} s (it is still {live[:7] or 'unreadable'})" + (f"; {then}" if then else ""))

    sleep = staticmethod(time.sleep)

    def resume_push(self, job: Job, ctx: Context, pending: dict) -> LaneResult:
        """The next attempt after a push failed: fresh checkouts, the run's deliverables and commit
        put back, the checks again (no agent run), then the push and the submission."""
        ws = self.prepare(job, ctx)
        restore_pending(ctx, job, ws, pending)
        problems = self.validate(job, ctx, ws)
        if problems:
            clear_pending(ctx, job)
            return LaneResult(job.id, "failed", "the kept run no longer passes its checks: " + "; ".join(problems[:5]))
        ws.extra.setdefault("notes", []).append(f"pushing the run kept after a failed push (try {pending.get('tries', 1) + 1})")
        return with_notes(self.deliver(job, ctx, ws), ws)


def elaborate_line(cfg: Config, ws: Workspace) -> str:
    """The briefs' ELABORATE: the command to elaborate Suggested.lean in the content checkout, or why
    it is not available (what the worker's own elaboration check will report too)."""
    if not cfg.gates.elaborate:
        return "not available (no elaboration command is configured; say so in the report)"
    setup = ws.extra.get("lake")
    if setup is None:
        return "not available (this lane does not elaborate; say so in the report)"
    if setup.reason:
        return f"not available — {setup.reason} (say so in the report)"
    line = f"`{cfg.gates.elaborate.format(file='<absolute path of Suggested.lean>')}` run from `{ws.content}`"
    if cfg.gates.build:
        line += ("; a `TauCeti.*` import whose object file is missing is built first with `lake build <that module>` "
                 "from the same directory (the worker's own check builds them through the targeted build of the module)")
    return line


def builds(job: Job) -> bool:
    """Whether the job's lane checks out the roadmap repository and builds it: the port, the red team,
    their fixes and the gate rounds."""
    return job.kind in ("port", "redteam", "gate") or (job.kind == "fix" and board.parse_job_id(job.id).subject == "RT")


PENDING_DIR = "pending-push"
PUSH_ATTEMPTS = 3       # attempts (each with its in-run retries) before a run whose push fails is given up
PUSH_RETRIES = 2        # retries of a push inside one attempt
PUSH_BACKOFF = 60       # seconds between them
HEAD_WAIT = 90          # seconds a pushed head may take to become the pull request's head
HEAD_POLL = 5           # seconds between the reads of the pull request while it does


def pr_head(ctx: Context, repo: str, number: int) -> str:
    """The head sha of pull request #number as GitHub reports it now ('' when a transient error
    leaves it unread; any other error stands)."""
    try:
        return ((ctx.gh.api(f"repos/{repo}/pulls/{number}") or {}).get("head") or {}).get("sha", "")
    except GHError as exc:
        if not transient(exc):
            raise
        return ""


def pending_path(ctx: Context) -> Path:
    """The worker's record of runs kept after a failed push (beside attempts.json)."""
    return ctx.wdir / "pending-push.json"


def _pending_all(ctx: Context) -> dict:
    try:
        data = json.loads(pending_path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_pending(ctx: Context, job: Job) -> dict | None:
    entry = _pending_all(ctx).get(job.id)
    return entry if isinstance(entry, dict) else None


def clear_pending(ctx: Context, job: Job) -> None:
    data = _pending_all(ctx)
    if data.pop(job.id, None) is not None:
        pending_path(ctx).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def save_pending(ctx: Context, job: Job, ws: Workspace, error: str) -> int:
    """Keep a run whose push failed: the job's deliverables in the board checkout (copied to
    `jobs/<JOB>/pending-push/board/`), and the commit in the content worktree (a bundle beside them,
    and a ref in the content checkout, so it survives the worktree), recorded in pending-push.json.
    Returns how many attempts have failed to push it."""
    keep = ws.job_dir / PENDING_DIR
    shutil.rmtree(keep, ignore_errors=True)
    (keep / "board").mkdir(parents=True, exist_ok=True)
    for rel in board.allowed_paths(job, ctx.cfg.board.packages_dir, board.order_file(ctx.cfg)):
        if (ws.board / rel).is_file():
            (keep / "board" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ws.board / rel, keep / "board" / rel)
    entry = {"tries": (read_pending(ctx, job) or {}).get("tries", 0) + 1, "error": error[:300],
             "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    if ws.workdir != ws.board and (ws.workdir / ".git").exists():
        sha = gitops.git(ws.workdir, "rev-parse", "HEAD").strip()
        base = ws.extra.get("push_base") or ws.extra.get("head") or ws.extra.get("base") or ""
        entry.update(sha=sha, base=base, branch=ws.extra.get("branch", ""))
        ref = f"refs/tauceti-pending/{claims.ref_safe(job.id)}"
        gitops.git(ws.workdir, "update-ref", ref, sha)
        if base and base != sha:
            gitops.git(ws.workdir, "bundle", "create", str(keep / "commit.bundle"), f"{base}..{ref}", check=False)
    data = _pending_all(ctx)
    data[job.id] = entry
    pending_path(ctx).parent.mkdir(parents=True, exist_ok=True)
    pending_path(ctx).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return entry["tries"]


def restore_pending(ctx: Context, job: Job, ws: Workspace, pending: dict) -> None:
    """Put a kept run back into fresh checkouts: the deliverables into the board checkout, the
    commit onto the branch in the content worktree; the push expects the head the run was built on."""
    keep = ws.job_dir / PENDING_DIR / "board"
    for path in keep.rglob("*") if keep.is_dir() else []:
        if path.is_file():
            dest = ws.board / path.relative_to(keep)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    if pending.get("sha") and ws.workdir != ws.board:
        ref = f"refs/tauceti-pending/{claims.ref_safe(job.id)}"
        if not gitops.git(ws.workdir, "rev-parse", "-q", "--verify", f"{pending['sha']}^{{commit}}", check=False).strip():
            bundle = ws.job_dir / PENDING_DIR / "commit.bundle"
            gitops.git(ws.workdir, "fetch", "-q", str(bundle), f"+{ref}:{ref}", check=False)
        branch = pending.get("branch") or ws.extra.get("branch", "")
        if branch:
            gitops.git(ws.workdir, "checkout", "-q", "-B", branch, pending["sha"])
        else:
            gitops.git(ws.workdir, "checkout", "-q", "--detach", pending["sha"])
    if pending.get("base"):
        ws.extra["push_base"] = pending["base"]


def with_notes(result: LaneResult, ws: Workspace) -> LaneResult:
    """The result, with the run's notes (requests dropped or completed) at the end of its detail, so
    the round log shows them."""
    if ws.extra.get("notes"):
        result.detail = (result.detail + " [" + "; ".join(ws.extra["notes"]) + "]").strip()
    return result


def count(n: int, noun: str) -> str:
    """`3 targets`, `1 gap`."""
    return f"{n} {noun}" + ("" if n == 1 else "s")


def ocr_line(cfg: Config) -> str:
    """What the briefs say about OCR on this worker (so an agent never guesses it is missing)."""
    if not cfg.sources.ocr:
        return "off (`[sources] ocr = false`): an image-only scan keeps only its text layer"
    why = sources.ocr_status()
    if why:
        return f"not available here ({why.split('. ', 1)[0]})"
    return ("available: the sources service recognises an image-only scan when it reads it (the text's header then "
            "says \"recognised by OCR\"), including a scan cached before OCR was installed")


def declared_gaps(pkg: Path) -> str:
    """The supplier gaps a roadmap's plan declares, one line each, for the briefs."""
    from .design import gaps_listing
    try:
        plan = json.loads((pkg / "design" / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "none declared (no readable design/plan.json)"
    return gaps_listing(plan if isinstance(plan, dict) else {})


def repair_brief(brief: str, problems: list[str]) -> str:
    return (brief.rstrip() + "\n\n## Repair\n\nThe worker's checks found these problems in your deliverables:\n\n"
            + "\n".join(f"- {p}" for p in problems[:60])
            + "\n\nRepair these problems in place; change nothing else. Keep every file you already wrote, edit only "
              "what each problem names, and keep the report in its required shape.\n")


def scan_problems(result: gates.GateResult) -> list[str]:
    return [f"{k}={v}: " + "; ".join(str(getattr(h, "text", h)) for h in result.details.get(k, [])[:3])
            for k, v in result.counts.items() if v]
