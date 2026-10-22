"""Board maintenance, run by the board's bot or a maintainer with write access (`board ...` commands).

* `intake(pr)`: take in one submission pull request on the packages repository (or an explorer
  import pull request: see explorer.py). It is merged only
  when it names an open job, changes only that job's deliverables, contains no local path, has
  valid JSON/TOML, its checks passed, and (for a job with `independent_of`) its author did none of
  the listed jobs. After the merge the job is done, the board records "Intake: merged #N from
  @login" (the authorship record the separation check reads), and the job's successors are created.
  Port of the explorer's intake.py merge path.
* `sync()`: target issues become TARGET jobs; an issue carrying several `state:` labels keeps one
  (the board's invariant: exactly one); live leases are mirrored as `state:claimed`;
  roadmap pull requests get their awaiting-author / awaiting-review labels, and a GATE job is
  created for the branch owner when humans are waiting for a reply; held pull requests stay drafts.
* `stale(release)`: claims whose lease lapsed and whose issue shows nothing for `stale_hours`, and
  submissions whose pull request was closed unmerged, go back to `state:available`.
* `board_lock` / `take_lock`: one intake or sync at a time, whoever runs it (the hosted workflow and
  a maintainer's shell share no filesystem): a lease ref in the board repository, which both can
  push; `repair_duplicates`: a job created twice (both open) keeps the twin that got furthest
  (its submission, a worker's claim, else the older issue) and the other takes nothing with it.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import socket

from . import board, boardpr, claims, depgraph, explorer, filers, prreviews, replies
from .board import Job, Outcome
from .gh import GHError
from .lanes.base import open_roadmap_prs
from .lanes.pr import events_from_api
from .lanes.port import pr_number_from_report
from .lanes.target import parse_triage

PENDING = ("queued", "in_progress", "waiting", "requested", "pending")


def _checks(ctx, repo: str, sha: str) -> tuple[list[str], bool]:
    """(failed check names, any still running) for a commit."""
    runs = (ctx.gh.api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs", [])
    latest = {}
    for run in runs:
        if run["name"] not in latest or (run.get("started_at") or "") >= (latest[run["name"]].get("started_at") or ""):
            latest[run["name"]] = run
    failed = [n for n, r in latest.items() if r.get("conclusion") not in (None, "success", "skipped", "neutral")]
    running = any(r.get("status") in PENDING for r in latest.values())
    return failed, running


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


def intake(ctx, number: int, yes: bool = False, pr: dict | None = None) -> str:
    """Take in one submission pull request. `pr`: its object from the open pull requests' listing
    (the same fields as a GET of it), so the intake reads it once for all of them."""
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
    if job.state in ("done", "blocked"):
        problems.append(f"{job.id} is {job.state}")
    files = ctx.gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True)
    allowed = board.allowed_paths(job, cfg.board.packages_dir, board.order_file(cfg))
    for f in files:
        if f.get("status") == "removed":
            problems.append(f"{f['filename']} is deleted")
            continue
        text = ctx.gh.raw_file(pr["head"]["repo"]["full_name"], f["filename"], pr["head"]["sha"]) \
            if f["filename"] in allowed else ""
        problems += board.file_problems(f["filename"], text, allowed)
        if f["filename"].endswith("/design/plan.json") and board.gaps_only(job):
            problems += board.plan_change_problems(f["filename"], base_text(ctx, repo, f["filename"], pr), text)
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
    ctx.gh.write("PUT", f"repos/{repo}/pulls/{number}/merge", {"merge_method": "squash"})
    ctx.board.comment(job.number, f"Intake: merged #{number} from @{author} ({sub.get('agent') or '?'}, "
                                  f"{sub.get('model') or '?'}).")
    job.state = "done"
    outcome = outcome_for(ctx, job, pr, jobs)
    ctx.board.set_state(job, "done")
    ctx.gh.write("PATCH", f"repos/{cfg.repositories.board_repo()}/issues/{job.number}", {"state": "closed"})
    return f"#{number}: merged {job.id}; " + advance(ctx, job, outcome, jobs)


_norm = board.title_key


def stuck_findings(repair_report: str, review: str) -> list[str]:
    """A review's blocking findings, when every one of them re-raises a finding the round's repair
    marked `not actionable in this package` (by `Re-raised: <id>` in the finding, or the same title);
    else []. Another repair round could not change them: a human decides."""
    settled = replies.not_actionable(repair_report)
    if not settled:
        return []
    by_title = {_norm(t): fid for fid, (t, _r) in settled.items() if _norm(t)}
    blocking = [f for f in prreviews.findings_with_bodies(review) if f[1] in ("P1", "P2")]
    out = []
    for fid, severity, title, body in blocking:
        hit = sorted(prreviews.reraised(title, body) & set(settled)) or (
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


def intake_open(ctx, yes: bool, only: int = 0, log=print) -> list[int]:
    """Every open submission or import pull request, each on its own: one listing for all of them (no
    GET per pull request), a pull request whose head, labels and update time are as at the last run
    with a stable outcome (left for the maintainer, under review, not a submission) is not read again
    (a cache in the state directory; empty on a fresh runner), and a GitHub error on one is logged and
    the run goes on. Returns the pull requests skipped by an error."""
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
            outcome = intake(ctx, n, yes=yes, pr=pr)
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


def add_to_order(ctx, name: str, readmes: tuple[str, ...] = (DESIGN_README, PACKAGE_README)) -> str:
    """Insert a roadmap into the merge order after its last supplier (packages repository), its
    suppliers read from the first of `readmes` (paths in its package folder on main) that exists."""
    cfg = ctx.cfg
    if cfg.roadmap.merge_order_repo != "packages":
        return "merge order not updated (not kept in the packages repository)"
    repo, path = cfg.repositories.packages_repo(), cfg.roadmap.merge_order_file
    pkg = board.package_dir(cfg.board.packages_dir, name)
    try:
        current = ctx.gh.api(f"repos/{repo}/contents/{path}")
        text = base64.b64decode(current["content"]).decode("utf-8")
        sha = current["sha"]
    except GHError:
        text, sha = "# Merge order: suppliers before consumers, one roadmap per line.\n", None
    order = depgraph.read_order(text)
    if name in order:
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
    edges = depgraph.supplier_edges({name: readme}, set(order), set(cfg.roadmap.main_extended))
    new = depgraph.insert_into_order(text, name, set(edges.get(name, {})))
    if new == text:
        return f"{name} already in the merge order"
    payload = {"message": f"Merge order: insert {name} after its last supplier",
               "content": base64.b64encode(new.encode()).decode()}
    if sha:
        payload["sha"] = sha
    ctx.gh.write("PUT", f"repos/{repo}/contents/{path}", payload)
    return f"{name} inserted into the merge order"


def add_to_order_for_port(ctx, name: str) -> str:
    """Insert the roadmap of a package whose port follows into the merge order when it is absent
    (an imported package never had its design accepted on the board), its suppliers read from the
    merged package README; a failed write is reported, not raised."""
    try:
        return add_to_order(ctx, name, (PACKAGE_README, DESIGN_README))
    except GHError as exc:
        return f"merge order not updated ({exc})"


def _held(ctx, pr: int) -> bool:
    try:
        held = (ctx.state / "hold-draft.txt").read_text(encoding="utf-8").split()
    except OSError:
        return False
    return str(pr) in held


# --- one run at a time ------------------------------------------------------------------------------

LOCK_TTL = 15 * 60     # seconds a board run holds the board; a run that dies leaves it for this long


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


def take_lock(lock, project: str) -> tuple[bool, str]:
    """(may run, line to log). Held by another run: (False, "intake already running (owner, since
    ...)"). A lock that cannot be written (no push access, no network) never stops a run: it is
    taken as the leases are, fail-open, and said so."""
    key = lock_key(project)
    status = lock.acquire(key, LOCK_TTL)
    if status == claims.HELD:
        lease = lock.read(key) or {}
        since = dt.datetime.fromtimestamp(int(lease.get("acquired_at", 0) or 0), dt.UTC)
        return False, f"intake already running ({lease.get('owner', '?')}, since {since:%Y-%m-%d %H:%M:%SZ}); nothing done"
    if status == claims.ERROR:
        return True, "board lock not taken (the lock ref could not be written); running unlocked"
    return True, ""


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
    lifetime (a worker that died before, or without, registering one: a full disk). A claim made by
    a `reviewing` comment holds for its own four hours. Only with a namespace that can be read now:
    an unreadable one never looks like one without leases."""
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
        if age is None or age <= (COMMENT_CLAIM_TTL if claim["comment"] else claims.JOB_TTL):
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
        events = events_from_api(reviews, comments)
        want, drop = replies.wanted_label(events, {p["owner"]}, p["head"], markers=r.review_markers,
                                          housekeeping=r.housekeeping_prefixes, approvers=r.approvers,
                                          ignore=set(r.ignore_logins), awaiting_author=r.awaiting_author,
                                          awaiting_review=r.awaiting_review)
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
            if not any(j.state in ("available", "claimed", "submitted") for j in mine):
                gate = board.new_job(board.make_id("gate", p["name"], len(mine) + 1), requires_operator=p["owner"],
                                     inputs={"pr": number, "operator": p["owner"]})
                ctx.board.create(gate)
                out.append(f"#{number}: {gate.id} created for @{p['owner']}")
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
