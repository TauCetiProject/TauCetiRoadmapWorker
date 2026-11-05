"""A thin wrapper around the `gh` CLI.

Every GitHub write goes through `GH.write`, so `--dry-run` covers all of them, and so does a fence:
while a lease guards a run (`claims.Heartbeat`), `GH.fence` refuses every write once the lease is
lost; a write that is not a REST call (a git push) asks `check_fence` first. Reads that come
back as an API error object (`{"message": ..., "documentation_url": ...}`), or empty where a body
was expected, raise instead of being taken as data: a rate-limited read once overwrote pull
request descriptions with the error text.

Pull request and issue edits use the REST endpoints (`PATCH repos/.../pulls/N`), not
`gh pr edit`, which fails on repositories that still have classic Projects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass


class GHError(Exception):
    pass


class GHHalt(Exception):
    """GitHub refused the account itself (bad credentials, a suspension, abuse detection, a secondary
    rate limit): every later call is refused locally. Not a GHError, so the handlers that tolerate a
    failed write do not absorb it."""


class GHRateLimited(Exception):
    """The primary rate limit is spent: calls are refused locally until `until` (epoch seconds)."""

    def __init__(self, message: str, until: float):
        super().__init__(message)
        self.until = until


# Answers about the account, not the request: a 403 because an issue is locked is the request's.
HALT = re.compile(r"HTTP 401|Bad credentials|account (?:was|has been|is) suspended", re.IGNORECASE)
# The primary hourly limit and the secondary (burst) limit both pass; a host running several workers
# as one account meets both every day, so they pause the worker rather than halt it (2026-10-27).
RATE_LIMITED = re.compile(r"API rate limit exceeded|rate limit exceeded for|secondary rate limit|abuse detection",
                          re.IGNORECASE)
RATE_PAUSE = 900      # seconds: the CLI's error does not carry the reset time


TRANSIENT = re.compile(r"HTTP 5\d\d|\b50[0-4]\b|timed? ?out|couldn't respond to your request in time|"
                       r"Server Error|Bad Gateway|Service Unavailable|connection (?:reset|refused)|EOF", re.IGNORECASE)


def transient(exc: BaseException) -> bool:
    """A GitHub error worth trying again: a 5xx answer, a timeout, a dropped connection."""
    return isinstance(exc, GHError) and bool(TRANSIENT.search(str(exc)))


def looks_like_api_error(text: str) -> bool:
    head = (text or "").lstrip()[:400]
    return head.startswith("{") and ("documentation_url" in head or '"message"' in head) and '"number"' not in head


READ_DELAYS = (2, 5, 15)     # seconds before the second, third and fourth try of a read GitHub answered 5xx or not at all


def check_fence(gh) -> None:
    """Raise as `gh`'s fence does (nothing when it has none)."""
    fence = getattr(gh, "fence", None)
    if fence is not None:
        fence()


@dataclass
class GH:
    dry_run: bool = False
    # The host-wide throttle on content writes (`throttle.WriteThrottle`), set once per process by the
    # CLI from the configuration; None writes unthrottled (tests, dry runs, hosted runs without a state dir).
    throttle = None
    timeout: int = 120
    read_delays: tuple = READ_DELAYS
    sleep: object = time.sleep
    fence: object = None         # called before every write; it raises to refuse the write

    def _read(self, args: list[str]) -> str:
        """A read (GET), tried again after a transient failure (a 5xx answer, a timeout, a dropped
        connection): a blip never costs a whole run. Writes are not retried here (not idempotent)."""
        for delay in (*self.read_delays, None):
            try:
                return self._run(args)
            except GHError as exc:
                if delay is None or not transient(exc):
                    raise
                self.sleep(delay)
        raise AssertionError("unreachable")

    def available(self) -> bool:
        return shutil.which("gh") is not None

    calls: int = 0               # REST requests made through this object (pages of a paginated listing each count)
    halted: str = ""             # why GitHub refused the account; set once, then nothing more is sent
    paused_until: float = 0.0    # the primary rate limit: nothing is sent before then
    clock: object = time.time

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        if self.halted:
            raise GHHalt(self.halted)
        if self.paused_until > self.clock():
            raise GHRateLimited(f"GitHub's rate limit is spent; nothing is sent before {self.paused_until:.0f}",
                                self.paused_until)
        self.calls += 1
        try:
            result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GHError(f"gh {' '.join(args[:3])}: {exc}") from exc
        if result.returncode != 0:
            message = f"gh {' '.join(args[:3])}: {result.stderr.strip()[:400]}"
            if HALT.search(result.stderr):
                self.halted = message
                raise GHHalt(message)
            if RATE_LIMITED.search(result.stderr):
                self.paused_until = self.clock() + RATE_PAUSE
                raise GHRateLimited(message, self.paused_until)
            raise GHError(message)
        return result.stdout

    # Conditional requests (2026-11-05). GitHub answers a GET that carries the ETag of the last answer
    # with 304 Not Modified when nothing changed, and a 304 does not count against the hourly quota.
    # Almost every read the board and the workers make is a re-read of an unchanged page (job lists,
    # comment threads, pull-request listings), so with `cache_dir` set every plain GET (no `jq`) sends
    # the ETag it holds and keeps the body it got; a paginated listing is read page by page with one
    # ETag per page. Entries are files named by the hash of the path, written atomically, so every
    # worker on the machine may share one directory.
    cache_dir: Path | None = None
    cache_hits: int = 0          # answers taken from the cache after a 304 (requests that cost nothing)

    def _cache_file(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def _cached_get(self, path: str) -> str:
        """The body of `GET path`, through the ETag cache: the request carries the stored ETag; a 304
        returns the stored body (and counts as a hit, not a call); a 200 stores body and ETag."""
        entry = None
        file = self._cache_file(path)
        try:
            entry = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entry = None
        args = ["api", "-i", path]
        if entry and entry.get("etag"):
            args += ["-H", f"If-None-Match: {entry['etag']}"]
        try:
            out = self._read(args)
        except GHError as exc:
            # `gh api` exits 1 on a 304 although it printed the status line; that is the hit we asked for.
            if entry is not None and "304" in str(exc) and not RATE_LIMITED.search(str(exc)):
                self.calls -= 1
                self.cache_hits += 1
                return entry["body"]
            raise
        head, sep, body = out.partition("\r\n\r\n")
        if not sep:
            head, sep, body = out.partition("\n\n")
        status = head.split("\n", 1)[0].strip()
        if " 304" in status and entry is not None:
            self.calls -= 1
            self.cache_hits += 1
            return entry["body"]
        etag = ""
        for line in head.splitlines():
            if line.lower().startswith("etag:"):
                etag = line.split(":", 1)[1].strip()
        if etag:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = file.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"etag": etag, "body": body}), encoding="utf-8")
                os.replace(tmp, file)
            except OSError:
                pass
        return body

    def _cached_pages(self, path: str) -> list:
        """A paginated listing read page by page through the cache (`per_page=100`, stop at a short page)."""
        sep = "&" if "?" in path else "?"
        base = path if "per_page=" in path else f"{path}{sep}per_page=100"
        items, page = [], 1
        while True:
            body = self._cached_get(f"{base}&page={page}")
            if looks_like_api_error(body):
                raise GHError(f"GET {path}: API error: {body[:200]}")
            value = json.loads(body) if body.strip() else []
            chunk = value if isinstance(value, list) else [value]
            items.extend(chunk)
            if len(chunk) < 100 or not isinstance(value, list):
                return items
            page += 1

    def api(self, path: str, *, paginate: bool = False, jq: str | None = None):
        """GET a REST path and return parsed JSON (a list for paginated listings)."""
        if self.cache_dir is not None and jq is None and not path.startswith("graphql"):
            if paginate:
                return self._cached_pages(path)
            body = self._cached_get(path)
            if looks_like_api_error(body):
                raise GHError(f"GET {path}: API error: {body[:200]}")
            return json.loads(body) if body.strip() else None
        args = ["api", path]
        if paginate:
            args.insert(1, "--paginate")
        if jq:
            args += ["--jq", jq]
        out = self._read(args)
        if jq:
            return out
        if looks_like_api_error(out):
            raise GHError(f"GET {path}: API error: {out[:200]}")
        if paginate:
            # --paginate concatenates JSON arrays; parse each top-level array and join them.
            items, decoder, pos, text, pages = [], json.JSONDecoder(), 0, out.strip(), 0
            while pos < len(text):
                value, end = decoder.raw_decode(text, pos)
                items.extend(value if isinstance(value, list) else [value])
                pos, pages = end, pages + 1
                while pos < len(text) and text[pos].isspace():
                    pos += 1
            self.calls += max(pages - 1, 0)      # every further page was a request of its own
            return items
        return json.loads(out) if out.strip() else None

    def graphql(self, query: str, **variables) -> dict:
        """A GraphQL read: the `data` object of the answer (retried like every read)."""
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is not None:
                args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        out = json.loads(self._read(args) or "{}")
        if out.get("errors"):
            raise GHError(f"graphql: {str(out['errors'])[:300]}")
        return out.get("data") or {}

    def write(self, method: str, path: str, payload: dict | None = None) -> dict | None:
        """POST/PATCH/PUT/DELETE a REST path with a JSON payload; prints instead in dry-run mode."""
        check_fence(self)
        if self.dry_run:
            print(f"[dry-run] {method} {path} {json.dumps(payload or {})[:300]}")
            return None
        if self.throttle is not None:
            self.throttle.wait()
        out = self._run(["api", "-X", method, path, "--input", "-"], stdin=json.dumps(payload or {}))
        return json.loads(out) if out.strip() else None

    def cli_write(self, *args: str) -> str:
        """A `gh` subcommand that writes (for the few operations REST lacks, such as marking a pull
        request ready or back to draft); printed instead in dry-run mode."""
        check_fence(self)
        if self.dry_run:
            print("[dry-run] gh " + " ".join(args))
            return ""
        if self.throttle is not None:
            self.throttle.wait()
        return self._run(list(args))

    def rate_limit(self, repo: str) -> dict:
        """{remaining, limit, reset} of the token's core REST budget, from the headers of a request
        (for the Actions token: 1,000 per hour per repository), or {} when they cannot be read."""
        try:
            out = self._run(["api", "--include", f"repos/{repo}", "--jq", ".id"])
        except GHError:
            return {}
        found = {}
        for line in out.splitlines():
            name, _, value = line.partition(":")
            key = name.strip().lower()
            if key in ("x-ratelimit-remaining", "x-ratelimit-limit", "x-ratelimit-reset") and value.strip().isdigit():
                found[key.rsplit("-", 1)[1]] = int(value.strip())
        return found

    def login(self) -> str:
        return self._run(["api", "user", "--jq", ".login"]).strip()

    def auth_status(self) -> tuple[bool, str]:
        """(logged in, one line of `gh auth status` worth showing). Never prints a token."""
        try:
            result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        text = (result.stdout + result.stderr).strip()
        first = next((line.strip() for line in text.splitlines() if "Logged in" in line), text[:120])
        return result.returncode == 0, first

    def can_push(self, repo: str) -> bool | None:
        """True/False from the repository's permissions; None when GitHub did not answer the probe
        (a limit, an outage), which is not the same as "cannot push"."""
        try:
            return self._run(["api", f"repos/{repo}", "--jq", ".permissions.push"]).strip() == "true"
        except GHError:
            return None

    def raw_file(self, repo: str, path: str, ref: str = "") -> str:
        """The raw contents of a file at a ref (empty ref = default branch)."""
        suffix = f"?ref={ref}" if ref else ""
        return self._read(["api", f"repos/{repo}/contents/{path}{suffix}", "-H", "Accept: application/vnd.github.raw"])
