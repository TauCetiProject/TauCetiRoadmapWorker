"""`tauceti-roadmap reformat-reviews`: the latest AI review of each roadmap folder of each open roadmap
pull request, rebuilt in place in the posted layout (`prreviews.render_posted`), without a model.

For every open pull request that adds or changes a roadmap folder (any author's), the command reads its
comments, takes the AI review comments (`prreviews.read_posted`), and keeps the latest of each roadmap
folder. When that comment is ours (posted by the operator running the command, or a login given with
`--login`), it is rebuilt: the header lines as they are, a summary composed from the verdict, the
findings' own titles and the counts, the findings, the questions for the human reviewers and the source
issues word for word, and what was checked (the gate's sentence, the earlier findings, what could not
be verified) in one collapsed block; `## Verified sound`, `Source independence:` and `Research link:`
are left out. Old reviews have no small corrections or format notes: their findings, P3 ones included,
stay findings.

Nothing the pipeline reads may change: the verdict, the reviewed head, the folder, the findings' ids,
severities and titles, the marker, the questions for the human reviewers and the source issues
(`prreviews.posted_identity`), and how the timeline reads the comment (`replies.is_review`, the AI review
and approval patterns), are compared before and after, and a comment for which any differs is left as
it is. The rebuilt comment pins the text version of the comment it replaces (`prreviews.pin_marker`), so
that `replies.text_version` gives the same version and a reply that answered the review still does.

A pull request with a review or gate round claimed or submitted is skipped and reported (a review is
never edited under a running round). The command is idempotent (a comment already in the layout is
left alone); without `--yes` it is a dry run that prints each comment's size before and after; its
edits are `--pace` seconds apart (one a second by default); a rate-limit answer stops it.
"""

from __future__ import annotations

import time

from . import openroadmaps, prbatch, prreviews, replies
from .config import ignored_logins
from .gh import GHError, GHHalt, GHRateLimited
from .lanes.base import pr_answerers

UNDER_WAY = ("claimed", "submitted")


def timeline_reading(cfg, body: str, login: str, owner: str) -> tuple:
    """How the board's reading of a pull request's timeline takes a comment: as a review to answer or
    not (`replies.is_review`, as posted by `login` on a pull request by `owner`), as an AI review, as a
    carried review, as an approving verdict, and the text version it records."""
    r = cfg.replies
    event = replies.Event("", login, body)
    review = replies.is_review(event, {x.lower() for x in pr_answerers(cfg, owner)}, r.review_markers,
                               ignored_logins(cfg), set(r.maintainer_logins), r.maintainer_review_prefix, owner,
                               r.housekeeping_prefixes)
    bare = replies.undecorated(body)
    return (review, bool(replies.AI_REVIEW.match(bare)), bool(replies.CARRIED_REVIEW.match(bare)),
            bool(replies.APPROVE_VERDICT.search(body)), replies.text_version(body))


def folder_key(p: prreviews.Posted, body: str) -> str:
    """The roadmap folder a review comment is of: its `Roadmap folder:` line, else the roadmap its
    marker's job names."""
    if p.folder:
        return p.folder.split(":", 1)[1].strip(" `/")
    job = str(prreviews.prreview_marker(body).get("job") or "")
    return job.split("-", 1)[-1].split("~")[0] if job else ""


def latest_reviews(comments: list[dict]) -> dict[str, dict]:
    """Folder -> the latest AI review comment of that folder among a pull request's comments."""
    out: dict[str, tuple] = {}
    for c in comments:
        body = c.get("body") or ""
        p = prreviews.read_posted(body)
        if p is None:
            continue
        key = folder_key(p, body)
        order = (str(c.get("created_at") or ""), int(c.get("id") or 0))
        if key not in out or order > out[key][0]:
            out[key] = (order, c)
    return {key: c for key, (_order, c) in sorted(out.items())}


def rebuilt(cfg, c: dict, owner: str) -> tuple[str | None, str]:
    """(the comment rebuilt in the posted layout, '') or (None, why it is left as it is)."""
    body, login = c.get("body") or "", str((c.get("user") or {}).get("login") or "")
    new = prreviews.reformat_posted(body, replies.text_version(body))
    if new is None:
        return None, "rebuilding it would change what the pipeline reads from it"
    if new == body:
        return None, "already in the posted layout"
    if timeline_reading(cfg, new, login, owner) != timeline_reading(cfg, body, login, owner):
        return None, "rebuilding it would change how the timeline reads it"
    return new, ""


def run(ctx, *, numbers=(), logins=(), write: bool = False, pace: float = 1.0, limit: int = 0, report=print,
        sleep=time.sleep) -> list[str]:
    """The command: every open roadmap pull request (or those `numbers`), the latest AI review of each
    of its roadmap folders, rebuilt when it is ours (`ctx.operator`, or one of `logins`). With `write`
    the comments are edited `pace` seconds apart, at most `limit` (0: all)."""
    cfg, repo, out = ctx.cfg, ctx.cfg.repositories.content, []

    def say(line: str) -> None:
        out.append(line)
        report(line)
    listed = openroadmaps.inventory(ctx.gh, repo, (cfg.roadmap.active_dir,), any_file=True)
    if numbers:
        have = {int(e["pr"]) for e in listed}
        for n in numbers:
            if int(n) not in have:
                say(f"#{n}: not an open roadmap pull request")
        listed = [e for e in listed if int(e["pr"]) in {int(n) for n in numbers}]
    jobs = [*ctx.board.kind_jobs("prreview", "open"), *ctx.board.kind_jobs("gate", "open")]

    def pr_of(job) -> int:
        try:
            return int(job.inputs.get("pr") or 0)
        except (TypeError, ValueError):
            return 0
    busy = {}
    for j in jobs:
        if j.under_way(UNDER_WAY):
            busy.setdefault(pr_of(j), []).append(f"{j.id} is {j.state}")
    ours = {str(x).lower() for x in (ctx.operator, *logins) if x}
    todo = [e for e in sorted(listed, key=lambda e: int(e["pr"])) if int(e["pr"]) not in busy]
    reader = prbatch.PullReader(ctx.gh, repo, [int(e["pr"]) for e in todo], cfg.board.graphql_batch)
    edited, before, after = 0, 0, 0
    for entry in sorted(listed, key=lambda e: int(e["pr"])):
        number, owner = int(entry["pr"]), str(entry.get("author") or "")
        if number in busy:
            say(f"#{number}: skipped: " + "; ".join(busy[number]) + " (run again once it is over)")
            continue
        try:
            try:
                comments = reader.comments(number)
            except GHError as exc:
                say(f"#{number}: comments not read ({str(exc).splitlines()[0][:160]})")
                continue
            for folder, c in latest_reviews(comments).items():
                where = f"#{number} {folder or '?'}"
                login = str((c.get("user") or {}).get("login") or "")
                if login.lower() not in ours:
                    say(f"{where}: the latest review is not ours ({login or '?'}): left as it is")
                    continue
                new, why = rebuilt(cfg, c, owner)
                if new is None:
                    say(f"{where}: comment {c.get('id')}: {why}")
                    continue
                size = f"{len(c.get('body') or ''):,} -> {len(new):,} characters"
                if not write:
                    say(f"[dry run] {where}: comment {c.get('id')}: {size}")
                    before, after = before + len(c.get("body") or ""), after + len(new)
                    continue
                if limit and edited >= limit:
                    say(f"{where}: not edited this run (--limit {limit})")
                    continue
                if edited:
                    sleep(pace)
                try:
                    ctx.gh.write("PATCH", f"repos/{repo}/issues/comments/{c['id']}", {"body": new})
                except GHError as exc:
                    say(f"{where}: comment {c.get('id')} not edited: {str(exc).splitlines()[0][:160]}")
                    continue
                edited += 1
                before, after = before + len(c.get("body") or ""), after + len(new)
                say(f"{where}: comment {c.get('id')} reformatted: {size}")
        except (GHRateLimited, GHHalt) as exc:
            say(f"stopped at #{number}: {str(exc).splitlines()[0][:200]}; {edited} edited, run again later")
            break
    if write:
        say(f"{edited} comment{'s' if edited != 1 else ''} reformatted ({before:,} -> {after:,} characters)")
    elif before:
        say(f"[dry run] in all: {before:,} -> {after:,} characters")
    return out
