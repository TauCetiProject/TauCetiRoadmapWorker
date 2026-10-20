"""Helpers for the lanes that work on a roadmap pull request in the content repository (port,
red-team fix, gate): worktrees with a shared Lake package cache, the head check, timelines."""

from __future__ import annotations

import os
from pathlib import Path

from .. import gitops, replies
from .base import Context


def link_lake_packages(content: Path, worktree: Path) -> None:
    """Share the content checkout's `.lake/packages` (Mathlib and Tau Ceti builds) with a worktree,
    so that a targeted build compiles only the roadmap module."""
    src = content / ".lake" / "packages"
    dst = worktree / ".lake" / "packages"
    if src.is_dir() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src, dst)


def fresh_worktree(ctx: Context, content: Path, name: str, kind: str) -> Path:
    """A detached worktree of the content checkout at its current HEAD (origin/<branch>)."""
    wt = ctx.wdir / kind / name
    if wt.exists():
        gitops.git(content, "worktree", "remove", "--force", str(wt), check=False)
    gitops.git(content, "worktree", "prune")
    wt.parent.mkdir(parents=True, exist_ok=True)
    gitops.git(content, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    link_lake_packages(content, wt)
    return wt


def pr_branch_worktree(ctx: Context, content: Path, name: str, pr: int, branch: str) -> tuple[Path, str]:
    """A worktree on the pull request's branch at its current head. Returns (worktree, head sha)."""
    wt = fresh_worktree(ctx, content, name, "pr")
    gitops.git(wt, "fetch", "-q", "origin", f"pull/{pr}/head")
    gitops.git(wt, "checkout", "-q", "-B", branch, "FETCH_HEAD")
    return wt, gitops.git(wt, "rev-parse", "HEAD").strip()


def head(worktree: Path) -> str:
    return gitops.git(worktree, "rev-parse", "HEAD").strip()


def timeline(ctx: Context, pr: int) -> list[replies.Event]:
    """Reviews and issue comments of a pull request as events (what label-sync read)."""
    repo = ctx.cfg.repositories.content
    reviews = ctx.gh.api(f"repos/{repo}/pulls/{pr}/reviews?per_page=100", paginate=True)
    comments = ctx.gh.api(f"repos/{repo}/issues/{pr}/comments?per_page=100", paginate=True)
    return events_from_api(reviews, comments)


def inline_comments(ctx: Context, pr: int) -> list[replies.Event]:
    repo = ctx.cfg.repositories.content
    items = ctx.gh.api(f"repos/{repo}/pulls/{pr}/comments?per_page=100", paginate=True)
    return [replies.Event(c.get("created_at", ""), (c.get("user") or {}).get("login", ""),
                          f"(on `{c.get('path')}` line {c.get('line') or c.get('original_line')}) {c.get('body') or ''}")
            for c in items]


def events_from_api(reviews: list[dict], comments: list[dict]) -> list[replies.Event]:
    out = [replies.Event(r.get("submitted_at") or "", (r.get("user") or {}).get("login", ""), r.get("body") or "",
                         approved=r.get("state") == "APPROVED", commit=r.get("commit_id") or "")
           for r in reviews if r.get("submitted_at")]
    out += [replies.Event(c.get("created_at", ""), (c.get("user") or {}).get("login", ""), c.get("body") or "")
            for c in comments]
    return sorted(out, key=lambda e: e.t)


def flip_labels(ctx: Context, pr: int, events: list[replies.Event], head_sha: str, owner: str) -> str:
    r = ctx.cfg.replies
    want, drop = replies.wanted_label(events, {owner}, head_sha, markers=r.review_markers,
                                      housekeeping=r.housekeeping_prefixes, approvers=r.approvers,
                                      ignore=set(r.ignore_logins), awaiting_author=r.awaiting_author,
                                      awaiting_review=r.awaiting_review)
    repo = ctx.cfg.repositories.content
    have = {label["name"] for label in ctx.gh.api(f"repos/{repo}/issues/{pr}/labels")}
    if want and want not in have:
        ctx.gh.write("POST", f"repos/{repo}/issues/{pr}/labels", {"labels": [want]})
    for label in drop:
        if label in have:
            ctx.gh.write("DELETE", f"repos/{repo}/issues/{pr}/labels/{label}")
    return want or "approved"
