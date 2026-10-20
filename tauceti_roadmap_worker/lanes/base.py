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

import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .. import agents, board, claims, fetcher, gates, gitops, prompts, references, sources
from ..board import Board, Job
from ..config import Config
from ..depgraph import read_order
from ..gh import GH, GHError
from ..paths import sanitize_worker_id

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

    def package(self, cfg: Config, name: str) -> Path:
        return self.board / board.package_dir(cfg.board.packages_dir, name)


@dataclass
class LaneResult:
    job: str
    status: str            # submitted | failed | limited | needs-references | skipped | dry-run
    detail: str = ""
    url: str = ""
    retry_at: dt.datetime | None = None


class LaneError(Exception):
    pass


# --- shared lookups -------------------------------------------------------------------------------

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
            out.append({"pr": pr["number"], "name": data["roadmap"], "head": pr["head"]["sha"],
                        "branch": pr["head"]["ref"], "owner": pr["user"]["login"]})
    (ctx.state / "prs.json").parent.mkdir(parents=True, exist_ok=True)
    (ctx.state / "prs.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return out


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


def dropped_names(cfg: Config) -> list[str]:
    if not cfg.gates.dropped_names_file:
        return []
    try:
        return read_order((cfg.state_dir() / cfg.gates.dropped_names_file).read_text(encoding="utf-8"))
    except OSError:
        return []


UNSUBMITTED = "unsubmitted"
# Machine paths in the files that hand work on (reports, the triage, verdicts, the manifest) are
# rewritten before the gate; README and Lean content is never rewritten (a path there is a defect).
SCRUBBED = ("target.md", "review.json", "sources.toml")
TEMP_ROOTS = re.compile(r"(?<![\w.~<>/-])/(?:private/(?:tmp|var)|tmp)/(?=[^\s/])")


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
            return f"needs the branch owner {job.requires_operator}"
        if job.independent_of and ctx.claims:
            problem = claims.separation_problem(job.independent_of, ctx.operator,
                                                ctx.claims.recorded_authors(job.independent_of))
            if problem:
                return problem
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
        return ""

    def records_author(self, job: Job) -> bool:
        return bool(board.artefact_of(job.id)) and job.kind != "review"

    def edits_manifest(self, job: Job, ctx: Context) -> bool:
        """Whether the job's deliverables include the roadmap's sources.toml (the board decides)."""
        return board.manifest_path(ctx.cfg.board.packages_dir, job.name) in board.allowed_paths(
            job, ctx.cfg.board.packages_dir)

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
        return Workspace(job_dir=job_dir, scratch=scratch, board=board_dir, content=content_dir,
                         workdir=board_dir, report=report, branch=branch)

    def common_values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        cfg = ctx.cfg
        order_file = order_path(cfg, ws.board, ws.content)
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
        elaborate = "not available (say so in the report)"
        if cfg.gates.elaborate and (ws.content / ".lake").is_dir():
            elaborate = f"`{cfg.gates.elaborate.format(file='<absolute path of Suggested.lean>')}` run from `{ws.content}`"
        return {
            "JOB": job.id, "ROADMAP": job.name, "ROUND": str(job.round), "WORKDIR": str(ws.workdir),
            "SCRATCH": str(ws.scratch), "REPORT": str(ws.report), "CONTENT_REPO": cfg.repositories.content,
            "CONTENT": str(ws.content), "ACTIVE_DIR": cfg.roadmap.active_dir, "COMPLETED_DIR": cfg.roadmap.completed_dir,
            "PACKAGES": str(ws.board / cfg.board.packages_dir), "PACKAGE": str(ws.package(cfg, job.name)),
            "ORDER": str(order_file), "SUPPLIERS": ", ".join(earlier) or "none yet",
            "DROPPED": ", ".join(dropped) or "none",
            "DEPCHECK": ctx.cli("gate", "--depcheck", str(ws.extra.get("readme") or ws.package(cfg, job.name) / "README.md"),
                                "--name", job.name, "--order", str(order_file)),
            "LIBRARY": library_description(ws.content),
            "SIBLINGS": "; ".join(f"{name} at `{path}`" for _pr, name, path in sibs) or
                        ("; ".join(p["name"] for p in prs) or "none"),
            "MAX_LEAN_LINES": str(cfg.gates.max_lean_lines), "ELABORATE": elaborate, "EXTRA_RULES": extra_rules,
            "SOURCE_TEXTS": ws.extra.get("source_texts", "none recorded yet"),
            "FETCH": self.fetch_command(job, ctx, ws),
        }

    def fetch_command(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """The command line an agent runs to fetch a source through the service."""
        args = ["sources", "fetch"]
        if self.edits_manifest(job, ctx):
            args += ["--manifest", str(ws.package(ctx.cfg, job.name) / "sources.toml")]
            if self.manifest_add_only:
                args.append("--add-only")
        return ctx.cli(*args, "--missing-out", str(ws.scratch / "needs-references.toml"))

    def prepare_sources(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """Bring every text the roadmap's manifest records into the sources cache, list them for the
        brief, and turn texts that are gone for good into a needs-references request. A request left
        by an earlier run of this job is archived first: its answers are in the manifest now."""
        request = ws.scratch / "needs-references.toml"
        if request.is_file():
            request.replace(ws.scratch / "needs-references.previous.toml")
        manifest_path = ws.package(ctx.cfg, job.name) / "sources.toml"
        if not manifest_path.is_file():
            return
        try:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            ws.extra["source_texts"] = f"none: the manifest does not parse ({exc})"
            return
        available = ctx.sources_fetcher().materialize(manifest, offline=ctx.dry_run)
        self.record_local_hashes(job, ctx, manifest_path, manifest, available)
        lines = []
        for a in available:
            if a.text:
                lines.append(f"- `{a.id}` ({a.label}): `{a.text}`")
            else:
                lines.append(f"- `{a.id}` ({a.label}): unavailable: {a.problem}")
        ws.extra["source_texts"] = "\n" + "\n".join(lines) if lines else "none recorded yet"
        for a in available:
            if a.text or not a.permanent:
                continue
            entry = sources.find_source(manifest, id=a.id) or {}
            references.add_missing(request, references.MissingReference(
                authors=str(entry.get("authors", "")), title=str(entry.get("title", a.id)), year=str(entry.get("year", "")),
                needed_for=", ".join(entry.get("cited_for", [])) or f"the roadmap's citations of {a.id}",
                tried=[a.problem], key=a.id, doi=str(entry.get("doi", "")), url=str(entry.get("url", "")),
                locator_scheme=str(entry.get("locator_scheme", "")), sha256=str(entry.get("sha256", ""))))

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

    def missing_references(self, ws: Workspace) -> list[references.MissingReference]:
        return references.read_missing(ws.scratch / "needs-references.toml")

    def briefs(self, job: Job, ctx: Context, ws: Workspace, values: dict) -> list[tuple[str, str]]:
        """(label, brief) for each agent run of the job, in order; '' labels the main brief."""
        return [("", prompts.render(self.prompt, values))]

    def after_runs(self, job: Job, ctx: Context, ws: Workspace) -> None:
        """Combine the outputs of several runs (the red-team lane merges its section passes)."""

    def run_agent(self, ctx: Context, ws: Workspace, brief: str, label: str = "") -> agents.AgentResult:
        attempt = len(list(ws.job_dir.glob("run-*.out"))) + 1
        stem = f"run-{attempt}" + (f"-{label}" if label else "")
        return agents.run_agent(agents.AgentRun(
            agent=ctx.agent, model=ctx.model, workdir=ws.workdir, brief=brief,
            log_out=ws.job_dir / f"{stem}.out", log_err=ws.job_dir / f"{stem}.err",
            timeout=ctx.cfg.agents.timeout_minutes * 60, sandbox_profile=ctx.cfg.sandbox.profile,
            proxy_url=ctx.cfg.sandbox.proxy_url, netjail=ctx.cfg.sandbox.netjail,
            claude_hook=ctx.cfg.sandbox.claude_hook, settings_dir=ctx.wdir / "settings"))

    def park(self, job: Job, ctx: Context, missing: list[references.MissingReference]) -> LaneResult:
        """Post the request (from the template, with its machine-readable marker, machine paths
        rewritten: the comment is public) and park the job."""
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

    def submit(self, job: Job, ctx: Context, ws: Workspace, summary: str) -> str:
        """Commit the job's own files on the worker's board branch, push it to the operator's fork
        and open the submission pull request that names the job."""
        cfg = ctx.cfg
        allowed = board.allowed_paths(job, cfg.board.packages_dir)
        changed = gitops.changed_files(ws.board)
        outside = [p for p in changed if p not in allowed]
        if outside:
            raise LaneError("the agent changed files outside the job's deliverables: " + ", ".join(outside[:10]))
        problems = []
        for path in changed:
            data = (ws.board / path).read_bytes() if (ws.board / path).is_file() else b""
            problems += board.file_problems(path, data.decode("utf-8", errors="replace"), allowed, data)
        if problems:
            raise LaneError("; ".join(problems))
        if not changed:
            raise LaneError("nothing to submit")
        message = (f"{job.id}: {summary}\n\nAI-assisted: {ctx.agent} ({ctx.model}), worker {ctx.worker_id}.")
        if ctx.dry_run:
            return f"[dry-run] would submit {', '.join(changed)}"
        gitops.commit_paths(ws.board, changed, message, ctx.operator)
        repo = cfg.repositories.packages_repo()
        fork = gitops.ensure_fork(ctx.gh, repo, ctx.operator)
        gitops.push(ws.board, fork, ws.branch)
        body = (f"Refs #{job.number}\n\n{summary}\n\nAgent: {ctx.agent} ({ctx.model}); operator @{ctx.operator}; "
                f"worker `{ctx.worker_id}`. Files: " + ", ".join(f"`{p}`" for p in changed) + "\n\n"
                + board.encode_submission(job.id, ctx.worker_id, ctx.operator, ctx.agent))
        pr = gitops.open_pull_request(ctx.gh, repo, f"{ctx.operator}:{ws.branch}", "main",
                                      f"{board.title_for(job)}: submission", body, draft=False)
        if self.records_author(job) and ctx.claims:
            ctx.claims.record_author(job.id)
        try:
            ctx.board.set_state(job, "submitted")
        except GHError:
            pass  # operators without write access on the board: `board sync` mirrors the state
        return (pr or {}).get("html_url", "")

    def run(self, job: Job, ctx: Context) -> LaneResult:
        reason = self.eligible(job, ctx)
        if reason:
            return LaneResult(job.id, "skipped", reason)
        ws = self.prepare(job, ctx)
        self.prepare_sources(job, ctx, ws)
        values = {**self.common_values(job, ctx, ws), **self.values(job, ctx, ws)}
        runs = self.briefs(job, ctx, ws, values)
        for label, brief in runs:
            (ws.job_dir / (f"brief-{label}.md" if label else "brief.md")).write_text(brief, encoding="utf-8")
        if ctx.dry_run:
            return LaneResult(job.id, "dry-run", f"{len(runs)} brief(s) written to {ws.job_dir}")
        for label, brief in runs:
            result = self.run_agent(ctx, ws, brief, label)
            where = f" ({label})" if label else ""
            if result.limited:
                return LaneResult(job.id, "limited", "usage limit" + where, retry_at=result.reset_at)
            if result.exit_code != 0:
                return LaneResult(job.id, "failed", f"agent exited {result.exit_code}{where}"
                                  + (" (timeout)" if result.timed_out else ""))
        self.after_runs(job, ctx, ws)
        self.scrub_outputs(job, ctx, ws)
        missing = self.missing_references(ws)
        if missing:
            return self.park(job, ctx, missing)
        problems = self.check(job, ctx, ws)
        if problems:
            (ws.job_dir / "problems.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
            return LaneResult(job.id, "failed", "; ".join(problems[:5]))
        note = self.finish(job, ctx, ws)
        try:
            url = self.submit(job, ctx, ws, note or f"{board.TITLE[job.kind].lower()} of {job.name}")
        except (LaneError, gitops.GitError, GHError) as exc:
            return LaneResult(job.id, "failed", str(exc))
        return LaneResult(job.id, "submitted", note, url)


def scan_problems(result: gates.GateResult) -> list[str]:
    return [f"{k}={v}: " + "; ".join(str(getattr(h, "text", h)) for h in result.details.get(k, [])[:3])
            for k, v in result.counts.items() if v]
