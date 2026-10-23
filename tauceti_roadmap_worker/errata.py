"""The register of mistakes found in published sources (restored from the explorer on 2026-10-23).

Reviews and red-team passes end with a `## Source issues` section (see the prompts); `sync_errata`
reads every such section from the board's pull-request comments and keeps two files on the board's
repository: `errata/source-issues.json` (the data, one entry per distinct mistake, with the job,
pull request, head and date that reported it) and `errata/REGISTER.md` (the same, readable, grouped
by source). The explorer's register, seeded under `errata/explorer/`, is counted in the header.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
from collections import defaultdict

from . import prreviews
from .gh import GHError

DATA = "errata/source-issues.json"
REGISTER = "errata/REGISTER.md"
EXPLORER = "errata/explorer/source-issues.json.gz"
KIND = {"misprint": "Misprint", "error": "Error", "gap": "Gap", "unparsed": "Unparsed"}


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


def explorer_totals(ctx, repo: str) -> dict:
    """The counts of the explorer's seeded register (its gzipped data), {} when absent."""
    try:
        blob = ctx.gh.api(f"repos/{repo}/contents/{EXPLORER}")
        raw = base64.b64decode(blob["content"]) if blob.get("content") else b""
        if not raw and blob.get("sha"):
            raw = base64.b64decode(ctx.gh.api(f"repos/{repo}/git/blobs/{blob['sha']}")["content"])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
    except (GHError, KeyError, ValueError, OSError):
        return {}
    issues = data.get("issues") or []
    status = defaultdict(int)
    for i in issues:
        status[str(i.get("status") or "unknown")] += 1
    return {"total": len(issues), "by_status": dict(status)}


def collect(ctx, repo: str, since: str) -> tuple[list[dict], str]:
    """Source issues from every review or red-team comment on the board's pull requests updated since
    `since` (ISO), with their provenance, and the newest comment time seen."""
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
                              "reporter": f"{(parsed.worker or {}).get('agent') or parsed.agent} "
                                          f"{(parsed.worker or {}).get('model', '')}".strip(),
                              "comment": c.get("html_url", "")})
                out.append(issue)
            newest = max(newest, c.get("created_at") or "")
    return out, newest


def render(data: dict, explorer: dict) -> str:
    issues = data.get("issues", [])
    counts = defaultdict(int)
    for i in issues:
        counts[i.get("kind", "unparsed")] += 1
    lines = ["# Mistakes found in published sources", "",
             ("Reviewers and red-team passes record every mistake they find in a source while checking a roadmap's "
              "locators: misprints (slips whose intended meaning is clear), errors (a false statement or proof step) "
              "and gaps (a step the source does not justify), each with the roadmap, the board pull request and the "
              "head that reported it. Collected by the board sync from the `## Source issues` sections of review and "
              "red-team comments; the data is in `errata/source-issues.json`."), "",
             f"{len(issues)} mistakes recorded on the board: {counts.get('misprint', 0)} misprints, "
             f"{counts.get('error', 0)} errors, {counts.get('gap', 0)} gaps"
             + (f", {counts['unparsed']} unparsed bullets" if counts.get("unparsed") else "") + "."]
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
                lines.append(f"- **{kind}** ({i.get('roadmap')}, #{i.get('board_pr')} at {i.get('head', '')[:7]}, "
                             f"{i.get('reported')}): {i.get('says')}")
                continue
            tail = f"; {i['note']}" if i.get("note") else ""
            lines.append(f"- **{kind}** at {i.get('locator')}: the source says {i.get('says')}; it should be "
                         f"{i.get('should')}; affects {i.get('affects')}{tail}. Found by {i.get('roadmap')} "
                         f"(#{i.get('board_pr')} at {i.get('head', '')[:7]}, {i.get('reported')}, {i.get('reporter')}).")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sync_errata(ctx) -> list[str]:
    """`board sync`: new source issues from the board's review and red-team comments added to the
    register; nothing written when there are none."""
    repo = ctx.cfg.repositories.packages_repo()
    data, sha = _read_json(ctx, repo, DATA)
    data.setdefault("purpose", "Mistakes found in published sources by the board's reviewers and red teams.")
    data.setdefault("issues", [])
    since = str(data.get("scanned_to") or "")
    found, newest = collect(ctx, repo, since)
    known = {key(i) for i in data["issues"]}
    added = []
    for issue in found:
        k = key(issue)
        if k in known:
            continue
        known.add(k)
        data["issues"].append(issue)
        added.append(issue)
    exists = True
    try:
        ctx.gh.api(f"repos/{repo}/contents/{REGISTER}")
    except GHError:
        exists = False
    if not added and newest == since and exists:
        return ["errata: nothing new"]
    data["scanned_to"] = newest or since
    data["updated"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if ctx.dry_run:
        return [f"errata: {len(added)} new source issue(s) (dry run: not written)"]
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
    return [f"errata: {len(added)} new source issue(s) recorded ({len(data['issues'])} in all)"]
