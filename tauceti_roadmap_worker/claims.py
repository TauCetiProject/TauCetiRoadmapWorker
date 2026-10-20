"""Cooperative leases in a claims namespace, and author/reviewer separation.

A lease is the TauCetiTools format (TauCetiWorker `scripts/claim.sh`): a custom ref
`refs/tauceti-claims/<key>` in a repository every worker can push to, pointing at an orphan commit
whose message is a JSON lease. Acquire, renew, takeover and release are all one atomic primitive,
`git push --force-with-lease=<ref>:<expected>`: an empty expectation is create-only, an oid is a
compare-and-swap, so races have exactly one winner and an expired lease never blocks anyone.

Keys used by this project (all under `roadmap/<project>/`):

    job/<JOB-ID>       the worker currently doing a job (short TTL, renewed while it runs)
    author/<JOB-ID>    the operator who submitted a design, package or fix (long TTL)

Git ref names may not contain `~`, so `REV-PKG-Name~2` is stored as `REV-PKG-Name_r2`.

Leases only avoid duplicate work; they are fail-open. Author/reviewer separation is enforced
twice: here, before a worker spends anything on a review, and again by the intake, which compares
the submission's GitHub author with the logins the board recorded for the reviewed jobs.

Namespace choice (as TauCetiWorker): `$CLAIM_REPO` if set; else `repositories.claims` when this
account can push there; else the operator's own fork of the packages repository.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

NS = "refs/tauceti-claims"
SCHEMA = "tauceti-claim/v1"
JOB_TTL = 1500
AUTHOR_TTL = 10 * 365 * 24 * 3600
GIT_ID = {"GIT_AUTHOR_NAME": "tauceti-claim", "GIT_AUTHOR_EMAIL": "claim@tauceti.invalid",
          "GIT_COMMITTER_NAME": "tauceti-claim", "GIT_COMMITTER_EMAIL": "claim@tauceti.invalid"}

ACQUIRED, HELD, ERROR = 0, 1, 2


def ref_safe(job_id: str) -> str:
    return job_id.replace("~", "_r")


def job_key(project: str, job_id: str) -> str:
    return f"roadmap/{project}/job/{ref_safe(job_id)}"


def author_key(project: str, job_id: str) -> str:
    return f"roadmap/{project}/author/{ref_safe(job_id)}"


def lease_payload(key: str, owner: str, operator: str, project: str, ttl: int, now: int,
                  host: str = "", pid: int = 0, extra: dict | None = None) -> dict:
    return {"schema": SCHEMA, "owner": owner, "operator": operator, "project": project, "host": host or socket.gethostname(),
            "pid": pid or os.getpid(), "acquired_at": now, "expires_at": now + ttl, "resource": key, **(extra or {})}


def lease_live(lease: dict | None, now: int) -> bool:
    try:
        return bool(lease) and int(lease.get("expires_at", 0)) > now
    except (TypeError, ValueError):
        return False


def held_by_other(lease: dict | None, owner: str, now: int) -> bool:
    return lease_live(lease, now) and lease.get("owner") != owner


SEPARATION = ("operator", "agent")


def authorship(record) -> tuple[str, str, str]:
    """(operator, agent, model) from an author record (a login, or a dict with those keys)."""
    if isinstance(record, dict):
        return (str(record.get("operator") or record.get("login") or ""), str(record.get("agent") or ""),
                str(record.get("model") or ""))
    return (str(record or ""), "", "")


def independent(mode: str, reviewer: tuple[str, str, str], author: tuple[str, str, str]) -> bool:
    """Whether a reviewer (operator, agent, model) is independent of an author. Another operator
    always is. In `agent` mode (a single-operator project) the same operator is too, when the
    reviewing agent and model differ from the authoring ones recorded for the job; an author record
    that lacks its agent or its model never counts as different."""
    if not author[0] or reviewer[0].lower() != author[0].lower():
        return True
    return mode == "agent" and bool(author[1] and author[2]) and (reviewer[1], reviewer[2]) != (author[1], author[2])


def separation_problem(review_independent_of: list[str], operator: str, recorded: dict, mode: str = "operator",
                       agent: str = "", model: str = "") -> str:
    """'' when `operator` (running `agent` and `model`) may take a job independent of the listed
    jobs, else the reason. `recorded` maps job id -> its author record (a login, or a dict with
    operator, agent and model)."""
    clash = [jid for jid in review_independent_of
             if jid in recorded and not independent(mode, (operator, agent, model), authorship(recorded[jid]))]
    if not clash:
        return ""
    if mode == "agent":
        return (f"operator {operator} authored {', '.join(clash)} with the same agent and model as this reviewer "
                f"({agent}, {model}), or none recorded")
    return f"operator {operator} authored {', '.join(clash)}"


@dataclass
class Claims:
    repo: str
    gitdir: Path
    owner: str
    operator: str
    project: str
    dry_run: bool = False

    # -- plumbing (git, with gh as the credential helper so no global setup is needed) --
    def _git(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, **GIT_ID, "GIT_TERMINAL_PROMPT": "0"}
        cmd = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
               "-C", str(self.gitdir), *args]
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=120)

    def ensure_repo(self) -> None:
        if not self.gitdir.is_dir():
            self.gitdir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", str(self.gitdir)], check=True, capture_output=True)
        url = f"https://github.com/{self.repo}"
        if self._git("remote", "get-url", "origin").returncode != 0:
            self._git("remote", "add", "origin", url)
        self._git("remote", "set-url", "origin", url)

    def _ref(self, key: str) -> str:
        return f"{NS}/{key}"

    def remote_oid(self, ref: str) -> str:
        out = self._git("ls-remote", "origin", ref)
        return out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else ""

    def lease_at(self, oid: str, ref: str) -> dict | None:
        if self._git("cat-file", "-e", oid).returncode != 0:
            self._git("fetch", "-q", "--no-tags", "origin", ref)
        out = self._git("cat-file", "commit", oid)
        if out.returncode != 0:
            return None
        message = out.stdout.split("\n\n", 1)[1] if "\n\n" in out.stdout else ""
        try:
            return json.loads(message)
        except ValueError:
            return None

    def _build(self, payload: dict) -> str:
        tree = self._git("hash-object", "-t", "tree", "-w", "--stdin", stdin="").stdout.strip()
        return self._git("commit-tree", tree, stdin=json.dumps(payload, sort_keys=True)).stdout.strip()

    def _push_cas(self, ref: str, expected: str, new: str) -> int:
        if self.dry_run:
            print(f"[dry-run] lease push {ref} (expected {expected or 'absent'})")
            return ACQUIRED
        out = self._git("push", f"--force-with-lease={ref}:{expected}", "origin", f"{new}:{ref}")
        if out.returncode == 0:
            return ACQUIRED
        text = (out.stdout + out.stderr).lower()
        return HELD if any(s in text for s in ("rejected", "stale info", "failed to push")) else ERROR

    # -- the lease protocol --
    def acquire(self, key: str, ttl: int = JOB_TTL, extra: dict | None = None) -> int:
        """0 acquired (or renewed our own) · 1 held by another · 2 error (proceed unclaimed, fail-open)."""
        try:
            self.ensure_repo()
            ref, now = self._ref(key), int(time.time())
            cur = self.remote_oid(ref)
            if cur and held_by_other(self.lease_at(cur, ref), self.owner, now):
                return HELD
            new = self._build(lease_payload(key, self.owner, self.operator, self.project, ttl, now, extra=extra))
            return self._push_cas(ref, cur, new)
        except (OSError, subprocess.SubprocessError):
            return ERROR

    def renew(self, key: str, ttl: int = JOB_TTL) -> int:
        try:
            self.ensure_repo()
            ref, now = self._ref(key), int(time.time())
            cur = self.remote_oid(ref)
            lease = self.lease_at(cur, ref) if cur else None
            if not lease or lease.get("owner") != self.owner:
                return HELD
            return self._push_cas(ref, cur, self._build(lease_payload(key, self.owner, self.operator, self.project, ttl, now)))
        except (OSError, subprocess.SubprocessError):
            return ERROR

    def release(self, key: str) -> None:
        try:
            self.ensure_repo()
            ref = self._ref(key)
            cur = self.remote_oid(ref)
            lease = self.lease_at(cur, ref) if cur else None
            if lease and lease.get("owner") == self.owner and not self.dry_run:
                self._git("push", f"--force-with-lease={ref}:{cur}", "origin", f":{ref}")
        except (OSError, subprocess.SubprocessError):
            pass

    def read(self, key: str) -> dict | None:
        try:
            self.ensure_repo()
            ref = self._ref(key)
            cur = self.remote_oid(ref)
            return self.lease_at(cur, ref) if cur else None
        except (OSError, subprocess.SubprocessError):
            return None

    def live_keys(self, prefix: str) -> list[str]:
        """Keys under a prefix that have a ref (expiry not checked: read each lease for that)."""
        self.ensure_repo()
        out = self._git("ls-remote", "origin", f"{NS}/{prefix}*")
        return [line.split()[1][len(NS) + 1:] for line in out.stdout.splitlines() if len(line.split()) == 2]

    # -- authorship --
    def record_author(self, job_id: str, agent: str = "", model: str = "") -> int:
        return self.acquire(author_key(self.project, job_id), AUTHOR_TTL,
                            extra={"job": job_id, "agent": agent, "model": model})

    def recorded_authors(self, job_ids: list[str]) -> dict[str, dict]:
        """job id -> {operator, agent, model} from the author records."""
        out = {}
        for jid in job_ids:
            lease = self.read(author_key(self.project, jid))
            if lease and lease.get("operator"):
                out[jid] = {"operator": lease["operator"], "agent": lease.get("agent", ""), "model": lease.get("model", "")}
        return out


def namespace_for(cfg, gh, operator: str) -> str:
    """The claims repository by TauCetiWorker's precedence (see the module docstring)."""
    if os.environ.get("CLAIM_REPO"):
        return os.environ["CLAIM_REPO"]
    shared = cfg.repositories.claims
    if shared and gh.can_push(shared):
        return shared
    return f"{operator}/{cfg.repositories.packages_repo().split('/', 1)[1]}"


def open_claims(cfg, gh, operator: str, worker_id: str, dry_run: bool = False) -> Claims:
    repo = namespace_for(cfg, gh, operator)
    gitdir = cfg.state_dir() / "claims" / (repo.replace("/", "__") + ".git")
    return Claims(repo=repo, gitdir=gitdir, owner=f"{operator}/{worker_id}", operator=operator,
                  project=cfg.project.name, dry_run=dry_run)
