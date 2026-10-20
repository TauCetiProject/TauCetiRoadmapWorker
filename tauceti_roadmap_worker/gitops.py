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


def git(cwd: Path | None, *args: str, check: bool = True, timeout: int = 900) -> str:
    cmd = ["git", *CRED] + (["-C", str(cwd)] if cwd else []) + list(args)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and out.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {out.stderr.strip()[:300]}")
    return out.stdout


def url(repo: str) -> str:
    return f"https://github.com/{repo}"


def sync_checkout(repo: str, dest: Path, ref: str = "main") -> str:
    """A clean detached checkout of `repo` at `origin/<ref>` (cloned on first use, blobs on demand).
    Returns the commit. Only for directories the worker owns."""
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(None, "clone", "-q", "--filter=blob:none", "--no-checkout", url(repo), str(dest))
    git(dest, "fetch", "-q", "origin", ref)
    git(dest, "checkout", "-q", "--detach", "FETCH_HEAD")
    git(dest, "reset", "-q", "--hard", "FETCH_HEAD")
    git(dest, "clean", "-qfd")
    return git(dest, "rev-parse", "HEAD").strip()


def checkout_pr(repo: str, dest: Path, number: int) -> str:
    """A read-only checkout of pull request #number at its head (refs/pull/N/head)."""
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(None, "clone", "-q", "--filter=blob:none", "--no-checkout", url(repo), str(dest))
    git(dest, "fetch", "-q", "origin", f"pull/{number}/head", "main")
    git(dest, "checkout", "-q", "--detach", "FETCH_HEAD")
    git(dest, "reset", "-q", "--hard", "FETCH_HEAD")
    git(dest, "clean", "-qfd")
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


def push(worktree: Path, fork: str, branch: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[dry-run] push {branch} to {fork}")
        return
    remote = git(worktree, "remote").split()
    if "fork" not in remote:
        git(worktree, "remote", "add", "fork", url(fork))
    else:
        git(worktree, "remote", "set-url", "fork", url(fork))
    git(worktree, "push", "-q", "--force-with-lease", "fork", f"HEAD:refs/heads/{branch}")


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
    return gh.write("POST", f"repos/{repo}/pulls", {"title": title, "head": head, "base": base, "body": body, "draft": draft})
