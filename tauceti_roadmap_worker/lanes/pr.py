"""Helpers for the lanes that work on a roadmap pull request in the content repository (port,
red-team fix, gate): worktrees with a shared Lake package cache, the head check, timelines."""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import gitops, replies
from .base import Context, Workspace, free_gb
from .lake import (CACHE_GET, CACHE_GET_TIMEOUT, PIN_FILES, LakeSetup, ensure_lake_cache, lake_pin,  # noqa: F401
                   link_lake_packages, prepare_lake, shared_packages)


def fresh_worktree(ctx: Context, content: Path, name: str, kind: str, ws: Workspace | None = None) -> Path:
    """A detached worktree of the content checkout at its current HEAD (origin/<branch>), with its
    Lake packages linked to the machine's shared copy for its pin (fetched once). With a workspace,
    what the wiring delivered is kept in `ws.extra["lake"]` for the lane's checks and briefs."""
    wt = ctx.wdir / kind / name
    remove_worktree(content, wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    gitops.git(content, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    fetch = bool(ctx.cfg.gates.build or ctx.cfg.gates.elaborate) and not ctx.dry_run
    setup = prepare_lake(wt, ctx.state, content=content, fetch=fetch, free=free_gb)
    if ws is not None:
        ws.extra["lake"] = setup
    return wt


def remove_worktree(content: Path, wt: Path) -> None:
    """Remove a worktree of the content checkout (its `.lake/packages` link goes, the shared packages stay)."""
    link = wt / ".lake" / "packages"
    if link.is_symlink():
        link.unlink()             # never let a removal follow the link into the shared packages
    if wt.exists():
        gitops.git(content, "worktree", "remove", "--force", str(wt), check=False)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    gitops.git(content, "worktree", "prune", check=False)


ROADMAP_WORKTREES = ("pr", "port")     # never build/: it holds the worker's snapshot of the briefs


def is_worktree_of(wt: Path, content: Path) -> bool:
    """Whether `wt` is a git worktree of the content checkout (its `.git` file points into
    `<content>/.git/worktrees/`)."""
    marker = wt / ".git"
    if not marker.is_file():
        return False
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    gitdir = Path(line.removeprefix("gitdir:").strip())
    return line.startswith("gitdir:") and gitdir.resolve().is_relative_to((content / ".git" / "worktrees").resolve())


def sweep_worktrees(wdir: Path, content: Path, open_names: set[str], keep: str = "") -> list[str]:
    """Remove the worker's pr/<Name> and port/<Name> worktrees whose roadmap has no open job left (and
    never `keep`): only git worktrees of the content checkout, never anything else (the build/
    snapshot of the briefs above all). Returns the ones removed."""
    out = []
    for kind in ROADMAP_WORKTREES:
        for wt in sorted((wdir / kind).iterdir()) if (wdir / kind).is_dir() else []:
            if wt.name not in open_names and wt.name != keep and is_worktree_of(wt, content):
                remove_worktree(content, wt)
                out.append(f"{kind}/{wt.name}")
    return out


def worktrees_holding(content: Path, branch: str) -> list[Path]:
    """The worktrees of the content checkout that have `branch` checked out."""
    out, current = [], None
    for line in gitops.git(content, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and current is not None:
            out.append(current)
    return out


def release_branch(content: Path, branch: str, keep: Path | None = None) -> list[Path]:
    """Free a roadmap branch for a new worktree: an earlier lane's worktree of this worker that still
    has it checked out (a finished pr/ or port/ lane) is removed, and the content checkout itself is
    detached if it holds it. Returns the worktrees removed."""
    removed = []
    for wt in worktrees_holding(content, branch):
        if keep is not None and wt.resolve() == keep.resolve():
            continue
        if wt.resolve() == content.resolve():
            gitops.git(content, "checkout", "-q", "--detach", check=False)
            continue
        remove_worktree(content, wt)
        removed.append(wt)
    return removed


def pr_branch_worktree(ctx: Context, content: Path, name: str, pr: int, branch: str,
                       ws: Workspace | None = None) -> tuple[Path, str]:
    """A worktree on the pull request's branch at its current head. Returns (worktree, head sha)."""
    wt = fresh_worktree(ctx, content, name, "pr", ws)
    gitops.git(wt, "fetch", "-q", "origin", f"pull/{pr}/head")
    release_branch(content, branch, keep=wt)      # an earlier lane's worktree may still hold the branch
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
    return inline_events(ctx.gh.api(f"repos/{repo}/pulls/{pr}/comments?per_page=100", paginate=True))


def _ref(kind: str, item: dict) -> str:
    return f"{kind}-{item['id']}" if item.get("id") else ""


def inline_events(items: list[dict]) -> list[replies.Event]:
    return [replies.Event(c.get("created_at", ""), (c.get("user") or {}).get("login", ""),
                          f"(on `{c.get('path')}` line {c.get('line') or c.get('original_line')}) "
                          f"{c.get('body') or ''}", review_id=c.get("pull_request_review_id") or 0,
                          ref=_ref("inline", c), version=replies.text_version(c.get("body") or ""))
            for c in items]


def events_from_api(reviews: list[dict], comments: list[dict], inline: list[dict] = ()) -> list[replies.Event]:
    out = [replies.Event(r.get("submitted_at") or "", (r.get("user") or {}).get("login", ""), r.get("body") or "",
                         approved=r.get("state") == "APPROVED", commit=r.get("commit_id") or "",
                         review_id=r.get("id") or 0, ref=_ref("review", r),
                         version=replies.text_version(r.get("body") or ""))
           for r in reviews if r.get("submitted_at")]
    out += [replies.Event(c.get("created_at", ""), (c.get("user") or {}).get("login", ""), c.get("body") or "",
                          ref=_ref("comment", c), version=replies.text_version(c.get("body") or ""))
            for c in comments]
    return sorted(out + inline_events(inline), key=lambda e: e.t)


def flip_labels(ctx: Context, pr: int, events: list[replies.Event], head_sha: str, owner: str) -> str:
    r = ctx.cfg.replies
    want, drop = replies.wanted_label(events, {owner}, head_sha, markers=r.review_markers,
                                      housekeeping=r.housekeeping_prefixes, approvers=r.approvers,
                                      ignore=set(r.ignore_logins), awaiting_author=r.awaiting_author,
                                      awaiting_review=r.awaiting_review, maintainers=set(r.maintainer_logins),
                                      maintainer_prefix=r.maintainer_review_prefix)
    repo = ctx.cfg.repositories.content
    have = {label["name"] for label in ctx.gh.api(f"repos/{repo}/issues/{pr}/labels")}
    if want and want not in have:
        ctx.gh.write("POST", f"repos/{repo}/issues/{pr}/labels", {"labels": [want]})
    for label in drop:
        if label in have:
            ctx.gh.write("DELETE", f"repos/{repo}/issues/{pr}/labels/{label}")
    return want or "approved"
