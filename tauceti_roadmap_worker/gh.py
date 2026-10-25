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

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass


class GHError(Exception):
    pass


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

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        self.calls += 1
        try:
            result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GHError(f"gh {' '.join(args[:3])}: {exc}") from exc
        if result.returncode != 0:
            raise GHError(f"gh {' '.join(args[:3])}: {result.stderr.strip()[:400]}")
        return result.stdout

    def api(self, path: str, *, paginate: bool = False, jq: str | None = None):
        """GET a REST path and return parsed JSON (a list for paginated listings)."""
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
        out = self._run(["api", "-X", method, path, "--input", "-"], stdin=json.dumps(payload or {}))
        return json.loads(out) if out.strip() else None

    def cli_write(self, *args: str) -> str:
        """A `gh` subcommand that writes (for the few operations REST lacks, such as marking a pull
        request ready or back to draft); printed instead in dry-run mode."""
        check_fence(self)
        if self.dry_run:
            print("[dry-run] gh " + " ".join(args))
            return ""
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

    def can_push(self, repo: str) -> bool:
        try:
            return self._run(["api", f"repos/{repo}", "--jq", ".permissions.push"]).strip() == "true"
        except GHError:
            return False

    def raw_file(self, repo: str, path: str, ref: str = "") -> str:
        """The raw contents of a file at a ref (empty ref = default branch)."""
        suffix = f"?ref={ref}" if ref else ""
        return self._read(["api", f"repos/{repo}/contents/{path}{suffix}", "-H", "Accept: application/vnd.github.raw"])
