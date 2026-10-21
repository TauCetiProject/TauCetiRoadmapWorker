"""Helpers for the lanes that work on a roadmap pull request in the content repository (port,
red-team fix, gate): worktrees with a shared Lake package cache, the head check, timelines."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

from .. import gitops, replies
from .base import Context

PIN_FILES = ("lake-manifest.json", "lean-toolchain", "lakefile.lean", "lakefile.toml")
CACHE_GET = ("lake", "exe", "cache", "get")
CACHE_GET_TIMEOUT = 3600


def lake_pin(worktree: Path) -> str:
    """What decides a checkout's Lake packages (its manifest, toolchain and lakefile), as a short hash."""
    h = hashlib.sha256()
    for name in PIN_FILES:
        path = worktree / name
        h.update(name.encode() + b"\0" + (path.read_bytes() if path.is_file() else b"") + b"\0")
    return h.hexdigest()[:16]


def shared_packages(state: Path, worktree: Path) -> Path:
    """The machine's one Lake package directory for the checkout's pin: `<state>/lake/<pin>/packages`."""
    return Path(state) / "lake" / lake_pin(worktree) / "packages"


def link_lake_packages(content: Path, worktree: Path, state: Path | None = None) -> Path | None:
    """Give a worktree a `.lake/packages` that is a link to one shared copy (Mathlib and Tau Ceti,
    16 GB), never a copy of its own: the content checkout's own packages when it has them (the
    `TAUCETI_ROADMAP_LAKE_PACKAGES` setup), else `<state>/lake/<pin>/packages`, one per pin of the
    manifest, shared by every worker of the machine. Returns the shared directory."""
    own = content / ".lake" / "packages"
    src = own if own.is_dir() else (shared_packages(state, worktree) if state is not None else None)
    if src is None:
        return None
    src.mkdir(parents=True, exist_ok=True)
    dst = worktree / ".lake" / "packages"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)            # a copy left by an earlier build: the shared one replaces it
    os.symlink(src.resolve(), dst)
    return src


def ensure_lake_cache(worktree: Path, shared: Path | None, run=subprocess.run) -> str:
    """Fetch the prebuilt Mathlib (and other) oleans into the shared packages once per pin: `lake exe
    cache get` runs only when the pin's directory is not marked ready, under a lock so two workers do
    not fetch at once. Returns what happened."""
    if shared is None:
        return "no shared Lake packages"
    ready = shared.parent / ".ready"
    if ready.exists():
        return "shared Lake packages ready"
    with open(shared.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready.exists():
            return "shared Lake packages ready"
        try:
            done = run(list(CACHE_GET), cwd=worktree, capture_output=True, text=True, timeout=CACHE_GET_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"lake exe cache get failed ({exc}); the build fetches what it needs"
        if done.returncode != 0:
            return f"lake exe cache get exited {done.returncode}; the build fetches what it needs"
        ready.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        return "shared Lake packages fetched"


def fresh_worktree(ctx: Context, content: Path, name: str, kind: str) -> Path:
    """A detached worktree of the content checkout at its current HEAD (origin/<branch>), with its
    Lake packages linked to the machine's shared copy for its pin (fetched once)."""
    wt = ctx.wdir / kind / name
    remove_worktree(content, wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    gitops.git(content, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    shared = link_lake_packages(content, wt, ctx.state)
    if shared is not None and (ctx.cfg.gates.build or ctx.cfg.gates.elaborate) and not ctx.dry_run:
        ensure_lake_cache(wt, shared)
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


def sweep_worktrees(wdir: Path, content: Path, open_names: set[str], keep: str = "") -> list[str]:
    """Remove the worker's pr/<Name> and port/<Name> worktrees whose roadmap has no open job left
    (and never `keep`). Returns the ones removed."""
    out = []
    for kind in ("pr", "port", "build"):
        for wt in sorted((wdir / kind).iterdir()) if (wdir / kind).is_dir() else []:
            if wt.is_dir() and wt.name not in open_names and wt.name != keep:
                remove_worktree(content, wt)
                out.append(f"{kind}/{wt.name}")
    return out


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
