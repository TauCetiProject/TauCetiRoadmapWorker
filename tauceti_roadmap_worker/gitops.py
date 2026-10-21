"""Git plumbing for the lanes: checkouts under the state directory, forks, pushes, pull requests.

Every git command that talks to GitHub uses `gh` as its credential helper, so a worker needs only
`gh auth login` (no global git configuration). Pushes go to the operator's fork, never to the
canonical repositories; a push uses `--force-with-lease` so two workers cannot clobber a branch.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .gh import GH, GHError

CRED = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


class GitError(Exception):
    pass


class PushError(GitError):
    """A push that git refused or could not make (the work it would have pushed is intact)."""


class BranchMoved(GitError):
    """The branch a push was to move is no longer at the commit the work was built on."""

    def __init__(self, branch: str, expected: str, found: str):
        self.branch, self.expected, self.found = branch, expected, found
        super().__init__(f"the pull request moved from {expected[:7] or '(no branch)'} to {found[:7] or '(no branch)'} "
                         "while the fix ran; nothing was pushed (a human, or the next round, rebases the work)")


def git(cwd: Path | None, *args: str, check: bool = True, timeout: int = 900) -> str:
    cmd = ["git", *CRED] + (["-C", str(cwd)] if cwd else []) + list(args)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and out.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {out.stderr.strip()[:300]}")
    return out.stdout


def url(repo: str) -> str:
    return f"https://github.com/{repo}"


def sync_checkout(repo: str, dest: Path, ref: str = "main", scrub: tuple[str, ...] = ()) -> str:
    """A clean detached checkout of `repo` at `origin/<ref>` (cloned on first use, blobs on demand).
    Returns the commit. Only for directories the worker owns.

    Whatever an earlier round left in the tree is discarded and the remote's copy wins: edits to
    tracked files, and untracked files, including one the new commit now tracks (a plain checkout
    refuses to overwrite it, and then every later round fails the same way), nested repositories an
    agent cloned into the tree included. Under the `scrub` paths ignored files go too. A caller that
    wants to keep unsubmitted work copies it first."""
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(None, "clone", "-q", "--filter=blob:none", "--no-checkout", url(repo), str(dest))
    git(dest, "fetch", "-q", "origin", f"+refs/heads/{ref}:refs/remotes/origin/{ref}")
    git(dest, "checkout", "-q", "--force", "--detach", f"origin/{ref}")
    git(dest, "reset", "-q", "--hard", f"origin/{ref}")
    git(dest, "clean", "-qffd")
    if scrub:
        git(dest, "clean", "-qffdx", "--", *scrub)
    return git(dest, "rev-parse", "HEAD").strip()


def checkout_pr(repo: str, dest: Path, number: int) -> str:
    """A read-only checkout of pull request #number at its head (refs/pull/N/head)."""
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(None, "clone", "-q", "--filter=blob:none", "--no-checkout", url(repo), str(dest))
    git(dest, "fetch", "-q", "origin", f"pull/{number}/head", "main")
    git(dest, "checkout", "-q", "--force", "--detach", "FETCH_HEAD")
    git(dest, "reset", "-q", "--hard", "FETCH_HEAD")
    git(dest, "clean", "-qffd")
    return git(dest, "rev-parse", "HEAD").strip()


def ensure_fork(gh: GH, repo: str, login: str) -> str:
    """The operator's fork of `repo` (created on first use)."""
    name = repo.split("/", 1)[1]
    try:
        data = gh.api(f"repos/{login}/{name}")
        if data and data.get("fork"):
            return f"{login}/{name}"
    except GHError:
        pass
    gh.write("POST", f"repos/{repo}/forks", {})
    return f"{login}/{name}"


def remote_head(worktree: Path, fork: str, branch: str) -> str:
    """The commit the fork's branch points at now ('' when it does not exist), read from the
    remote itself: no remote-tracking ref of the checkout is trusted."""
    remote = git(worktree, "remote").split()
    git(worktree, "remote", "add" if "fork" not in remote else "set-url", "fork", url(fork))
    out = git(worktree, "ls-remote", "fork", f"refs/heads/{branch}", check=False)
    return out.split()[0] if out.strip() else ""


def push(worktree: Path, fork: str, branch: str, dry_run: bool = False, expected: str | None = None) -> None:
    """Push HEAD to the fork's branch with an explicit lease. With `expected` (the head the work was
    built on), the branch must still be there (else BranchMoved, and nothing is pushed); without,
    whatever the branch holds now may be replaced, but nothing that changes in between. A refused or
    failed push raises PushError."""
    if dry_run:
        print(f"[dry-run] push {branch} to {fork}")
        return
    found = remote_head(worktree, fork, branch)
    if expected is not None and found != expected:
        raise BranchMoved(branch, expected, found)
    lease = expected if expected is not None else found
    try:
        git(worktree, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{lease}", "fork",
            f"HEAD:refs/heads/{branch}")
    except GitError as exc:
        raise PushError(str(exc)) from exc


def push_branch(worktree: Path, fork: str, branch: str, expected: str | None = None) -> None:
    """Push HEAD onto an existing branch of the fork (a fix on a pull request's branch): with
    `expected`, only while the branch is still at the head the fix was built on; without, as a
    fast-forward. Never a force push over someone else's work."""
    if expected is not None:
        return push(worktree, fork, branch, expected=expected)
    remote = git(worktree, "remote").split()
    git(worktree, "remote", "add" if "fork" not in remote else "set-url", "fork", url(fork))
    try:
        git(worktree, "push", "-q", "fork", f"HEAD:refs/heads/{branch}")
    except GitError as exc:
        raise PushError(str(exc)) from exc


def set_identity(checkout: Path, login: str) -> None:
    """The worker's author and committer in the checkout's own configuration (worktrees share it),
    so that an agent's `git commit` works too; global configuration is left alone."""
    login = login or "tauceti-roadmap-worker"
    git(checkout, "config", "user.name", login)
    git(checkout, "config", "user.email", f"{login}@users.noreply.github.com")


def current_branch(worktree: Path) -> str:
    """The branch checked out in `worktree`, or '' (detached, or not a checkout yet)."""
    if not (worktree / ".git").exists():
        return ""
    name = git(worktree, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    return "" if name == "HEAD" else name


def changed_files(worktree: Path, base: str = "HEAD") -> list[str]:
    """Paths changed in the working tree relative to `base`, including untracked files."""
    tracked = git(worktree, "diff", "--name-only", base).splitlines()
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({p for p in tracked + untracked if p.strip()})


def identity(login: str) -> list[str]:
    """Process-local author and committer: the operator's GitHub noreply address (global git
    configuration is left alone, as TauCetiWorker does)."""
    return ["-c", f"user.name={login}", "-c", f"user.email={login}@users.noreply.github.com"]


def commit_paths(worktree: Path, paths: list[str], message: str, login: str) -> str:
    git(worktree, "add", "--", *paths)
    git(worktree, *identity(login), "commit", "-q", "-m", message)
    return git(worktree, "rev-parse", "HEAD").strip()


def kebab(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", s)
    return s.lower()


def open_pull_request(gh: GH, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> dict | None:
    """Open the pull request, or, when one is already open for this head (an earlier attempt opened
    it before its answer was lost), return that one."""
    try:
        return gh.write("POST", f"repos/{repo}/pulls", {"title": title, "head": head, "base": base, "body": body,
                                                        "draft": draft})
    except GHError as exc:
        if "already exists" not in str(exc):
            raise
        found = gh.api(f"repos/{repo}/pulls?head={head}&state=open&per_page=10") or []
        if not found:
            raise
        return found[0]
