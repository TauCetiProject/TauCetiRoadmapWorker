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
"""

from __future__ import annotations

import base64
import datetime as dt
import json

from . import board, claims, depgraph, explorer, filers, replies
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


def intake(ctx, number: int, yes: bool = False) -> str:
    cfg, repo = ctx.cfg, ctx.cfg.repositories.packages_repo()
    pr = ctx.gh.api(f"repos/{repo}/pulls/{number}")
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
    problems = []
    if pr.get("draft"):
        return f"#{number}: a draft; the intake waits until it is ready"
    if pr.get("state") != "open":
        return f"#{number}: not open"
    if job.state in ("done", "blocked"):
        problems.append(f"{job.id} is {job.state}")
    files = ctx.gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True)
    allowed = board.allowed_paths(job, cfg.board.packages_dir)
    for f in files:
        if f.get("status") == "removed":
            problems.append(f"{f['filename']} is deleted")
            continue
        text = ctx.gh.raw_file(pr["head"]["repo"]["full_name"], f["filename"], pr["head"]["sha"]) \
            if f["filename"] in allowed else ""
        problems += board.file_problems(f["filename"], text, allowed)
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


def advance(ctx, job: Job, outcome: Outcome, jobs: list[Job]) -> str:
    cfg = ctx.cfg
    new = board.successors(job, outcome, max_rounds=cfg.board.max_rounds, redteam_rounds=cfg.redteam.rounds,
                           redteam_agents=tuple(p.agent for p in cfg.redteam.passes),
                           redteam_models=tuple(p.model for p in cfg.redteam.passes))
    existing = {j.id for j in jobs}
    created = []
    for s in new:
        if s.id not in existing:
            ctx.board.create(s)
            created.append(s.id)
    notes = []
    if job.kind == "target" and outcome.verdict == "merge-into":
        notes.append(absorb(ctx, job, outcome.into, jobs))
    reason = board.blocked_reason(job, outcome, max_rounds=cfg.board.max_rounds)
    if reason:
        ctx.board.comment(job.number, f"Parked for a human: {reason}")
        notes.append("parked")
    if job.kind == "review" and outcome.verdict == "accepted" and board.parse_job_id(job.id).subject == "DESIGN":
        notes.append(add_to_order(ctx, job.name))
    if job.kind == "fix" and board.parse_job_id(job.id).subject == "RT" and not new and cfg.redteam.ready_after:
        pr = job.inputs.get("pr")
        if pr and not _held(ctx, int(pr)):
            ctx.gh.cli_write("pr", "ready", str(pr), "-R", cfg.repositories.content)
            notes.append(f"#{pr} marked ready")
    return "created " + (", ".join(created) if created else "nothing") + (f" ({'; '.join(notes)})" if notes else "")


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


def add_to_order(ctx, name: str) -> str:
    """Insert an accepted design into the merge order after its last supplier (packages repository)."""
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
    readme = ctx.gh.raw_file(repo, f"{pkg}/design/README.md")
    order = depgraph.read_order(text)
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


def _held(ctx, pr: int) -> bool:
    try:
        held = (ctx.state / "hold-draft.txt").read_text(encoding="utf-8").split()
    except OSError:
        return False
    return str(pr) in held


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


def sync_expired(ctx, shared: claims.Claims | None) -> list[str]:
    """Claimed jobs whose lease has expired: the worker stopped renewing it (it was killed, or lost
    its network) and never released the job, so it goes back to `state:available` now rather than
    after `stale_hours`. A claim with no lease at all (a worker that could not register one) is
    left to `board stale`."""
    if not shared:
        return []
    out, now = [], int(dt.datetime.now(dt.UTC).timestamp())
    for job in ctx.board.list_jobs(("claimed",)):
        lease = shared.read(claims.job_key(ctx.cfg.project.name, job.id))
        if lease and not claims.lease_live(lease, now):
            ended = dt.datetime.fromtimestamp(int(lease.get("expires_at", 0)), dt.UTC)
            ctx.board.set_state(job, "available", f"Released: the lease of `{lease.get('owner', '?')}` expired at "
                                                  f"{ended:%Y-%m-%d %H:%MZ} without a release.")
            out.append(f"{job.id}: released (lease of {lease.get('owner', '?')} expired)")
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
            ctx.gh.cli_write("pr", "ready", str(number), "--undo", "-R", repo)
            out.append(f"#{number}: held, converted back to draft")
        reviews = ctx.gh.api(f"repos/{repo}/pulls/{number}/reviews?per_page=100", paginate=True)
        comments = ctx.gh.api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True)
        events = events_from_api(reviews, comments)
        want, drop = replies.wanted_label(events, {p["owner"]}, p["head"], markers=r.review_markers,
                                          housekeeping=r.housekeeping_prefixes, approvers=r.approvers,
                                          ignore=set(r.ignore_logins), awaiting_author=r.awaiting_author,
                                          awaiting_review=r.awaiting_review)
        have = {label["name"] for label in data.get("labels", [])}
        if want and want not in have:
            ctx.gh.write("POST", f"repos/{repo}/issues/{number}/labels", {"labels": [want]})
        for label in drop:
            if label in have:
                ctx.gh.write("DELETE", f"repos/{repo}/issues/{number}/labels/{label}")
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
