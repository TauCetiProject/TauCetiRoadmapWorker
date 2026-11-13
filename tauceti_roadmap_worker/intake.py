"""Board maintenance, run by the board's bot or a maintainer with write access (`board ...` commands).

* `intake(pr)`: take in one submission pull request on the packages repository (or an explorer
  import pull request: see explorer.py). It is merged only
  when its author and the job admit it (`admission_problems`), it delivers what its kind must, with
  an outcome its successors can follow (`deliverable_problems`), it names an open job, adds or changes only that job's deliverables as regular files
  (`submission_files`), contains no local path, has valid JSON/TOML, its checks passed, and (for a
  job with `independent_of`) its author did none of the listed jobs; the merge names the head checked. After the merge the job is done, the board records "Intake: merged #N from
  @login" (the authorship record the separation check reads), and the job's successors are created.
  Port of the explorer's intake.py merge path.
* `sync()`: target issues become TARGET jobs; an issue carrying several `state:` labels keeps one
  (the board's invariant: exactly one); live leases are mirrored as `state:claimed`;
  roadmap pull requests get their awaiting-author / awaiting-review labels, and a GATE job is
  created for the branch owner when humans are waiting for a reply, except while a review of a pool
  author's pull request is claimed (it repairs the pull request itself: `repairing_review`); held
  pull requests stay drafts;
  with `[prreview] enabled`, every roadmap folder an open pull request adds or edits gets a PRREVIEW
  job per new head, and the pull request carries the AI-approved label exactly while an accepted AI
  review of its head covers every one of those folders (`sync_pr_reviews`).
* `replace_upward_roadmaps()`: every roadmap the merge order on the board's main lists above a
  supplier its board README names is moved right after the last of them (`board intake`'s last step;
  idempotent), as the merge of a package whose port follows does for that package.
* `stale(release)`: claims whose lease lapsed and whose issue shows nothing for `stale_hours`, and
  submissions whose pull request was closed unmerged, go back to `state:available`.
* `board_lock` / `take_lock`: one writing board run at a time, whoever runs it (the hosted workflow
  and a maintainer's shell share no filesystem): a lease ref in the board repository, which both can
  push, renewed while the run lasts; `repair_duplicates`: a job created twice (both open) keeps
  the twin that got furthest (its submission, a worker's claim, else the older issue) and the other
  takes nothing with it.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import socket
import subprocess
from dataclasses import replace
from urllib.parse import quote

from . import (
    board,
    boardpr,
    claims,
    depgraph,
    explorer,
    filers,
    gates,
    gitops,
    openroadmaps,
    overlaps,
    prbatch,
    prreviews,
    replies,
    summary,
)
from .board import Job, Outcome
from .gh import GHError, transient
from .lanes.base import open_roadmap_prs, pr_answerers, sibling_readmes
from .lanes.port import pr_number_from_report
from .lanes.pr import events_from_api
from .lanes.target import parse_triage

PENDING = ("queued", "in_progress", "waiting", "requested", "pending")
# The board workflow's own job: its `pull_request_target` run on a submission IS the intake (it never
# runs the submission's code), so its outcome says nothing about the submission. It is cancelled
# whenever a newer run joins the `board` concurrency group while it waits for a runner, and it fails
# whenever some unrelated step of that intake hits a GitHub error, so it is not a check on the pull request.
OWN_CHECKS = frozenset({"board"})
# A cancelled run never judged the commit.
NOT_A_VERDICT = (None, "success", "skipped", "neutral", "cancelled")


def _checks(ctx, repo: str, sha: str) -> tuple[list[str], bool]:
    """(failed check names, any still running) for a commit, the board's own workflow job left out."""
    runs = (ctx.gh.api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs", [])
    latest = {}
    for run in runs:
        if run["name"] in OWN_CHECKS:
            continue
        if run["name"] not in latest or (run.get("started_at") or "") >= (latest[run["name"]].get("started_at") or ""):
            latest[run["name"]] = run
    failed = [n for n, r in latest.items() if r.get("conclusion") not in NOT_A_VERDICT]
    running = any(r.get("status") in PENDING for r in latest.values())
    return failed, running


class HeadMoved(GHError):
    """A pull request's head changed while the intake read it: nothing read is used, and the next
    run (which catches a GHError per pull request) reads it again."""


def submission_files(ctx, repo: str, number: int, pr: dict) -> tuple[list[dict], list[str]]:
    """(the files a pull request adds or changes, why its change cannot be taken in as a whole), at
    the head `pr` names. The listing is read by pull request number, so the pull request is read
    again after it: a head that moved raises HeadMoved, and a listing shorter than its file count is
    refused. Deletions and renames are refused (`board.listing_problems`), and so is every path that
    is not a regular file at the head (`board.mode_problems`, from the head commit's tree)."""
    head = pr["head"]["sha"]
    files = ctx.gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True) or []
    now = ctx.gh.api(f"repos/{repo}/pulls/{number}") or {}
    if (now.get("head") or {}).get("sha") != head:
        raise HeadMoved(f"#{number}: the head moved from `{head[:7]}` while its files were read")
    problems = board.listing_problems(files)
    if isinstance(now.get("changed_files"), int) and now["changed_files"] != len(files):
        problems.append(f"the file listing is incomplete ({len(files)} of {now['changed_files']} files)")
    kept = [f for f in files if f.get("status") != "removed"]
    if kept:
        tree = ctx.gh.api(f"repos/{pr['head']['repo']['full_name']}/git/trees/{head}?recursive=1") or {}
        if tree.get("truncated"):
            problems.append(f"the tree of `{head[:7]}` is too large to read whole")
        modes = {e.get("path"): e.get("mode") for e in tree.get("tree", []) if isinstance(e, dict)}
        problems += board.mode_problems(modes, [f["filename"] for f in kept])
    return kept, problems


def outcome_for(ctx, job: Job, pr: dict, all_jobs: list[Job]) -> Outcome:
    """What the merged submission says about the job, read from the packages repository's main."""
    repo, pkg = ctx.cfg.repositories.packages_repo(), board.package_dir(ctx.cfg.board.packages_dir, job.name)

    def read(path: str) -> str:
        try:
            return ctx.gh.raw_file(repo, path)
        except GHError:
            return ""
    if job.kind == "review":
        art = board.artefact_of(job.id)
        verdict = read(f"{pkg}/design/review.json" if art == "DESIGN" else f"{pkg}/review.json")
        try:
            status = json.loads(verdict).get("review", {}).get("status", "")
        except ValueError:
            status = ""
        return Outcome(verdict=status)
    if job.kind == "target":
        t = parse_triage(read(f"{pkg}/target.md"))
        return Outcome(verdict=t["verdict"], name=t["name"], into=t["into"])
    if job.kind == "port":
        return Outcome(pr=pr_number_from_report(read(f"{pkg}/reports/{job.id}.md")), operator=pr["user"]["login"])
    if job.kind == "redteam":
        j = board.parse_job_id(job.id)
        passes = board.redteam_passes(j.name, j.round, len(ctx.cfg.redteam.passes))
        done = {x.id for x in all_jobs if x.state == "done"} | {job.id}
        return Outcome(round_complete=all(p in done for p in passes))
    return Outcome()


WRITE_ACCESS = ("OWNER", "MEMBER", "COLLABORATOR")


def admitted(ctx, job: Job, login: str, pr: dict, shared=None) -> bool:
    """Whether `login` may submit `job`: a login `[board] operators` (default: the running login) or
    `[board] bot_logins` lists, one with write access to the board, the login that labelled the job
    claimed (adding a label needs write access), or the operator of the job's lease or author record
    in the claims namespace (`shared`; writing there needs write access to it). An operator who works
    through a fork with none of these is admitted by listing the login in `[board] operators`."""
    cfg, who = ctx.cfg, login.lower()
    listed = {x.lower() for x in [*(cfg.board.operators or [getattr(ctx, "operator", "")]), *cfg.board.bot_logins] if x}
    if who in listed or pr.get("author_association") in WRITE_ACCESS:
        return True
    if ctx.board.labelled_by(job, "state:claimed").lower() == who:
        return True
    if not shared:
        return False
    try:
        lease = shared.read(claims.job_key(cfg.project.name, job.id)) or {}
        record = shared.recorded_authors([job.id]).get(job.id) or {}
    except (OSError, subprocess.SubprocessError):
        return False
    return who in {str(lease.get("operator", "")).lower(), str(record.get("operator", "")).lower()} - {""}


def admission_problems(ctx, job: Job, pr: dict, sub: dict, jobs: list[Job], shared=None) -> list[str]:
    """Why `pr` may not submit `job`, whatever its files hold: it is not against the board's main, its
    author is not admitted (`admitted`), its marker names another operator, the job is another
    operator's (a pool member's job is every member's) or needs another agent or model than the
    marker records, or the job follows a job that is not done."""
    login, out = pr["user"]["login"], []
    base = (pr.get("base") or {}).get("ref", "main")
    if base != "main":
        out.append(f"the pull request is against `{base}`, not `main`")
    if not admitted(ctx, job, login, pr, shared):
        out.append(f"`{login}` is not an operator of this board (not in `[board] operators`, no write access, "
                   f"no claim of {job.id})")
    if sub.get("operator") and sub["operator"].lower() != login.lower():
        out.append(f"the submission names operator `{sub['operator']}`, but `{login}` opened it")
    if job.requires_operator and job.requires_operator.lower() != login.lower():
        # A pool member acts for another on a managed roadmap pull request (`Lane.eligible` takes the
        # round on the same rule), so the pool's rounds are admitted from any member.
        pool = {p.lower() for p in ctx.cfg.roadmap.pool}
        if not (job.requires_operator.lower() in pool and login.lower() in pool):
            out.append(f"{job.id} is `{job.requires_operator}`'s (it owns the pull request branch)")
    for field, need in (("agent", job.requires_agent), ("model", job.requires_model)):
        if need and sub.get(field) != need:
            out.append(f"{job.id} needs {field} {need}, the submission records {sub.get(field) or 'none'}")
    done = {j.id for j in jobs if j.state == "done"}
    waiting = [a for a in job.after if a not in done]
    if waiting:
        out.append(f"{job.id} follows {', '.join(waiting)}, not done yet")
    return out


def deliverable_problems(ctx, job: Job, pr: dict, texts: dict[str, str], jobs: list[Job]) -> list[str]:
    """What a submission must deliver, read at its head: its report among the files it changes, the
    rest of its kind's deliverables (`board.required_paths`) there or already at the head, and an
    outcome the job's successors can follow: a triage `lanes.target` accepts (a merge-into naming a
    job on the board), a review verdict `lanes.review` accepts, a port report naming the roadmap
    pull request its author opened, a pull-request review's verdict naming the pull request and head
    its job names."""
    from .lanes.review import verdict_problems
    from .lanes.target import triage_problems
    pkg = board.package_dir(ctx.cfg.board.packages_dir, job.name)
    report, out = f"{pkg}/reports/{job.id}.md", []
    if report not in texts:
        out.append(f"no report ({report})")

    def read(path: str) -> str | None:
        if path in texts:
            return texts[path]
        try:
            return ctx.gh.raw_file(pr["head"]["repo"]["full_name"], path, pr["head"]["sha"])
        except GHError:
            return None
    found = {path: read(path) for path in sorted(board.required_paths(job, ctx.cfg.board.packages_dir) - {report})}
    out += [f"{path} is missing" for path, text in found.items() if text is None]
    if job.kind == "target" and found.get(f"{pkg}/target.md") is not None:
        text = found[f"{pkg}/target.md"]
        out += triage_problems(text)
        into = parse_triage(text)["into"]
        if parse_triage(text)["verdict"] == "merge-into" and into and not ctx.board.find(into, jobs):
            out.append(f"target.md: merge-into names {into}, which is not on the board")
    if job.kind == "review" and (verdict := next((t for p, t in found.items() if p.endswith("review.json")), None)):
        try:
            out += verdict_problems(json.loads(verdict), job.id)
        except ValueError:
            pass      # file_problems reports the invalid JSON
    if job.kind == "prreview" and (verdict := found.get(board.prreview_verdict_path(ctx.cfg.board.packages_dir, job))):
        from .lanes.prreview import prreview_verdict_problems
        try:
            out += prreview_verdict_problems(json.loads(verdict), job)
        except ValueError:
            pass
    if job.kind == "port" and report in texts:
        number = pr_number_from_report(texts[report])
        if not number:
            out.append(f"{report} names no roadmap pull request (`Pull request: #N`)")
        else:
            try:
                opener = ((ctx.gh.api(f"repos/{ctx.cfg.repositories.content}/pulls/{number}") or {}).get("user") or {})
            except GHError:
                opener = None
            if opener is None or opener.get("login", "").lower() != pr["user"]["login"].lower():
                out.append(f"{report} names #{number} on {ctx.cfg.repositories.content}, which `{pr['user']['login']}` "
                           "did not open")
    return out


def merge_base_text(ctx, repo: str, path: str, pr: dict) -> str:
    """A file as the pull request's merge base has it ('' when it has none): what the pull request
    changed it from, unlike main or the recorded base, which may hold later commits of others."""
    compared = ctx.gh.api(f"repos/{repo}/compare/{(pr.get('base') or {}).get('sha') or 'main'}...{pr['head']['sha']}") or {}
    sha = (compared.get("merge_base_commit") or {}).get("sha", "")
    if not sha:
        raise GHError(f"#{pr.get('number')}: no merge base with main")
    try:
        return ctx.gh.raw_file(repo, path, sha)
    except GHError:
        return ""


def intake(ctx, number: int, yes: bool = False, pr: dict | None = None, shared=None) -> str:
    """Take in one submission pull request. `pr`: its object from the open pull requests' listing
    (the same fields as a GET of it), so the intake reads it once for all of them; `shared`: the
    claims namespace, read when the author's admission rests on a claim (`admitted`)."""
    cfg, repo = ctx.cfg, ctx.cfg.repositories.packages_repo()
    pr = pr or ctx.gh.api(f"repos/{repo}/pulls/{number}")
    if boardpr.NEEDS_REVIEW in {label["name"] for label in pr.get("labels", [])}:
        return f"#{number}: under review on the board (read by the review scan)"
    sub = board.decode_submission(pr.get("body") or "")
    if not sub:
        found = explorer.decode_import(pr.get("body") or "")
        if found:
            return explorer.intake_import(ctx, number, pr, found, yes)
        return f"#{number}: not a submission (no marker); left for the maintainer"
    jobs = ctx.board.all_jobs()
    job = ctx.board.find(sub["job"], jobs)
    if not job:
        return f"#{number}: names unknown job {sub['job']}"
    named = board.submission_issue(pr.get("body") or "")
    if named and job.number != named:     # twins not yet repaired: the issue the worker claimed is the job's
        job = next((j for j in jobs if j.id == sub["job"] and j.number == named), job)
    problems = []
    if pr.get("draft"):
        return f"#{number}: a draft; the intake waits until it is ready"
    if pr.get("state") != "open":
        return f"#{number}: not open"
    if job.state in ("done", "blocked", "needs-references"):
        problems.append(f"{job.id} is {job.state}")
    problems += admission_problems(ctx, job, pr, sub, jobs, shared)
    files, listed = submission_files(ctx, repo, number, pr)
    problems += listed
    order = board.order_file(cfg)
    allowed, texts = board.allowed_paths(job, cfg.board.packages_dir, order), {}
    for f in files:
        text = ctx.gh.raw_file(pr["head"]["repo"]["full_name"], f["filename"], pr["head"]["sha"]) \
            if f["filename"] in allowed else ""
        problems += board.file_problems(f["filename"], text, allowed)
        if f["filename"] in allowed:
            texts[f["filename"]] = text
        if f["filename"].endswith("/design/plan.json") and board.gaps_only(job):
            problems += board.plan_change_problems(f["filename"], base_text(ctx, repo, f["filename"], pr), text)
        if order and f["filename"] == order:
            problems += board.order_change_problems(order, merge_base_text(ctx, repo, order, pr), text, {job.name})
    problems += deliverable_problems(ctx, job, pr, texts, jobs)
    author = pr["user"]["login"]
    if job.independent_of:
        problems += board.separation_problems(job, author, ctx.board.authors(job.independent_of, jobs),
                                              cfg.review.separation, sub.get("agent", ""), sub.get("model", ""),
                                              cfg.redteam.independent_of_fix_rounds,
                                              cfg.review.independent_of_earlier_fix_rounds)
    failed, running = _checks(ctx, repo, pr["head"]["sha"])
    if running:
        return f"#{number}: checks still running"
    if failed:
        problems.append("checks failed: " + ", ".join(failed))
    if problems:
        note = ("Intake: left for the maintainer, because " + "; ".join(problems) + ". Nothing was merged. "
                "A submission changes only its job's deliverables and report.")
        if yes:
            ctx.gh.write("POST", f"repos/{repo}/issues/{number}/comments", {"body": note})
        return f"#{number}: not merged: " + "; ".join(problems)
    if board.artefact_of(job.id) and job.kind != "review":
        # A design or package is reviewed (and a package red-teamed) on its open pull request.
        return boardpr.submission_under_review(ctx, number, pr, sub, job, jobs, yes)
    if not yes:
        return f"#{number}: would merge {job.id} (dry run; pass --yes)"
    if job.state != "submitted":
        # `submitted` before the merge is what `reconcile_merged` finishes from.
        ctx.board.set_state(job, "submitted", f"Submitted: #{number} is being merged (this issue said `state:{job.state}`).")
    try:
        ctx.gh.write("PUT", f"repos/{repo}/pulls/{number}/merge", {"merge_method": "squash", "sha": pr["head"]["sha"]})
    except GHError as exc:
        # Two rounds on one roadmap each change its sources.toml: the second to merge conflicts with
        # main and GitHub never merges it, so its job stayed `submitted` for good. It is squashed onto
        # main in a checkout, the manifest merged by key, as an accepted board pull request is.
        if "conflict" not in str(exc).lower() and not boardpr.conflicts_with_main(ctx, number, pr):
            raise
        done, detail = boardpr.squash_with_main(ctx, number, pr)
        if not done:
            return f"#{number}: not merged: {detail}"
    ctx.board.comment(job.number, merged_note(number, author, sub))
    return f"#{number}: merged {job.id}; " + complete_merged(ctx, job, pr, jobs)


def merged_note(number: int, author: str, sub: dict) -> str:
    """The authorship record of a merged submission (board.INTAKE_NOTE reads it)."""
    return f"Intake: merged #{number} from `{author}` ({sub.get('agent') or '?'}, {sub.get('model') or '?'})."


def complete_merged(ctx, job: Job, pr: dict, jobs: list[Job]) -> str:
    """What follows a submission's merge: its successors, then the job done and its issue closed. The
    job becomes done last, so a run stopped on the way leaves it unfinished with its pull request
    merged, and `reconcile_merged` finishes it (the board creates no job twice)."""
    note = advance(ctx, job, outcome_for(ctx, job, pr, jobs), jobs)
    ctx.board.set_state(job, "done")
    ctx.gh.write("PATCH", f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}", {"state": "closed"})
    return note


RECONCILE_WINDOW = 50   # the most recently closed pull requests a run looks through


def reconcile_merged(ctx, yes: bool) -> list[str]:
    """Merged submissions whose job is still `submitted`, finished as the intake would have: a run
    stopped between a merge and the end of its transition (`complete_merged`), or a maintainer who
    merged a submission by hand. A job in any other state is left alone (a human may have reopened
    it). Their authorship record is written when the job's issue lacks it. A design or package (whose
    review followed its submission) is only marked done. One listing of the most recently closed
    pull requests; a job the board already finished costs nothing more."""
    repo = ctx.cfg.repositories.packages_repo()
    pulls = ctx.gh.api(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={RECONCILE_WINDOW}") or []
    out, jobs = [], None
    for pr in pulls:
        sub = board.decode_submission(pr.get("body") or "") if pr.get("merged_at") else None
        if not sub:
            continue
        jobs = jobs if jobs is not None else ctx.board.all_jobs()
        named = board.submission_issue(pr.get("body") or "")
        job = next((j for j in jobs if j.id == sub["job"] and (not named or j.number == named)), None) \
            or ctx.board.find(sub["job"], jobs)
        if not job or job.state != "submitted":
            continue
        number, author = int(pr["number"]), pr["user"]["login"]
        if not yes:
            out.append(f"#{number}: merged, but {job.id} is {job.state}; would finish it (dry run)")
            continue
        if not any(m and int(m.group(1)) == number for m in (board.INTAKE_NOTE.match(c.get("body") or "")
                                                               for c in ctx.board.comments(job.number))):
            ctx.board.comment(job.number, merged_note(number, author, sub))
        if board.artefact_of(job.id) and job.kind != "review":
            boardpr.finish_job(ctx, job, f"Intake: #{number} was merged.")
            out.append(f"#{number}: merged; {job.id} done")
            continue
        out.append(f"#{number}: merged; finished {job.id}; " + complete_merged(ctx, job, pr, jobs))
        jobs = ctx.board.all_jobs()
    return out


_norm = board.title_key


def stuck_findings(repair_report: str, review: str) -> list[str]:
    """A review's blocking findings, when every one of them re-raises a finding the round's repair
    marked `not actionable in this package` (by `Re-raised: <id>` in the finding, an id that names
    exactly one of the repair's findings as `replies.names` reads it, or the same title); else [].
    Another repair round could not change them: a human decides."""
    settled = replies.not_actionable(repair_report)
    if not settled:
        return []
    ids = [fid for fid, _how in replies.dispositions(repair_report)]
    by_title = {_norm(t): fid for fid, (t, _r) in settled.items() if _norm(t)}
    blocking = list(prreviews.findings_with_bodies(review))     # every finding asks for a change
    out = []
    for fid, severity, title, body in blocking:
        named = [replies.names(r, ids) for r in prreviews.reraised(title, body)]
        hit = sorted({n[0] for n in named if len(n) == 1 and n[0] in settled}) or (
            [by_title[_norm(title)]] if _norm(title) in by_title else [])
        if not hit:
            return []
        out.append(f"`{fid}` ({severity}) {title}: re-raises `{hit[0]}`, which the repair marked not actionable "
                   f"({settled[hit[0]][1] or 'no reason given'})")
    return out


def stuck_on(ctx, job: Job, outcome: Outcome) -> list[str]:
    """`stuck_findings` for a review that asks for changes after a repair round (round 2 on): the
    review's text and that round's repair report, from the board pull request's head when the
    repair is there, else from the board's main."""
    j = board.parse_job_id(job.id)
    if job.kind != "review" or outcome.verdict != "needs_changes" or j.round < 2:
        return []
    repo, pkg = ctx.cfg.repositories.packages_repo(), board.package_dir(ctx.cfg.board.packages_dir, job.name)

    def read(path: str, ref: str = "") -> str:
        try:
            return ctx.gh.raw_file(repo, path, ref)
        except GHError:
            return ""
    repair = f"{pkg}/reports/{board.make_id('fix', job.name, j.round, j.subject)}.md"
    return stuck_findings((read(repair, outcome.head) if outcome.head else "") or read(repair),
                          outcome.report or read(f"{pkg}/reports/{job.id}.md"))


STABLE = ("#{n}: not merged", "#{n}: not a submission", "#{n}: names unknown job", "#{n}: a draft",
          "#{n}: under review on the board")


def pr_key(pr: dict) -> list:
    """What a submission's intake outcome depends on, as the listing shows it: head, labels, update time."""
    return [(pr.get("head") or {}).get("sha", ""), sorted(label.get("name", "") for label in pr.get("labels", [])),
            pr.get("updated_at", "")]


def intake_open(ctx, yes: bool, only: int = 0, log=print, shared=None) -> list[int]:
    """Every open submission or import pull request, each on its own: one listing for all of them (no
    GET per pull request), a pull request whose head, labels and update time are as at the last run
    with a stable outcome (left for the maintainer, under review, not a submission) is not read again
    (a cache in the state directory; empty on a fresh runner), and a GitHub error on one is logged and
    the run goes on. Then (unless `only`) the open explorer imports the merge order lacks are inserted
    into it (`backfill_import_order`, from the same listing). Returns the pull requests skipped by an
    error."""
    repo = ctx.cfg.repositories.packages_repo()
    pulls = ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True) or []
    path = ctx.state / "intake-prs.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    seen, skipped = {}, []
    for pr in pulls:
        n = int(pr["number"])
        if only and n != only:
            continue
        body = pr.get("body") or ""
        if not (board.decode_submission(body) or explorer.decode_import(body)):
            continue
        key, cached = pr_key(pr), (cache.get(str(n)) if isinstance(cache, dict) else None) or {}
        if cached.get("key") == key and any(cached.get("outcome", "").startswith(p.format(n=n)) for p in STABLE):
            log(f"#{n}: unchanged since the last run ({cached['outcome'][:120]})")
            seen[str(n)] = cached
            continue
        try:
            outcome = intake(ctx, n, yes=yes, pr=pr, shared=shared)
        except GHError as exc:
            skipped.append(n)
            log(f"#{n}: skipped: GitHub did not answer ({str(exc).splitlines()[0][:200]}); the next run tries again")
            continue
        log(outcome)
        if yes:
            seen[str(n)] = {"key": key, "outcome": outcome}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass
    if not only:
        try:
            backfill_import_order(ctx, pulls, yes, log)
        except GHError as exc:
            log(f"merge order of open imports: skipped: GitHub did not answer ({str(exc).splitlines()[0][:200]}); "
                "the next run tries again")
    return skipped


def base_text(ctx, repo: str, path: str, pr: dict) -> str:
    """A file as the pull request's base has it ('' when it has none)."""
    try:
        return ctx.gh.raw_file(repo, path, (pr.get("base") or {}).get("sha", ""))
    except GHError:
        return ""


def advance(ctx, job: Job, outcome: Outcome, jobs: list[Job]) -> str:
    cfg = ctx.cfg
    new = board.inherit_origin(job, board.successors(
        job, outcome, max_rounds=cfg.board.max_rounds, redteam_rounds=cfg.redteam.rounds,
        redteam_agents=tuple(p.agent for p in cfg.redteam.passes),
        redteam_models=tuple(p.model for p in cfg.redteam.passes)))
    stuck = stuck_on(ctx, job, outcome) if any(s.kind == "fix" for s in new) else []
    reason = board.blocked_reason(job, outcome, max_rounds=cfg.board.max_rounds)
    capped = bool(reason) and job.kind == "review" and outcome.verdict == "needs_changes"
    existing = {j.id for j in jobs}
    created = []
    for s in new:
        cut = cut_short(s, jobs)
        if cut:
            # A red-team pass a merge of its board pull request cut short (closed as superseded, never
            # run): its round resumes on the roadmap pull request, so the pass runs there.
            ctx.board.reopen(cut, s, f"Intake: this pass runs after all, after `{job.id}`.")
            created.append(f"{s.id} (reopened, #{cut.number})")
            continue
        if s.id not in existing:
            if stuck and s.kind == "fix":
                # Parked: the human who unblocks it lets one more repair round run.
                number = ctx.board.create(s, state="blocked",
                                          preamble=STUCK_NOTE + "\n\n" + "\n".join(f"- {f}" for f in stuck))
                created.append(_created(ctx, s, number, " (blocked)"))
                continue
            created.append(_created(ctx, s, ctx.board.create(s)))
    notes = []
    if stuck:
        ctx.board.comment(job.number, "Parked for a human: " + STUCK_NOTE + "\n\n" + "\n".join(f"- {f}" for f in stuck))
        notes.append("blocked: the review re-raises only findings the repair marked not actionable")
    if capped:
        fix = park_after_rounds(ctx, job, outcome, existing, outcome.head)
        if fix:
            created.append(f"{fix} (blocked)")
        notes.append(f"parked after {board.parse_job_id(job.id).round} rounds")
    if job.kind == "target" and outcome.verdict == "merge-into":
        notes.append(absorb(ctx, job, outcome.into, jobs))
    if board.artefact_of(job.id) == "DESIGN" and job.kind != "review":
        notes.append(announce_gaps(ctx, job, jobs))
    if reason and not capped:
        ctx.board.comment(job.number, f"Parked for a human: {reason}")
        notes.append("parked")
    if job.kind == "review" and outcome.verdict == "accepted" and board.parse_job_id(job.id).subject == "DESIGN":
        notes.append(add_to_order(ctx, job.name))
    if any(s.kind == "port" for s in new):
        notes.append(add_to_order_for_port(ctx, job.name))
    return "created " + (", ".join(created) if created else "nothing") + (f" ({'; '.join(notes)})" if notes else "")


def cut_short(new: Job, jobs: list[Job]) -> Job | None:
    """The job on the board with the id of the red-team pass `new` that was closed as superseded
    before it ran (`boardpr.redteam_after_port`), or None: such a pass is reopened, where any other
    job the board already has is left as it is."""
    if new.kind != "redteam":
        return None
    return next((j for j in jobs if j.id == new.id and j.state == "done" and board.SUPERSEDED_LABEL in j.labels), None)


def _created(ctx, job: Job, number: int, suffix: str = "") -> str:
    """The `created ...` entry for a job: its id, or where the live board already had it."""
    if job.id in getattr(ctx.board, "reused", {}):
        return f"{job.id} (already on the board as #{number})"
    return job.id + suffix


PARKED_ROUNDS = ("Parked for a human after {n} rounds: move this fix to state:available to run round {next}, or "
                 "close it to stop.")
CAPPED = re.compile(r"Parked for a human(?: after \d+ rounds|: \S+: the (?:design|package) still needs changes after "
                    r"\d+ rounds)")


def review_text(ctx, job: Job, outcome: Outcome | None = None) -> str:
    """A review's text: its comments on a board pull request, else its report on the board's main."""
    if outcome is not None and outcome.report:
        return outcome.report
    pkg = board.package_dir(ctx.cfg.board.packages_dir, job.name)
    try:
        return ctx.gh.raw_file(ctx.cfg.repositories.packages_repo(), f"{pkg}/reports/{job.id}.md")
    except GHError:
        return ""


def park_after_rounds(ctx, review: Job, outcome: Outcome | None, existing: set[str], head: str = "") -> str:
    """At `[board] max_rounds` (and after every round beyond it), the next repair round is created
    as state:blocked, listing the review's findings, and the review's issue says how a human goes on:
    moving the fix to state:available runs the next round (the counter keeps counting), closing it
    stops. Returns the fix's id, or '' when it exists already."""
    [fix] = board.inherit_origin(review, [board.fix_for_review(review)])
    n = board.parse_job_id(review.id).round
    note = PARKED_ROUNDS.format(n=n, next=n + 1)
    if fix.id in existing:
        return ""
    if head:
        fix.inputs["base_head"] = head        # the head its push must move (board pull request)
    found = [f for f in prreviews.findings_with_bodies(review_text(ctx, review, outcome))]
    listed = "\n".join(f"- `{fid}` ({sev or 'P?'}) {title}" for fid, sev, title, _b in found) or \
        f"- see the review `{review.id}`"
    number = ctx.board.create(fix, state="blocked", preamble=note + "\n\nThe findings of `" + review.id + "`:\n\n" + listed)
    ctx.board.comment(review.number, f"Parked for a human after {n} rounds: `{fix.id}` (#{number}) is blocked; move it "
                                     f"to state:available to run round {n + 1}, or close it to stop.")
    return fix.id


GAP_REGISTRY_NOTE = "<!-- tauceti-roadmap-gap-registry-note -->"
REGISTRY_REASON = re.compile(r"gap registry.*outside.*permitted files|outside.*permitted files.*gap", re.IGNORECASE)


def note_gap_registry_blocks(ctx) -> list[str]:
    """`board sync`, once per job: a repair round parked because its only re-raised findings were
    marked "not actionable" for "gap registry is outside permitted files" can go on now that the
    package stage may add to the plan's `gaps`; its issue says so (a human moves it to available)."""
    out = []
    for job in ctx.board.list_jobs(("blocked",), ("fix",)):
        try:
            body = (ctx.gh.api(f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}") or {}).get("body") or ""
            comments = ctx.board.comments(job.number)
        except GHError:
            continue
        reasons = re.findall(r"^- `[^`]+` \(P[123?]\).*marked not actionable \((.*)\)\s*$", body, re.MULTILINE)
        if not reasons or not all(REGISTRY_REASON.search(r) for r in reasons):
            continue
        if any(GAP_REGISTRY_NOTE in (c.get("body") or "") for c in comments):
            continue
        ctx.board.comment(job.number, "Board: the findings this repair round was parked on were marked not actionable "
                                      "because the gap registry was outside the package's files. The package stage may now "
                                      "add supplier gaps to the top-level `gaps` list of design/plan.json (and state them "
                                      "under the README's \"Exact supplier contracts\"), so they are resolvable: move this "
                                      "job to state:available to run it.\n\n" + GAP_REGISTRY_NOTE)
        out.append(f"{job.id}: noted as resolvable by package-stage gaps")
    return out


def sync_origins(ctx) -> list[str]:
    """`board sync`: an open job that does not say where its roadmap came from gets the origin of its
    roadmap (a target a human filed, `target:<issue>`, or an import, `explorer:<job>`) recorded."""
    jobs = ctx.board.all_jobs()
    known = board.origins(jobs)
    out = []
    for job in jobs:
        if job.state == "done" or board.own_origin(job) or not known.get(job.name):
            continue
        job.inputs["origin"] = known[job.name]
        ctx.board.update_spec(job)
        out.append(f"{job.id}: origin {known[job.name]}")
    return out


def repark_capped(ctx) -> list[str]:
    """`board sync`: a review parked at max_rounds before the parked fix existed (it created nothing)
    gets its blocked next fix now. Only for the last review of its roadmap and artefact, when no later
    fix or next stage exists and its issue carries the parking comment; idempotent."""
    jobs = ctx.board.all_jobs()
    ids = {j.id for j in jobs}
    last: dict[tuple[str, str], Job] = {}
    for j in jobs:
        jid = board.parse_job_id(j.id)
        if j.kind == "review" and j.state == "done":
            key = (j.name, jid.subject)
            if key not in last or jid.round > board.parse_job_id(last[key].id).round:
                last[key] = j
    out = []
    for (name, subject), review in sorted(last.items()):
        fix = board.fix_for_review(review)
        later = {"DESIGN": [board.make_id("package", name)],
                 "PKG": [board.make_id("port", name)] + board.redteam_passes(name, 1, 1)}.get(subject, [])
        if fix.id in ids or any(x in ids for x in later):
            continue
        try:
            comments = ctx.board.comments(review.number)
        except GHError:
            continue
        if not any(CAPPED.search(c.get("body") or "") for c in comments):
            continue
        head = ""
        if review.inputs.get("board_pr"):
            try:
                head = (ctx.gh.api(f"repos/{ctx.cfg.repositories.packages_repo()}/pulls/{review.inputs['board_pr']}")
                        or {}).get("head", {}).get("sha", "")
            except GHError:
                head = ""
        made = park_after_rounds(ctx, review, None, ids, head)
        if made:
            out.append(f"{review.id}: parked at max_rounds; created {made} (blocked)")
    return out


RESUME_STAGE = {"TARGET": 0, "DESIGN": 1, "PKG": 2, "RT": 3}
PR_INPUTS = ("board_pr", "after_comment", "base_head", "merge_pending")


def resume_key(job: Job) -> tuple[int, int, int]:
    """Where a job stands in its roadmap's pipeline: its stage, its round, and the author before the
    reviewer (a red-team pass before the round's repair)."""
    j = board.parse_job_id(job.id)
    stage = j.subject or j.prefix
    return RESUME_STAGE[stage], j.round, int(j.prefix == "REV" or (j.prefix, stage) == ("FIX", "RT"))


def outcome_at_rest(ctx, job: Job, jobs: list[Job]) -> Outcome:
    """What a job that was finished earlier said, as the board's main records it: a review's
    verdict from the head of its report, a target's triage, whether a red-team round is complete."""
    if job.kind == "review":
        return Outcome(verdict=explorer.verdict_of(review_text(ctx, job)), pr=int(job.inputs.get("board_pr") or 0),
                       operator=job.inputs.get("operator", ""))
    if job.kind in ("target", "redteam"):
        return outcome_for(ctx, job, {}, jobs)
    return Outcome()


def port_resuming_red_team(last: Job, jobs: list[Job]) -> Job:
    """The port of a package whose board pull request was merged between two red-team rounds: the
    red team goes on at the next round on the roadmap pull request (`redteam_round`), with the
    passes of the round before and, when that round was repaired, the repair to verify."""
    j = board.parse_job_id(last.id)
    if last.kind == "redteam":
        previous, repair = sorted(x.id for x in jobs if x.kind == "redteam" and x.name == last.name
                                  and board.parse_job_id(x.id).round == j.round), ""
    else:
        previous, repair = list(last.inputs.get("reviews", [])), last.id
    [port] = board.inherit_origin(last, [board.new_job(
        board.make_id("port", last.name), after=[repair] if repair else list(previous),
        inputs={"redteam_round": j.round + 1, "previous": previous, "repair": repair})])
    return port


def resume_roadmap(ctx, name: str, jobs: list[Job], yes: bool) -> str:
    cfg = ctx.cfg
    mine = [j for j in jobs if j.name == name and j.kind != "errata"]
    if not mine:
        return "no job on the board"
    held = [j for j in mine if j.state != "done"]
    if held:
        return f"not resumed: {held[0].id} is {held[0].state}"
    if any(j.kind in ("port", "gate", "prreview") for j in mine):
        return "not resumed: it has been ported"
    last = max(mine, key=resume_key)
    jid = board.parse_job_id(last.id)
    outcome = outcome_at_rest(ctx, last, jobs)
    if last.kind == "review" and not outcome.verdict:
        return f"not resumed: the report of {last.id} on the board's main names no verdict"
    number = int(last.inputs.get("board_pr") or 0)
    if number and (ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/pulls/{number}") or {}).get("merged"):
        # Nothing runs on a merged pull request: what follows works on the board's main
        # (`boardpr.merged_by_hand`), and a red team goes on on the roadmap pull request.
        last = replace(last, inputs={k: v for k, v in last.inputs.items() if k not in PR_INPUTS})
        outcome.pr = 0
    else:
        number = 0
    red_team = last.kind == "redteam" or (last.kind == "fix" and jid.subject == "RT")
    if number and red_team and jid.round < cfg.redteam.rounds:
        new = [port_resuming_red_team(last, jobs)]
    elif number and last.kind == "redteam":
        return (f"not resumed: the last red-team round of {name} has no repair and its board pull request #{number} "
                "is merged; a human decides how to continue")
    else:
        new = board.inherit_origin(last, board.successors(
            last, outcome, max_rounds=cfg.board.max_rounds, redteam_rounds=cfg.redteam.rounds,
            redteam_agents=tuple(p.agent for p in cfg.redteam.passes),
            redteam_models=tuple(p.model for p in cfg.redteam.passes)))
    ids = {j.id for j in jobs}
    capped = last.kind == "review" and outcome.verdict == "needs_changes" and not new
    if capped:
        new = [board.fix_for_review(last)]            # parked for a human by `advance`, as a blocked fix
    if not new:
        said = f" ({outcome.verdict})" if outcome.verdict else ""
        return f"nothing follows {last.id}{said}; a human decides how to continue"
    missing = [s.id for s in new if s.id not in ids or cut_short(s, jobs)]
    if not missing:
        return f"{last.id} is followed already (by {', '.join(s.id for s in new)})"
    where = " on the board's main" if number and any(s.kind in ("review", "fix") for s in new) else ""
    if not yes:
        return f"{last.id} would release {', '.join(missing)}{where} (dry run)"
    if any(s.kind == "port" for s in new):
        deferred = order_ports_after_suppliers(ctx, new, jobs)
        made = [board.made_note(ctx.board, s, ctx.board.create(s)) for s in new]
        return (f"{last.id}: created {', '.join(made)}" + ("; " + "; ".join(deferred) if deferred else "") + "; "
                + add_to_order_for_port(ctx, name))
    return f"{last.id}: " + advance(ctx, last, outcome, jobs) + where


def resume_unfollowed(ctx, names, yes: bool = False) -> list[str]:
    """`board resume --names A,B`: a named roadmap whose every job is done, and which was not
    ported, goes on from its last job when that job released nothing: its submission was merged on
    GitHub before the intake read it, or a run stopped between finishing the job and creating what
    follows. What follows is what the intake would have created then (`advance`), from the verdict
    at the head of the review's report on the board's main. When the job's board pull request is
    merged, what follows works on the board's main, as `boardpr.merged_by_hand` has it: a review
    or a repair names no pull request, an accepted package is ported with its red team on the
    roadmap pull request, and a package merged between two red-team rounds is ported with the next
    round there. Only the roadmaps named: one a human stopped stays stopped. One line per roadmap."""
    jobs = ctx.board.all_jobs()
    out = []
    for name in names:
        try:
            out.append(f"{name}: {resume_roadmap(ctx, name, jobs, yes)}")
        except GHError as exc:
            out.append(f"{name}: skipped: GitHub did not answer ({str(exc).splitlines()[0][:200]})")
    return out


STUCK_NOTE = ("every blocking finding of this review re-raises a finding the last repair round marked not "
              "actionable in this package, so another repair round cannot settle them. Decide each one (a supplier "
              "contract, a scope change, a waiver), then move the repair job to `state:available` to run it, or close "
              "it. The findings:")


def read_plan(ctx, name: str, ref: str = "") -> dict | None:
    """A roadmap's design/plan.json on the board (at `ref`, else its main), or None."""
    pkg = board.package_dir(ctx.cfg.board.packages_dir, name)
    try:
        plan = json.loads(ctx.gh.raw_file(ctx.cfg.repositories.packages_repo(), f"{pkg}/design/plan.json", ref))
    except (GHError, ValueError):
        return None
    return plan if isinstance(plan, dict) else None


def announce_package_gaps(ctx, job: Job, jobs: list[Job], head: str) -> str:
    """The supplier gaps a package added to the plan (at its board pull request's `head`, not yet on
    the board's main), announced as at design acceptance."""
    from .lanes.design import supplier_gaps
    before, after = read_plan(ctx, job.name) or {}, read_plan(ctx, job.name, head)
    if after is None:
        return "no plan.json at the head"
    known = {str(g.get("slug") or g.get("id")) for g in supplier_gaps(before)}
    added = [g for g in supplier_gaps(after) if str(g.get("slug") or g.get("id")) not in known]
    return announce_gaps(ctx, job, jobs, {"gaps": added}) if added else "no supplier gaps added by the package"


def announce_gaps(ctx, job: Job, jobs: list[Job], plan: dict | None = None) -> str:
    """Each gap of a design's plan that a named supplier must close becomes a comment on that
    supplier's open job on the board (its latest design, package, review or fix), or a note on the
    job's own issue when the supplier has none. `plan`: these gaps instead of the plan on main."""
    from .lanes.design import supplier_gaps
    if plan is None:
        plan = read_plan(ctx, job.name)
        if plan is None:
            return "no plan.json to read gaps from"
    gaps = supplier_gaps(plan)
    for gap in gaps:
        supplier, slug = str(gap["supplier"]), str(gap.get("slug") or gap.get("id"))
        what = (f"`{job.name}` needs from `{supplier}` {gap.get('section') or gap.get('target') or ''}: "
                + board.neutralise(str(gap.get("needs", ""))).strip() + f" (gap `{slug}` of `{job.id}`, #{job.number})")
        theirs = [j for j in jobs if j.name == supplier and j.state != "done"
                  and j.kind in ("design", "package", "review", "fix", "target")]
        if theirs:
            ctx.board.comment(max(theirs, key=lambda j: j.number).number, f"Intake: a supplier contract is missing. {what}.")
        else:
            ctx.board.comment(job.number, f"Intake: a supplier contract is missing, and `{supplier}` has no open job on "
                                          f"the board to carry it. {what}.")
    return f"{len(gaps)} supplier gap(s) announced" if gaps else "no supplier gaps"


def supplier_name(raw: str) -> str:
    """The roadmap name a gap's `supplier` field means: `SelmerIwasawaCohomology:L1` and
    `tauceti:TauCetiRoadmap/ProfiniteArithmetic#layer-0` both name a roadmap with a locator glued on."""
    head = str(raw).split("#", 1)[0]
    tokens = [t for t in re.findall(r"[A-Z][A-Za-z0-9]{2,}", head) if t != "TauCetiRoadmap"]
    return max(tokens, key=len) if tokens else str(raw).strip()


def order_ports_after_suppliers(ctx, nxt: list[Job], jobs: list[Job]) -> list[str]:
    """Ports in supplier order (maintainer rule, 2026-10-23): every port job in `nxt` is made to
    follow the port of each gap supplier of its plan that is neither a roadmap on main nor in an open
    roadmap pull request, and a target is filed for a supplier that exists nowhere (no roadmap, no
    pull request, no job on the board), so the supplier is built first and the consumer's pull
    request can list it in its merge order. Returns the notes (what was deferred or filed)."""
    from . import openroadmaps, targets
    from .lanes.design import supplier_gaps
    ports = [j for j in nxt if j.kind == "port"]
    if not ports:
        return []
    try:
        index = openroadmaps.refresh(ctx.gh, ctx.cfg)
    except Exception:          # noqa: BLE001 - a stale index is better than none here
        try:
            index = openroadmaps.load(ctx.cfg.state_dir())
        except Exception:      # noqa: BLE001
            index = {}
    if not index.get("main_roadmaps") and not index.get("prs"):
        return []              # no view of main and the open pull requests: impose nothing rather than block every port
    on_main = set(index.get("main_roadmaps", []))
    in_prs = {n for p in index.get("prs", []) for n in p.get("roadmaps", [])}
    on_board = {j.name for j in jobs}
    notes = []
    for port in ports:
        plan = read_plan(ctx, port.name) or {}
        for gap in supplier_gaps(plan):
            supplier = supplier_name(gap["supplier"])
            if supplier in on_main or supplier in in_prs or supplier == port.name:
                continue
            dep = board.make_id("port", supplier)
            if dep not in port.after:
                port.after.append(dep)
            if supplier not in on_board:
                slug = str(gap.get("slug") or gap.get("id"))
                fields = {"name": supplier, "title": f"{supplier}: supplier of {port.name} ({slug})",
                          "statement": board.neutralise(str(gap.get("needs", ""))).strip()
                                       or f"The roadmap {supplier}, which {port.name} names as a supplier.",
                          "why": (f"{port.name} declares the supplier gap `{slug}` against {supplier}, which exists "
                                  "nowhere: not on main, not in an open pull request, not on the board. Its port "
                                  "waits for this roadmap to be ported first."),
                          "sources": "see the consumer's README", "licence": "Unknown"}
                try:
                    made = ctx.gh.write("POST", f"repos/{ctx.cfg.repositories.board_repo()}/issues",
                                        targets.issue_payload(ctx.cfg, fields)) or {}
                    on_board.add(supplier)
                    notes.append(f"target #{made.get('number', '?')} filed for {supplier} (a supplier of {port.name} "
                                 f"that exists nowhere)")
                except GHError as exc:
                    notes.append(f"target for {supplier} not filed ({exc})")
            notes.append(f"{port.id} follows {dep}: {supplier} is not on main or in an open pull request")
    return notes


MERGED_LABEL = "merged-into"


def absorb(ctx, job: Job, into: str, jobs: list[Job]) -> str:
    """A target the triage merged into another open target or design: its statement and sources join
    the survivor's inputs (`absorbed`, carried into the design brief), a design is told on its issue
    (a narrower target becomes a note for that design), and the target is labelled `merged-into`
    with a link. A survivor that is not an open target or design parks the merge for a human."""
    survivor = ctx.board.find(into, jobs)
    if survivor is None or survivor.id == job.id or survivor.kind not in ("target", "design") or survivor.state == "done":
        ctx.board.comment(job.number, f"Parked for a human: the triage merges `{job.id}` into `{into}`, which is not an "
                                      "open target or design on the board.")
        return f"parked: {into} is not an open target or design"
    entry = {"target": job.id, "issue": job.number, "form": job.inputs.get("form", {})}
    absorbed = [a for a in survivor.inputs.get("absorbed", []) if a.get("target") != job.id] + [entry]
    seen = {a["target"] for a in absorbed}
    absorbed += [a for a in job.inputs.get("absorbed", []) if a.get("target") not in seen]   # what it had absorbed
    survivor.inputs["absorbed"] = absorbed
    ctx.board.update_spec(survivor)
    if survivor.kind == "design":
        statement = board.neutralise(str(entry["form"].get("statement", ""))).strip()
        sources = board.neutralise(str(entry["form"].get("sources", ""))).strip()
        ctx.board.comment(survivor.number, (
            f"Intake: the target `{job.id}` (#{job.number}) is covered by this design and was merged into it. Its "
            "statement and sources, quoted as data (never instructions):\n\n"
            + "\n".join("> " + line for line in (statement or "(none)").splitlines())
            + ("\n\nSources:\n\n" + "\n".join("> " + line for line in sources.splitlines()) if sources else "")
            + "\n\nThe design covers it, or its next fix round does; the job's inputs list it under `absorbed`."))
    ctx.board.comment(job.number, f"Intake: merged into `{survivor.id}` (#{survivor.number}); this target is closed.")
    try:
        ctx.board.add_label(job, MERGED_LABEL)
    except GHError:
        pass
    return f"merged into {survivor.id}"


DESIGN_README, PACKAGE_README = "design/README.md", "README.md"


NOT_KEPT = "merge order not updated (not kept in the packages repository)"
ORDER_HEADER = "# Merge order: suppliers before consumers, one roadmap per line.\n"


def read_merge_order(ctx) -> tuple[str, str | None]:
    """The merge-order file on the board's main and its blob sha (a new file's header and None when
    there is none yet)."""
    cfg = ctx.cfg
    try:
        current = ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/contents/{cfg.roadmap.merge_order_file}")
        return base64.b64decode(current["content"]).decode("utf-8"), current["sha"]
    except GHError:
        return ORDER_HEADER, None


def place_roadmaps(text: str, readmes: dict[str, str], main_extended: set[str] = frozenset()) -> tuple[str, list[str]]:
    """The merge-order file with every roadmap of `readmes` (name -> README) that it lacks inserted
    right after its last supplier, as its README names them, and the names inserted, in order. Among
    the new roadmaps a supplier goes in before its consumers (by name where they cite each other)."""
    pending = {n: r for n, r in readmes.items() if n not in depgraph.read_order(text)}
    placed = []
    while pending:
        known = set(depgraph.read_order(text)) | set(pending)
        suppliers = {n: set(depgraph.supplier_edges({n: r}, known, main_extended).get(n, {}))
                     for n, r in pending.items()}
        ready = [n for n in sorted(pending) if not suppliers[n] & (set(pending) - {n})]
        name = (ready or sorted(pending))[0]
        text = depgraph.insert_into_order(text, name, suppliers[name])
        placed.append(name)
        del pending[name]
    return text, placed


def write_placed(ctx, text: str, sha: str | None, readmes: dict[str, str], yes: bool = True) -> dict[str, str]:
    """Insert the roadmaps of `readmes` the merge-order file `text` (blob `sha`) lacks and write it to
    the board's main in one commit. Returns name -> what happened to it."""
    cfg = ctx.cfg
    listed = set(depgraph.read_order(text))
    notes = {n: f"{n} already in the merge order" for n in readmes if n in listed}
    new, placed = place_roadmaps(text, readmes, set(cfg.roadmap.main_extended))
    if not placed:
        return notes
    if yes:
        payload = {"message": (f"Merge order: insert {placed[0]} after its last supplier" if len(placed) == 1 else
                               f"Merge order: insert {', '.join(placed)} after their last suppliers"),
                   "content": base64.b64encode(new.encode()).decode()}
        if sha:
            payload["sha"] = sha
        path = f"repos/{cfg.repositories.packages_repo()}/contents/{cfg.roadmap.merge_order_file}"
        ctx.gh.write("PUT", path, payload)
    for n in placed:
        notes[n] = f"{n} inserted into the merge order" + ("" if yes else " (dry run: not written)")
    return notes


def insert_roadmaps(ctx, readmes: dict[str, str], yes: bool = True) -> dict[str, str]:
    """`write_placed` on the merge order of the board's main: the README texts are in hand (an
    explorer import), so nothing is read but the order. Returns name -> what happened to it."""
    if ctx.cfg.roadmap.merge_order_repo != "packages":
        return {n: NOT_KEPT for n in readmes}
    if not readmes:
        return {}
    text, sha = read_merge_order(ctx)
    return write_placed(ctx, text, sha, readmes, yes)


def add_to_order(ctx, name: str, readmes: tuple[str, ...] = (DESIGN_README, PACKAGE_README)) -> str:
    """Insert a roadmap into the merge order after its last supplier (packages repository), its
    suppliers read from the first of `readmes` (paths in its package folder on main) that exists."""
    cfg = ctx.cfg
    if cfg.roadmap.merge_order_repo != "packages":
        return NOT_KEPT
    repo = cfg.repositories.packages_repo()
    pkg = board.package_dir(cfg.board.packages_dir, name)
    text, sha = read_merge_order(ctx)
    if name in depgraph.read_order(text):
        return f"{name} already in the merge order"
    readme = None
    for rel in readmes:
        try:
            readme = ctx.gh.raw_file(repo, f"{pkg}/{rel}")
            break
        except GHError:
            continue
    if readme is None:
        return f"merge order not updated (no {' or '.join(readmes)} of {name} on main)"
    return write_placed(ctx, text, sha, {name: readme})[name]


IMPORT_TITLE = re.compile(rf"^Import ({board.NAME_RE}) from the explorer \(")


def backfill_import_order(ctx, pulls: list[dict], yes: bool, log=print) -> None:
    """Every open explorer import (`Import <Name> from the explorer (...)`) whose roadmap the merge
    order on the board's main lacks is inserted after its last supplier, as the README at the pull
    request's head names them (its package README, else its design README), all in one commit; an
    import before its design was accepted is left to the design review. One line per insertion.
    Imports opened before the import placed them (or whose placement failed) are caught up; once
    they are all placed, a run reads only the merge order."""
    if ctx.cfg.roadmap.merge_order_repo != "packages":
        return
    imports: dict[str, dict] = {}
    for pr in pulls:
        m = IMPORT_TITLE.match(pr.get("title") or "")
        if m:
            imports.setdefault(m.group(1), pr)
    if not imports:
        return
    text, sha = read_merge_order(ctx)
    listed = set(depgraph.read_order(text))
    readmes = {}
    for name, pr in sorted(imports.items()):
        found = explorer.decode_import(pr.get("body") or "")
        if name in listed or (found and not any(explorer.enters_order(j) for j in found["jobs"])):
            continue
        readme = head_readme(ctx, pr, name)
        if readme is None:
            log(f"#{pr['number']}: {name} is not in the merge order, and the import's head has no README to place "
                "it by")
            continue
        readmes[name] = readme
    for name, note in write_placed(ctx, text, sha, readmes, yes).items():
        log(f"#{imports[name]['number']}: {note} (an open explorer import)")


def head_readme(ctx, pr: dict, name: str) -> str | None:
    """The roadmap's package README at the pull request's head, else its design README; None when it
    has neither (a GitHub error other than a missing file is raised)."""
    head = pr.get("head") or {}
    repo, ref = (head.get("repo") or {}).get("full_name", ""), head.get("sha", "")
    if not repo:
        return None
    for rel in (PACKAGE_README, DESIGN_README):
        try:
            return ctx.gh.raw_file(repo, f"{board.package_dir(ctx.cfg.board.packages_dir, name)}/{rel}", ref)
        except GHError as exc:
            if transient(exc):
                raise
    return None


def add_to_order_for_port(ctx, name: str) -> str:
    """Insert the roadmap of a package whose port follows into the merge order when it is absent
    (an imported package never had its design accepted on the board), its suppliers read from the
    merged package README; a listed roadmap that the order puts above one of those suppliers is moved
    right after the last of them (`move_listed`). A failed write is reported, not raised."""
    try:
        note = add_to_order(ctx, name, (PACKAGE_README, DESIGN_README))
    except GHError as exc:
        return f"merge order not updated ({exc})"
    if note != f"{name} already in the merge order":
        return note
    try:
        moved = move_listed(ctx, name)
    except (GHError, gitops.GitError, OSError) as exc:
        return f"{note}; its place was not checked against its suppliers ({str(exc).splitlines()[0][:200]})"
    return f"{note}; {moved}" if moved else note


# --- a roadmap listed above one of its suppliers ------------------------------------------------------

SWEEP_CHECKOUT = ("order-sweep", "board")


def readme_path(packages_dir: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(packages_dir.strip('/'))}/({board.NAME_RE})/(?:design/)?README\.md$")


def pr_roadmaps(pr: dict) -> set[str]:
    """The roadmaps a board pull request carries: the job its submission marker names, or the jobs of
    its import marker."""
    body = pr.get("body") or ""
    sub = board.decode_submission(body)
    if sub:
        try:
            return {board.parse_job_id(sub["job"]).name}
        except board.JobIdError:
            return set()
    found = explorer.decode_import(body)
    return {j.name for j in found["jobs"]} if found else set()


def board_readmes(ctx, pulls: list[dict]) -> dict[str, str]:
    """Every roadmap README on the board, by git (no REST request): `packages/<Name>/README.md`, else
    its `design/README.md`, on main, and for a roadmap that an open pull request under review carries
    (`needs-review`: a design or package the intake took in, an import), the one at that pull
    request's head. One shallow fetch of main and those heads into the state directory."""
    cfg = ctx.cfg
    pkgs = cfg.board.packages_dir.strip("/")
    dest = ctx.state.joinpath(*SWEEP_CHECKOUT)
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        gitops.git(dest, "init", "-q")
    gitops.git(dest, "config", "remote.origin.url", gitops.url(cfg.repositories.packages_repo()))
    carried = {int(p["number"]): pr_roadmaps(p) for p in pulls
               if boardpr.NEEDS_REVIEW in {label.get("name") for label in p.get("labels") or []}}
    carried = {n: names for n, names in carried.items() if names}
    gitops.git(dest, "fetch", "-q", "--depth=1", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
               *(f"+refs/pull/{n}/head:refs/remotes/pull/{n}" for n in sorted(carried)))
    main, path = "refs/remotes/origin/main", readme_path(pkgs)
    found: dict[str, tuple[str, str]] = {}        # name -> (ref, path); README.md wins over design/README.md
    for rel in gitops.git(dest, "ls-tree", "-r", "--name-only", main, "--", pkgs).splitlines():
        m = path.match(rel)
        if m and (m.group(1) not in found or not rel.endswith("/design/README.md")):
            found[m.group(1)] = (main, rel)
    for n, names in sorted(carried.items()):
        for name in names:
            for rel in (f"{pkgs}/{name}/README.md", f"{pkgs}/{name}/design/README.md"):
                if gitops.git(dest, "ls-tree", "--name-only", f"refs/remotes/pull/{n}", "--", rel).strip():
                    found[name] = (f"refs/remotes/pull/{n}", rel)
                    break
    return {name: gitops.git(dest, "show", f"{ref}:{rel}") for name, (ref, rel) in found.items()}


def main_roadmaps(ctx) -> set[str]:
    """The roadmaps on the content repository's main, under the active and the completed directories
    (`gates.roadmap_names` of its tree: one REST read): suppliers available to every pull request,
    whose place in the merge order of open pull requests means nothing."""
    cfg = ctx.cfg
    data = ctx.gh.api(f"repos/{cfg.repositories.content}/git/trees/{quote(cfg.project.branch, safe='')}"
                      "?recursive=1") or {}
    paths = [t.get("path", "") for t in data.get("tree") or [] if isinstance(t, dict) and t.get("type") == "blob"]
    return gates.roadmap_names(paths, (cfg.roadmap.active_dir, cfg.roadmap.completed_dir))


def reorder(ctx, readmes: dict[str, str], only: set[str] | None = None, yes: bool = True,
            current: tuple[str, str | None] | None = None, merged: set[str] | None = None) -> dict[str, str]:
    """`depgraph.repair_upward` on the merge order of the board's main (`current`: its text and blob
    sha, when in hand), written in one commit when anything moved; a roadmap on the content
    repository's main (`merged`, else `main_roadmaps`) is neither moved nor moved after. Returns name
    -> what happened (a move, or why it was not made)."""
    cfg = ctx.cfg
    text, sha = current or read_merge_order(ctx)
    merged = main_roadmaps(ctx) if merged is None else merged
    new, notes = depgraph.repair_upward(text, readmes, set(cfg.roadmap.main_extended), only, merged)
    if new == text:
        return notes
    moved = [n for n in depgraph.read_order(text) if n in notes and " moved in the merge order " in notes[n]]
    if not yes:
        return {n: (v + " (dry run: not written)" if n in moved else v) for n, v in notes.items()}
    payload = {"message": (f"Merge order: move {moved[0]} after its last supplier" if len(moved) == 1 else
                           f"Merge order: move {', '.join(moved)} after their last suppliers"),
               "content": base64.b64encode(new.encode()).decode()}
    if sha:
        payload["sha"] = sha
    ctx.gh.write("PUT", f"repos/{cfg.repositories.packages_repo()}/contents/{cfg.roadmap.merge_order_file}", payload)
    return notes


def move_listed(ctx, name: str) -> str:
    """The merge path's repair: when the merge order lists the roadmap above a supplier its README on
    the board's main names, it is moved right after the last of them (the board's READMEs read for
    its consumers only then). Returns what happened ('' when its place is right)."""
    cfg = ctx.cfg
    if cfg.roadmap.merge_order_repo != "packages":
        return ""
    text, sha = read_merge_order(ctx)
    order = depgraph.read_order(text)
    pkg = board.package_dir(cfg.board.packages_dir, name)
    readme = None
    for rel in (PACKAGE_README, DESIGN_README):
        try:
            readme = ctx.gh.raw_file(cfg.repositories.packages_repo(), f"{pkg}/{rel}")
            break
        except GHError as exc:
            if transient(exc):
                raise
    if readme is None or name not in order:
        return ""
    merged = main_roadmaps(ctx)
    pos = {n: i for i, n in enumerate(order)}
    suppliers = depgraph.supplier_edges({name: readme}, set(order), set(cfg.roadmap.main_extended)).get(name, {})
    if name in merged or not any(pos[s] > pos[name] for s in suppliers if s in pos and s not in merged):
        return ""
    pulls = ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/pulls?state=open&per_page=100", paginate=True) or []
    readmes = {**board_readmes(ctx, pulls), name: readme}
    return reorder(ctx, readmes, only={name}, current=(text, sha), merged=merged).get(name, "")


def replace_upward_roadmaps(ctx, yes: bool = False) -> list[str]:
    """`board intake`'s catch-up of the merge order (idempotent: a clean order is only read): every
    roadmap the order on the board's main lists above a supplier its README names (its board README at
    the head of its open pull request, else on main; else the snapshot of its content pull request) is
    moved right after the last of them, all in one commit,
    unless that would put one of its consumers before it (logged, left as it is). It repairs roadmaps
    placed by a README that named its suppliers under names the dependency check did not know, which
    a later fix repointed. One line per roadmap moved or left."""
    cfg = ctx.cfg
    if cfg.roadmap.merge_order_repo != "packages":
        return []
    pulls = ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/pulls?state=open&per_page=100", paginate=True) or []
    # The content repository's open roadmap pull requests too (their README snapshots in the state
    # directory, as the lanes read them): a pool pull request has no board package, and its review-lane
    # repair gates against a moved copy that only this catch-up lands on main (2026-10-29).
    readmes = {**sibling_readmes(ctx.state, cfg.roadmap.active_dir), **board_readmes(ctx, pulls)}
    notes = reorder(ctx, readmes, yes=yes)
    return [f"merge order: {line}" for _name, line in sorted(notes.items())]


def prune_order(ctx, yes: bool = False) -> list[str]:
    """`board sync --prune-order`: every roadmap the merge order on the board's main lists that is on
    the content repository's main (`main_roadmaps`) is removed from it, in one commit through the
    contents API, as the intake writes its insertions; the other lines stay as they are, in their
    order. Written with `yes` only. One line per name removed."""
    cfg = ctx.cfg
    if cfg.roadmap.merge_order_repo != "packages":
        return [f"merge order: not pruned ({NOT_KEPT})"]
    merged = main_roadmaps(ctx)
    text, sha = read_merge_order(ctx)
    gone = [n for n in depgraph.read_order(text) if n in merged]
    if not gone:
        return ["merge order: it lists no roadmap on the content repository's main"]
    if yes:
        payload = {"message": (f"Merge order: remove {len(gone)} roadmap{'s' if len(gone) != 1 else ''} already on "
                               "the content repository's main\n\n" + ", ".join(gone)),
                   "content": base64.b64encode(depgraph.remove_from_order(text, set(gone)).encode()).decode()}
        if sha:
            payload["sha"] = sha
        ctx.gh.write("PUT", f"repos/{cfg.repositories.packages_repo()}/contents/{cfg.roadmap.merge_order_file}", payload)
    return [f"merge order: {n} removed (on the content repository's main)" + ("" if yes else " (dry run: not written)")
            for n in gone]


def _held(ctx, pr: int) -> bool:
    try:
        held = (ctx.state / "hold-draft.txt").read_text(encoding="utf-8").split()
    except OSError:
        return False
    return str(pr) in held


# --- one run at a time ------------------------------------------------------------------------------

LOCK_TTL = 15 * 60     # seconds a board run holds the board; a run that dies leaves it for this long
LOCK_RENEW = 3 * 60    # seconds between the renewals of a running board run's lock


def lock_key(project: str) -> str:
    return f"roadmap/{project}/board/intake"


def lock_owner() -> str:
    """`intake:run-<id>` on GitHub Actions, `intake:<host>-<pid>` elsewhere."""
    run = os.environ.get("GITHUB_RUN_ID", "")
    return f"intake:run-{run}" if run else f"intake:{socket.gethostname()}-{os.getpid()}"


def board_lock(cfg, ctx) -> claims.Claims:
    """The board lock: a lease (the workers' mechanism, `claims.py`) under the board repository's
    own refs, which the hosted workflow's token and a maintainer can both push; the claims
    namespace is another repository the workflow's token cannot write to."""
    repo = cfg.repositories.board_repo()
    return claims.Claims(repo=repo, gitdir=cfg.state_dir() / "claims" / (repo.replace("/", "__") + ".git"),
                         owner=lock_owner(), operator=ctx.operator, project=cfg.project.name, dry_run=ctx.dry_run)


def take_lock(lock, project: str) -> tuple[int, str]:
    """(the acquisition's `claims` status, line to log). ACQUIRED: ''. HELD by another run: "intake
    already running (owner, since ...)". ERROR, a lock that cannot be written (no push access, no
    network): the run does nothing, since a board write without the lock could interleave with
    another run's."""
    key = lock_key(project)
    status = lock.acquire(key, LOCK_TTL)
    if status == claims.HELD:
        return status, held_line(lock, key, "intake") + "; nothing done"
    if status == claims.ERROR:
        return status, "board lock not taken (the lock ref could not be written); nothing done"
    return status, ""


def reviews_key(project: str) -> str:
    return f"roadmap/{project}/board/reviews"


def held_line(lock, key: str, what: str) -> str:
    lease = lock.read(key) or {}
    since = dt.datetime.fromtimestamp(int(lease.get("acquired_at", 0) or 0), dt.UTC)
    return f"{what} already running ({lease.get('owner', '?')}, since {since:%Y-%m-%d %H:%M:%SZ})"


def take_reviews_lock(lock, project: str) -> tuple[int, str]:
    """The reviews lock, a second lease next to the board lock: whoever reads the pull-request
    reviews (`sync_pr_reviews`: the review label, superseded rounds, new rounds) holds it, so that
    step runs once at a time while a `board sync` that finds the board held by a long intake still
    runs it. (The acquisition's `claims` status, line to log), as `take_lock`."""
    key = reviews_key(project)
    status = lock.acquire(key, LOCK_TTL)
    if status == claims.HELD:
        return status, held_line(lock, key, "pull-request reviews") + "; left to that run"
    if status == claims.ERROR:
        return status, "pull-request reviews: not read (the reviews lock could not be written)"
    return status, ""


def locked_pr_reviews(ctx, lock) -> list[str]:
    """The pull-request reviews step of a full `board sync` that writes, under the reviews lock. The
    step is short next to the lock's lifetime, so nothing renews it; `Board.create` re-reads the
    board before every creation in any case."""
    status, line = take_reviews_lock(lock, ctx.cfg.project.name)
    if status != claims.ACQUIRED:
        return [line]
    try:
        return sync_pr_reviews(ctx)
    finally:
        lock.release(reviews_key(ctx.cfg.project.name))


# --- duplicate jobs ---------------------------------------------------------------------------------

# How far a twin got, the furthest kept: finished; submitted (an open submission pull request names
# it, or its label says so); claimed by a worker that still holds the job's lease; claimed at all, or
# parked; nothing yet. Among equals the older issue is the job.
PROGRESS = {"done": 6, "submitted": 5, "claimed": 3, "blocked": 2, "needs-references": 2, "available": 1}


def twin_progress(job: Job, has_pr: bool, worker_claim: bool, live: bool) -> tuple[int, int]:
    """Sort key of a twin: the greatest is the one to keep."""
    rank = PROGRESS.get(job.state, 0)
    if has_pr:
        rank = max(rank, PROGRESS["submitted"])
    elif job.state == "claimed" and worker_claim and live:
        rank = 4
    return rank, -job.number


def worker_claimed(ctx, job: Job) -> bool:
    """Whether a worker (or a listed reviewer's `reviewing` comment) claimed this issue itself, as
    its claim note says; a `state:claimed` the lease mirror set names only the lease."""
    if job.state != "claimed":
        return False
    try:
        claim = board.last_claim(ctx.board.comments(job.number), job.id)
    except GHError:
        return False
    return bool(claim and (claim["worker"] or claim["comment"]))


def repair_duplicates(ctx, shared: claims.Claims | None, yes: bool = False) -> list[str]:
    """`board intake`: a job whose issue exists twice, both open (two runs created it at once),
    keeps the twin that got furthest (`twin_progress`: a submission pull request that names it by
    `Refs #n`, or the `submitted` label; then a worker's own claim while the job's lease is live;
    then any claim or park; then the older issue) and closes the others as `Duplicate of #n` with the
    `duplicate` label (no longer a job: `Board._job_from_issue` ignores them, so every lookup by job
    id or title finds the kept twin). A twin that is `state:done` is never closed. The kept twin is
    never left behind a closed one: it takes over the inputs the closed twin gained (its pull
    request, the comments it handled) and, when the job has an open submission, `state:submitted`;
    when the job's lease is live, `state:claimed`. The lease is the job's (one per job id, shared by
    its twins) and stays with the kept twin: releasing it would free a running worker's job (board
    2026-10-21, #582/#583)."""
    groups = ctx.board.open_twins()
    if not groups:
        return []
    out, now, cfg = [], int(dt.datetime.now(dt.UTC).timestamp()), ctx.cfg
    pulls = ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/pulls?state=open&per_page=100", paginate=True) or []
    submissions: dict[str, list[dict]] = {}
    for p in pulls:
        sub = board.decode_submission(p.get("body") or "")
        if sub and p.get("state", "open") == "open":
            submissions.setdefault(sub["job"], []).append(p)
    for group in groups:
        job_id = group[0].id
        prs = submissions.get(job_id, [])
        numbers = {int(p["number"]) for p in prs}
        named = {board.submission_issue(p.get("body") or "") for p in prs}
        # The twin a submission names (`Refs #n`); with an unnamed one, the twin whose label or spec says so.
        has_pr = {j.number: j.number in named
                  or bool(prs) and (j.state == "submitted" or j.inputs.get("review_pr") in numbers) for j in group}
        lease = shared.read(claims.job_key(cfg.project.name, job_id)) if shared else None
        live = claims.lease_live(lease, now)
        held = {j.number: worker_claimed(ctx, j) for j in group}
        ranked = sorted(group, key=lambda j: twin_progress(j, has_pr[j.number], held[j.number], live), reverse=True)
        keep, *rest = ranked
        rank = twin_progress(keep, has_pr[keep.number], held[keep.number], live)[0]
        why = (f"it is {keep.state}" if keep.state == "done"
               else f"it carries the submission #{min(numbers)}" if has_pr[keep.number]
               else "a worker holds it" if rank == 4
               else f"it is {keep.state}" if rank > PROGRESS["available"]
               else "it is the older issue")
        closed = False
        for dup in rest:
            if dup.state == "done":
                out.append(f"{dup.id}: #{dup.number} duplicates #{keep.number} but is done; left as it is")
                continue
            if not yes:
                out.append(f"{dup.id}: would close #{dup.number} as a duplicate of #{keep.number} ({why}; dry run)")
                continue
            ctx.board.close(dup, f"Duplicate of #{keep.number}: this job was created twice; #{keep.number} is the job "
                                 f"({why}). Closed by the board.", board.DUPLICATE_LABEL)
            closed = True
            if carry_inputs(keep, dup):
                ctx.board.update_spec(keep)
            out.append(f"{dup.id}: closed #{dup.number} as a duplicate of #{keep.number} ({why})"
                       + (f"; the lease of {lease.get('owner', '?')} stays with #{keep.number}" if live else ""))
        if not closed or keep.state not in ("available", "claimed"):
            continue
        if prs:
            ctx.board.set_state(keep, "submitted", f"Submitted: #{min(numbers)} is this job's open submission (its "
                                                   f"twin was closed as a duplicate; this issue said "
                                                   f"`state:{keep.state}`).")
            out.append(f"{keep.id}: #{keep.number} moved to submitted (#{min(numbers)} is open)")
        elif live and keep.state == "available":
            ctx.board.set_state(keep, "claimed", f"Claimed for `{keep.id}` by `{lease.get('owner')}` (lease).")
            out.append(f"{keep.id}: #{keep.number} moved to claimed (the lease of {lease.get('owner', '?')} is live)")
    return out


def carry_inputs(keep: Job, dup: Job) -> bool:
    """The inputs a closed twin gained that the kept one lacks (its submission's `review_pr`, a
    pending merge, the head a fix must move) go to the kept twin; the comments handled are merged.
    True when the kept twin's spec changed."""
    changed = False
    for key, value in dup.inputs.items():
        if key == "handled":
            merged = sorted(set(keep.inputs.get("handled", [])) | set(value))
            if merged != keep.inputs.get("handled", []):
                keep.inputs["handled"], changed = merged, True
        elif key not in keep.inputs:
            keep.inputs[key], changed = value, True
    return changed


# --- sync ------------------------------------------------------------------------------------------

def sync_targets(ctx) -> list[str]:
    cfg, out = ctx.cfg, []
    repo = cfg.repositories.board_repo()
    issues = ctx.gh.api(f"repos/{repo}/issues?labels={cfg.board.namespace_label},kind:target&state=open&per_page=100",
                        paginate=True)
    for issue in issues:
        # Synced issues carry a state label (only the board sets one); an unsynced form is human text,
        # so any marker typed into it is neutralised before the board appends the real spec.
        if issue.get("pull_request") or any(label["name"].startswith("state:") for label in issue.get("labels", [])):
            continue
        author = (issue.get("user") or {}).get("login", "")
        ok, note = filers.allowed(ctx, author)
        if ok is None:
            out.append(f"#{issue['number']}: may @{author} file targets? Unknown ({note}); left as it is")
            continue
        if not ok:
            ctx.gh.write("POST", f"repos/{repo}/issues/{issue['number']}/comments", {"body": note})
            ctx.gh.write("POST", f"repos/{repo}/issues/{issue['number']}/labels", {"labels": [filers.LABEL]})
            ctx.gh.write("PATCH", f"repos/{repo}/issues/{issue['number']}", {"state": "closed"})
            out.append(f"#{issue['number']}: closed, @{author} may not file targets")
            continue
        form = board.parse_target_form(issue.get("body") or "")
        name = board.camel_name(form.get("name") or issue.get("title", ""))
        if not form.get("statement") or not name:
            out.append(f"#{issue['number']}: target form incomplete (statement and a usable name are needed)")
            continue
        job = board.new_job(board.make_id("target", name), inputs={"form": form, "issue": issue["number"]})
        body = board.neutralise(issue.get("body") or "") + "\n\n" + board.encode_spec(job)
        ctx.gh.write("PATCH", f"repos/{repo}/issues/{issue['number']}", {"body": body})
        job.number, job.labels = issue["number"], [label["name"] for label in issue.get("labels", [])]
        ctx.board.set_state(job, "available")
        out.append(f"#{issue['number']}: {job.id} available")
    return out


def sync_states(ctx, shared: claims.Claims | None) -> list[str]:
    """The board's invariant: every open job issue carries exactly one `state:` label. An issue that
    carries several keeps the one `board.repaired_state` picks from the label added last and the
    lease (with no readable claims namespace, no lease information)."""
    out, now = [], int(dt.datetime.now(dt.UTC).timestamp())
    for job in ctx.board.state_conflicts():
        states = board.state_labels(job.labels)
        live = claims.lease_live(shared.read(claims.job_key(ctx.cfg.project.name, job.id)), now) if shared else None
        last = ctx.board.last_state_added(job)
        keep = board.repaired_state(states, live, last)
        why = "; ".join(([f"`state:{last}` added last"] if last else [])
                        + ["no lease information" if live is None else ("a live lease" if live else "no live lease")])
        ctx.board.set_state(job, keep, "Board: this job carried " + ", ".join(f"`state:{s}`" for s in states)
                            + f"; it keeps `state:{keep}` ({why}).")
        out.append(f"{job.id}: {' + '.join(states)} -> {keep} ({why})")
    return out


def claim_age(claim: dict, now: dt.datetime) -> float | None:
    try:
        return (now - dt.datetime.fromisoformat(str(claim["at"]))).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None


def sync_expired(ctx, shared: claims.Claims | None) -> list[str]:
    """Claimed jobs nobody holds any more, back to `state:available` now rather than after
    `stale_hours`: a lease that expired (the worker stopped renewing it: killed, or off the network),
    and a claim with no lease at all whose "Claimed for" comment is older than the job lease's
    lifetime, `[claims] job_ttl_seconds` (a worker that died before, or without, registering one: a
    full disk). A claim made by a `reviewing` comment holds for its own four hours. Only with a
    namespace that can be read now: an unreadable one never looks like one without leases."""
    if not shared:
        return []
    from .boardpr import COMMENT_CLAIM_TTL
    out, now = [], int(dt.datetime.now(dt.UTC).timestamp())
    reachable = None
    for job in ctx.board.list_jobs(("claimed",)):
        lease = shared.read(claims.job_key(ctx.cfg.project.name, job.id))
        if lease and not claims.lease_live(lease, now):
            ended = dt.datetime.fromtimestamp(int(lease.get("expires_at", 0)), dt.UTC)
            ctx.board.set_state(job, "available", f"Released: the lease of `{lease.get('owner', '?')}` expired at "
                                                  f"{ended:%Y-%m-%d %H:%MZ} without a release.")
            out.append(f"{job.id}: released (lease of {lease.get('owner', '?')} expired)")
            continue
        if lease:
            continue
        reachable = shared.reachable() if reachable is None else reachable
        if not reachable:
            continue
        try:
            claim = board.last_claim(ctx.board.comments(job.number), job.id)
        except GHError:
            continue
        age = claim_age(claim, dt.datetime.fromtimestamp(now, dt.UTC)) if claim else None
        if age is None or age <= (COMMENT_CLAIM_TTL if claim["comment"] else ctx.cfg.claims.job_ttl_seconds):
            continue
        current = ctx.board.current_state(job)
        if current != "claimed":
            # The listing lagged the worker: a lane longer than the lease that submitted while this
            # run read the board (PKG-SelmerIwasawaCohomologyPartII, #582: submitted 14:44:51Z,
            # released 14:45:00Z and rebuilt). Its lease is gone because its round ended.
            out.append(f"{job.id}: {current} now; left as it is")
            continue
        ctx.board.set_state(job, "available", f"Released: claimed by {claim['who']} at {claim['at']} with no live lease.")
        out.append(f"{job.id}: released (claimed by {claim['who']}, no lease)")
    return out


def sync_leases(ctx, shared: claims.Claims | None) -> list[str]:
    if not shared:
        return ["leases not mirrored (no readable claims namespace)"]
    out, now = [], int(dt.datetime.now(dt.timezone.utc).timestamp())
    for job in ctx.board.list_jobs(("available",)):
        lease = shared.read(claims.job_key(ctx.cfg.project.name, job.id))
        if claims.lease_live(lease, now):
            ctx.board.set_state(job, "claimed", f"Claimed for `{job.id}` by `{lease.get('owner')}` (lease).")
            out.append(f"{job.id}: claimed by lease")
    return out


GATE_OPEN = ("available", "claimed", "submitted", "blocked")     # a gate round under way (or a human's)


def round_key(job: Job) -> tuple[int, int]:
    """A job's place among the rounds of its roadmap: its round, then its issue."""
    return board.parse_job_id(job.id).round, job.number or 0


def supersede_waiting_gates(ctx, yes: bool = False) -> list[str]:
    """`board sync`: of the open gate rounds of one roadmap parked for references
    (`needs-references`), the newest stays and every other is closed as superseded (`supersede`):
    they wait on the same references, which answer one round. Written with `yes` only; one line per
    round."""
    parked: dict[str, list[Job]] = {}
    for job in ctx.board.kind_jobs("gate", "open"):
        if job.state == "needs-references":
            parked.setdefault(job.name, []).append(job)
    out = []
    for _name, rounds in sorted(parked.items()):
        newest = max(rounds, key=round_key)
        for job in sorted((j for j in rounds if j is not newest), key=round_key):
            note = f"Superseded by {newest.id}, which waits on the same references"
            if not yes:
                out.append(f"{job.id}: superseded by {newest.id} (waits on the same references; dry run: not written)")
            elif supersede(ctx, job, note, expect="needs-references", closing="This round is no longer needed"):
                out.append(f"{job.id}: superseded by {newest.id} (waits on the same references)")
    return out


NOT_FOUND = re.compile(r"\bHTTP 404\b")     # the status GitHub answers, never a 404 inside a path or a sha


def revises_completed(ctx, p: dict) -> bool:
    """Whether an open roadmap pull request revises its roadmap in place under `completed_dir`: at
    its head the roadmap's folder holds a README.md there and `active_dir` has no folder of its name
    (two REST reads). A completed roadmap is the maintainers' to judge, and the gate lane works under
    `active_dir` only, so a gate round for such a pull request can never pass: its review is answered
    by hand (2026-11-10: a round ran its agent for 17 minutes on a folder that does not exist, failed
    on missing files and was offered again). False whenever a read fails for another reason than the
    active folder's absence, and for a pull request that brings the roadmap back under `active_dir`."""
    cfg = ctx.cfg
    repo, head = cfg.repositories.content, quote(p["head"], safe="")
    completed, active = cfg.roadmap.completed_dir.strip("/"), cfg.roadmap.active_dir.strip("/")
    if not completed:
        return False
    try:
        items = ctx.gh.api(f"repos/{repo}/contents/{completed}/{p['name']}?ref={head}")
    except GHError:
        return False
    if not any(isinstance(i, dict) and i.get("name") == "README.md" for i in items or []):
        return False
    try:
        ctx.gh.api(f"repos/{repo}/contents/{active}/{p['name']}?ref={head}")
    except GHError as exc:
        return bool(NOT_FOUND.search(str(exc)))
    return False


def repairing_review(cfg, jobs: list[Job], number: int, owner: str) -> Job | None:
    """The review of pull request #number that is claimed while its author is in the pool and
    `[prreview] repair_pool` is on: a review that asks for changes goes on to repair the pull request
    in the same job, so no gate round is filed for it meanwhile; None when there is none."""
    if not cfg.prreview.repair_pool or owner not in set(cfg.roadmap.pool):
        return None
    return next((j for j in jobs if j.kind == "prreview" and j.under_way(("claimed",))
                 and str(j.inputs.get("pr")) == str(number)), None)


def sync_pull_requests(ctx) -> list[str]:
    """The awaiting-author / awaiting-review labels of every open roadmap pull request the workers
    manage (`open_roadmap_prs`), a GATE job when a review waits for a reply (none while a pool
    review repairs it: `repairing_review`; none for a completed roadmap revised in place:
    `revises_completed`), and held pull requests back to draft. Each pull request,
    its reviews, issue comments and inline comments are read `[board] graphql_batch` at a time by
    GraphQL (`prbatch.PullReader`; REST for the rest)."""
    cfg, out = ctx.cfg, []
    repo = cfg.repositories.content
    r = cfg.replies
    jobs = ctx.board.all_jobs()
    held = set()
    try:
        held = set((ctx.state / "hold-draft.txt").read_text(encoding="utf-8").split())
    except OSError:
        pass
    prs = open_roadmap_prs(ctx)
    reader = prbatch.PullReader(ctx.gh, repo, [p["pr"] for p in prs], cfg.board.graphql_batch)
    for p in prs:
        number = p["pr"]
        pulled = reader.get(number)
        data = pulled.pull
        if str(number) in held and not data.get("draft"):
            try:
                ctx.gh.cli_write("pr", "ready", str(number), "--undo", "-R", repo)
                out.append(f"#{number}: held, converted back to draft")
            except GHError as exc:
                out.append(f"#{number}: held, but not converted back to draft ({exc})")
        events = events_from_api(pulled.reviews, pulled.comments, pulled.inline)
        want, drop = replies.wanted_label(events, pr_answerers(cfg, p["owner"]), p["head"], markers=r.review_markers,
                                          housekeeping=r.housekeeping_prefixes, approvers=r.approvers,
                                          ignore=set(r.ignore_logins), awaiting_author=r.awaiting_author,
                                          awaiting_review=r.awaiting_review, maintainers=set(r.maintainer_logins),
                                          maintainer_prefix=r.maintainer_review_prefix, owner=p["owner"])
        have = {label["name"] for label in data.get("labels", [])}
        try:   # a token of the board alone (its workflow's) cannot write to the roadmap repository
            if want and want not in have:
                ctx.gh.write("POST", f"repos/{repo}/issues/{number}/labels", {"labels": [want]})
            for label in drop:
                if label in have:
                    ctx.gh.write("DELETE", f"repos/{repo}/issues/{number}/labels/{label}")
        except GHError as exc:
            out.append(f"#{number}: labels on {repo} not changed ({exc})")
        if want == r.awaiting_author:
            mine = [j for j in jobs if j.kind == "gate" and j.name == p["name"]]
            open_round = any(j.under_way(GATE_OPEN) for j in mine)
            # A round parked for references answers the review once they come: another round would
            # wait on the same references.
            waiting = None if open_round else max((j for j in mine if j.under_way(("needs-references",))),
                                                  key=round_key, default=None)
            open_round = open_round or waiting is not None
            repairing = None if open_round else repairing_review(cfg, jobs, number, p["owner"])
            if waiting is not None:
                out.append(f"#{number}: gate round not filed: {waiting.id} is waiting for references")
            elif repairing is not None:
                out.append(f"#{number}: gate round deferred: review {repairing.id} is repairing")
            elif not open_round and revises_completed(ctx, p):
                out.append(f"#{number}: gate round not filed: {p['name']} is a completed roadmap revised in place "
                           "(answered by hand)")
            elif not open_round:
                operator = p.get("operator", p["owner"])
                gate = board.new_job(board.make_id("gate", p["name"], len(mine) + 1), requires_operator=operator,
                                     inputs={"pr": number, "operator": operator, "owner": p["owner"]})
                ctx.board.create(gate)
                out.append(f"#{number}: {gate.id} created for " + (f"@{operator}" if operator else "the pool"))
        out.append(f"#{number} {p['name']}: {want or 'approved'}")
    if prs and (reader.queries or reader.rest_reads):
        out.append(reader.summary())
    return out


# --- the AI review of every roadmap pull request ------------------------------------------------------

OPEN_ROUND = ("available", "claimed", "submitted")     # a review or gate round that is not over


# A pull request whose title is a conventional-commit chore (`chore: ...`, `chore(Name): ...`) is one.
CHORE_TITLE = re.compile(r"^\s*(?:chore|archive)\b", re.IGNORECASE)


def review_folders(entry: dict, active_dir: str, completed_dir: str, on_main: set[str],
                   parents: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    """The roadmap folders a pull request adds or edits, relative to `active_dir`, each with the change
    of its README.md in the pull request ('' when the pull request leaves it alone), from its
    `openroadmaps.inventory` entry and `on_main`, the folders holding a README.md on main
    (`openroadmaps.readme_folders`).

    A roadmap folder is a folder under `active_dir`, at any depth, named like a roadmap (`NAME_RE`:
    `references/` is not one), that holds a README.md at the head: one the pull request adds or
    changes, or one on main it does not remove (a README the pull request leaves alone is at its head
    as at its merge base). A folder with a roadmap folder below it is a family's index
    (`RepresentationTheory/`), not a roadmap. Each changed file belongs to the nearest roadmap folder
    above it; a file under `completed_dir` or outside `active_dir`, in an index, or in no roadmap
    folder belongs to none. A folder counts when its README.md or a `*.lean` file in it changes, but
    not when every such change is a removal: a roadmap archived or deleted (the listing does not name
    the old path of a renamed file, so a README moved away still reads as on main)."""
    active, completed = active_dir.strip("/") + "/", completed_dir.strip("/") + "/"
    changes: dict[str, str] = {}
    for f in entry.get("files") or []:
        if f.get("previous"):
            changes.setdefault(f["previous"], "removed")
        changes[f.get("path", "")] = f.get("change") or "modified"
    changes = {path: c for path, c in changes.items() if path.startswith(active) and not path.startswith(completed)}
    readmes = set(on_main)
    for path, change in changes.items():
        if path.endswith("/README.md"):
            folder = path[len(active):-len("/README.md")]
            (readmes.discard if change == "removed" else readmes.add)(folder)
    roadmaps = {d for d in readmes if d and re.fullmatch(board.NAME_RE, d.rsplit("/", 1)[-1])}
    # a roadmap with sub-roadmaps below it is a family index (its README is a table of contents, not reviewed)
    # unless the configuration names it as a roadmap in its own right (`prreview.parent_roadmaps`)
    index = {d for d in roadmaps if any(other.startswith(d + "/") for other in roadmaps)} - set(parents)
    touched: dict[str, list[tuple[str, str]]] = {}
    for path, change in changes.items():
        parts = path[len(active):].split("/")[:-1]
        while parts and "/".join(parts) not in roadmaps:
            parts.pop()
        folder = "/".join(parts)
        if folder and folder not in index:
            touched.setdefault(folder, []).append((path[len(active):], change))
    out = {}
    for folder, items in sorted(touched.items()):
        reviewed = [c for path, c in items if path == f"{folder}/README.md" or path.endswith(".lean")]
        if any(c != "removed" for c in reviewed):
            out[folder] = changes.get(f"{active}{folder}/README.md", "")
    return out


def review_plan(entry: dict, cfg, on_main: set[str]) -> tuple[dict[str, str], str]:
    """(the roadmap folders of a pull request to review, as `review_folders` gives them; '' or why
    none is). A pull request that adds or edits no roadmap folder has none, and no reason (a progress
    report, a change under `completed_dir`). A chore has none: a pull request titled as one
    (`CHORE_TITLE`), or one that edits more than `[prreview] max_folders` roadmap folders, unless it
    adds the README.md of every one of them (a bundle of new roadmaps)."""
    folders = review_folders(entry, cfg.roadmap.active_dir, cfg.roadmap.completed_dir, on_main,
                             set(cfg.prreview.parent_roadmaps))
    if not folders:
        return {}, ""
    if CHORE_TITLE.match(entry.get("title") or ""):
        return {}, "chore by its title"
    if len(folders) > cfg.prreview.max_folders and any(c != "added" for c in folders.values()):
        return {}, f"chore: {len(folders)} roadmap folders"
    return folders, ""


def author_reviewed(p, author: str) -> bool:
    """Whether `[prreview] authors` and `skip_authors` let a pull request by `author` be reviewed."""
    who = (author or "").lower()
    if p.authors and who not in {a.lower() for a in p.authors}:
        return False
    return who not in {a.lower() for a in p.skip_authors}


def prreview_verdicts(ctx, name: str, jobs: list[Job] | None = None, head: str = "",
                      texts: dict[str, str | None] | None = None) -> dict[str, dict]:
    """The verdicts of the roadmap's merged pull-request reviews, read from the board's main: job id
    -> its `review` object ({status, head, pr, ...}). Only the done PRREVIEW jobs of `name` (among
    `jobs`, else the board's) are read, and with `head` only those that reviewed it: one read each,
    unless `texts` (path -> text, None for a file that is not there: `prbatch.verdict_texts`) has it.
    A verdict that is not there counts as none; a read GitHub did not answer raises (a label is never
    taken off for a blip)."""
    jobs = ctx.board.kind_jobs("prreview") if jobs is None else jobs
    repo, out = ctx.cfg.repositories.packages_repo(), {}
    for job in jobs:
        if job.kind != "prreview" or job.name != name or job.state != "done" or board.SUPERSEDED_LABEL in job.labels:
            continue
        if head and job.inputs.get("head") != head:
            continue
        path = board.prreview_verdict_path(ctx.cfg.board.packages_dir, job)
        try:
            if texts is not None and path in texts:
                if texts[path] is None:
                    continue
                data = json.loads(texts[path])
            else:
                data = json.loads(ctx.gh.raw_file(repo, path))
        except GHError as exc:
            if transient(exc):
                raise
            continue
        except ValueError:
            continue
        review = data.get("review") if isinstance(data, dict) else None
        if isinstance(review, dict):
            out[job.id] = review
    return out


def previous_review(jobs: list[Job]) -> Job | None:
    """The review a new round of one roadmap folder of a pull request follows, among `jobs` (that
    folder's earlier reviews of that pull request): the latest done one (a superseded job reviewed
    nothing), else the latest submitted one; None when there is neither, or when it names no head."""
    done = [j for j in jobs if j.state == "done" and board.SUPERSEDED_LABEL not in j.labels]
    found = max(done or [j for j in jobs if j.state == "submitted"],
                key=lambda j: (board.parse_job_id(j.id).round, j.number or 0), default=None)
    return found if found is not None and found.inputs.get("head") else None


def rereview_inputs(jobs: list[Job]) -> dict:
    """The inputs that make a new review round a re-review of a repaired head: `previous_head` and
    `previous_job` from `previous_review`, and `rereview` (False for a first review)."""
    before = previous_review(jobs)
    if before is None:
        return {"rereview": False}
    return {"previous_head": str(before.inputs["head"]), "previous_job": before.id, "rereview": True}


def dispute_pending(jobs: list[Job], comments: list[dict], head: str, verdict: dict | None) -> bool:
    """Whether the reviews of `head` among `jobs` (one folder's, all finished) ended in a dispute that
    no round has re-examined: the latest verdict of `head` does not accept, and a reply on the pull
    request records `head` itself as the head it answered at (`replies.answered_record`), so nothing
    was pushed in answer. A head moved by its repair is re-reviewed by the ordinary filing."""
    if not jobs or any(j.under_way(OPEN_ROUND) for j in jobs):
        return False
    if not verdict or verdict.get("status") == "accepted":
        return False
    return bool(head_worker(comments, head))


def text_pending(jobs: list[Job], comments: list[dict], head: str, verdict: dict | None) -> bool:
    """Whether the reviews of `head` among `jobs` (one folder's, all finished) were last answered by a
    rewrite of the pull request's title or description that no round has read: the latest verdict of
    `head` does not accept, and the latest reply recording `head` as the head it answered at that
    answers the latest of those reviews records that it rewrote the text (`text`,
    `replies.answered_marker`). Replies to another folder's reviews of the same head are passed over:
    each folder's review is answered on its own. The head did not move, so nothing else tells the
    intake that what the review read has changed."""
    if not dispute_pending(jobs, comments, head, verdict):
        return False
    last = max(jobs, key=lambda j: (board.parse_job_id(j.id).round, j.number or 0))
    for c in reversed(comments):
        record = replies.answered_record(c.get("body") or "")
        if record and record.get("head") == head and record.get("worker") and record.get("job") == last.id:
            return bool(record.get("text"))
    return False


def head_worker(comments: list[dict], head: str) -> str:
    """The worker whose reply on a pull request records `head` as the head it answered at (its
    answered record, `replies.answered_record`: a gate round's, or a review's repair), the latest such
    reply's; '' when none does."""
    for c in reversed(comments):
        record = replies.answered_record(c.get("body") or "")
        if record and record.get("head") == head and record.get("worker"):
            return str(record["worker"])
    return ""


def supersede(ctx, job: Job, note: str, expect: str = "available",
              closing: str = "This review is no longer needed") -> bool:
    """Close a job nobody has taken (still in state `expect`) as done, its issue labelled `superseded`
    with `note` and `closing`: it stays a job of the board (its round and head count), and is never
    taken. False when a worker took it meanwhile (it is left to finish)."""
    if not ctx.board.set_state(job, "done", expect=expect):
        return False
    ctx.board.close(job, f"{note}. {closing}; closed by the board.", board.SUPERSEDED_LABEL)
    return True


def carried_from(comments: list[dict], head: str) -> str:
    """The head whose acceptance the latest carried record for `head` on a pull request claims
    (`replies.carried_record`); '' when none does."""
    for c in reversed(comments or []):
        record = replies.carried_record(c.get("body") or "")
        if record and record.get("head") == head and record.get("from"):
            return str(record["from"])
    return ""


def folder_change_lines(ctx, repo: str, old: str, new: str, folders, active_dir: str) -> int | None:
    """Insertions plus deletions between `old` and `new` inside the roadmap folders `folders` (names
    under `active_dir`); None when the comparison cannot be read or a file outside them changed (then
    nothing is carried)."""
    try:
        compared = ctx.gh.api(f"repos/{repo}/compare/{old}...{new}") or {}
    except GHError:
        return None
    prefixes = [f"{active_dir.strip('/')}/{str(name).strip('/')}/" for name in folders]
    total = 0
    for f in compared.get("files") or []:
        path = str(f.get("filename") or "")
        if not any(path.startswith(prefix) for prefix in prefixes):
            return None
        total += int(f.get("additions") or 0) + int(f.get("deletions") or 0)
    return total


def carried_acceptance(ctx, repo: str, number: int, head: str, folders, jobs: list[Job], texts, p,
                       threads: dict, batch: int) -> tuple[str, int]:
    """(the accepted head whose acceptance carries to `head`, the lines changed between them), or
    ('', 0): a chain of carried records leads from `head` back to a head an AI review of every folder
    accepted, and the folders changed by at most `[prreview] small_change_lines` lines in all. Nothing
    is carried past a review of `head` itself that does not accept (`refused_at`): a later review,
    such as a check of the text that asked for changes, stands even when its worker could not take the
    label off."""
    if refused_at(ctx, folders, jobs, head, texts):
        return "", 0
    if number not in threads:
        try:
            threads[number] = prbatch.PullReader(ctx.gh, repo, [number], batch).comments(number)
        except GHError:
            threads[number] = None
    comments, seen, at = threads.get(number) or [], set(), head
    while True:
        source = carried_from(comments, at)
        if not source or source == at or source in seen:
            return "", 0
        seen.add(source)
        if folders_accepted(ctx, folders, jobs, source, texts)[0]:
            lines = folder_change_lines(ctx, repo, source, head, folders, ctx.cfg.roadmap.active_dir)
            return (source, lines) if lines is not None and lines <= p.small_change_lines else ("", 0)
        at = source


def chore_accepted(ctx, repo: str, number: int, head: str, why: str, p, threads: dict,
                   batch: int) -> tuple[bool, str]:
    """(whether a chore review accepts `head`, '' or why not): the latest comment on the pull request
    whose chore record (`replies.chore_record`) names `head`, by a pool member or a
    `[review] comment_reviewers` login, with status `accepted`. A chore gets no folder review
    (`why` says so); its AI review is of the whole change and is posted by the operator who ran it."""
    if number not in threads:
        try:
            threads[number] = prbatch.PullReader(ctx.gh, repo, [number], batch).comments(number)
        except GHError:
            threads[number] = None
    trusted = {str(x).lower() for x in list(ctx.cfg.roadmap.pool) + list(ctx.cfg.review.comment_reviewers)}
    for c in reversed(threads.get(number) or []):
        record = replies.chore_record(c.get("body") or "")
        if not record or record.get("head") != head:
            continue
        who = str(((c.get("user") or {}).get("login")) or "").lower()
        if who not in trusted:
            continue
        if record.get("status") == "accepted":
            return True, ""
        return False, f"the chore review of {head[:7]} does not accept it"
    return False, f"not reviewed: {why}; no chore review of {head[:7]}"


def finished_reviews(jobs: list[Job], head: str) -> list[Job]:
    """The done reviews of `head` among `jobs` (a superseded job reviewed nothing)."""
    return [j for j in jobs
            if j.state == "done" and j.inputs.get("head") == head and board.SUPERSEDED_LABEL not in j.labels]


def latest_verdict(ctx, jobs: list[Job], head: str, texts: dict[str, str | None] | None = None) -> dict | None:
    """The verdict of the latest finished review of `head` among `jobs` (one roadmap folder's reviews
    of a pull request), read from the board's main newest round first (`texts`: those already read),
    one read each until one is there; None when none is."""
    return latest_reviewed(ctx, jobs, head, texts)[1]


def latest_reviewed(ctx, jobs: list[Job], head: str,
                    texts: dict[str, str | None] | None = None) -> tuple[Job | None, dict | None]:
    """(the latest finished review of `head` among `jobs` whose verdict is on the board, that verdict),
    as `latest_verdict` finds it; (None, None) when there is none."""
    done = finished_reviews(jobs, head)
    for job in sorted(done, key=lambda j: board.parse_job_id(j.id).round, reverse=True):
        found = prreview_verdicts(ctx, job.name, [job], head, texts)
        if job.id in found:
            return job, found[job.id]
    return None, None


def refused_at(ctx, folders, jobs: list[Job], head: str, texts) -> bool:
    """Whether a folder's latest finished review of `head` itself does not accept: then no acceptance of
    an earlier head is carried to `head` (`carried_acceptance`, `unchanged_acceptance`). A check of the
    text that asked for changes is such a review."""
    for folder in folders:
        here = [j for j in finished_reviews(jobs, head) if board.prreview_folder(j) == folder]
        if here:
            verdict = latest_verdict(ctx, here, head, texts)
            if not verdict or verdict.get("status") != "accepted":
                return True
    return False


def folders_accepted(ctx, folders, jobs: list[Job], head: str,
                     texts: dict[str, str | None] | None = None) -> tuple[bool, str]:
    """(whether an AI review of `head` accepts every roadmap folder of a pull request, '' or why not).
    `jobs`: the pull request's PRREVIEW jobs. Verdicts are read only once every folder has a finished
    review of `head`, the most recent first, and no further than the first that does not accept."""
    if not folders:
        return False, "no roadmap folder to review"
    finished = {f: [j for j in jobs if board.prreview_folder(j) == f and j.inputs.get("head") == head
                    and j.state == "done" and board.SUPERSEDED_LABEL not in j.labels] for f in folders}
    missing = [f for f in folders if not finished[f]]
    if len(missing) == len(finished):
        if not jobs:
            return False, "no AI review accepted it"
        return False, ("head moved" if all(j.inputs.get("head") != head for j in jobs)
                       else f"no AI review of {head[:7]} has finished")
    if missing:
        return False, f"{len(missing)} of {len(finished)} roadmap folders not reviewed at {head[:7]}"
    for folder in sorted(finished, key=lambda f: max(j.number for j in finished[f]), reverse=True):
        verdict = latest_verdict(ctx, finished[folder], head, texts)
        if not verdict or verdict.get("status") != "accepted" or verdict.get("head") != head:
            which = f" ({folder})" if len(finished) > 1 else ""
            return False, f"the AI review of {head[:7]} does not accept it{which}"
    return True, ""


def carried_record(verdict: dict | None, small: int) -> dict | None:
    """The record a review's verdict carries when the review ended with its repair (`[prreview]
    carry_minor`; the lane's `carry_record`), or None: the verdict asks for changes, names the head it
    reviewed as the record's `from`, and the repair changed at most `small` lines of the folder."""
    record = (verdict or {}).get("carried")
    if not isinstance(record, dict) or (verdict or {}).get("status") != "needs_changes":
        return None
    lines = record.get("lines")
    if not record.get("head") or not isinstance(record.get("head"), str) or record.get("from") != verdict.get("head"):
        return None
    if isinstance(lines, bool) or not isinstance(lines, int) or not 0 <= lines <= small:
        return None
    return record


def settle_points(ctx, folders, jobs: list[Job], small: int,
                  texts: dict[str, str | None] | None = None, minor: bool = True
                  ) -> tuple[dict[str, str], list[tuple[str, str, set[str], bool]]]:
    """(folder -> the head its latest finished review settles it at, the P3-only repairs as steps
    (from, to, the folders changed, whether a file outside them changed)): a review settles its folder
    at the head it accepted, or (`minor`) at the head its repair left when every finding was P3
    (`carried_record`). One verdict is read per folder."""
    at: dict[str, str] = {}
    steps: list[tuple[str, str, set[str], bool]] = []          # (from, to, the folders changed, changed outside them)
    for folder in folders:
        done = [j for j in jobs if board.prreview_folder(j) == folder and j.state == "done"
                and board.SUPERSEDED_LABEL not in j.labels and j.inputs.get("head")]
        if not done:
            continue
        job = max(done, key=round_key)
        verdict = prreview_verdicts(ctx, job.name, [job], "", texts).get(job.id)
        if not verdict or verdict.get("head") != job.inputs.get("head"):
            continue
        if verdict.get("status") == "accepted":
            at[folder] = str(verdict["head"])
        elif minor and (record := carried_record(verdict, small)):
            at[folder] = record["head"]
            if record["head"] != record["from"]:
                changed = record.get("folders")
                steps.append((str(record.get("onto") or record["from"]), record["head"],
                              {str(f) for f in changed} if isinstance(changed, list) else set(folders),
                              bool(record.get("outside")) or not isinstance(changed, list)))
    return at, steps


def settled_folders(ctx, folders, jobs: list[Job], head: str, small: int,
                    texts: dict[str, str | None] | None = None) -> set[str]:
    """With `[prreview] carry_minor`: the roadmap folders of a pull request (`jobs`: its PRREVIEW jobs)
    that need no review at `head`. A folder's latest finished review settles it at one head
    (`settle_points`). The folder is settled at `head` when that is this head, or when only P3-only
    repairs of the other folders lie between the two, none of which changed this folder's files or a
    file outside the pull request's roadmap folders (a merge of the base branch is such a change)."""
    at, steps = settle_points(ctx, folders, jobs, small, texts)
    out = set()
    for folder, start in at.items():
        seen, todo = {start}, [start]
        while todo and head not in seen:
            here = todo.pop()
            for source, target, changed, outside in steps:
                if source == here and target not in seen and folder not in changed and not outside:
                    seen.add(target)
                    todo.append(target)
        if head in seen:
            out.add(folder)
    return out


REVIEWED_HEADS = 3            # earlier heads a moved head is compared with (`unchanged_acceptance`), per source


def reviewed_heads(folders, jobs: list[Job], head: str) -> list[str]:
    """The heads other than `head` at which every folder of `folders` has a finished review among
    `jobs` (a pull request's PRREVIEW jobs), the most recently reviewed first."""
    last: dict[str, int] = {}
    per: list[set[str]] = []
    for folder in folders:
        heads = set()
        for j in jobs:
            at = j.inputs.get("head")
            if (board.prreview_folder(j) == folder and j.state == "done" and board.SUPERSEDED_LABEL not in j.labels
                    and at and at != head):
                heads.add(str(at))
                last[str(at)] = max(last.get(str(at), 0), j.number or 0)
        per.append(heads)
    return sorted(set.intersection(*per), key=lambda h: last[h], reverse=True) if per else []


def unchanged_folders(ctx, repo: str, head: str, folders, jobs: list[Job], small: int,
                      texts: dict[str, str | None] | None, batch: int, minor: bool
                      ) -> tuple[dict[str, str], bool]:
    """With `[prreview] carry_unchanged_folders`: (roadmap folder -> the head its latest finished review
    settled it at, for the folders of a pull request that need no review at `head`; whether that could
    be told). A folder needs none when that review settles it (`settle_points`: it accepted, or with
    `minor` found P3 points only and had its repair) at `head`, or at an earlier head where the folder
    held the files it holds at `head` (one git tree, `prbatch.tree_ids`): the head moved by a repair of
    another folder or a merge of the base branch, and this folder is byte for byte what was read. The
    review that counts is the folder's latest finished one, so an acceptance is never carried past a
    later review of the folder; a folder with a review under way is left to that review. ({}, False)
    when GitHub did not give a tree: nothing is known, and the caller files nothing and labels nothing
    on that ground."""
    at, _steps = settle_points(ctx, folders, jobs, small, texts, minor)
    busy = {board.prreview_folder(j) for j in jobs if j.under_way(("claimed", "submitted"))}
    at = {folder: start for folder, start in at.items() if folder not in busy}
    moved = {folder: start for folder, start in at.items() if start != head}
    if not moved:
        return at, True
    base = ctx.cfg.roadmap.active_dir.strip("/")
    path_of = {folder: f"{base}/{str(folder).strip('/')}" for folder in moved}
    ids = prbatch.tree_ids(ctx.gh, repo, [(sha, path_of[folder]) for folder, start in moved.items()
                                          for sha in (head, start)], batch)
    out = {folder: start for folder, start in at.items() if start == head}
    for folder, start in moved.items():
        here, there = (head, path_of[folder]), (start, path_of[folder])
        if here not in ids or there not in ids:
            return {}, False
        if ids[here] and ids[here] == ids[there]:
            out[folder] = start
    return out, True


def unchanged_acceptance(ctx, repo: str, number: int, head: str, folders, jobs: list[Job], texts, p,
                         threads: dict, batch: int, labelled: bool) -> tuple[str, bool]:
    """With `[prreview] carry_unchanged`: (the earlier head whose acceptance stands at `head`, whether
    the comparison could be made). The acceptance of an earlier head stands at `head` when the git tree
    of every roadmap folder of the pull request is the same at both (the head only moved: a rebase, a
    merge of the base branch, a push outside the folders) and the earlier head was accepted: every
    folder accepted there by its review, or the head settled by `carry_accept` or `carry_minor`. The
    earlier heads tried are the last `REVIEWED_HEADS` at which every folder has a finished review, the
    heads P3-only repairs left (`carry_minor`), and, on a labelled pull request, the heads its carried
    records name (`carry_accept`). A finished review of `head` itself that does not accept stands: then
    nothing is carried. So does the latest finished review of a folder, when it read the files `head`
    holds and did not accept them: an acceptance is not carried past a later review of the same files.
    ('', False) when GitHub did not give the trees: nothing is known, and the caller leaves the pull
    request as it is."""
    if refused_at(ctx, folders, jobs, head, texts):
        return "", True
    tried = reviewed_heads(folders, jobs, head)[:REVIEWED_HEADS]
    if p.carry_minor:
        at, _steps = settle_points(ctx, folders, jobs, p.small_change_lines, texts)
        tried += [h for h in at.values() if h != head]
    marked = p.carry_accept and labelled
    if marked:
        if number not in threads:
            try:
                threads[number] = prbatch.PullReader(ctx.gh, repo, [number], batch).comments(number)
            except GHError:
                threads[number] = None
        named = []
        for c in reversed(threads.get(number) or []):
            record = replies.carried_record(c.get("body") or "")
            if record and record.get("head") and record.get("head") != head and record["head"] not in named:
                named.append(str(record["head"]))
        tried += named[:REVIEWED_HEADS]
    tried = list(dict.fromkeys(tried))
    if not tried:
        return "", True
    path_of = {folder: f"{ctx.cfg.roadmap.active_dir.strip('/')}/{str(folder).strip('/')}" for folder in folders}
    paths = list(path_of.values())
    last: dict = {}             # each folder's latest finished review, of whatever earlier head
    for folder in folders:
        done = [j for j in jobs if board.prreview_folder(j) == folder and j.state == "done"
                and board.SUPERSEDED_LABEL not in j.labels and j.inputs.get("head") not in ("", None, head)]
        if done:
            last[folder] = max(done, key=round_key)
    ids = prbatch.tree_ids(ctx.gh, repo, [(sha, path) for sha in [head] + tried for path in paths]
                           + [(str(job.inputs["head"]), path_of[folder]) for folder, job in last.items()], batch)
    if any((head, path) not in ids for path in paths):
        return "", False
    for folder, job in last.items():
        at, path = str(job.inputs["head"]), path_of[folder]
        if (at, path) not in ids:
            return "", False
        if ids[(head, path)] and ids[(at, path)] == ids[(head, path)]:
            verdict = latest_verdict(ctx, [job], at, texts)
            if not verdict or verdict.get("status") != "accepted":
                return "", True
    known = True
    for source in tried:
        if any((source, path) not in ids for path in paths):
            known = False
            continue
        if any(not ids[(head, path)] or ids[(source, path)] != ids[(head, path)] for path in paths):
            continue
        if folders_accepted(ctx, folders, jobs, source, texts)[0]:
            return source, True
        if marked and carried_acceptance(ctx, repo, number, source, folders, jobs, texts, p, threads, batch)[0]:
            return source, True
        if p.carry_minor and set(folders) <= settled_folders(ctx, folders, jobs, source, p.small_change_lines, texts):
            return source, True
    return "", known


def acceptance_sources(ctx, repo: str, number: int, source: str, folders, jobs: list[Job], texts, p,
                       threads: dict, batch: int, labelled: bool) -> dict[str, str]:
    """Folder -> the head of the review that establishes its acceptance at `source`, an earlier head
    whose acceptance `unchanged_acceptance` carried, decided in the order that function tries them:
    `source` itself when every folder's review of it accepts; else the accepted head a chain of carried
    records leads back to (`carry_accept`); else, settled by the folders' latest reviews and their
    P3-only repairs (`carry_minor`), the head each folder's latest review read. Never a later review of
    other files: a review that read files `source` does not hold is not what accepted it."""
    if folders_accepted(ctx, folders, jobs, source, texts)[0]:
        return dict.fromkeys(folders, source)
    if p.carry_accept and labelled:
        accepted, _lines = carried_acceptance(ctx, repo, number, source, folders, jobs, texts, p, threads, batch)
        if accepted:
            return dict.fromkeys(folders, accepted)
    return {f: last_reviewed_head(jobs, f, source) for f in folders}


def answer_worker(comments: list[dict], head: str, job_id: str) -> str:
    """The worker whose latest reply records `head` as the head it answered at and answers the review
    `job_id` (its answered record), '' when none does: the one that repaired that folder's text, which
    may not take the round that reads its repair."""
    for c in reversed(comments):
        record = replies.answered_record(c.get("body") or "")
        if record and record.get("head") == head and record.get("worker") and record.get("job") == job_id:
            return str(record["worker"])
    return ""


def last_reviewed_head(jobs: list[Job], folder: str, default: str) -> str:
    """The head the latest finished review of `folder` among `jobs` read (`previous_review`), else `default`."""
    job = previous_review([j for j in jobs if board.prreview_folder(j) == folder])
    return str((job.inputs.get("head") if job else "") or default)


def next_review_round(reviews, leaf: str) -> int:
    """The round a new review of a folder takes: one past the highest the board has for it. A count of
    the folder's rounds names a round that exists as soon as one number is missing from the board (an
    issue deleted or filed under another kind), and `create` then returns that finished round, so the
    folder gets no review however often the intake runs."""
    return 1 + max((board.parse_job_id(j.id).round for j in reviews if j.name == leaf), default=0)


def sync_pr_reviews(ctx) -> list[str]:
    """`board sync`, with `[prreview] enabled`: the AI review of every roadmap folder an open pull
    request of the content repository adds or edits, whoever opened it, drafts included.

    The pull requests come from `openroadmaps.inventory`: one GraphQL listing gives every open pull
    request with its head, author, labels and changed files (with how each changes), and one read of
    main's tree (`openroadmaps.readme_folders`) says which folders hold a README.md the pull requests
    leave alone. `review_plan` turns them into the roadmap folders to review, nested ones included,
    and says which pull requests are chores. `openroadmaps.refresh` is not used: it also fetches every
    head into a mirror, which a hosted board run (a fresh runner each time) would clone again on every
    run, and it keeps an index for half an hour, while filing needs the heads as they are now.

    * An available PRREVIEW job whose pull request moved to another head, or was closed, is closed as
      superseded (`supersede`); a claimed or submitted one is left to finish.
    * `[prreview] label` is on a pull request exactly when an AI review of its current head accepts
      every roadmap folder it adds or edits (`folders_accepted`, from the verdicts on the board's
      main): added when the last folder's verdict lands, removed otherwise; while a review of that
      head is being finished (claimed or submitted) it is left as it is.
      With `[prreview] carry_unchanged`, a head that only moved (a rebase, a merge of the base
      branch: every roadmap folder's git tree is that of an accepted earlier head) counts as that
      head does (`unchanged_acceptance`): the label stays or is put back, the reviews of the new head
      nobody took are closed as superseded, and none is filed. When GitHub does not give the trees,
      the pull request is left as it is for this run.
      With `[prreview] carry_unchanged_folders` the same holds folder by folder (`unchanged_folders`):
      a folder its latest finished review settled, and whose git tree is the one that review read, is
      not reviewed at a head another folder's repair left, and counts as accepted there; a review of
      it filed for that head and not taken is closed as superseded.
      With `[prreview] chore_review`, a chore (no folder review) carries the label while a comment by
      a pool member records an accepting chore review of its current head (`chore_accepted`).
    * `PRREVIEW-<Leaf>~N` (`<Leaf>` the folder's last component, N one more than that name's PRREVIEW
      jobs; inputs `folder`, `pr`, `head`, `owner`, `name`, `roadmaps`, every folder of the pull
      request, when it has several, and `rereview`: after an earlier review of that folder of that
      pull request, a re-review of the repaired head, with that review's `previous_head` and
      `previous_job` (`rereview_inputs`), and `not_worker`, the worker whose reply records that it
      produced this head (`head_worker`), which may not take it) is filed for each roadmap folder of a
      pull request whose authors
      `[prreview] authors` and `skip_authors` admit, unless a review of that folder of that pull
      request names this head or is open, or a gate round of the pull request is open (it is about to
      move the head): at most `[prreview] per_run` jobs per run, oldest pull request first, every
      folder of one pull request before the next. Any operator may take it except the author
      (`PRReviewLane.eligible`).
    A label the token cannot change (a hosted board run's token may not write to the content
    repository) is reported, not raised."""
    cfg = ctx.cfg
    p = cfg.prreview
    if not p.enabled:
        return []
    repo = cfg.repositories.content
    listed = sorted(openroadmaps.inventory(ctx.gh, repo, (cfg.roadmap.active_dir,), any_file=True),
                    key=lambda x: int(x["pr"]))
    live = {int(x["pr"]): x for x in listed}
    on_main = openroadmaps.readme_folders(ctx.gh, repo, cfg.project.branch, cfg.roadmap.active_dir) if listed else set()
    plans = {int(x["pr"]): review_plan(x, cfg, on_main) for x in listed}
    reviews = sorted(ctx.board.kind_jobs("prreview"), key=lambda j: j.number)
    gate_rounds = ctx.board.kind_jobs("gate", "open")
    out = []

    def pr_of(job: Job) -> int:
        try:
            return int(job.inputs.get("pr") or 0)
        except (TypeError, ValueError):
            return 0

    batch = cfg.board.graphql_batch
    available = [j for j in reviews if j.state == "available"]
    unlisted = prbatch.PullReader(ctx.gh, repo, {pr_of(j) for j in available if pr_of(j) not in live}, batch)
    for job in available:
        number, entry = pr_of(job), live.get(pr_of(job))
        if entry is not None:
            head, state = entry.get("head", ""), "open"
        else:
            try:     # not in the listing: closed, or no longer touching a roadmap folder
                data = unlisted.pull(number)
            except GHError as exc:
                out.append(f"#{number}: {job.id} not checked ({str(exc).splitlines()[0][:160]})")
                continue
            head, state = (data.get("head") or {}).get("sha", ""), data.get("state") or "?"
        if state != "open":
            note = f"Closed: #{number} is closed"
        elif head != job.inputs.get("head"):
            note = f"Superseded: #{number} is at {head[:7]}"
        else:
            continue
        if supersede(ctx, job, note):
            out.append(f"#{number}: {job.id} superseded")

    # The verdicts the label rule may read, all at once (`prbatch.verdict_texts`; REST for the rest).
    wanted = [j for entry in listed if plans[int(entry["pr"])][0]
              for j in finished_reviews([j for j in reviews if pr_of(j) == int(entry["pr"])], entry.get("head", ""))]
    if p.carry_minor or p.carry_unchanged_folders:      # and each folder's latest finished review, which may have
        # ended at an earlier head
        latest: dict[tuple[int, str], Job] = {}
        for j in reviews:
            if j.state == "done" and board.SUPERSEDED_LABEL not in j.labels and pr_of(j) in live:
                key = (pr_of(j), board.prreview_folder(j))
                if key not in latest or round_key(j) > round_key(latest[key]):
                    latest[key] = j
        have = {w.id for w in wanted}
        wanted += [j for j in latest.values() if j.id not in have]
    if p.carry_unchanged:        # and the reviews of the heads a moved head may be compared with
        have = {w.id for w in wanted}
        for entry in listed:
            folders, head = plans[int(entry["pr"])][0], entry.get("head", "")
            mine = [j for j in reviews if pr_of(j) == int(entry["pr"])]
            if not folders or not head or finished_reviews(mine, head):
                continue
            for earlier in reviewed_heads(folders, mine, head)[:REVIEWED_HEADS]:
                wanted += [j for j in finished_reviews(mine, earlier) if j.id not in have]
    texts = prbatch.verdict_texts(ctx.gh, cfg.repositories.packages_repo(), [
        board.prreview_verdict_path(cfg.board.packages_dir, j) for j in wanted], batch)
    minor: dict[int, set[str]] = {}

    def minor_settled(number: int, folders, mine: list[Job], head: str) -> set[str]:
        """The folders of #number that P3-only repairs settled at `head` (`settled_folders`), read once."""
        if not p.carry_minor or not folders or not head:
            return set()
        if number not in minor:
            minor[number] = settled_folders(ctx, folders, mine, head, p.small_change_lines, texts)
        return minor[number]

    kept: dict[int, tuple[dict[str, str], bool]] = {}

    def kept_folders(number: int, folders, mine: list[Job], head: str) -> tuple[dict[str, str], bool]:
        """The folders of #number that need no review at `head`, each with the head its latest review
        settled it at, and whether that could be told (`unchanged_folders`), read once."""
        if not p.carry_unchanged_folders or not folders or not head:
            return {}, True
        if number not in kept:
            kept[number] = unchanged_folders(ctx, repo, head, folders, mine, p.small_change_lines, texts, batch,
                                             p.carry_minor)
        return kept[number]

    def close_untaken(number: int, mine: list[Job], head: str, still: dict[str, str]) -> None:
        """Closes the reviews of `head` nobody took whose folder needs none: it holds the files of the
        earlier head its last review settled it at (`still`, from `kept_folders`)."""
        for job in mine:
            folder = board.prreview_folder(job)
            # (a check of the text is about the description, which the folder's files do not settle)
            untaken = job.state == "available" and job.inputs.get("head") == head and not job.inputs.get("text_check")
            if untaken and still.get(folder, head) != head and supersede(
                    ctx, job, f"Superseded: {folder} of #{number} at {head[:7]} holds the files of "
                              f"{still[folder][:7]}, where its last review settled it"):
                out.append(f"#{number}: {job.id} superseded")

    accepted_prs: set[int] = set()
    carried_prs: set[int] = set()
    by_folder: set[int] = set()             # accepted folder by folder, some at an earlier head with the same files
    unchanged: dict[int, str] = {}          # a head that only moved: the earlier head whose acceptance stands
    accepted_at: dict[int, dict[str, str]] = {}     # an accepted pull request: folder -> the head reviewed for it
    threads: dict[int, list[dict] | None] = {}
    for entry in listed:
        number, head = int(entry["pr"]), entry.get("head", "")
        labelled = p.label in entry.get("labels", [])
        folders, why = plans[number]
        chore = bool(why) and not folders and p.chore_review
        if not head or not (labelled or folders or chore):
            continue
        mine = [j for j in reviews if pr_of(j) == number]
        if any(j.under_way(("claimed", "submitted")) and j.inputs.get("head") == head for j in mine):
            # being finished: its lane labels, or a later run does. Its other folders get no review of a
            # head that only moved (`unchanged_acceptance`): the review under way decides for its own.
            if folders and p.carry_unchanged:
                source, known = unchanged_acceptance(ctx, repo, number, head, folders, mine, texts, p, threads,
                                                     batch, labelled)
                if source or not known:
                    carried_prs.add(number)
            still, known = kept_folders(number, folders, mine, head)
            if known:
                close_untaken(number, mine, head, still)
            continue
        accepted, reason = (folders_accepted(ctx, folders, mine, head, texts) if folders
                            else chore_accepted(ctx, repo, number, head, why, p, threads, batch) if chore
                            else (False, f"not reviewed: {why}" if why else "no roadmap folder to review"))
        if accepted and folders:
            accepted_at[number] = dict.fromkeys(folders, head)
        if not accepted and labelled and folders and p.carry_accept:
            source, lines = carried_acceptance(ctx, repo, number, head, folders, mine, texts, p, threads, batch)
            if source:
                accepted, reason = True, f"carried from {source[:7]}"
                accepted_at[number] = dict.fromkeys(folders, source)
                carried_prs.add(number)
                out.append(f"#{number}: {p.label} kept (the acceptance of {source[:7]} carries to {head[:7]}: "
                           f"{lines} lines of repairs inside the roadmap folders, no review filed)")
        carried = False
        if not accepted and folders and set(folders) <= minor_settled(number, folders, mine, head):
            accepted, carried = True, True
            accepted_at[number] = {f: last_reviewed_head(mine, f, head) for f in folders}
        if not accepted and folders and p.carry_unchanged:
            source, known = unchanged_acceptance(ctx, repo, number, head, folders, mine, texts, p, threads, batch,
                                                 labelled)
            if not known:
                carried_prs.add(number)         # nothing is known this run: the label stays as it is, no review is filed
                out.append(f"#{number}: {p.label} not checked (the roadmap folders at {head[:7]} could not be "
                           "compared with those of an earlier head)")
                continue
            if source:
                accepted, unchanged[number] = True, source
                accepted_at[number] = acceptance_sources(ctx, repo, number, source, folders, mine, texts, p, threads,
                                                         batch, labelled)
                carried_prs.add(number)
                for job in mine:
                    if (job.state == "available" and job.inputs.get("head") == head
                            and not job.inputs.get("text_check") and supersede(
                            ctx, job, f"Superseded: the roadmap folders of #{number} at {head[:7]} are those of "
                                      f"{source[:7]}, whose acceptance stands")):
                        out.append(f"#{number}: {job.id} superseded")
                if labelled:
                    out.append(f"#{number}: {p.label} kept (the roadmap folders at {head[:7]} are those of "
                               f"{source[:7]}, whose acceptance stands: the head only moved, no review filed)")
        if not accepted and folders and p.carry_unchanged_folders:
            still, known = kept_folders(number, folders, mine, head)
            if not known:
                carried_prs.add(number)         # as above: nothing is known this run
                out.append(f"#{number}: {p.label} not checked (the roadmap folders at {head[:7]} could not be "
                           "compared with those their last reviews read)")
                continue
            close_untaken(number, mine, head, still)
            if set(folders) <= set(still):
                accepted = True
                accepted_at[number] = {f: last_reviewed_head(mine, f, still[f]) for f in folders}
                by_folder.add(number)
                carried_prs.add(number)
                if labelled:
                    out.append(f"#{number}: {p.label} kept (every roadmap folder at {head[:7]} holds the files "
                               "its last review settled: no review filed)")
        if accepted:
            accepted_prs.add(number)
        try:
            if accepted and not labelled:
                ctx.gh.write("POST", f"repos/{repo}/issues/{number}/labels", {"labels": [p.label]})
                out.append(f"#{number}: {p.label} added ("
                           + (f"the roadmap folders at {head[:7]} are those of {unchanged[number][:7]}, whose "
                              "acceptance stands: the head only moved" if number in unchanged
                              else f"every roadmap folder at {head[:7]} holds the files its last review settled"
                              if number in by_folder
                              else f"every roadmap folder is settled at {head[:7]}: the last reviews accepted, or found "
                              "P3 points only and their repairs are in" if carried
                              else f"the AI review of {head[:7]} accepts "
                              + (f"all {len(folders)} roadmap folders" if len(folders) > 1 else "it")) + ")")
            elif labelled and not accepted:
                ctx.gh.write("DELETE", f"repos/{repo}/issues/{number}/labels/{quote(p.label, safe='')}")
                out.append(f"#{number}: {p.label} removed ({reason})")
        except GHError as exc:
            out.append(f"#{number}: {p.label} on {repo} not {'added' if accepted else 'removed'} "
                       f"({str(exc).splitlines()[0][:160]})")

    made, waiting = 0, 0
    text_due: list[tuple[int, dict, list[str], str, list[Job]]] = []

    def file_round(number: int, entry: dict, folder: str, ours: list[Job], mine: list[Job], extra: dict,
                   dispute: bool = False, note: str = "", answered: str = "") -> bool:
        """Create one PRREVIEW job of `folder` of pull request `number` at its current head; False when
        it was not filed this run (its comments could not be read). `answered`: the review of this
        folder whose answer at the unmoved head the round reads (a dispute or a text round); the worker
        that answered it is the one that may not take the round (`answer_worker`), else the worker
        that produced the head (`head_worker`)."""
        nonlocal made
        head, author = entry.get("head", ""), entry.get("author", "")
        leaf = folder.rsplit("/", 1)[-1]
        inputs = {"pr": number, "head": head, "owner": author, "name": leaf, "folder": folder}
        if len(plans[number][0]) > 1:
            inputs["roadmaps"] = list(plans[number][0])
        inputs.update(rereview_inputs(ours))
        if dispute:
            inputs.update({"rereview": False, "dispute_rereview": True})
        inputs.update(extra)
        if inputs["rereview"] or dispute:
            if number not in threads:
                try:
                    threads[number] = prbatch.PullReader(ctx.gh, repo, [number], batch).comments(number)
                except GHError as exc:
                    threads[number] = None
                    out.append(f"#{number}: not filed this run (its comments could not be read: "
                               f"{str(exc).splitlines()[0][:160]})")
            if threads[number] is None:
                return False
            worker = answered and answer_worker(threads[number], head, answered)
            if worker := worker or head_worker(threads[number], head):
                inputs["not_worker"] = worker
        job = board.new_job(board.make_id("prreview", leaf, next_review_round(reviews, leaf)), inputs=inputs)
        issue = ctx.board.create(job)
        job.number, job.state = issue, "available"
        reviews.append(job)
        mine.append(job)
        made += 1
        if job.id in getattr(ctx.board, "reused", {}):
            out.append(f"#{number}: {job.id} already on the board as #{issue}")
        else:
            out.append(f"#{number}: {job.id} created{note}")
        return True

    for entry in listed:
        number, head, author = int(entry["pr"]), entry.get("head", ""), entry.get("author", "")
        settled = number in accepted_prs or p.label in entry.get("labels", [])     # an accepted head: no dispute
        folders, why = plans[number]
        if why:
            out.append(f"#{number}: not reviewed ({why})")
            continue
        if not folders or not author_reviewed(p, author) or not head:
            continue
        if number in p.operator_prs:
            out.append(f"#{number}: rounds filed by the operator (prreview.operator_prs)")
            continue
        if any(pr_of(g) == number and g.state in OPEN_ROUND for g in gate_rounds):
            continue
        mine = [j for j in reviews if pr_of(j) == number]
        if number in accepted_at and p.text_per_run and not any(j.under_way(OPEN_ROUND) for j in mine):
            # An accepted pull request: its text may not be what the reviews of its folders read.
            text_due.append((number, entry, accepted_at[number], mine))
        if number in carried_prs:
            continue             # small repairs on an accepted head: the acceptance carries, no round
        # (an accepted head needs no comparison: its folders are reviewed there, or settled by P3-only repairs)
        still, known = ({}, True) if number in accepted_prs else kept_folders(number, folders, mine, head)
        if not known:
            out.append(f"#{number}: no review filed (the roadmap folders at {head[:7]} could not be compared "
                       "with those their last reviews read)")
            continue
        for folder in folders:
            ours = [j for j in mine if board.prreview_folder(j) == folder]
            if any(j.under_way(OPEN_ROUND) for j in ours):
                continue
            if folder in minor_settled(number, folders, mine, head):
                continue         # its last review accepted, or found P3 points only and the repair is in
            if still.get(folder, head) != head:
                continue         # settled at an earlier head, and the folder holds the files it held there
            same = [j for j in ours if j.inputs.get("head") == head]
            dispute = text_round = False
            if same and settled:
                continue
            if same:
                # 2026-11-02: a review answered without moving the head (the reply's answered record names the
                # reviewed head: a dispute) gets one more round, a full review by another worker; a head whose
                # reply moved it is re-reviewed by the ordinary path, and a head reviewed twice is settled.
                # 2026-11-10: unless the answer to the second review rewrote the title or description (its
                # record says `text`): that text gets one check round, an incremental one, and then the head
                # is settled (a revision whose second review asked only for a corrected description had it
                # corrected and stayed unapproved, with no round left to read the correction)
                # 2026-11-11: a check round of an accepted head's text (`text_check`) counts as that second
                # review: a correction it asks for that is answered without a push gets the same check round.
                # A text check starts a sequence of its own (the text it read is later than what the earlier
                # rounds of the head settled), so only the rounds from the latest one on count here.
                checks = [j for j in same if j.inputs.get("text_check")]
                if checks:
                    since = round_key(max(checks, key=round_key))
                    same = [j for j in same if round_key(j) >= since]
                if any(j.inputs.get("text_rereview") for j in same):
                    continue
                twice = any(j.inputs.get("dispute_rereview") or j.inputs.get("text_check") for j in same)
                if number not in threads:
                    try:
                        threads[number] = prbatch.PullReader(ctx.gh, repo, [number], batch).comments(number)
                    except GHError as exc:
                        out.append(f"#{number}: comments not read ({str(exc).splitlines()[0][:160]})")
                        continue
                if threads[number] is None:
                    continue             # an earlier step of this run could not read its comments
                verdict = latest_verdict(ctx, same, head, texts)
                if twice and not text_pending(same, threads[number], head, verdict):
                    continue
                if not twice and not dispute_pending(same, threads[number], head, verdict):
                    continue
                dispute, text_round = not twice, twice
            if made >= p.per_run:
                waiting += 1
                continue
            file_round(number, entry, folder, ours, mine, {"text_rereview": True} if text_round else {}, dispute,
                       answered=max(same, key=round_key).id if same and (dispute or text_round) else "")
    if waiting:
        out.append(f"{waiting} review job(s) wait ([prreview] per_run = {p.per_run})")
    if text_due and p.text_per_run:
        file_text_checks(ctx, listed, text_due, texts, file_round, lambda: made, out)
    return out


def dependency_depths(listed: list[dict], bodies: dict[int, dict | None], label: str) -> dict[int, int]:
    """How deep each open pull request of `listed` sits in the dependency graph of the open pull
    requests: 0 for one that waits for nothing (without `label`, when one is given: `roadmaps
    dependency-labels` keeps that label on exactly the pull requests that wait; else when its
    description names no open pull request), otherwise one more than the deepest open pull request its
    description names (`overlaps.declared_items` of `bodies`: its `<!-- depends-on -->` block and its
    prose), 1 when it names none. A cycle is cut where it closes."""
    live = {int(e["pr"]) for e in listed}
    labelled = {int(e["pr"]) for e in listed if label and label in e.get("labels", [])}
    edges: dict[int, set[int]] = {}
    for e in listed:
        n = int(e["pr"])
        body = (bodies.get(n) or {}).get("body") or ""
        edges[n] = {m for m, _roadmap in overlaps.declared_items(body, own=n) if m and m in live and m != n}
    depth: dict[int, int] = {}

    def walk(n: int, path: set[int]) -> int:
        if n in depth:
            return depth[n]
        if not (n in labelled if label else edges.get(n)):
            depth[n] = 0
            return 0
        path.add(n)
        d = 1 + max((walk(m, path) for m in sorted(edges.get(n, ())) if m not in path), default=0)
        path.discard(n)
        depth[n] = d
        return d

    for n in sorted(live):
        walk(n, set())
    return depth


def text_read(ctx, ours: list[Job], at: str, head: str, digest: str, texts: dict) -> tuple[bool, str]:
    """(whether a review of one roadmap folder read the pull request's text whose digest is `digest`, the
    digest the latest review read). `ours` are the folder's PRREVIEW jobs, `head` the pull request's
    current head and `at` the head reviewed for the folder (`head`, or the earlier head an acceptance
    comes from). The latest review of `head`, else of `at`, decides: its verdict's `text` (the digest
    the worker records from the text it fetched when the round started), whatever the round was filed
    for. When that verdict records none and the review is a check of the text filed for `digest`, it
    finished on a worker of an older build, which records none: it counts as having read that text, so
    that it is not filed again on every run. An older check never overrides a newer verdict's digest."""
    job, verdict = None, None
    for h in dict.fromkeys((head, at)):
        job, verdict = latest_reviewed(ctx, [j for j in ours if j.inputs.get("head") == h], h, texts)
        if verdict is not None:
            break
    reviewed = str((verdict or {}).get("text") or "")
    if reviewed:
        return reviewed == digest, reviewed
    return bool(job and job.inputs.get("text_check") and job.inputs.get("text_digest") == digest), ""


def file_text_checks(ctx, listed: list[dict], due: list, texts: dict, file_round, made, out: list[str]) -> None:
    """The check rounds of accepted pull requests' title and description (`[prreview] text_per_run`),
    filed by `sync_pr_reviews` after its other rounds, within `[prreview] per_run`.

    `due` holds the pull requests an AI review accepts, by any of `sync_pr_reviews`'s routes, with no
    round under way: (number, listing entry, {roadmap folder: the head reviewed for it}, PRREVIEW
    jobs). Their titles and descriptions are read in batches (`prbatch.pull_texts`). A review reads
    the text against its own folder's change only, so each folder is checked on its own: a folder is
    due when no review of it read the current text (`text_read`, `summary.text_digest`), because the
    text changed since or the review predates the record. A due folder gets one incremental round
    (`text_check`, with the digest as `text_digest`). The folders due are taken least deep in the
    dependency graph first (`dependency_depths`: a pull request that waits for nothing, then one
    waiting only for those, and so on), then `awaiting-review` before the rest, non-drafts before
    drafts, oldest first, the folders of one pull request together, so that the pull requests
    closest to merging have their text checked first. The worker whose reply answered the folder's
    latest review at this head may not take its check (`file_round`'s `answered`)."""
    cfg, p = ctx.cfg, ctx.cfg.prreview
    room = min(p.text_per_run, p.per_run - made())
    if room <= 0:
        return
    bodies = prbatch.pull_texts(ctx.gh, cfg.repositories.content, [int(e["pr"]) for e in listed],
                                cfg.board.graphql_batch)
    ready = []
    for number, entry, heads, mine in due:
        got, head = bodies.get(number), entry.get("head", "")
        if got is None:
            out.append(f"#{number}: text not checked (its title and description could not be read)")
            continue
        digest = summary.text_digest(got["title"], got["body"])
        for folder, at in heads.items():
            read, reviewed = text_read(ctx, [j for j in mine if board.prreview_folder(j) == folder], at, head,
                                       digest, texts)
            if not read:
                ready.append((number, entry, folder, digest, mine,
                              "changed since its review" if reviewed else "its review did not record it"))
    if not ready:
        return
    depths = dependency_depths(listed, bodies, p.dependency_label)
    review_label = cfg.replies.awaiting_review
    ready.sort(key=lambda x: (depths.get(x[0], 0), review_label not in x[1].get("labels", []),
                              bool(x[1].get("draft")), x[0]))
    filed = 0
    for number, entry, folder, digest, mine, why in ready:
        if filed >= room:
            break
        ours = [j for j in mine if board.prreview_folder(j) == folder]
        # The worker that answered the folder's latest review at this head (a P3-only repair of its text,
        # say) wrote what the check reads: it may not take the check, whoever answered the other folders.
        last = previous_review(ours)
        if file_round(number, entry, folder, ours, mine, {"text_check": True, "text_digest": digest},
                      note=f" (a check of its title and description: {why}; dependency depth {depths.get(number, 0)})",
                      answered=last.id if last else ""):
            filed += 1
    if len(ready) > filed:
        out.append(f"{len(ready) - filed} roadmap folder(s) of accepted pull requests wait for a check of their "
                   f"text ([prreview] text_per_run = {p.text_per_run})")


def stale(ctx, shared: claims.Claims | None, release: bool) -> list[str]:
    cfg, out = ctx.cfg, []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cfg.board.stale_hours)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    issues = {i["number"]: i for i in ctx.gh.api(
        f"repos/{cfg.repositories.board_repo()}/issues?labels={cfg.board.namespace_label}&state=open&per_page=100", paginate=True)}
    for job in ctx.board.list_jobs(("claimed",)):
        lease = shared.read(claims.job_key(cfg.project.name, job.id)) if shared else None
        updated = dt.datetime.fromisoformat(issues.get(job.number, {}).get("updated_at", "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
        if claims.lease_live(lease, now) or updated > cutoff:
            continue
        out.append(f"{job.id}: claimed, no lease and no activity since {updated:%Y-%m-%d %H:%M}")
        if release:
            ctx.board.set_state(job, "available", f"Released: no live lease and no activity for {cfg.board.stale_hours:g} hours.")
    subs = ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/pulls?state=closed&sort=updated&direction=desc&per_page=100")
    closed_unmerged = set()
    for p in subs or []:
        sub = board.decode_submission(p.get("body") or "")
        if sub and not p.get("merged_at"):
            closed_unmerged.add(sub["job"])
    for job in ctx.board.list_jobs(("submitted",)):
        if job.id in closed_unmerged:
            out.append(f"{job.id}: its submission was closed unmerged")
            if release:
                ctx.board.set_state(job, "available", "Released: the submission pull request was closed without merging.")
    return out
