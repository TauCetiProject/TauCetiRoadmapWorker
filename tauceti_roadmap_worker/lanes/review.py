"""The `review` lane: an independent review of a design or a package, read-only on the artefact.
The one file it may add to is the sources manifest, where it records what it fetched to check a
locator; the author's entries stay as they were.

The reviewer is never the author's operator: `independent_of` lists every job that wrote the
round under review, and both the lease namespace (before the run) and the intake (on submission)
refuse a reviewer who did any of them. Source locators are checked on a seeded sample: the seed is
the job id, so the sample is reproducible and differs between rounds.
"""

from __future__ import annotations

import hashlib
import json
import random
import tomllib

from .. import board, gitops, prreviews, sources
from ..board import Job
from ..gates import readme_targets
from .base import Context, Lane, Workspace, checkout_board_pr, count
from .design import manifest_problems
from .package import build_tauceti_imports

SAMPLE_SIZE = 15


def sample_items(readme: str) -> list[str]:
    """The README's targets, in order, as the summaries count them (`gates.readme_targets`): the
    `### k.n` subsections of its layers, else the bold target names inside them."""
    return readme_targets(readme)


def seeded_sample(items: list[str], k: int, seed: str) -> list[str]:
    """k items chosen reproducibly from the seed, kept in document order."""
    if len(items) <= k:
        return list(items)
    rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big"))
    picked = set(rng.sample(range(len(items)), k))
    return [item for i, item in enumerate(items) if i in picked]


def verdict_problems(data: dict, job_id: str, declared: set[str] | None = None) -> list[str]:
    review = data.get("review") if isinstance(data, dict) else None
    if not isinstance(review, dict):
        return ["verdict file: no review object"]
    problems = []
    if review.get("status") not in ("accepted", "needs_changes", "rejected"):
        problems.append("verdict file: status must be accepted, needs_changes or rejected")
    if review.get("reviewer") != f"independent-review-{job_id}":
        problems.append(f"verdict file: reviewer must be independent-review-{job_id}")
    if review.get("status") == "accepted" and int(review.get("blocking") or 0) > 0:
        problems.append("verdict file: accepted with blocking findings")
    conditional = review.get("conditional_gaps")
    if conditional is not None:
        if not isinstance(conditional, list) or not all(isinstance(g, str) for g in conditional):
            problems.append("verdict file: conditional_gaps must be a list of gap slugs")
        elif conditional and review.get("status") != "accepted":
            problems.append("verdict file: conditional_gaps belong to an acceptance only")
        elif declared is not None and set(conditional) - declared:
            problems.append("verdict file: conditional_gaps names gaps the plan does not declare: "
                            + ", ".join(sorted(set(conditional) - declared)))
    return problems


def declared_slugs(pkg) -> set[str] | None:
    """The gap slugs a roadmap's design/plan.json declares, or None when it cannot be read."""
    try:
        plan = json.loads((pkg / "design" / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {str(g.get("slug") or g.get("id")) for g in plan.get("gaps", []) if isinstance(g, dict)} \
        if isinstance(plan, dict) else None


def review_summary(what: str, verdict: dict, report: str) -> str:
    """`review of the Name package, round 2 at abc1234: needs changes, 5 findings (2 P1/P2)`: the
    findings are the report's `## R01 (P1)` blocks, else the verdict file's blocking count."""
    findings, _rest = prreviews.report_findings(report)
    blocking = sum(1 for severity, _t, _b in findings if severity in ("P1", "P2"))
    if not findings:
        blocking = int(verdict.get("blocking") or 0)
    status = str(verdict.get("status") or "?").replace("_", " ")
    tally = f"{count(len(findings), 'finding')} ({blocking} P1/P2)" if findings else f"{blocking} blocking"
    return f"{what}: {status}, {tally}"


class ReviewLane(Lane):
    kind = "review"
    prompt = "review"
    manifest_add_only = True

    def subject(self, job: Job) -> str:
        return "design" if board.parse_job_id(job.id).subject == "DESIGN" else "package"

    def elaborates(self, job: Job) -> bool:
        return self.subject(job) == "package"       # the reviewer elaborates with the brief's command

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        ws = super().prepare(job, ctx)
        if self.board_pr(job):
            checkout_board_pr(ctx, ws, self.board_pr(job))   # the design or package under review is its head
        if self.elaborates(job):
            build_tauceti_imports(ctx, ws, job.name)         # the reviewer's `lake env lean` finds their object files
        if self.subject(job) == "design":
            # The dependency check runs on the design's README: the package README does not exist yet.
            ws.extra["readme"] = ws.package(ctx.cfg, job.name) / "design" / "README.md"
        return ws

    def comment_body(self, job: Job, ctx: Context, ws: Workspace) -> str:
        try:
            review = json.loads(self.verdict_file(job, ctx, ws).read_text(encoding="utf-8"))["review"]
            status, conditional = review["status"], review.get("conditional_gaps") or []
        except (OSError, ValueError, KeyError, TypeError):
            status, conditional = "needs_changes", []
        return prreviews.render(prreviews.review_head(ctx.agent, ws.extra["head"]), status,
                                ws.report.read_text(encoding="utf-8"), self.marker(job, ctx, ws),
                                conditional if status == "accepted" and isinstance(conditional, list) else None)

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        try:
            verdict = json.loads(self.verdict_file(job, ctx, ws).read_text(encoding="utf-8"))["review"]
            report = ws.report.read_text(encoding="utf-8") if ws.report.is_file() else ""
        except (OSError, ValueError, KeyError, TypeError):
            return ""
        at = f" at {ws.extra['head'][:7]}" if self.board_pr(job) and ws.extra.get("head") else ""
        what = f"review of the {job.name} {self.subject(job)}, round {job.round}{at}"
        return review_summary(what, verdict if isinstance(verdict, dict) else {}, report)

    def submit_on_board_pr(self, job: Job, ctx: Context, ws: Workspace, summary: str) -> str:
        """The review as a comment on the pull request, in the shape the board reads."""
        body = self.comment_body(job, ctx, ws)
        if not ctx.dry_run:
            self.after_board_submission(job, ctx)
        return self.comment_on_board_pr(job, ctx, ws, body)

    def files(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        pkg = ws.package(ctx.cfg, job.name)
        if self.subject(job) == "design":
            return [str(pkg / "design" / "README.md"), str(pkg / "design" / "plan.json"), str(pkg / "sources.toml")]
        return [str(pkg / f) for f in ("README.md", "Suggested.lean", "metadata.toml", "sources.toml")]

    def verdict_file(self, job: Job, ctx: Context, ws: Workspace):
        pkg = ws.package(ctx.cfg, job.name)
        return pkg / "design" / "review.json" if self.subject(job) == "design" else pkg / "review.json"

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        pkg = ws.package(ctx.cfg, job.name)
        readme = pkg / ("design/README.md" if self.subject(job) == "design" else "README.md")
        items = sample_items(readme.read_text(encoding="utf-8")) if readme.is_file() else []
        sample = seeded_sample(items, SAMPLE_SIZE, job.id)
        j = board.parse_job_id(job.id)
        previous = ""
        if j.round > 1:
            prev_id = board.format_job_id(board.JobId("REV", j.name, j.round - 1, j.subject))
            fix_id = board.format_job_id(board.JobId("FIX", j.name, j.round, j.subject))
            previous = (f"`{pkg / 'reports' / (prev_id + '.md')}` (its repair report: "
                        f"`{pkg / 'reports' / (fix_id + '.md')}`)")
        return {"SUBJECT": self.subject(job), "FILES": "\n".join(f"- `{f}`" for f in self.files(job, ctx, ws)),
                "SAMPLE": "; ".join(f"“{s}”" for s in sample) or f"a random sample of {SAMPLE_SIZE} targets (none found by heading)",
                "VERDICT_FILE": str(self.verdict_file(job, ctx, ws)), "PREVIOUS": previous or "none"}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        vf = self.verdict_file(job, ctx, ws)
        try:
            problems += verdict_problems(json.loads(vf.read_text(encoding="utf-8")), job.id,
                                         declared_slugs(ws.package(ctx.cfg, job.name)))
        except (OSError, ValueError) as exc:
            problems.append(f"verdict file: {exc}")
        if ws.report.is_file() and not ws.report.read_text(encoding="utf-8").startswith(f"# Review of {job.name}"):
            problems.append("report: first line must be '# Review of <Name> (...)'")
        manifest = board.manifest_path(ctx.cfg.board.packages_dir, job.name)
        changed = gitops.changed_files(ws.board)
        touched = [p for p in changed if str(ws.board / p) in self.files(job, ctx, ws) and p != manifest]
        if touched:
            problems.append("a review changes none of the reviewed files: " + ", ".join(touched))
        if manifest in changed:
            problems += manifest_record_problems(ctx, ws, manifest)
        if self.board_pr(job) and not problems and ws.report.is_file():
            problems += self.comment_problems(self.comment_body(job, ctx, ws))
        return problems


def manifest_record_problems(ctx: Context, ws: Workspace, manifest: str) -> list[str]:
    """A review records in the sources manifest the sources it fetched that the manifest lacked
    (its fetches run with `--add-only`): it never removes or rewrites an entry the author recorded."""
    path = ws.board / manifest
    if not path.is_file():
        return ["a review keeps the sources manifest: sources.toml was deleted"]
    problems = manifest_problems(ctx, path)
    if problems:
        return problems
    try:
        before = tomllib.loads(gitops.git(ws.board, "show", f"HEAD:{manifest}", check=False))
    except tomllib.TOMLDecodeError:
        before = {}
    after = tomllib.loads(path.read_text(encoding="utf-8"))
    return [f"sources.toml: a review keeps the author's entries: {p}" for p in sources.altered_entries(before, after)]
