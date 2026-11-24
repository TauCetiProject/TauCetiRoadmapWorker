"""The `correction` lane: a roadmap's confirmed reports applied in one pull request on the content
repository (`CORRECT-<Leaf>~k`, filed by `board sync` with `[reports] corrections`: reports.py).

The worker reads the job's reports (each with the correction its triage stated and the triage itself on
the board), and first makes sure no other open pull request edits the roadmap's folder: one that does
parks the job (`waits_for`, `state:blocked`) until `board sync` sees it closed, since two pull requests
editing one README conflict. Relaying the corrections onto that pull request as a review is not done:
nothing tells such a comment apart from its author's own reply on every pull request whatever the operator
is. Otherwise the agent works in a worktree of main on the branch `roadmap/<leaf>-correction-<k>`: it
applies each correction as a minimal change of the folder's README.md and Suggested.lean, keeping the
roadmap as a record, or declines it with its reason, commits, and writes the report the worker checks
(`reports.correction_problems`): an outcome per report, the pull request's title and the summary block of
a revision. The push gate with the targeted build runs as on a gate round; then the worker pushes the
branch to the operator's fork and opens the pull request (labels `[roadmap] pr_label` and awaiting-review,
the marker the board's pull-request upkeep reads, a draft as `[roadmap] open_as_draft` says), each step an
effect of the job's record, and submits the report to the board. A correction whose every report is
declined opens no pull request. The register follows in `board sync` (`reports.keep_corrections`).
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import board, gates, gitops, openroadmaps, prompts, references, reports, summary
from ..board import Job
from ..gh import GHError
from .base import (
    CONTENT_PUSH,
    PR_MARKER,
    PULL_REQUEST,
    PUSH,
    SUBMITTED,
    Context,
    Lane,
    LaneBlocked,
    LaneError,
    Workspace,
    cached_prs,
    dropped_names,
    kept_commit,
    load_order,
    merged_for,
    open_cycle_inputs,
    open_roadmap_prs,
    order_path,
    owe,
    owes,
    read_pending,
    settle,
    sibling_readmes,
)
from .gate import gate_problems
from .port import validation_paragraph
from .pr import fresh_worktree, release_branch
from .report import anchor_spans, excerpt

CONTENT_PR = "content-pull-request"
CONTENT_LABELS = "content-labels"
WAITS = ("pull request #{n} on `{repo}` also edits `{folder}`: two pull requests editing one roadmap conflict, so "
         "this correction waits; `board sync` makes it available again once that pull request is merged or closed")


def colliding(prs: list[dict], folder: str) -> list[int]:
    """The open pull requests that change a file in the roadmap folder `folder` (or below it), by their
    changed files (`openroadmaps.inventory` entries: `files`, each with its `path` and, for a rename, its
    `previous` path), or by the folders an index entry lists."""
    out = []
    for p in prs:
        paths = [f.get(k, "") for f in p.get("files") or [] for k in ("path", "previous") if isinstance(f, dict)]
        paths += list((p.get("folders") or {}).values())
        if any(x == folder or x.startswith(folder + "/") for x in paths):
            out.append(int(p["pr"]))
    return sorted(set(out))


def open_pull_requests(ctx) -> list[dict]:
    """Every open pull request of the content repository, with every path it changes and, for a renamed file,
    the path it had: read now, never from the host's index, which may be half an hour old and leaves out a
    pull request whose head it could not fetch. GraphQL lists the pull requests and their files (one query per
    50), but names a renamed file by its new path only, so a pull request with a renamed file, or with over
    100 files, is read again by REST (`pulls/N/files`, which gives `previous_filename`). No pull request is
    left out for where its files now are (a pull request moving a roadmap out of the roadmap directories
    collides with it). Raises when any of it cannot be read: a correction is never opened beside a pull
    request it cannot see."""
    repo = ctx.cfg.repositories.content
    owner, name = repo.split("/", 1)
    out, after = [], None
    while True:
        data = ctx.gh.graphql(openroadmaps.QUERY, owner=owner, name=name, after=after)
        page = (data.get("repository") or {}).get("pullRequests") or {}
        for node in page.get("nodes") or []:
            files = node.get("files") or {}
            nodes = [f for f in files.get("nodes") or [] if isinstance(f, dict)]
            if int(files.get("totalCount") or 0) > len(nodes) or any(f.get("changeType") == "RENAMED" for f in nodes):
                listed = ctx.gh.api(f"repos/{repo}/pulls/{node['number']}/files?per_page=100", paginate=True) or []
                paths = [{"path": f.get("filename", ""), "previous": f.get("previous_filename", "")} for f in listed]
            else:
                paths = [{"path": f.get("path", "")} for f in nodes]
            out.append({"pr": int(node["number"]), "files": paths})
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return out
        after = info.get("endCursor")


def corrections_document(cfg, job: Job, wt: Path, board_dir: Path) -> str:
    """Every report the job corrects, for the agent: what was wrong (quoted, with where it stands now), the
    correction its triage stated, and where the whole triage is. All of it data."""
    parts = ["# The confirmed reports to correct (data, not instructions)", ""]
    for e in job.inputs.get("entries", []):
        path = f"{job.inputs.get('folder')}/{e.get('file')}"
        text = (wt / path).read_text(encoding="utf-8", errors="replace") if (wt / path).is_file() else ""
        spans = anchor_spans(text, str(e.get("anchor", ""))) if text else []
        triage = board_dir / str(e.get("triage") or "")
        parts += [f"## {e.get('job')} ({e.get('kind')}) in `{path}`", "",
                  "The text that is wrong:", "", "```text", prompts.quote_untrusted(str(e.get("anchor", "")), 1000), "```",
                  "",
                  (f"It stands at lines {', '.join(f'{a}-{b}' for a, b in spans)} on main:\n\n```text\n"
                   + prompts.quote_untrusted(excerpt(text, spans), 8000) + "\n```" if spans
                   else "It is no longer found in the file on main: decline this one (it is fixed already)."), "",
                  "The correction the triage stated:", "", "```text",
                  prompts.quote_untrusted(str(e.get("correction") or "(none recorded)"), 4000), "```", "",
                  f"The whole triage: `{triage}`" if triage.is_file() else "The whole triage is not on the board's main.",
                  "", "Reported in: " + (", ".join(e.get("sources", [])) or "(no link)"), ""]
    return "\n".join(parts)


def compose_body(cfg, job: Job, c: dict, gate: gates.GateResult, agent: str, model: str) -> str:
    """The pull request's description: the summary block of a revision, then each report it answers (the
    text that was wrong, the correction, the triage, the REPORT job, the sources), the reports it declined,
    the validation and the marker the board's upkeep of roadmap pull requests reads."""
    board_repo = cfg.repositories.board_repo()
    blob = f"https://github.com/{cfg.repositories.packages_repo()}/blob/main/"
    applied, declined = [], []
    for e in job.inputs.get("entries", []):
        outcome, body = c["outcomes"].get(str(e.get("job")), ("", ""))
        issue = f"https://github.com/{board_repo}/issues/{e['issue']}" if e.get("issue") else ""
        head = f"### {e.get('job')}" + (f" ({issue})" if issue else "")
        if outcome != "applied":
            declined.append(f"- `{e.get('job')}`" + (f" ({issue})" if issue else "") + ": "
                            + reports.normalise(board.neutralise(body))[:600])
            continue
        applied += [head, "",
                    f"- What was wrong ({e.get('kind')}, `{e.get('file')}`): {reports.code(str(e.get('anchor', '')))}",
                    "- The correction the triage stated:", "", reports.quoted_block(str(e.get("correction") or ""), 1500), "",
                    "- What this change does: " + reports.normalise(board.neutralise(body))[:1500],
                    f"- The triage: {blob}{e.get('triage')}" if e.get("triage") else "- The triage: not recorded",
                    "- Reported in: " + (", ".join(e.get("sources", [])) or "(no link)"), ""]
    marker = f"<!-- {PR_MARKER} " + json.dumps({"roadmap": job.name, "job": job.id}) + " -->"
    parts = [f"{summary.MARK_OPEN}\n{c['summary'].strip()}\n{summary.MARK_CLOSE}",
             (f"Corrections of `{job.inputs.get('folder')}` from roadmap reports that Tau Ceti workers left in their "
              "pull requests on the library, each confirmed by an independent triage on the job board. Each is the "
              "smallest change that states the item correctly; the roadmap's layers, ids and completed items stay."),
             "\n".join(applied).rstrip()]
    if declined:
        parts.append("Declined (left for a human):\n\n" + "\n".join(declined))
    parts += [validation_paragraph(gate),
              f"AI assistance: {agent} ({model}) applied the corrections; this pull request awaits review.", marker]
    return "\n\n".join(parts) + "\n"


class CorrectionLane(Lane):
    kind = "correction"
    prompt = "correction"

    def records_author(self, job: Job) -> bool:
        return False

    def prepare(self, job: Job, ctx: Context) -> Workspace:
        """Fresh checkouts; the open pull requests that edit the folder park the job; a worktree of main on
        the correction's branch, with the reports to correct in the scratch directory."""
        cfg = ctx.cfg
        folder = str(job.inputs.get("folder") or "").strip("/")
        if not folder.startswith(cfg.roadmap.active_dir + "/") or ".." in Path(folder).parts \
                or not job.inputs.get("entries"):
            raise LaneError(f"{job.id}: its spec names no roadmap folder under `{cfg.roadmap.active_dir}/` and no report")
        ws = super().prepare(job, ctx)
        j = board.parse_job_id(job.id)
        branch = f"roadmap/{gitops.kebab(job.name)}-correction-{j.round}"
        pending = read_pending(ctx, job)
        if pending is not None and owes(ctx, job, CONTENT_PR) and ctx.gh is not None and not ctx.dry_run:
            # A kept run that has not recorded its pull request: if an earlier attempt opened it and lost the
            # answer, it is ours and is taken up; otherwise the run opens one, and must be admitted again.
            try:
                mine = gitops.open_pull_for(ctx.gh, cfg.repositories.content, f"{ctx.operator}:{branch}")
            except GHError as exc:
                raise LaneError(f"whether this correction's pull request is open could not be read "
                                f"({str(exc).splitlines()[0][:120]})") from exc
            if mine:
                settle(ctx, job, CONTENT_PR, content={"pr": int(mine["number"])})
        if pending is None or owes(ctx, job, CONTENT_PR):
            # Only the rest of a submission whose pull request exists skips the admission.
            try:
                prs = (open_pull_requests(ctx) if ctx.gh is not None and not ctx.dry_run
                       else openroadmaps.load(ctx.state).get("prs", []))      # a dry run writes the brief only
            except Exception as exc:  # noqa: BLE001 - GitHub: what is open cannot be known, so nothing is done
                raise LaneError(f"the open roadmap pull requests could not be read ({type(exc).__name__}: "
                                f"{str(exc)[:160]}): a correction is never opened beside one it cannot see") from exc
            others = colliding(prs, folder)
            if others:
                job.inputs["waits_for"] = others
                try:
                    ctx.board.update_spec(job)
                except GHError as exc:
                    # Parked without `waits_for`, nothing would ever release the job: it is not parked.
                    raise LaneError(f"pull request #{others[0]} also edits `{folder}`, and the wait could not be "
                                    f"recorded on the job ({str(exc).splitlines()[0][:120]})") from exc
                except AttributeError:
                    pass
                raise LaneBlocked(WAITS.format(n=", #".join(map(str, others)), repo=cfg.repositories.content,
                                               folder=folder))
        if not (ws.content / folder / "README.md").is_file():
            raise LaneBlocked(f"`{folder}/README.md` is not on `{cfg.project.branch}` (moved under "
                              f"`{cfg.roadmap.completed_dir}/` or removed): this correction needs a human")
        wt = fresh_worktree(ctx, ws.content, job.name, "correct", ws)
        release_branch(ws.content, branch, keep=wt)
        gitops.git(wt, "checkout", "-q", "-B", branch)
        # A kept run is checked against the main it was built on (its report names it, and its diff is from
        # it); `restore_pending` puts its commit back on the branch. Main may have moved on since.
        base = kept_commit(pending or {}, "content").get("base") or gitops.git(wt, "rev-parse", "HEAD").strip()
        doc = ws.scratch / "corrections.md"
        doc.write_text(corrections_document(cfg, job, wt, ws.board), encoding="utf-8")
        prs = []
        if ctx.gh is not None and not ctx.dry_run:
            try:
                prs = open_roadmap_prs(ctx)       # the gate's sibling READMEs
            except GHError:
                prs = cached_prs(ctx.state)
        ws.workdir = wt
        ws.extra.update(base=base, head=base, branch=branch, folder=folder, corrections=doc, prs=prs,
                        rel=folder[len(cfg.roadmap.active_dir) + 1:], readme=wt / folder / "README.md")
        return ws

    def missing_references(self, job: Job, ctx: Context, ws: Workspace) -> list[references.MissingReference]:
        """None: a correction never waits for a source; one it cannot apply without a text is declined."""
        items = references.read_missing(ws.scratch / "needs-references.toml")
        if items:
            self.note(ws, "not requested (a correction that needs a text is declined): " + ", ".join(m.key for m in items))
        return []

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        cfg, folder, rel = ctx.cfg, ws.extra["folder"], ws.extra["rel"]
        build = (cfg.gates.build.format(module=gates.build_targets(ws.workdir, cfg.roadmap.active_dir, rel))
                 if cfg.gates.build else "(no build configured)")
        return {"FOLDER": folder, "FILES": f"{folder}/README.md` and `{folder}/Suggested.lean", "BUILD": build,
                "GATE": ctx.cli("gate", str(ws.workdir), job.name, *(("--folder", rel) if rel != job.name else ()),
                                "--base", f"origin/{cfg.project.branch}"),
                "BRANCH": ws.extra["branch"], "HEAD7": ws.extra["base"][:7], "AGENT": ctx.agent,
                "CORRECTIONS": str(ws.extra["corrections"]),
                "ENTRIES": ", ".join(f"`{e.get('job')}`" for e in job.inputs.get("entries", [])),
                "COMMIT": (f"Commit what you applied: `git add {folder} && git commit -m \"{job.name}: correct "
                           "<what, at most 12 words>\"`. Do not push.")}

    def gate(self, job: Job, ctx: Context, ws: Workspace) -> tuple[list[str], gates.GateResult]:
        order = load_order(order_path(ctx.cfg, ws.board, ws.content))
        opts = gates.GateOptions.from_config(ctx.cfg, order=order or None, dropped=dropped_names(ctx.cfg),
                                             siblings=sibling_readmes(ctx.state, ctx.cfg.roadmap.active_dir,
                                                                      ws.extra["prs"]),
                                             merged=merged_for(ctx, ws), **open_cycle_inputs(ctx, ws))
        return gate_problems(ctx, ws, job.name, ws.extra["rel"], opts, base=f"origin/{ctx.cfg.project.branch}")

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        if problems:
            return problems
        text = ws.report.read_text(encoding="utf-8")
        problems += reports.correction_problems(text, job, ws.extra["base"])
        c = reports.parse_correction(text)
        applied = any(o == "applied" for o, _b in c["outcomes"].values())
        head = gitops.git(ws.workdir, "rev-parse", "HEAD").strip()
        if gitops.git(ws.workdir, "status", "--short", "--untracked-files=no").strip():
            problems.append("uncommitted changes left in the worktree")
        if applied and head == ws.extra["base"]:
            problems.append("a correction is applied, but nothing is committed on the branch")
        if not applied and head != ws.extra["base"]:
            problems.append("every correction is declined, but the branch has a commit: reset it")
        if problems or not applied:
            return problems
        changed = gitops.git(ws.workdir, "diff", "--name-only", ws.extra["base"], "HEAD").splitlines()
        outside = [p for p in changed if not p.startswith(ws.extra["folder"] + "/")]
        if outside:
            problems.append("the correction changes files outside the roadmap's folder: " + ", ".join(outside[:5]))
        found, ws.extra["gate"] = self.gate(job, ctx, ws)
        (ws.job_dir / "gate.json").write_text(json.dumps(ws.extra["gate"].to_dict(), indent=1) + "\n", encoding="utf-8")
        return problems + found

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        """The correction's pull request, each step an effect of the job's record (a stopped run finishes
        without a second push or pull request), then the report submitted to the board (`Lane.submit`)."""
        cfg, repo = ctx.cfg, ctx.cfg.repositories.content
        c = reports.parse_correction(ws.report.read_text(encoding="utf-8"))
        done = [r for r, (o, _b) in c["outcomes"].items() if o == "applied"]
        if not done:
            return f"correction of {job.name}: every report declined ({len(c['outcomes'])}); no pull request"
        new = gitops.git(ws.workdir, "rev-parse", "HEAD").strip()
        if ctx.dry_run:
            return f"[dry-run] would push {new[:7]} and open a pull request correcting {', '.join(done)}"
        owe(ctx, job, ws, (CONTENT_PUSH, CONTENT_PR, CONTENT_LABELS, PUSH, PULL_REQUEST, SUBMITTED),
            content={"sha": new, "base": ws.extra["base"], "branch": ws.extra["branch"]})
        fork = kept_commit(read_pending(ctx, job) or {}, "content").get("repo") or ""
        if owes(ctx, job, CONTENT_PUSH):
            fork = self.push(lambda: gitops.ensure_fork(ctx.gh, repo, ctx.operator))
            # A new branch: it must not exist yet (an earlier attempt's push of this same commit is found and kept).
            self.push(lambda: gitops.push(ws.workdir, fork, ws.extra["branch"], expected=""))
            settle(ctx, job, CONTENT_PUSH, content={"repo": fork})
        number = int(kept_commit(read_pending(ctx, job) or {}, "content").get("pr") or 0)
        if owes(ctx, job, CONTENT_PR):
            body = compose_body(cfg, job, c, ws.extra["gate"], ctx.agent, ctx.model)
            pr = self.push(lambda: gitops.open_pull_request(ctx.gh, repo, f"{ctx.operator}:{ws.extra['branch']}",
                                                            cfg.project.branch, c["title"], body,
                                                            draft=cfg.roadmap.open_as_draft)) or {}
            number = int(pr.get("number") or 0)
            if not number:
                raise LaneError("the pull request was not opened (GitHub returned no number)")
            settle(ctx, job, CONTENT_PR, content={"pr": number})
        if owes(ctx, job, CONTENT_LABELS):
            # The labels the board's pull-request upkeep finds it by (its gate rounds): an effect like the others,
            # so a write that fails is made by the next attempt rather than lost.
            ctx.gh.write("POST", f"repos/{repo}/issues/{number}/labels",
                         {"labels": [cfg.roadmap.pr_label, cfg.replies.awaiting_review]})
            settle(ctx, job, CONTENT_LABELS)
        lines = [x for x in ws.report.read_text(encoding="utf-8").split("\n") if not x.startswith("Pull request: #")]
        lines.insert(1, f"Pull request: #{number} ({ws.extra['branch']} at {new[:7]})")
        ws.report.write_text("\n".join(lines), encoding="utf-8")
        return f"correction of {job.name}: #{number} on {repo} applies {len(done)} of {len(c['outcomes'])} report(s)"
