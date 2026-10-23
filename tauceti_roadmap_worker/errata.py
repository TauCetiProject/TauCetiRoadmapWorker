"""The register of mistakes found in published sources (restored from the explorer on 2026-10-23).

Reviews and red-team passes end with a `## Source issues` section (see the prompts), and an errata
job (`ERRATA-<Name>`, lanes/errata.py) reads every source a roadmap cites at the locators it cites
and posts its `## Source issues` as a comment on the job's own issue. `sync_errata` reads every such
section, from the board's pull-request comments and from the errata jobs that were submitted, and
keeps two files on the board's repository: `errata/source-issues.json` (the data, one entry per
distinct mistake, with the job, pull request or job, head and date that reported it, and a `covered`
list of the errata jobs read, so that a roadmap whose sources had no mistake counts as covered) and
`errata/REGISTER.md` (the same, readable, grouped by source). An errata job read into the register is
marked done. The explorer's register, seeded under `errata/explorer/`, is counted in the header.

`seed` (`board errata seed`) creates the errata jobs: one per roadmap on the content repository's
main and per roadmap an open roadmap pull request adds, unless the roadmap has an errata job, the
explorer's register covers it, or the board's register already holds its errata.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
from collections import Counter, defaultdict

from . import board, boardpr, gitops, openroadmaps, prreviews
from .board import Job, JobIdError
from .gh import GHError

DATA = "errata/source-issues.json"
REGISTER = "errata/REGISTER.md"
EXPLORER = "errata/explorer/source-issues.json.gz"
KIND = {"misprint": "Misprint", "error": "Error", "gap": "Gap", "unparsed": "Unparsed"}
READ_STATES = ("submitted", "done")     # the errata jobs whose comment the sync reads


def key(issue: dict) -> str:
    return "|".join(" ".join(str(issue.get(k, "")).split()).lower() for k in ("source", "locator", "says"))


def _read_json(ctx, repo: str, path: str) -> tuple[dict, str | None]:
    try:
        current = ctx.gh.api(f"repos/{repo}/contents/{path}")
        return json.loads(base64.b64decode(current["content"]).decode("utf-8")), current["sha"]
    except (GHError, KeyError, ValueError):
        return {}, None


def _write(ctx, repo: str, path: str, text: str, sha: str | None, message: str) -> None:
    payload = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode()}
    if sha:
        payload["sha"] = sha
    ctx.gh.write("PUT", f"repos/{repo}/contents/{path}", payload)


def explorer_data(ctx, repo: str) -> dict:
    """The explorer's seeded register (its gzipped data), {} when absent or unreadable."""
    try:
        blob = ctx.gh.api(f"repos/{repo}/contents/{EXPLORER}")
        raw = base64.b64decode(blob["content"]) if blob.get("content") else b""
        if not raw and blob.get("sha"):
            raw = base64.b64decode(ctx.gh.api(f"repos/{repo}/git/blobs/{blob['sha']}")["content"])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
    except (GHError, KeyError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def explorer_totals(ctx, repo: str, data: dict | None = None) -> dict:
    """The counts of the explorer's seeded register, {} when absent."""
    data = explorer_data(ctx, repo) if data is None else data
    if not data:
        return {}
    issues = data.get("issues") or []
    status = defaultdict(int)
    for i in issues:
        status[str(i.get("status") or "unknown")] += 1
    return {"total": len(issues), "by_status": dict(status)}


def explorer_names(data: dict) -> set[str]:
    """The roadmaps the explorer's register covers: the name before the `/` of an issue's id
    (`AInfCohomology/E1`), and an issue's `roadmapId` or `roadmap` field."""
    out = set()
    for issue in data.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        ident = str(issue.get("id") or "")
        if "/" in ident:
            out.add(ident.split("/", 1)[0])
        out |= {str(issue[k]) for k in ("roadmapId", "roadmap") if issue.get(k)}
    return out


def reporter(parsed: prreviews.Parsed) -> str:
    return f"{(parsed.worker or {}).get('agent') or parsed.agent} {(parsed.worker or {}).get('model', '')}".strip()


def errata_comment(ctx, job: Job, shared=None) -> tuple[dict, prreviews.Parsed] | None:
    """The last errata comment on the job's issue from a login the board trusts for the job (as
    `boardpr.trusted` decides for a review: a listed login, or the worker that did the job by its
    marker and the claims record), or None."""
    found = None
    for c in ctx.board.comments(job.number):
        parsed = prreviews.parse(c.get("body") or "")
        if parsed and parsed.kind == "errata" and boardpr.trusted(ctx, (c.get("user") or {}).get("login", ""), parsed,
                                                                  job, shared):
            found = (c, parsed)
    return found


def collect(ctx, repo: str, since: str, shared=None,
            covered: set[str] | frozenset = frozenset()) -> tuple[list[dict], str, list[tuple[Job, dict | None]]]:
    """Source issues from every review or red-team comment on the board's pull requests updated since
    `since` (ISO), and from the errata comment of every errata job submitted or done that `covered`
    (the job ids the register has read) lacks, with their provenance; the newest pull-request comment
    time seen; and the errata jobs read: (job, its `covered` entry), or (job, None) for a job the
    register covers that is still `submitted` (its marking as done failed)."""
    newest, out = since, []
    pulls = ctx.gh.api(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100", paginate=False) or []
    for pr in pulls:
        if since and (pr.get("updated_at") or "") <= since:
            continue
        number = pr["number"]
        query = f"repos/{repo}/issues/{number}/comments?per_page=100" + (f"&since={since}" if since else "")
        comments = ctx.gh.api(query, paginate=True) or []
        for c in comments:
            body = c.get("body") or ""
            parsed = prreviews.parse(body)
            if parsed is None or parsed.kind not in ("review", "redteam"):
                continue
            issues = prreviews.source_issues(body)
            if not issues:
                continue
            job = (parsed.worker or {}).get("job", "")
            for issue in issues:
                issue.update({"roadmap": job.split("-")[-1].split("~")[0] if job else "",
                              "job": job, "board_pr": number, "head": parsed.sha, "reported": (c.get("created_at") or "")[:10],
                              "reporter": reporter(parsed), "comment": c.get("html_url", "")})
                out.append(issue)
            newest = max(newest, c.get("created_at") or "")
    read: list[tuple[Job, dict | None]] = []
    for job in ctx.board.kind_jobs("errata"):
        if job.state not in READ_STATES:
            continue
        if job.id in covered:
            if job.state == "submitted":
                read.append((job, None))
            continue
        found = errata_comment(ctx, job, shared)
        if not found:
            continue
        c, parsed = found
        reported = (c.get("created_at") or "")[:10]
        issues = prreviews.source_issues(c.get("body") or "")
        for issue in issues:
            issue.update({"roadmap": job.name, "job": job.id, "issue": job.number, "head": parsed.sha,
                          "reported": reported, "reporter": reporter(parsed), "comment": c.get("html_url", "")})
        out += issues
        kinds = Counter(i["kind"] for i in issues)
        counts = {k: kinds[k] for k in ("misprint", "error", "gap")} | ({"unparsed": kinds["unparsed"]}
                                                                         if kinds["unparsed"] else {})
        read.append((job, {"job": job.id, "roadmap": job.name, "head": parsed.sha, "date": reported,
                           "issues": len(issues), "counts": counts, "comment": c.get("html_url", "")}))
    return out, newest, read


def provenance(issue: dict) -> str:
    """Where an entry was reported: the board pull request of a review or red-team pass, else the
    errata job."""
    return f"#{issue['board_pr']}" if issue.get("board_pr") else str(issue.get("job") or "?")


def render(data: dict, explorer: dict) -> str:
    issues = data.get("issues", [])
    counts = defaultdict(int)
    for i in issues:
        counts[i.get("kind", "unparsed")] += 1
    lines = ["# Mistakes found in published sources", "",
             ("Reviewers, red-team passes and errata jobs record every mistake they find in a source while checking "
              "a roadmap's locators: misprints (slips whose intended meaning is clear), errors (a false statement or "
              "proof step) and gaps (a step the source does not justify), each with the roadmap, the board pull "
              "request or errata job and the head that reported it. Collected by the board sync from the "
              "`## Source issues` sections of review, red-team and errata comments; the data is in "
              "`errata/source-issues.json`."), "",
             f"{len(issues)} mistakes recorded on the board: {counts.get('misprint', 0)} misprints, "
             f"{counts.get('error', 0)} errors, {counts.get('gap', 0)} gaps"
             + (f", {counts['unparsed']} unparsed bullets" if counts.get("unparsed") else "") + "."]
    covered = data.get("covered") or []
    if covered:
        roadmaps = {c.get("roadmap") for c in covered}
        clean = {c.get("roadmap") for c in covered if not c.get("issues")}
        lines.append(f"Errata jobs have read the cited sources of {len(roadmaps)} roadmaps; in those of {len(clean)} "
                     "they found no mistake.")
    if explorer:
        st = explorer.get("by_status", {})
        lines.append(f"The explorer's register (to 2026-10-21) holds {explorer.get('total', 0)} more, kept in "
                     f"`errata/explorer/`: " + ", ".join(f"{v} {k}" for k, v in sorted(st.items())) + ".")
    lines.append("")
    by_source: dict[str, list[dict]] = defaultdict(list)
    for i in issues:
        by_source[i.get("source") or "(source not named)"].append(i)
    for source in sorted(by_source, key=str.lower):
        lines += [f"## {source}", ""]
        for i in sorted(by_source[source], key=lambda x: (x.get("locator", ""), x.get("reported", ""))):
            kind = KIND.get(i.get("kind", "unparsed"), "Unparsed")
            if i.get("kind") == "unparsed":
                lines.append(f"- **{kind}** ({i.get('roadmap')}, {provenance(i)} at {i.get('head', '')[:7]}, "
                             f"{i.get('reported')}): {i.get('says')}")
                continue
            tail = f"; {i['note']}" if i.get("note") else ""
            lines.append(f"- **{kind}** at {i.get('locator')}: the source says {i.get('says')}; it should be "
                         f"{i.get('should')}; affects {i.get('affects')}{tail}. Found by {i.get('roadmap')} "
                         f"({provenance(i)} at {i.get('head', '')[:7]}, {i.get('reported')}, {i.get('reporter')}).")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sync_errata(ctx, shared=None) -> list[str]:
    """`board sync`: new source issues from the board's review and red-team comments and from the
    submitted errata jobs added to the register (nothing written when there are none), and every
    errata job the register has read marked done (its issue closed). `shared` is the claims namespace
    that tells which worker did a job."""
    repo = ctx.cfg.repositories.packages_repo()
    data, sha = _read_json(ctx, repo, DATA)
    data.setdefault("purpose", "Mistakes found in published sources by the board's reviewers, red teams and "
                               "errata jobs.")
    data.setdefault("issues", [])
    data.setdefault("covered", [])
    since = str(data.get("scanned_to") or "")
    found, newest, read = collect(ctx, repo, since, shared, {str(c.get("job", "")) for c in data["covered"]})
    known = {key(i) for i in data["issues"]}
    added = []
    for issue in found:
        k = key(issue)
        if k in known:
            continue
        known.add(k)
        data["issues"].append(issue)
        added.append(issue)
    entries = [entry for _job, entry in read if entry]
    data["covered"] += entries
    finished = [job for job, _entry in read if job.state == "submitted"]
    exists = True
    try:
        ctx.gh.api(f"repos/{repo}/contents/{REGISTER}")
    except GHError:
        exists = False
    changed = bool(added or entries or newest != since or not exists)
    if not changed and not finished:
        return ["errata: nothing new"]
    out = []
    if changed:
        data["scanned_to"] = newest or since
        data["updated"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ctx.dry_run:
            out.append(f"errata: {len(added)} new source issue(s) (dry run: not written)")
        else:
            _write(ctx, repo, DATA, json.dumps(data, ensure_ascii=False, indent=1) + "\n", sha,
                   f"Errata: {len(added)} new source issue(s)")
            reg_sha = None
            try:
                current = ctx.gh.api(f"repos/{repo}/contents/{REGISTER}")
                reg_sha = current["sha"]
            except (GHError, KeyError):
                pass
            _write(ctx, repo, REGISTER, render(data, explorer_totals(ctx, repo)), reg_sha,
                   f"Errata register: {len(data['issues'])} source issue(s)")
            out.append(f"errata: {len(added)} new source issue(s) recorded ({len(data['issues'])} in all)")
    out += [f"errata: {e['job']} read at {e['head'][:7]}: {e['issues']} source issue(s)" for e in entries]
    # Done only once the register holds the job: a write that failed leaves it submitted for the next run.
    for job in finished:
        if ctx.dry_run:
            out.append(f"errata: {job.id} would be marked done")
            continue
        ctx.board.set_state(job, "done", f"Recorded in the errata register (`{REGISTER}`) by the board sync.")
        ctx.gh.write("PATCH", f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}", {"state": "closed"})
        out.append(f"errata: {job.id} done")
    return out


# --- seeding the errata jobs ------------------------------------------------------------------------

def plan_seed(main: dict[str, str], prs: list[dict], jobs: list[Job], explorer: set[str],
              recorded: set[str]) -> list[tuple[str, Job | None, str]]:
    """(roadmap, the errata job to create or None, why not) for every roadmap an open roadmap pull
    request adds (first, oldest pull request first: such a roadmap can still be repaired), then for
    every roadmap on main (`main`: name -> folder). A roadmap is skipped when it has an errata job in
    any state (`jobs`), when the explorer's register covers it (`explorer`: roadmap names), or when the
    board's register holds the errata of `ERRATA-<Name>` (`recorded`: job ids). Pure."""
    have = {j.name for j in jobs if j.kind == "errata"}
    candidates = [(name, {"pr": int(p["pr"]), "folder": (p.get("folders") or {}).get(name, "")})
                  for p in sorted(prs, key=lambda p: int(p["pr"])) for name in p.get("adds", [])]
    candidates += [(name, {"ref": "main", "folder": folder}) for name, folder in sorted(main.items())]
    out, seen = [], set()
    for name, inputs in candidates:
        if name in seen:
            continue             # a roadmap two pull requests add: the older one's
        seen.add(name)
        try:
            job = board.new_job(board.make_id("errata", name), inputs=inputs)
        except JobIdError:
            out.append((name, None, "not a name a job id can carry"))
            continue
        why = (f"{job.id} is on the board already" if name in have
               else "covered by the explorer's register" if name in explorer
               else f"the board's register holds the errata of {job.id}" if job.id in recorded else "")
        out.append((name, None if why else job, why))
    return out


def preamble(job: Job, content: str) -> str:
    where = (f"`{job.inputs.get('folder')}` in pull request #{job.inputs['pr']} of `{content}`" if job.inputs.get("pr")
             else f"`{job.inputs.get('folder')}` on the main branch of `{content}`")
    return (f"The errata of the sources of `{job.name}` ({where}): every source the roadmap cites, read at the "
            "locators it cites, with the misprints, errors and gaps found in the sources. The worker posts them as "
            f"a comment on this issue; `board sync` collects them into `{REGISTER}` and marks the job done.")


def seed(ctx, yes: bool = False, limit: int = 0) -> list[str]:
    """`board errata seed`: an `ERRATA-<Name>` job for every roadmap on the content repository's main
    and every roadmap an open roadmap pull request adds (`plan_seed` says which are skipped), one line
    per roadmap; written only with `yes`, at most `limit` jobs per run (0: all)."""
    cfg, out = ctx.cfg, []
    state = cfg.state_dir()
    try:
        index = openroadmaps.refresh(ctx.gh, cfg)       # GitHub reads and the host's mirror; no GitHub write
    except (GHError, gitops.GitError, OSError) as exc:
        index = openroadmaps.load(state)
        why = (str(exc).splitlines() or [type(exc).__name__])[0][:200]
        if not index:
            return [f"errata seed: the open roadmap pull requests could not be read ({why}); nothing created"]
        out.append(f"errata seed: the index of open roadmap pull requests was not refreshed ({why}); using the one "
                   f"of {index.get('at')}")
    main = openroadmaps.main_roadmaps(state, cfg.project.branch, (cfg.roadmap.active_dir, cfg.roadmap.completed_dir))
    repo = cfg.repositories.packages_repo()
    data, _sha = _read_json(ctx, repo, DATA)
    recorded = ({str(i.get("job", "")) for i in data.get("issues", [])}
                | {str(c.get("job", "")) for c in data.get("covered", [])})
    explorer = explorer_names(explorer_data(ctx, repo))
    plan = plan_seed(main, index.get("prs", []), ctx.board.kind_jobs("errata"), explorer, recorded)
    made = 0
    for name, job, why in plan:
        if job is None:
            out.append(f"skip {name}: {why}")
            continue
        where = f"#{job.inputs['pr']}" if job.inputs.get("pr") else "main"
        if limit and made >= limit:
            out.append(f"later {job.id} ({where}): not created this run (--limit {limit})")
            continue
        made += 1
        if not yes:
            out.append(f"would create {job.id} ({where}, {job.inputs.get('folder')}) (dry run; pass --yes)")
            continue
        number = ctx.board.create(job, preamble=preamble(job, cfg.repositories.content))
        out.append(f"created {board.made_note(ctx.board, job, number)} ({where})")
    skipped = sum(1 for _n, job, _w in plan if job is None)
    left = len(plan) - skipped - made
    done = "created" if yes else "to create"
    out.append(f"errata seed: {len(plan)} roadmaps, {made} job(s) {done}, {skipped} skipped"
               + (f", {left} left for a later run" if left else ""))
    return out
