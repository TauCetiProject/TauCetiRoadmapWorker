"""The board pull request of a design or package, kept open while it is reviewed and red-teamed.

`board intake` does not merge a design or package submission: it labels the pull request
`needs-review`, pins the review brief as its first comment, records the author, and creates the
review job, which names the pull request (`board_pr`). From then on every step is on that pull
request: a review or red-team pass is a comment in the shape prreviews.py reads (from a worker,
or from a browser model a listed login drives), a fix pushes to its branch, and the next round
reviews the new head. `scan` (run by `board intake`) reads the comments and advances:

* a review: accepted moves a design on (the pull request is merged, the package job follows) and
  a package to its red-team rounds; needs_changes opens the fix round for the pull request's owner;
  rejected, or too many rounds, parks it for a human;
* a red-team pass completes its pass job; a complete round opens the round's fix;
* a fix that was pushed is done, and the next review or red-team round follows; after the last
  red-team round's fix the pull request is merged, the port job created and the roadmap inserted
  into the merge order when it is absent (moved after its last supplier when the order lists it
  above one). A merge the intake's checks refuse leaves the fix
  submitted, and every scan tries the merge again (no new push needed).

At every merge a pull request may carry the deliverables of the jobs that wrote it, and, for an
explorer import, the files its import marker declares (`explorer.import_deliverables`: the
explorer's review.json, which no board job writes), each through the same checks.

A comment counts only from a login of `[review] comment_reviewers`, the board's bots, or the
worker that claimed the job (its claim note on the job's issue names the login); it must review
the current head (an older one is answered once as stale) and be independent of the authors in
the configured separation mode. `reviewing` from a listed login claims an open review; a worker
that holds one is never preempted, and both reviews count. Reports and the verdict are copied to
the board's main when a round ends, so they outlive the pull request.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import tomllib

from . import board, claims, depgraph, explorer, prompts, prreviews, sources
from .board import Job, Outcome
from .gh import GHError

NEEDS_REVIEW = "needs-review"
BRIEF_MARK = "tauceti-roadmap-review-brief"
AUTHOR_NOTE = "Intake: authored by @{login} ({agent}, {model}) in #{pr}."   # board.AUTHOR_NOTE reads it
COMMENT_CLAIM_TTL = 4 * 3600   # a `reviewing` comment holds the review this long (a browser review takes hours)


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def repo(ctx) -> str:
    return ctx.cfg.repositories.packages_repo()


def jobs_of(pr: int, jobs: list[Job]) -> list[Job]:
    """The reviews, red-team passes and fixes working on a board pull request."""
    return [j for j in jobs if j.inputs.get("board_pr") == pr]


def authors_of(pr: int, sub_job: str, jobs: list[Job]) -> list[Job]:
    """The design or package a board pull request carries and the fixes pushed to it."""
    return [j for j in jobs if (j.inputs.get("review_pr") == pr or j.id == sub_job
                                or (j.kind == "fix" and j.inputs.get("board_pr") == pr))
            and j.kind in ("design", "package", "fix")]


def put_file(ctx, path: str, text: str, message: str) -> None:
    """Write a file to the board's main through the contents API (replacing it if it exists)."""
    payload = {"message": message, "content": base64.b64encode(text.encode()).decode()}
    try:
        payload["sha"] = ctx.gh.api(f"repos/{repo(ctx)}/contents/{path}")["sha"]
    except (GHError, KeyError, TypeError):
        pass
    ctx.gh.write("PUT", f"repos/{repo(ctx)}/contents/{path}", payload)


# --- the brief pinned on the pull request ------------------------------------------------------------

def raw(ctx, sha: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo(ctx)}/{sha}/{path}"


def brief(ctx, job: Job, pr: dict) -> str:
    """The brief a browser model reads on the pull request: the job, the files at the head, the
    principles, the review shape, and the rules for sources it cannot hold."""
    cfg, head = ctx.cfg, pr["head"]["sha"]
    pkg = board.package_dir(cfg.board.packages_dir, job.name)
    j = board.parse_job_id(job.id)
    design = j.subject == "DESIGN" or job.kind == "design"
    names = ("design/README.md", "design/plan.json", "sources.toml") if design else \
        ("README.md", "Suggested.lean", "metadata.toml", "sources.toml")
    files = "\n".join(f"- {raw(ctx, head, f'{pkg}/{n}')}" for n in names)
    head_line = (prreviews.redteam_head(j.round, j.pass_ or "a", "<agent>", head) if job.kind == "redteam"
                 else prreviews.review_head("<agent>", head))
    values = {"JOB": job.id, "ROADMAP": job.name, "SUBJECT": "design" if design else "package", "ROUND": str(j.round),
              "FILES": files, "HEAD": head[:7], "HEAD_LINE": head_line,
              "PR": f"https://github.com/{repo(ctx)}/pull/{pr['number']}",
              "ORDER": raw(ctx, "main", cfg.roadmap.merge_order_file) if cfg.roadmap.merge_order_repo == "packages"
              else f"`{cfg.roadmap.merge_order_file}` in {cfg.repositories.content}",
              "CONTENT_REPO": cfg.repositories.content, "ACTIVE_DIR": cfg.roadmap.active_dir,
              "COMPLETED_DIR": cfg.roadmap.completed_dir, "MAX_LEAN_LINES": str(cfg.gates.max_lean_lines),
              "KIND": "red-team pass" if job.kind == "redteam" else "review", "GAPS": web_gaps(ctx, pkg, head)}
    return prompts.render("review-web", values) + f"\n<!-- {BRIEF_MARK} {json.dumps({'job': job.id})} -->\n"


def web_gaps(ctx, pkg: str, head: str) -> str:
    """The supplier gaps the plan at the head declares, for the brief."""
    from .lanes.design import gaps_listing
    try:
        plan = json.loads(ctx.gh.raw_file(repo(ctx), f"{pkg}/design/plan.json", head))
    except (GHError, ValueError):
        return "none declared (no readable design/plan.json at this head)"
    return gaps_listing(plan if isinstance(plan, dict) else {})


def pin_brief(ctx, number: int, job: Job, pr: dict, comments: list[dict] | None = None) -> None:
    """Post the brief as the pull request's first bot comment, or refresh it in place."""
    text = brief(ctx, job, pr)
    for c in comments if comments is not None else ctx.gh.api(
            f"repos/{repo(ctx)}/issues/{number}/comments?per_page=100", paginate=True) or []:
        if BRIEF_MARK in (c.get("body") or "") and (c.get("user") or {}).get("login") in (
                ctx.cfg.board.bot_logins + [ctx.operator]):
            if c.get("body") != text:
                ctx.gh.write("PATCH", f"repos/{repo(ctx)}/issues/comments/{c['id']}", {"body": text})
            return
    ctx.gh.write("POST", f"repos/{repo(ctx)}/issues/{number}/comments", {"body": text})


# --- opening a pull request for review ---------------------------------------------------------------

def open_for_review(ctx, number: int, pr: dict, new: list[Job], all_jobs: list[Job], yes: bool) -> str:
    """Keep a board pull request open for its reviews: label it, pin the brief, and create the
    review (or red-team) jobs naming it."""
    owner = pr["user"]["login"]
    for job in new:
        job.inputs["board_pr"] = number
        job.inputs.setdefault("operator", owner)
    existing = {j.id for j in all_jobs}
    todo = [j for j in new if j.id not in existing]
    if not yes:
        return f"#{number}: would keep it open for review and create {', '.join(j.id for j in todo) or 'nothing'} (dry run)"
    ctx.gh.write("POST", f"repos/{repo(ctx)}/issues/{number}/labels", {"labels": [NEEDS_REVIEW]})
    comments = ctx.gh.api(f"repos/{repo(ctx)}/issues/{number}/comments?per_page=100", paginate=True) or []
    for job in todo:   # earlier comments answer earlier rounds: only a review of this head counts from them
        job.inputs["after_comment"] = max((int(c.get("id") or 0) for c in comments), default=0)
    pin_brief(ctx, number, new[0], pr, comments)
    created = [board.made_note(ctx.board, j, ctx.board.create(j)) for j in todo]
    return f"#{number}: open for review; created " + (", ".join(created) or "nothing")


def submission_under_review(ctx, number: int, pr: dict, sub: dict, job: Job, jobs: list[Job], yes: bool) -> str:
    """A design or package submission that passed the intake's checks: it stays open, its author is
    recorded, and its review follows on it."""
    login = pr["user"]["login"]
    new = board.inherit_origin(job, board.successors(job, Outcome()))
    if not yes:
        return open_for_review(ctx, number, pr, new, jobs, yes)
    ctx.board.comment(job.number, AUTHOR_NOTE.format(login=login, agent=sub.get("agent") or "?",
                                                     model=sub.get("model") or "?", pr=number))
    job.inputs["review_pr"] = number      # not `board_pr`: a redone design or package opens its own pull request
    ctx.board.update_spec(job)
    if job.state in ("available", "claimed"):
        # The worker set `submitted` on another issue (a twin closed under it) or could not write the
        # board: a job under review on its pull request is never available for another worker.
        ctx.board.set_state(job, "submitted", f"Submitted: #{number} is this job's open submission (this issue said "
                                              f"`state:{job.state}`).")
    return open_for_review(ctx, number, pr, new, jobs, yes)


# --- reading the comments ----------------------------------------------------------------------------

def trusted(ctx, login: str, parsed: prreviews.Parsed, job: Job, shared=None) -> bool:
    """A listed login, the board's bot, or the worker that did this job: its marker names the job,
    and its login is the one the claims namespace recorded for the job, or the one that labelled
    the job claimed (adding a label needs write access to the board; a comment anyone can write)."""
    if login in ctx.cfg.review.comment_reviewers or login in ctx.cfg.board.bot_logins:
        return True
    if parsed.worker.get("job") != job.id:
        return False
    if shared:
        record = shared.recorded_authors([job.id]).get(job.id) or {}
        if str(record.get("operator", "")).lower() == login.lower():
            return True
    return ctx.board.labelled_by(job, "state:claimed").lower() == login.lower()


def independent(ctx, login: str, parsed: prreviews.Parsed, job: Job, jobs: list[Job], shared) -> bool:
    if not job.independent_of:
        return True
    mode = ctx.cfg.review.separation
    reviewer = (login, parsed.agent if not parsed.worker else parsed.worker.get("agent", parsed.agent),
                parsed.worker.get("model", ""))
    records = ctx.board.authors(job.independent_of, jobs)
    if shared:
        for jid, rec in shared.recorded_authors(job.independent_of).items():
            records.setdefault(jid, []).append(rec)
    return all(claims.independent(mode, reviewer, claims.authorship(r)) for rs in records.values() for r in rs)


def claim(ctx, job: Job, login: str, agent: str, shared) -> str:
    """A `reviewing` comment from a listed login takes an available review job (a lease under that
    login, held as long as an author record)."""
    if job.state != "available":
        return ""
    if shared:
        holder = claims.Claims(repo=shared.repo, gitdir=shared.gitdir, owner=f"{login}/comment", operator=login,
                               project=shared.project, dry_run=shared.dry_run)
        if holder.acquire(claims.job_key(ctx.cfg.project.name, job.id), COMMENT_CLAIM_TTL,
                          extra={"agent": agent or "browser"}) == claims.HELD:
            return ""
    ctx.board.set_state(job, "claimed", f"Claimed for `{job.id}` by @{login} ({agent or 'browser'}), by a comment on "
                                        f"#{job.inputs.get('board_pr')}.")
    return f"{job.id} claimed by @{login}"


def worker_pending(ctx, job: Job, reviews: list, shared) -> bool:
    """A worker holds the job and has not posted its review yet: wait for it (it is never preempted)."""
    if not shared:
        return False
    lease = shared.read(claims.job_key(ctx.cfg.project.name, job.id))
    if not claims.lease_live(lease, int(now().timestamp())) or str(lease.get("owner", "")).endswith("/comment"):
        return False
    return not any(p.worker for _c, p in reviews)


def note(ctx, number: int, text: str) -> None:
    ctx.gh.write("POST", f"repos/{repo(ctx)}/issues/{number}/comments", {"body": text})


def reviews_for(ctx, job: Job, comments: list[dict], head: str, jobs: list[Job], shared, number: int,
                out: list[str]) -> list[tuple[dict, prreviews.Parsed]]:
    """The valid comments for this job at the current head; claims, stale and dependent reviews
    are acted on or answered once."""
    handled = set(job.inputs.get("handled", []))
    j = board.parse_job_id(job.id)
    found = []
    for c in comments:
        parsed = prreviews.parse(c.get("body") or "")
        login = (c.get("user") or {}).get("login", "")
        if not parsed or login in ctx.cfg.board.bot_logins and BRIEF_MARK in (c.get("body") or ""):
            continue
        cid = int(c.get("id") or 0)
        if cid <= int(job.inputs.get("after_comment", 0)) and not (parsed.kind != "claim" and head.startswith(parsed.sha)):
            continue        # it answered an earlier round
        if parsed.kind == "claim":
            if cid not in handled and (login in ctx.cfg.review.comment_reviewers):
                done = claim(ctx, job, login, parsed.agent, shared)
                if done:
                    out.append(done)
                handled.add(cid)
            continue
        if job.kind == "review" and parsed.kind != "review":
            continue
        if job.kind == "redteam" and (parsed.kind != "redteam" or parsed.round != j.round or parsed.pass_ != j.pass_):
            continue
        if not trusted(ctx, login, parsed, job, shared):
            continue
        refused = ""
        if not head.startswith(parsed.sha):
            refused = (f"Not counted: @{login}'s {parsed.kind} is of `{parsed.sha[:7]}`, but the head is `{head[:7]}`. "
                       "Review the current head.")
        elif not independent(ctx, login, parsed, job, jobs, shared):
            refused = (f"Not counted for `{job.id}`: @{login} ({parsed.agent}) is not independent of its authors "
                       f"(separation by {ctx.cfg.review.separation}).")
        if refused:
            if cid not in handled:
                note(ctx, number, refused)
                handled.add(cid)
                if parsed.worker and job.state in ("submitted", "claimed"):
                    ctx.board.set_state(job, "available", f"Released by the board: {refused}")   # another worker retakes it
            continue
        found.append((c, parsed))
    if handled != set(job.inputs.get("handled", [])):
        job.inputs["handled"] = sorted(handled)
        ctx.board.update_spec(job)
    return found


# --- advancing -----------------------------------------------------------------------------------------

def report_text(reviews: list[tuple[dict, prreviews.Parsed]]) -> str:
    return "\n\n---\n\n".join(f"<!-- from @{(c.get('user') or {}).get('login', '?')}, {c.get('html_url', '')} -->\n"
                              + (c.get("body") or "").strip() for c, _p in reviews) + "\n"


def finish_job(ctx, job: Job, text: str) -> None:
    ctx.board.set_state(job, "done", text)
    ctx.gh.write("PATCH", f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}", {"state": "closed"})


def imported_files(ctx, pr: dict) -> set[str]:
    """The files an explorer import pull request placed on itself (none for a board submission)."""
    return explorer.import_deliverables(explorer.decode_import(pr.get("body") or ""), ctx.cfg.board.packages_dir)


def merge_problems(ctx, number: int, pr: dict, jobs: list[Job]) -> list[str]:
    """The intake's checks again, on the head about to be merged (fixes pushed since the submission):
    only the deliverables of the jobs that wrote it (and of an explorer import, the files its marker
    declares), no source material, local path or bad data, and passing checks."""
    from .intake import _checks, submission_files
    sub = board.decode_submission(pr.get("body") or "") or {}
    od = board.order_file(ctx.cfg)
    writers = authors_of(number, sub.get("job", ""), jobs)
    allowed = set().union(*(board.allowed_paths(j, ctx.cfg.board.packages_dir, od) for j in writers), set())
    if not allowed:
        allowed = {f for j in jobs_of(number, jobs) for f in board.allowed_paths(j, ctx.cfg.board.packages_dir, od)}
    allowed |= imported_files(ctx, pr)
    # A pull request that carries a package (no job of the design stage wrote it) changes plan.json in its gaps only.
    gaps_only = bool(writers) and all(board.gaps_only(j) for j in writers)
    files, problems = submission_files(ctx, repo(ctx), number, pr)
    for f in files:
        text = ctx.gh.raw_file(pr["head"]["repo"]["full_name"], f["filename"], pr["head"]["sha"]) \
            if f["filename"] in allowed else ""
        problems += board.file_problems(f["filename"], text, allowed)
        if f["filename"].endswith("/design/plan.json") and gaps_only:
            from .intake import base_text
            problems += board.plan_change_problems(f["filename"], base_text(ctx, repo(ctx), f["filename"], pr), text)
    failed, running = _checks(ctx, repo(ctx), pr["head"]["sha"])
    if running:
        problems.append("checks still running")
    if failed:
        problems.append("checks failed: " + ", ".join(failed))
    return problems


def merge(ctx, number: int, pr: dict, jobs: list[Job], verdict: dict | None, blocker: Job | None = None) -> str:
    """Merge the pull request at the head that was reviewed, after the intake's checks; then write
    its verdict file and close the jobs that wrote it. Returns '' when merged, else why not (then
    nothing was closed and the next scan tries again)."""
    problems = merge_problems(ctx, number, pr, jobs)
    if problems:
        note(ctx, number, "Intake: not merged yet, because " + "; ".join(problems) + ".")
        return "; ".join(problems)
    try:
        ctx.gh.write("PUT", f"repos/{repo(ctx)}/pulls/{number}/merge", {"merge_method": "squash", "sha": pr["head"]["sha"]})
    except GHError as exc:
        if "conflict" not in str(exc).lower() and not conflicts_with_main(ctx, number, pr):
            return f"merge failed ({exc})"
        done, detail = squash_with_main(ctx, number, pr)
        if not done:
            if blocker is not None and detail.startswith("conflicts with main in "):
                ctx.board.set_state(blocker, "blocked", f"Blocked: the pull request #{number} {detail}; a human "
                                                        "rebases it.")
            return f"merge failed ({detail})"
    pr["merged"] = True
    sub = board.decode_submission(pr.get("body") or "") or {}
    for j in authors_of(number, sub.get("job", ""), jobs):
        if j.state != "done":
            finish_job(ctx, j, f"Intake: merged #{number} from @{pr['user']['login']} ({sub.get('agent') or '?'}, "
                               f"{sub.get('model') or '?'}).")
    if verdict:
        put_file(ctx, verdict["path"], json.dumps({"review": verdict["review"]}, indent=1) + "\n",
                 f"Board: verdict of #{number}")
    return ""


MANIFEST = "sources.toml"


def conflicts_with_main(ctx, number: int, pr: dict) -> bool:
    """Whether GitHub reports the pull request as conflicting with main (`mergeable_state: dirty`,
    `mergeable: false`), read from `pr` when it carries the fields (a single pull request's record),
    else fetched (a list entry lacks them). A merge the API refuses with any error, a 403 included,
    is then resolved in a checkout; one of a clean pull request is a real failure."""
    if "mergeable_state" not in pr and "mergeable" not in pr:
        try:
            pr = ctx.gh.api(f"repos/{repo(ctx)}/pulls/{number}") or {}
        except GHError:
            return False
    return pr.get("mergeable_state") == "dirty" or pr.get("mergeable") is False


def order_after_squash(ctx, dest, main_text: str, pr: dict) -> tuple[str, list[str]]:
    """Main's merge-order file, with every roadmap the pull request carries that it lacks inserted
    right after its last supplier, as the README in the squashed tree names them (the package README,
    else the design README; a roadmap with neither stays out). Returns the text and the names inserted."""
    from .intake import DESIGN_README, PACKAGE_README, place_roadmaps, pr_roadmaps
    listed = set(depgraph.read_order(main_text))
    readmes = {}
    for name in sorted(pr_roadmaps(pr) - listed):
        pkg = dest / board.package_dir(ctx.cfg.board.packages_dir, name)
        for rel in (PACKAGE_README, DESIGN_README):
            if (pkg / rel).is_file():
                readmes[name] = (pkg / rel).read_text(encoding="utf-8")
                break
    return place_roadmaps(main_text, readmes, set(ctx.cfg.roadmap.main_extended))


def squash_with_main(ctx, number: int, pr: dict) -> tuple[bool, str]:
    """The merge of an accepted board pull request that conflicts with main: squashed onto main in a
    checkout of the board, each conflicting file resolved, committed with the pull request's author,
    pushed to main, and the pull request closed with a note. A conflicting sources.toml (the intake
    rewrote the roadmap's manifest on main with answers while the branch changed it too) is merged by
    key, main's answered entries kept. A conflicting merge-order file (an older import branch placed
    its roadmap in the order itself while the intake rewrote the order on main) is taken from main,
    which is authoritative, with the pull request's roadmap inserted after its last supplier when
    main's copy lacks it. Any other conflicting file stops it: (False, "conflicts with main in <files>")."""
    from . import gitops
    board_repo = repo(ctx)
    dest = ctx.state / "mainmerge" / "board"
    head, title = pr["head"]["sha"], pr.get("title") or f"#{number}"
    author = (pr.get("user") or {}).get("login", "board")
    order_file = board.order_file(ctx.cfg)
    try:
        gitops.sync_checkout(board_repo, dest, "main")
        gitops.set_identity(dest, ctx.operator or "github-actions[bot]")
        gitops.git(dest, "fetch", "-q", "origin", f"pull/{number}/head")
        if gitops.git(dest, "rev-parse", "FETCH_HEAD").strip() != head:
            return False, "the pull request moved while it was being merged"
        gitops.git(dest, "merge", "--squash", "FETCH_HEAD", check=False)
        conflicts = [f for f in gitops.git(dest, "diff", "--name-only", "--diff-filter=U").splitlines() if f.strip()]
        others = [f for f in conflicts if not f.endswith("/" + MANIFEST) and not (order_file and f == order_file)]
        if others:
            gitops.git(dest, "reset", "-q", "--hard", "origin/main")
            return False, "conflicts with main in " + ", ".join(sorted(others))
        resolved = []
        for path in conflicts:
            main_text = gitops.git(dest, "show", f":2:{path}", check=False)
            if path == order_file:
                text, placed = order_after_squash(ctx, dest, main_text, pr)
                resolved.append(f"{path} taken from main" + (f" with {', '.join(placed)} inserted after its last supplier"
                                                              if placed else ""))
            else:
                pr_text = gitops.git(dest, "show", f":3:{path}", check=False)
                merged = sources.merge_manifests(tomllib.loads(pr_text or ""), tomllib.loads(main_text or ""))
                text = sources.dump_manifest(merged)
                resolved.append(f"{path} merged by key (main's answered entries kept)")
            (dest / path).write_text(text, encoding="utf-8")
            gitops.git(dest, "add", "--", path)
        how = "; ".join(resolved)
        message = (f"{title} (#{number})\n\nMerged by the board's intake onto main" + (f", with {how}" if how else "") + ".")
        gitops.git(dest, "commit", "-q", "--author", f"{author} <{author}@users.noreply.github.com>", "-m", message)
        gitops.git(dest, "push", "-q", "origin", "HEAD:refs/heads/main")
        sha = gitops.git(dest, "rev-parse", "HEAD").strip()
    except (gitops.GitError, tomllib.TOMLDecodeError, OSError) as exc:
        return False, f"could not merge it with main ({str(exc).splitlines()[0][:200]})"
    note(ctx, number, f"Intake: merged as `{sha[:7]}` on main: the pull request conflicted with main"
                      + (f": {how}" if how else "") + ". Closing it.")
    ctx.gh.write("PATCH", f"repos/{board_repo}/pulls/{number}", {"state": "closed"})
    return True, sha


def conclude_review(ctx, job: Job, reviews, number: int, pr: dict, jobs: list[Job], yes: bool) -> str:
    cfg = ctx.cfg
    verdicts = {p.verdict for _c, p in reviews}
    verdict = "rejected" if "rejected" in verdicts else "needs_changes" if "needs_changes" in verdicts else "accepted"
    if not yes:
        return f"#{number}: {job.id} {verdict} (dry run)"
    pkg = board.package_dir(cfg.board.packages_dir, job.name)
    j = board.parse_job_id(job.id)
    if job.kind == "review" and verdict == "accepted" and j.subject == "DESIGN":
        # Merge first: a merge that fails leaves the review open in `merge_pending` (the reviewed head
        # and the accepting comments), and every scan retries it.
        why = merge(ctx, number, pr, jobs, {"path": f"{pkg}/design/review.json",
                                            "review": verdict_record(job, reviews, "accepted")}, blocker=job)
        if why:
            if job.state != "blocked":
                job.inputs["merge_pending"] = {"head": pr["head"]["sha"],
                                               "comments": [int(c.get("id") or 0) for c, _p in reviews]}
                ctx.board.update_spec(job)
            return f"#{number}: {job.id} accepted, but not merged yet ({why}); the intake retries the merge"
    return after_verdict(ctx, job, verdict, reviews, number, pr, jobs)


def after_verdict(ctx, job: Job, verdict: str, reviews, number: int, pr: dict, jobs: list[Job],
                  open_pr: bool = True) -> str:
    """A concluded review's report and verdict on the board, its job closed, and what follows created
    (`open_pr` false: its pull request is merged already, so nothing more runs on it)."""
    pkg = board.package_dir(ctx.cfg.board.packages_dir, job.name)
    j = board.parse_job_id(job.id)
    put_file(ctx, f"{pkg}/reports/{job.id}.md", report_text(reviews), f"Board: {job.id} ({verdict})")
    who = ", ".join(f"@{(c.get('user') or {}).get('login', '?')} ({p.agent})" for c, p in reviews)
    job.inputs.pop("merge_pending", None)
    finish_job(ctx, job, f"Intake: {verdict} on #{number} by {who}.")
    outcome = Outcome(verdict=verdict, pr=number if open_pr else 0, operator=pr["user"]["login"],
                      report=report_text(reviews), head=pr["head"]["sha"])
    if job.kind == "review" and verdict == "accepted" and j.subject == "DESIGN":
        from .intake import announce_gaps
        design = next((x for x in jobs if x.id == board.make_id("design", job.name)), None)
        if design:
            announce_gaps(ctx, design, jobs)
    elif job.kind == "review" and verdict == "accepted":
        job.inputs["accepted_review"] = verdict_record(job, reviews, "accepted")   # written when the PR merges
        ctx.board.update_spec(job)
        from .intake import announce_package_gaps
        package = next((x for x in jobs if x.id == board.make_id("package", job.name)), job)
        announce_package_gaps(ctx, package, jobs, pr["head"]["sha"])
    return f"#{number}: {job.id} {verdict}; " + advance_on(ctx, job, outcome, jobs, number, pr)


def verdict_record(job: Job, reviews, status: str) -> dict:
    record = {"status": status, "reviewer": f"independent-review-{job.id}", "date": now().date().isoformat(),
              "findings": sum(len(p.findings) for _c, p in reviews), "blocking": sum(p.blocking for _c, p in reviews),
              "notes": "comment review on the board pull request by " + ", ".join(
                  f"@{(c.get('user') or {}).get('login', '?')} ({p.agent}) of {p.sha[:7]}" for c, p in reviews)}
    conditional = sorted({g for _c, p in reviews for g in p.conditional})
    if conditional and status == "accepted":
        record["conditional_gaps"] = conditional
    return record


def conclude_pass(ctx, job: Job, reviews, number: int, pr: dict, jobs: list[Job], yes: bool) -> str:
    c, p = reviews[0]
    if not yes:
        return f"#{number}: {job.id} done (dry run)"
    pkg = board.package_dir(ctx.cfg.board.packages_dir, job.name)
    put_file(ctx, f"{pkg}/reports/{job.id}.md", report_text([(c, p)]), f"Board: {job.id}")
    finish_job(ctx, job, f"Intake: red-team pass on #{number} by @{(c.get('user') or {}).get('login', '?')} "
                         f"({p.agent}), {len(p.findings)} finding(s).")
    j = board.parse_job_id(job.id)
    done = {x.id for x in jobs if x.state == "done"} | {job.id}
    passes = board.redteam_passes(j.name, j.round, len(ctx.cfg.redteam.passes))
    return f"#{number}: {job.id} done; " + advance_on(ctx, job, Outcome(round_complete=all(x in done for x in passes)),
                                                      jobs, number, pr)


def advance_on(ctx, job: Job, outcome: Outcome, jobs: list[Job], number: int, pr: dict) -> str:
    """The intake's advance, then the new reviews and passes on this pull request take it up: they
    answer only comments after their creation (or reviews of the current head), and the brief shows
    the new round."""
    from .intake import advance
    before = {j.id for j in ctx.board.all_jobs()}
    note_ = advance(ctx, job, outcome, jobs)
    created = [j for j in ctx.board.all_jobs() if j.id not in before and j.inputs.get("board_pr") == number]
    for fix in (j for j in created if j.kind == "fix"):
        fix.inputs["base_head"] = pr["head"]["sha"]     # the head its push must move
        ctx.board.update_spec(fix)
    new = [j for j in created if j.kind in ("review", "redteam")]
    if new and not pr.get("merged"):
        comments = ctx.gh.api(f"repos/{repo(ctx)}/issues/{number}/comments?per_page=100", paginate=True) or []
        last = max((int(c.get("id") or 0) for c in comments), default=0)
        for j in new:
            j.inputs["after_comment"] = last
            ctx.board.update_spec(j)
        pin_brief(ctx, number, new[0], pr, comments)
    return note_


def pusher(comments: list[dict], fix: Job) -> dict:
    """Who pushed a fix: the login and the marker of its `Fix ... pushed` comment."""
    for c in reversed(comments):
        parsed = prreviews.MARK_RE.findall(c.get("body") or "")
        if parsed and (c.get("body") or "").startswith(f"Fix `{fix.id}` pushed"):
            try:
                marker = json.loads(parsed[-1])
            except ValueError:
                marker = {}
            marker = {k: v for k, v in marker.items() if isinstance(v, str)} if isinstance(marker, dict) else {}
            return {"login": (c.get("user") or {}).get("login", ""), **marker}
    return {}


def fix_pushed(ctx, fix: Job, number: int, pr: dict, jobs: list[Job], yes: bool, comments: list[dict]) -> str:
    """A fix the pull request's owner pushed: recorded as its author, done, and the next round (or,
    after the last red-team round, the merge and the port) follows. A fix that did not move the
    head goes back to available."""
    if not yes:
        return f"#{number}: {fix.id} pushed; the next round follows (dry run)"
    head = pr["head"]["sha"]
    if fix.inputs.get("base_head") and head == fix.inputs["base_head"]:
        ctx.board.set_state(fix, "available", f"Released by the board: #{number} is still at `{head[:7]}`; the fix "
                                              "was not pushed.")
        return f"#{number}: {fix.id} did not move the head; available again"
    nxt = board.inherit_origin(fix, board.successors(
        fix, Outcome(), max_rounds=ctx.cfg.board.max_rounds, redteam_rounds=ctx.cfg.redteam.rounds,
        redteam_agents=tuple(p.agent for p in ctx.cfg.redteam.passes),
        redteam_models=tuple(p.model for p in ctx.cfg.redteam.passes)))
    if any(j.kind == "port" for j in nxt):
        accepted = next((j.inputs.get("accepted_review") for j in jobs if j.kind == "review"
                         and j.inputs.get("board_pr") == number and j.inputs.get("accepted_review")), None)
        pkg = board.package_dir(ctx.cfg.board.packages_dir, fix.name)
        why = merge(ctx, number, pr, jobs, {"path": f"{pkg}/review.json", "review": accepted} if accepted else None,
                    blocker=fix)
        if why:
            return f"#{number}: {fix.id} pushed, but not merged yet ({why})"
    who = pusher(comments, fix)
    if who.get("login"):
        ctx.board.comment(fix.number, AUTHOR_NOTE.format(login=who["login"], agent=who.get("agent") or "?",
                                                         model=who.get("model") or "?", pr=number))
    if fix.state != "done":
        finish_job(ctx, fix, f"Intake: fix pushed to #{number} at `{head[:7]}`.")
    if any(j.kind == "port" for j in nxt):
        from .intake import add_to_order_for_port, order_ports_after_suppliers
        deferred = order_ports_after_suppliers(ctx, nxt, jobs)
        existing = {j.id for j in ctx.board.all_jobs()}
        created = [board.made_note(ctx.board, j, ctx.board.create(j)) for j in nxt if j.id not in existing]
        return (f"#{number}: {fix.id} done; merged; created " + (", ".join(created) or "nothing")
                + ("; " + "; ".join(deferred) if deferred else "")
                + "; " + add_to_order_for_port(ctx, fix.name))
    for j in nxt:
        j.inputs["board_pr"] = number
    return open_for_review(ctx, number, pr, nxt, jobs, yes).replace("open for review", f"{fix.id} done; next round")


def accepted_reviews(ctx, job: Job, comments: list[dict], shared, ids: list[int] | None = None):
    """The accepting review comments of a job: those recorded in `merge_pending` (by id), else the
    last review of the job from a trusted login when it accepts; [] when there is none."""
    found = []
    for c in comments:
        parsed = prreviews.parse(c.get("body") or "")
        if not parsed or parsed.kind != "review":
            continue
        if ids is not None:
            if int(c.get("id") or 0) in ids:
                found.append((c, parsed))
            continue
        if trusted(ctx, (c.get("user") or {}).get("login", ""), parsed, job, shared):
            found = [(c, parsed)]
    return [(c, p) for c, p in found if p.verdict == "accepted"]


def owned_changes(ctx, number: int, old: str, new: str, jobs: list[Job], sub_job: str,
                  pr: dict | None = None) -> list[str] | None:
    """What a pull request's own deliverables (with an import's declared files) changed between two
    heads, sources.toml aside (a human rebase that merged main, whose answers rewrote the manifest);
    None when it cannot be read."""
    try:
        compared = ctx.gh.api(f"repos/{repo(ctx)}/compare/{old}...{new}") or {}
    except GHError:
        return None
    od = board.order_file(ctx.cfg)
    owned = set().union(*(board.allowed_paths(j, ctx.cfg.board.packages_dir, od)
                          for j in authors_of(number, sub_job, jobs)), set()) | imported_files(ctx, pr or {})
    return sorted(f["filename"] for f in compared.get("files", [])
                  if f.get("filename") in owned and not f["filename"].endswith("/sources.toml"))


def retry_merges(ctx, number: int, pr: dict, jobs: list[Job], comments: list[dict], shared, yes: bool) -> list[str]:
    """Accepted designs whose merge failed (`merge_pending`): merged now while the head is the
    reviewed one, or a new head whose own deliverables did not change beyond sources.toml (a human
    rebase); a new head with other changes needs a fresh review."""
    out = []
    sub = (board.decode_submission(pr.get("body") or "") or {}).get("job", "")
    for job in [j for j in jobs_of(number, jobs) if j.kind == "review" and j.inputs.get("merge_pending")
                and j.state not in ("done", "blocked")]:
        pending, head = job.inputs["merge_pending"], pr["head"]["sha"]
        if head != pending.get("head"):
            changed = owned_changes(ctx, number, pending.get("head", ""), head, jobs, sub, pr)
            if changed is None:
                continue
            if changed:
                job.inputs.pop("merge_pending")
                ctx.board.update_spec(job)
                note(ctx, number, f"Intake: the head moved from `{pending.get('head', '')[:7]}` to `{head[:7]}` and "
                                  f"changed {', '.join(changed)} since the accepted review: review the new head.")
                out.append(f"#{number}: {job.id} needs a fresh review (the head changed {', '.join(changed)})")
                continue
        reviews = accepted_reviews(ctx, job, comments, shared, pending.get("comments"))
        if not reviews or not yes:
            out.append(f"#{number}: {job.id} accepted, merge to retry" + ("" if reviews else " (no accepting review found)"))
            continue
        pkg = board.package_dir(ctx.cfg.board.packages_dir, job.name)
        why = merge(ctx, number, pr, jobs, {"path": f"{pkg}/design/review.json",
                                            "review": verdict_record(job, reviews, "accepted")}, blocker=job)
        if why:
            if head != pending.get("head"):
                job.inputs["merge_pending"] = {**pending, "head": head}   # the rebased head is the one to merge
                ctx.board.update_spec(job)
            out.append(f"#{number}: {job.id} accepted, but still not merged ({why})")
            continue
        out.append(after_verdict(ctx, job, "accepted", reviews, number, pr, jobs))
    return out


def merged_by_hand(ctx, open_numbers: set[int], jobs: list[Job], shared, yes: bool) -> list[str]:
    """Review jobs whose board pull request was merged on GitHub (not by the intake): finished from
    their recorded or last accepting review, and what follows created, never reopened. A package
    merged that way skips the board red team (there is no open pull request for it): its port
    follows, and the red team runs on the roadmap pull request."""
    out = []
    pending = [j for j in jobs if j.kind == "review" and j.state != "done" and j.inputs.get("board_pr")
               and int(j.inputs["board_pr"]) not in open_numbers]
    for number in sorted({int(j.inputs["board_pr"]) for j in pending}):
        try:
            pr = ctx.gh.api(f"repos/{repo(ctx)}/pulls/{number}") or {}
            if not pr.get("merged"):
                continue
            comments = ctx.gh.api(f"repos/{repo(ctx)}/issues/{number}/comments?per_page=100", paginate=True) or []
        except GHError:
            continue
        sub = board.decode_submission(pr.get("body") or "") or {}
        for job in [j for j in pending if int(j.inputs["board_pr"]) == number]:
            reviews = accepted_reviews(ctx, job, comments, shared, (job.inputs.get("merge_pending") or {}).get("comments"))
            if not reviews:
                continue
            if not yes:
                out.append(f"#{number}: merged on GitHub; would finish {job.id} from its accepted review (dry run)")
                continue
            for author in authors_of(number, sub.get("job", ""), jobs):
                if author.state != "done":
                    finish_job(ctx, author, f"Intake: #{number} was merged on GitHub.")
            pkg = board.package_dir(ctx.cfg.board.packages_dir, job.name)
            verdict = f"{pkg}/design/review.json" if board.parse_job_id(job.id).subject == "DESIGN" else f"{pkg}/review.json"
            put_file(ctx, verdict, json.dumps({"review": verdict_record(job, reviews, "accepted")}, indent=1) + "\n",
                     f"Board: verdict of #{number}")
            if board.parse_job_id(job.id).subject != "DESIGN":
                job.inputs.pop("board_pr", None)     # no open pull request to red-team: the port and its red team follow
            out.append(f"#{number}: merged on GitHub; " + after_verdict(ctx, job, "accepted", reviews, number, pr, jobs,
                                                                     open_pr=False))
    return out


def scan(ctx, shared=None, yes: bool = False) -> list[str]:
    """Every open `needs-review` pull request on the board, advanced as its comments allow; accepted
    designs whose merge failed are merged when they can be; reviews of pull requests merged on
    GitHub are finished from their accepting review."""
    out, jobs = [], ctx.board.all_jobs()
    pulls = ctx.gh.api(f"repos/{repo(ctx)}/pulls?state=open&per_page=100", paginate=True) or []
    out += merged_by_hand(ctx, {int(p["number"]) for p in pulls}, jobs, shared, yes)
    for pr in pulls:
        if NEEDS_REVIEW not in {label["name"] for label in pr.get("labels", [])}:
            continue
        number, head = pr["number"], pr["head"]["sha"]
        mine = [j for j in jobs_of(number, jobs) if j.state != "done"]
        pushed = [j for j in mine if j.kind == "fix" and j.state == "submitted"]
        open_jobs = [j for j in mine if j.kind in ("review", "redteam") and not j.inputs.get("merge_pending")]
        retrying = [j for j in mine if j.inputs.get("merge_pending")]
        if not pushed and not open_jobs and not retrying:
            continue
        try:
            comments = ctx.gh.api(f"repos/{repo(ctx)}/issues/{number}/comments?per_page=100", paginate=True) or []
            if retrying:
                out += retry_merges(ctx, number, pr, jobs, comments, shared, yes)
                jobs = ctx.board.all_jobs()
            if pushed:
                out += [fix_pushed(ctx, f, number, pr, jobs, yes, comments) for f in pushed]
                continue
            for job in sorted(open_jobs, key=lambda j: j.id):
                found = reviews_for(ctx, job, comments, head, jobs, shared, number, out)
                if not found or worker_pending(ctx, job, found, shared):
                    continue
                conclude = conclude_review if job.kind == "review" else conclude_pass
                out.append(conclude(ctx, job, found, number, pr, jobs, yes))
                jobs = ctx.board.all_jobs()
        except GHError as exc:     # one pull request's trouble never stops the scan of the others
            out.append(f"#{number}: skipped: GitHub did not answer ({str(exc).splitlines()[0][:200]}); the next run "
                       "tries again")
    return out


# --- the review queue: what a browser reviewer can pick up ---------------------------------------------

QUEUE_LABEL = "review-queue"
QUEUE_MARK = "<!-- tauceti-roadmap-review-queue -->"


def queue_body(ctx, jobs: list[Job], pulls: list[dict]) -> str:
    """Every open review and red-team pass on a board pull request, with direct links."""
    lines = [("Reviews and red-team passes open on the job board's pull requests. To take one: open the pull "
              "request, read the brief pinned as its first comment, comment `reviewing`, then leave your review as "
              "one comment in the shape the brief gives. Comments are read only from the logins the project lists."), ""]
    rows = []
    for pr in sorted(pulls, key=lambda p: p["number"]):
        if NEEDS_REVIEW not in {label["name"] for label in pr.get("labels", [])}:
            continue
        number, head = pr["number"], pr["head"]["sha"]
        for job in sorted((j for j in jobs_of(number, jobs) if j.kind in ("review", "redteam") and j.state != "done"),
                          key=lambda j: j.id):
            rows.append(f"- [#{number}](https://github.com/{repo(ctx)}/pull/{number}) `{job.name}`: `{job.id}` "
                        f"({job.state}), head `{head[:7]}`")
    lines += rows or ["Nothing is waiting for a review."]
    return "\n".join(lines) + "\n\n" + QUEUE_MARK + "\n"


def review_queue(ctx, yes: bool = False) -> list[str]:
    """Keep the board's pinned "Review queue" issue current (edited in place, never a new comment)."""
    board_repo = ctx.cfg.repositories.board_repo()
    pulls = ctx.gh.api(f"repos/{repo(ctx)}/pulls?state=open&per_page=100", paginate=True) or []
    body = queue_body(ctx, ctx.board.all_jobs(), pulls)
    issues = ctx.gh.api(f"repos/{board_repo}/issues?labels={QUEUE_LABEL}&state=open&per_page=100", paginate=True) or []
    mine = next((i for i in issues if QUEUE_MARK in (i.get("body") or "")), None)
    if mine and mine.get("body") == body:
        return []
    if not yes:
        return ["review queue: would update" if mine else "review queue: would create and pin it"]
    if mine:
        ctx.gh.write("PATCH", f"repos/{board_repo}/issues/{mine['number']}", {"body": body})
        return [f"review queue #{mine['number']} updated"]
    made = ctx.gh.write("POST", f"repos/{board_repo}/issues", {"title": "Review queue", "body": body,
                                                                 "labels": [QUEUE_LABEL]}) or {}
    if made.get("number"):
        try:
            ctx.gh.cli_write("issue", "pin", str(made["number"]), "-R", board_repo)
        except GHError:
            pass
    return [f"review queue #{made.get('number', '?')} created"]
