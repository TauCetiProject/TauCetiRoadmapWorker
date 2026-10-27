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
  created for the branch owner when humans are waiting for a reply; held pull requests stay drafts.
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

from . import board, boardpr, claims, depgraph, explorer, filers, gitops, prreviews, replies
from .board import Job, Outcome
from .gh import GHError, transient
from .lanes.base import open_roadmap_prs, pr_answerers
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
    pull request its author opened."""
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
                                              cfg.review.separation, sub.get("agent", ""), sub.get("model", ""))
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
    ctx.gh.write("PUT", f"repos/{repo}/pulls/{number}/merge", {"merge_method": "squash", "sha": pr["head"]["sha"]})
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
    blocking = [f for f in prreviews.findings_with_bodies(review) if f[1] in ("P1", "P2")]
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


def reorder(ctx, readmes: dict[str, str], only: set[str] | None = None, yes: bool = True,
            current: tuple[str, str | None] | None = None) -> dict[str, str]:
    """`depgraph.repair_upward` on the merge order of the board's main (`current`: its text and blob
    sha, when in hand), written in one commit when anything moved. Returns name -> what happened (a
    move, or why it was not made)."""
    cfg = ctx.cfg
    text, sha = current or read_merge_order(ctx)
    new, notes = depgraph.repair_upward(text, readmes, set(cfg.roadmap.main_extended), only)
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
    pos = {n: i for i, n in enumerate(order)}
    suppliers = depgraph.supplier_edges({name: readme}, set(order), set(cfg.roadmap.main_extended)).get(name, {})
    if not any(pos[s] > pos[name] for s in suppliers if s in pos):
        return ""
    pulls = ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/pulls?state=open&per_page=100", paginate=True) or []
    readmes = {**board_readmes(ctx, pulls), name: readme}
    return reorder(ctx, readmes, only={name}, current=(text, sha)).get(name, "")


def replace_upward_roadmaps(ctx, yes: bool = False) -> list[str]:
    """`board intake`'s catch-up of the merge order (idempotent: a clean order is only read): every
    roadmap the order on the board's main lists above a supplier its board README names (at the head
    of its open pull request, else on main) is moved right after the last of them, all in one commit,
    unless that would put one of its consumers before it (logged, left as it is). It repairs roadmaps
    placed by a README that named its suppliers under names the dependency check did not know, which
    a later fix repointed. One line per roadmap moved or left."""
    cfg = ctx.cfg
    if cfg.roadmap.merge_order_repo != "packages":
        return []
    pulls = ctx.gh.api(f"repos/{cfg.repositories.packages_repo()}/pulls?state=open&per_page=100", paginate=True) or []
    notes = reorder(ctx, board_readmes(ctx, pulls), yes=yes)
    return [f"merge order: {line}" for _name, line in sorted(notes.items())]


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
        lease = lock.read(key) or {}
        since = dt.datetime.fromtimestamp(int(lease.get("acquired_at", 0) or 0), dt.UTC)
        return status, (f"intake already running ({lease.get('owner', '?')}, since {since:%Y-%m-%d %H:%M:%SZ}); "
                        "nothing done")
    if status == claims.ERROR:
        return status, "board lock not taken (the lock ref could not be written); nothing done"
    return status, ""


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
            ctx.board.comment(dup.number, f"Duplicate of #{keep.number}: this job was created twice; #{keep.number} is "
                                          f"the job ({why}). Closed by the board.")
            try:
                ctx.board.add_label(dup, board.DUPLICATE_LABEL)
            except GHError:
                pass
            ctx.gh.write("PATCH", f"repos/{cfg.repositories.board_repo()}/issues/{dup.number}", {"state": "closed"})
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


def sync_pull_requests(ctx) -> list[str]:
    cfg, out = ctx.cfg, []
    repo = cfg.repositories.content
    r = cfg.replies
    jobs = ctx.board.all_jobs()
    held = set()
    try:
        held = set((ctx.state / "hold-draft.txt").read_text(encoding="utf-8").split())
    except OSError:
        pass
    for p in open_roadmap_prs(ctx):
        number = p["pr"]
        data = ctx.gh.api(f"repos/{repo}/pulls/{number}")
        if str(number) in held and not data.get("draft"):
            try:
                ctx.gh.cli_write("pr", "ready", str(number), "--undo", "-R", repo)
                out.append(f"#{number}: held, converted back to draft")
            except GHError as exc:
                out.append(f"#{number}: held, but not converted back to draft ({exc})")
        reviews = ctx.gh.api(f"repos/{repo}/pulls/{number}/reviews?per_page=100", paginate=True)
        comments = ctx.gh.api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True)
        inline = ctx.gh.api(f"repos/{repo}/pulls/{number}/comments?per_page=100", paginate=True)
        events = events_from_api(reviews, comments, inline)
        want, drop = replies.wanted_label(events, pr_answerers(cfg, p["owner"]), p["head"], markers=r.review_markers,
                                          housekeeping=r.housekeeping_prefixes, approvers=r.approvers,
                                          ignore=set(r.ignore_logins), awaiting_author=r.awaiting_author,
                                          awaiting_review=r.awaiting_review, maintainers=set(r.maintainer_logins),
                                          maintainer_prefix=r.maintainer_review_prefix)
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
            if not any(j.state in ("available", "claimed", "submitted", "blocked") for j in mine):
                operator = p.get("operator", p["owner"])
                gate = board.new_job(board.make_id("gate", p["name"], len(mine) + 1), requires_operator=operator,
                                     inputs={"pr": number, "operator": operator, "owner": p["owner"]})
                ctx.board.create(gate)
                out.append(f"#{number}: {gate.id} created for " + (f"@{operator}" if operator else "the pool"))
        out.append(f"#{number} {p['name']}: {want or 'approved'}")
    return out


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
