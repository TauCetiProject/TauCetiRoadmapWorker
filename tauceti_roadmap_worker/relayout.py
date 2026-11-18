"""`tauceti-roadmap layout`: the README section order of the README-shape brief, applied to open
roadmap pull requests as a pure reordering (`layout.reorder`), and to a local README.

For each pull request asked for (an explicit list, every open roadmap pull request of the pool's
authors, or of named authors), the command reads the README of each roadmap folder the pull request
adds or changes at its head, reorders its top-level sections, checks that the result is a permutation
of them (`layout.reorder` refuses otherwise), and, with `--yes`, makes one commit on the pull
request's branch that changes those READMEs and nothing else, then posts the housekeeping note the
intake also writes (`intake.layout_note`). The intake recognises such a push from the two heads' files
(`intake.LayoutCheck`) and carries the AI acceptance of the old head to the new one: the label stays
and no review is filed.

Refused, never pushed to: a pull request a human has approved, or one in the merge queue (a push would
dismiss the approval, or take it out of the queue), a closed one, and one whose state cannot be read.
Skipped and reported for a later pass: a pull request with a review or gate round claimed or
submitted (the round would read, or push onto, a head that moves under it). A README the pull request
leaves alone (it is as on main) is not touched, nor is one already in order.

The commit is made through GitHub's git data API on the branch's repository (a tree on the head's
tree, a commit whose only parent is the head, then the branch moved to it without force), so that a
branch that moved meanwhile refuses the update instead of losing a commit. Writes go one pull request
at a time, `--pace` seconds apart; a rate-limit answer stops the run, and a dry run (the default)
reads and prints what it would do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import intake, layout, openroadmaps, prbatch
from .gh import GHError, GHHalt, GHRateLimited

UNDER_WAY = ("claimed", "submitted")
STATUS_FIELDS = ("number state headRefOid headRefName headRepository { nameWithOwner } isInMergeQueue reviewDecision "
                 "latestReviews(first: 50) { nodes { state } }")


@dataclass
class Plan:
    number: int
    author: str = ""
    head: str = ""
    branch: str = ""
    head_repo: str = ""
    labelled: bool = False
    # folder -> (its README's path, the text at the head, the text reordered)
    readmes: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)                       # folders left alone, and why
    refused: str = ""                                                    # why nothing is pushed
    skipped: str = ""                                                    # ... for now (a round under way)

    @property
    def folders(self) -> list[str]:
        return list(self.readmes)


def statuses(gh, repo: str, numbers: list[int]) -> dict[int, dict | None]:
    """What decides whether a pull request may be pushed to, read by GraphQL in one query: its state,
    head, branch and the branch's repository, whether it is in the merge queue, its review decision and
    the states of its reviewers' latest reviews. None for a pull request the query did not give."""
    out: dict[int, dict | None] = {n: None for n in numbers}
    if not numbers:
        return out
    owner, name = repo.split("/", 1)
    for chunk in prbatch.chunks(sorted(set(numbers)), 25):
        fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ {STATUS_FIELDS} }}" for n in chunk)
        try:
            data = gh.graphql(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                              f"{{ {fields} }} }}", owner=owner, name=name)
        except GHError:
            continue
        nodes = (data or {}).get("repository") or {}
        for n in chunk:
            if isinstance(nodes.get(f"p{n}"), dict):
                out[n] = nodes[f"p{n}"]
    return out


def refusal(status: dict | None, head: str) -> str:
    """Why a pull request may not be pushed to ('' when it may): its state unread, closed, approved by
    a human (any approval stands until a push dismisses it), in the merge queue, or moved since the
    listing."""
    if status is None:
        return "its state could not be read"
    if str(status.get("state") or "").upper() != "OPEN":
        return f"it is {str(status.get('state') or 'not open').lower()}"
    approved = status.get("reviewDecision") == "APPROVED" or any(
        (r or {}).get("state") == "APPROVED" for r in ((status.get("latestReviews") or {}).get("nodes") or []))
    if approved:
        return "a human approval stands (a push would dismiss it): it merges as it is"
    if status.get("isInMergeQueue"):
        return "it is in the merge queue (a push would take it out): it merges as it is"
    if head and status.get("headRefOid") and status["headRefOid"] != head:
        return f"it moved to {status['headRefOid'][:7]} while it was read"
    if not status.get("headRefName") or not (status.get("headRepository") or {}).get("nameWithOwner"):
        return "its branch cannot be named (a deleted fork?)"
    return ""


def rounds_under_way(jobs, number: int) -> list[str]:
    """The review and gate rounds of a pull request that are claimed or submitted."""
    def pr_of(job) -> int:
        try:
            return int(job.inputs.get("pr") or 0)
        except (TypeError, ValueError):
            return 0
    return [f"{j.id} is {j.state}" for j in jobs if j.kind in ("prreview", "gate") and pr_of(j) == number
            and j.under_way(UNDER_WAY)]


def selected(ctx, listed: list[dict], numbers: list[int], authors: list[str], pool: bool) -> list[dict]:
    """The listing entries asked for: the pull requests named, in their order (one the listing lacks is
    kept as a bare entry, refused later), then those of the authors named and, with `pool`, of every
    author in `[roadmap] pool`, deepest in the dependency graph of the open pull requests first
    (`intake.dependency_depths`: one waiting for more of the others before one waiting for fewer)."""
    by_number = {int(e["pr"]): e for e in listed}
    out = [by_number.get(int(n)) or {"pr": int(n), "missing": True} for n in dict.fromkeys(numbers)]
    who = {a.lower() for a in authors} | ({str(a).lower() for a in ctx.cfg.roadmap.pool} if pool else set())
    if who:
        chosen = [e for e in listed if str(e.get("author") or "").lower() in who and int(e["pr"]) not in numbers]
        if chosen:
            bodies = prbatch.pull_texts(ctx.gh, ctx.cfg.repositories.content, [int(e["pr"]) for e in listed],
                                        ctx.cfg.board.graphql_batch)
            depths = intake.dependency_depths(listed, bodies, ctx.cfg.prreview.dependency_label)
            chosen.sort(key=lambda e: (-depths.get(int(e["pr"]), 0), int(e["pr"])))
        out += chosen
    return out


def plan(ctx, entry: dict, status: dict | None, jobs, on_main: set[str]) -> Plan:
    """What the command would do to one pull request (`entry`: its listing entry), reading its READMEs."""
    cfg = ctx.cfg
    number = int(entry["pr"])
    found = Plan(number, author=str(entry.get("author") or ""), head=str(entry.get("head") or ""),
                 labelled=cfg.prreview.label in (entry.get("labels") or []))
    if entry.get("missing"):
        found.refused = "not an open roadmap pull request"
        return found
    found.refused = ("a human approval stands (a push would dismiss it): it merges as it is" if entry.get("approved")
                     else refusal(status, found.head))
    if found.refused:
        return found
    found.branch, found.head_repo = status["headRefName"], status["headRepository"]["nameWithOwner"]
    busy = rounds_under_way(jobs, number)
    if busy:
        found.skipped = "; ".join(busy) + " (run again once it is over)"
        return found
    folders, why = intake.review_plan(entry, cfg, on_main)
    if not folders:
        found.refused = f"no roadmap folder to reorder ({why or 'none it adds or changes'})"
        return found
    active = cfg.roadmap.active_dir.strip("/")
    for folder, change in folders.items():
        path = f"{active}/{folder}/README.md"
        if not change:
            found.notes.append(f"{folder}: its README is as on main (not changed by this pull request): left alone")
            continue
        try:
            text = ctx.gh.raw_file(found.head_repo, path, ref=found.head)
        except GHError as exc:
            found.notes.append(f"{folder}: README not read ({str(exc).splitlines()[0][:120]})")
            continue
        try:
            new = layout.reorder(text)
        except layout.LayoutError as exc:
            found.notes.append(f"{folder}: not reordered ({exc})")
            continue
        if new == text:
            found.notes.append(f"{folder}: already in order")
            continue
        found.readmes[folder] = (path, text, new)
    return found


def describe(p: Plan) -> str:
    """One line on a plan, for the dry run and the log."""
    who = f"#{p.number}" + (f" ({p.author})" if p.author else "")
    if p.refused:
        return f"{who}: refused: {p.refused}"
    if p.skipped:
        return f"{who}: skipped: {p.skipped}"
    parts = []
    for path, old, _new in p.readmes.values():
        moved = ", ".join(layout.moved(old))
        parts.append(f"{path} (moves {moved}; {len(old.encode('utf-8')):,} bytes before and after)")
    parts += p.notes
    if not p.readmes:
        return f"{who}: nothing to do: " + "; ".join(p.notes)
    carry = ("the AI acceptance carries to the new head" if p.labelled
             else "not accepted at this head: the next review reads the new head")
    return f"{who}: reorders " + "; ".join(parts) + f" [{carry}]"


def commit_message(p: Plan) -> str:
    folders = ", ".join(p.folders)
    return (f"{folders}: move the scope and the supplier contracts to the end of the README\n\n"
            "Layout only: the README's top-level sections are reordered (scope and ownership and the exact "
            "supplier contracts before the references, the guide to the layers after the summary). Every "
            "section reads as before, word for word, and no other file changes.\n")


def apply(ctx, p: Plan) -> str:
    """The commit on the pull request's branch and the note; returns the new head. The branch is moved
    without force, so a branch that moved since the head was read refuses the update (GHError)."""
    gh, repo = ctx.gh, p.head_repo
    base = gh.api(f"repos/{repo}/git/commits/{p.head}") or {}
    tree_sha = (base.get("tree") or {}).get("sha")
    if not tree_sha:
        raise GHError(f"the tree of {p.head[:7]} could not be read")
    tree = gh.write("POST", f"repos/{repo}/git/trees", {
        "base_tree": tree_sha,
        "tree": [{"path": path, "mode": "100644", "type": "blob", "content": new}
                 for path, _old, new in p.readmes.values()]})
    commit = gh.write("POST", f"repos/{repo}/git/commits", {"message": commit_message(p), "tree": (tree or {}).get("sha"),
                                                            "parents": [p.head]})
    sha = str((commit or {}).get("sha") or "")
    if not sha:
        raise GHError("the commit was not made")
    live = ((gh.api(f"repos/{ctx.cfg.repositories.content}/pulls/{p.number}") or {}).get("head") or {}).get("sha", "")
    if live != p.head:
        raise GHError(f"the pull request moved to {live[:7] or '?'} before the push; nothing pushed")
    gh.write("PATCH", f"repos/{repo}/git/refs/heads/{quote(p.branch, safe='/')}", {"sha": sha, "force": False})
    return sha


def run(ctx, *, numbers=(), authors=(), pool: bool = False, write: bool = False, pace: float = 2.0, limit: int = 0,
        accepted_only: bool = False, report=print, sleep=time.sleep) -> list[str]:
    """The command: plan every pull request asked for, print what it would do, and with `write` push
    the reorderings one pull request at a time, `pace` seconds apart, at most `limit` (0: all). A
    rate-limit answer, or GitHub refusing the account, stops the run."""
    cfg, repo, out = ctx.cfg, ctx.cfg.repositories.content, []

    def say(line: str) -> None:
        out.append(line)
        report(line)
    listed = openroadmaps.inventory(ctx.gh, repo, (cfg.roadmap.active_dir,), any_file=True)
    entries = selected(ctx, listed, list(numbers), list(authors), pool)
    if accepted_only:
        entries = [e for e in entries if cfg.prreview.label in (e.get("labels") or [])]
    if not entries:
        say("no pull request selected")
        return out
    on_main = openroadmaps.readme_folders(ctx.gh, repo, cfg.project.branch, cfg.roadmap.active_dir)
    jobs = [*ctx.board.kind_jobs("prreview", "open"), *ctx.board.kind_jobs("gate", "open")]
    found = statuses(ctx.gh, repo, [int(e["pr"]) for e in entries if not e.get("missing")])
    pushed = 0
    for entry in entries:
        try:
            p = plan(ctx, entry, found.get(int(entry["pr"])), jobs, on_main)
            if not write or p.refused or p.skipped or not p.readmes:
                say(("[dry run] " if p.readmes and not (write or p.refused or p.skipped) else "") + describe(p))
                continue
            if limit and pushed >= limit:
                say(f"#{p.number}: not pushed this run (--limit {limit})")
                continue
            if pushed:
                sleep(pace)
            try:
                sha = apply(ctx, p)
            except GHError as exc:
                say(f"#{p.number}: not pushed: {str(exc).splitlines()[0][:200]}")
                continue
            pushed += 1
            line = f"#{p.number}: pushed {sha[:7]} on {p.head_repo}:{p.branch} (" + ", ".join(p.folders) + ")"
            try:
                ctx.gh.write("POST", f"repos/{repo}/issues/{p.number}/comments",
                             {"body": intake.layout_note(p.head, sha, p.folders)})
                line += "; note posted"
            except GHError as exc:
                line += f"; note not posted ({str(exc).splitlines()[0][:120]})"
            say(line)
        except (GHRateLimited, GHHalt) as exc:
            say(f"stopped at #{entry['pr']}: {str(exc).splitlines()[0][:200]}; {pushed} pushed, run again later")
            break
    if write:
        say(f"{pushed} pull request{'s' if pushed != 1 else ''} pushed")
    return out


def local(path, write: bool = False) -> tuple[str, int]:
    """The command on a README file: (what it says, exit status). With `write` the file is rewritten."""
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    try:
        new = layout.reorder(text)
    except layout.LayoutError as exc:
        return f"{target}: not reordered: {exc}", 1
    if new == text:
        return f"{target}: already in order", 0
    moved = ", ".join(layout.moved(text))
    if write:
        target.write_text(new, encoding="utf-8")
        return f"{target}: reordered ({moved})", 0
    return f"{target}: would reorder ({moved}); --write rewrites it", 0
