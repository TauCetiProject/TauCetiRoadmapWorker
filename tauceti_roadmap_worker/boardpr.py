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
  red-team round's fix the pull request is merged and the port job created.

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

from . import board, claims, prompts, prreviews
from .board import Job, Outcome
from .gh import GHError

NEEDS_REVIEW = "needs-review"
BRIEF_MARK = "tauceti-roadmap-review-brief"
AUTHOR_NOTE = "Intake: authored by @{login} ({agent}, {model}) in #{pr}."


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def repo(ctx) -> str:
    return ctx.cfg.repositories.packages_repo()


def jobs_of(pr: int, jobs: list[Job]) -> list[Job]:
    return [j for j in jobs if j.inputs.get("board_pr") == pr]


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
              "KIND": "red-team pass" if job.kind == "redteam" else "review"}
    return prompts.render("review-web", values) + f"\n<!-- {BRIEF_MARK} {json.dumps({'job': job.id})} -->\n"


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
    created = [f"{j.id} (#{ctx.board.create(j)})" for j in todo]
    return f"#{number}: open for review; created " + (", ".join(created) or "nothing")


def submission_under_review(ctx, number: int, pr: dict, sub: dict, job: Job, jobs: list[Job], yes: bool) -> str:
    """A design or package submission that passed the intake's checks: it stays open, its author is
    recorded, and its review follows on it."""
    login = pr["user"]["login"]
    new = board.successors(job, Outcome())
    if not yes:
        return open_for_review(ctx, number, pr, new, jobs, yes)
    ctx.board.comment(job.number, AUTHOR_NOTE.format(login=login, agent=sub.get("agent") or "?",
                                                     model=sub.get("model") or "?", pr=number))
    job.inputs["board_pr"] = number
    ctx.board.update_spec(job)
    return open_for_review(ctx, number, pr, new, jobs, yes)


# --- reading the comments ----------------------------------------------------------------------------

def trusted(ctx, login: str, parsed: prreviews.Parsed, job: Job) -> bool:
    """A listed login, the board's bot, or the worker that claimed this job (its marker names the job
    and its login wrote the claim note on the job's issue)."""
    if login in ctx.cfg.review.comment_reviewers or login in ctx.cfg.board.bot_logins:
        return True
    if parsed.worker.get("job") != job.id:
        return False
    return any((c.get("user") or {}).get("login") == login and (c.get("body") or "").startswith(f"Claimed for `{job.id}`")
               for c in ctx.board.comments(job.number))


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
        if holder.acquire(claims.job_key(ctx.cfg.project.name, job.id), claims.AUTHOR_TTL,
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
        if not trusted(ctx, login, parsed, job):
            continue
        if not head.startswith(parsed.sha):
            if cid not in handled:
                note(ctx, number, f"Not counted: @{login}'s {parsed.kind} is of `{parsed.sha[:7]}`, but the head is "
                                  f"`{head[:7]}`. Review the current head.")
                handled.add(cid)
            continue
        if not independent(ctx, login, parsed, job, jobs, shared):
            if cid not in handled:
                note(ctx, number, f"Not counted for `{job.id}`: @{login} ({parsed.agent}) is not independent of its "
                                  f"authors (separation by {ctx.cfg.review.separation}).")
                handled.add(cid)
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


def merge(ctx, number: int, pr: dict, jobs: list[Job], verdict: dict | None) -> None:
    """Merge the pull request, write its verdict file, and close the authoring jobs it carried."""
    ctx.gh.write("PUT", f"repos/{repo(ctx)}/pulls/{number}/merge", {"merge_method": "squash"})
    sub = board.decode_submission(pr.get("body") or "") or {}
    authoring = [j for j in jobs if (j.inputs.get("board_pr") == number or j.id == sub.get("job"))
                 and j.kind in ("design", "package", "fix") and j.state != "done"]
    for j in authoring:
        finish_job(ctx, j, f"Intake: merged #{number} from @{pr['user']['login']} ({sub.get('agent') or '?'}, "
                           f"{sub.get('model') or '?'}).")
    if verdict:
        path, data = verdict["path"], {"review": verdict["review"]}
        put_file(ctx, path, json.dumps(data, indent=1) + "\n", f"Board: verdict of #{number}")


def conclude_review(ctx, job: Job, reviews, number: int, pr: dict, jobs: list[Job], yes: bool) -> str:
    cfg = ctx.cfg
    verdicts = {p.verdict for _c, p in reviews}
    verdict = "rejected" if "rejected" in verdicts else "needs_changes" if "needs_changes" in verdicts else "accepted"
    if not yes:
        return f"#{number}: {job.id} {verdict} (dry run)"
    pkg = board.package_dir(cfg.board.packages_dir, job.name)
    put_file(ctx, f"{pkg}/reports/{job.id}.md", report_text(reviews), f"Board: {job.id} ({verdict})")
    who = ", ".join(f"@{(c.get('user') or {}).get('login', '?')} ({p.agent})" for c, p in reviews)
    finish_job(ctx, job, f"Intake: {verdict} on #{number} by {who}.")
    outcome = Outcome(verdict=verdict, pr=number, operator=pr["user"]["login"])
    j = board.parse_job_id(job.id)
    if job.kind == "review" and verdict == "accepted" and j.subject == "DESIGN":
        merge(ctx, number, pr, jobs, {"path": f"{pkg}/design/review.json", "review": verdict_record(job, reviews, "accepted")})
        from .intake import announce_gaps
        design = next((x for x in jobs if x.id == board.make_id("design", job.name)), None)
        if design:
            announce_gaps(ctx, design, jobs)
    elif job.kind == "review" and verdict == "accepted":
        job.inputs["accepted_review"] = verdict_record(job, reviews, "accepted")
    return f"#{number}: {job.id} {verdict}; " + advance_on(ctx, job, outcome, jobs, number, pr)


def verdict_record(job: Job, reviews, status: str) -> dict:
    return {"status": status, "reviewer": f"independent-review-{job.id}", "date": now().date().isoformat(),
            "findings": sum(len(p.findings) for _c, p in reviews), "blocking": sum(p.blocking for _c, p in reviews),
            "notes": "comment review on the board pull request by " + ", ".join(
                f"@{(c.get('user') or {}).get('login', '?')} ({p.agent}) of {p.sha[:7]}" for c, p in reviews)}


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
    new = [j for j in ctx.board.all_jobs() if j.id not in before and j.kind in ("review", "redteam")
           and j.inputs.get("board_pr") == number]
    if new and not pr.get("merged"):
        comments = ctx.gh.api(f"repos/{repo(ctx)}/issues/{number}/comments?per_page=100", paginate=True) or []
        last = max((int(c.get("id") or 0) for c in comments), default=0)
        for j in new:
            j.inputs["after_comment"] = last
            ctx.board.update_spec(j)
        pin_brief(ctx, number, new[0], pr, comments)
    return note_


def fix_pushed(ctx, fix: Job, number: int, pr: dict, jobs: list[Job], yes: bool) -> str:
    """A fix the pull request's owner pushed: done, and the next round (or the port) follows."""
    if not yes:
        return f"#{number}: {fix.id} pushed; the next round follows (dry run)"
    finish_job(ctx, fix, f"Intake: fix pushed to #{number} at `{pr['head']['sha'][:7]}`.")
    nxt = board.successors(fix, Outcome(), max_rounds=ctx.cfg.board.max_rounds, redteam_rounds=ctx.cfg.redteam.rounds,
                           redteam_agents=tuple(p.agent for p in ctx.cfg.redteam.passes),
                           redteam_models=tuple(p.model for p in ctx.cfg.redteam.passes))
    if any(j.kind == "port" for j in nxt):
        accepted = next((j.inputs.get("accepted_review") for j in jobs if j.kind == "review"
                         and j.inputs.get("board_pr") == number and j.inputs.get("accepted_review")), None)
        pkg = board.package_dir(ctx.cfg.board.packages_dir, fix.name)
        merge(ctx, number, pr, jobs, {"path": f"{pkg}/review.json", "review": accepted} if accepted else None)
        existing = {j.id for j in jobs}
        created = [f"{j.id} (#{ctx.board.create(j)})" for j in nxt if j.id not in existing]
        return f"#{number}: {fix.id} done; merged; created " + (", ".join(created) or "nothing")
    for j in nxt:
        j.inputs["board_pr"] = number
    return open_for_review(ctx, number, pr, nxt, jobs, yes).replace("open for review", f"{fix.id} done; next round")


def scan(ctx, shared=None, yes: bool = False) -> list[str]:
    """Every open `needs-review` pull request on the board, advanced as its comments allow."""
    out, jobs = [], ctx.board.all_jobs()
    pulls = ctx.gh.api(f"repos/{repo(ctx)}/pulls?state=open&per_page=100", paginate=True) or []
    for pr in pulls:
        if NEEDS_REVIEW not in {label["name"] for label in pr.get("labels", [])}:
            continue
        number, head = pr["number"], pr["head"]["sha"]
        mine = [j for j in jobs_of(number, jobs) if j.state != "done"]
        pushed = [j for j in mine if j.kind == "fix" and j.state == "submitted"]
        if pushed:
            out += [fix_pushed(ctx, f, number, pr, jobs, yes) for f in pushed]
            continue
        open_jobs = [j for j in mine if j.kind in ("review", "redteam")]
        if not open_jobs:
            continue
        comments = ctx.gh.api(f"repos/{repo(ctx)}/issues/{number}/comments?per_page=100", paginate=True) or []
        for job in sorted(open_jobs, key=lambda j: j.id):
            found = reviews_for(ctx, job, comments, head, jobs, shared, number, out)
            if not found or worker_pending(ctx, job, found, shared):
                continue
            conclude = conclude_review if job.kind == "review" else conclude_pass
            out.append(conclude(ctx, job, found, number, pr, jobs, yes))
            jobs = ctx.board.all_jobs()
    return out
