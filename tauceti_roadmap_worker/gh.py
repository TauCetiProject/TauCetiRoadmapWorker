"""A thin wrapper around the `gh` CLI.

Every GitHub write goes through `GH.write`, so `--dry-run` covers all of them. Reads that come
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


@dataclass
class GH:
    dry_run: bool = False
    timeout: int = 120

    def available(self) -> bool:
        return shutil.which("gh") is not None

    def _run(self, args: list[str], stdin: str | None = None) -> str:
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
        out = self._run(args)
        if jq:
            return out
        if looks_like_api_error(out):
            raise GHError(f"GET {path}: API error: {out[:200]}")
        if paginate:
            # --paginate concatenates JSON arrays; parse each top-level array and join them.
            items, decoder, pos, text = [], json.JSONDecoder(), 0, out.strip()
            while pos < len(text):
                value, end = decoder.raw_decode(text, pos)
                items.extend(value if isinstance(value, list) else [value])
                pos = end
                while pos < len(text) and text[pos].isspace():
                    pos += 1
            return items
        return json.loads(out) if out.strip() else None

    def write(self, method: str, path: str, payload: dict | None = None) -> dict | None:
        """POST/PATCH/PUT/DELETE a REST path with a JSON payload; prints instead in dry-run mode."""
        if self.dry_run:
            print(f"[dry-run] {method} {path} {json.dumps(payload or {})[:300]}")
            return None
        out = self._run(["api", "-X", method, path, "--input", "-"], stdin=json.dumps(payload or {}))
        return json.loads(out) if out.strip() else None

    def cli_write(self, *args: str) -> str:
        """A `gh` subcommand that writes (for the few operations REST lacks, such as marking a pull
        request ready or back to draft); printed instead in dry-run mode."""
        if self.dry_run:
            print("[dry-run] gh " + " ".join(args))
            return ""
        return self._run(list(args))

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
        return self._run(["api", f"repos/{repo}/contents/{path}{suffix}", "-H", "Accept: application/vnd.github.raw"])
