"""Sources: the per-roadmap manifest, licence classes, the cache layout and the `needs-references`
hand-off. Build-plan step 4 (the sources service) is not implemented yet: fetching and OCR raise
NotImplementedError; agents fetch what they need themselves within the rules in their brief and
record it in the manifest, and the lanes validate the manifest with the functions here.

The manifest is `packages/<Name>/sources.toml` (see docs/SOURCES.md):

    schema = "tauceti-roadmap-sources/v1"
    roadmap = "<Name>"

    [[source]]
    id = "AB21"                       # the key the README's References section uses
    authors = "A. Author, B. Author"
    title = "An example paper"
    year = 2021
    venue = "J. Example Math. 12"     # optional
    locator_scheme = "theorem"        # theorem | page | section | tag | chapter | equation
    url = "https://arxiv.org/abs/2101.00001"
    fetched_from = "https://arxiv.org/pdf/2101.00001v2"   # the exact bytes hashed ('' if not fetched)
    sha256 = "<64 hex>"               # of those bytes
    licence_class = "open-repository" # see LICENCE_CLASSES
    status = "certified"              # certified | uncertified | waived
    accessed = "2026-10-20"
    version_note = "arXiv v2; numbering agrees with the published version"
    cited_for = ["1.2", "Layer 3"]

Cache: `<state>/sources/<sha256[:2]>/<sha256>.<ext>` with the text extraction beside it as
`<sha256>.txt`, so that reviewers can quote locators by line.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import LICENCE_CLASSES
from .paths import ISSUE_TEMPLATES

SCHEMA = "tauceti-roadmap-sources/v1"
LOCATOR_SCHEMES = ("theorem", "page", "section", "tag", "chapter", "equation")
STATUSES = ("certified", "uncertified", "waived")
REQUIRED = ("id", "authors", "title", "year", "locator_scheme", "licence_class", "status")


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def host_matches(host: str, rule: str) -> bool:
    rule = rule.lower()
    return host == rule[1:] or host.endswith(rule) if rule.startswith(".") else host == rule


def classify_url(url: str, allow: dict[str, list[str]], deny: list[str]) -> str:
    """'denied', a licence class whose host list matches, or 'unknown'. Deny wins over allow."""
    host = host_of(url)
    if not host:
        return "unknown"
    if any(host_matches(host, r) for r in deny):
        return "denied"
    for cls, rules in allow.items():
        if any(host_matches(host, r) for r in rules):
            return cls
    return "unknown"


def load_manifest(path: Path) -> dict:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def validate_manifest(data: dict, allow: dict[str, list[str]], deny: list[str], uncertified_ok: bool = True) -> list[str]:
    """Problems with a manifest (empty = valid). The checks are mechanical; whether a locator really
    supports a statement is the reviewer's job."""
    problems = []
    if data.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}")
    entries = data.get("source", [])
    if not isinstance(entries, list) or not entries:
        problems.append("no [[source]] entries")
        return problems
    seen = set()
    for k, s in enumerate(entries, 1):
        where = f"source {k} ({s.get('id', '?')})"
        for key in REQUIRED:
            if key not in s or s[key] in ("", None):
                problems.append(f"{where}: missing {key}")
        if s.get("id") in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(s.get("id"))
        if s.get("locator_scheme") and s["locator_scheme"] not in LOCATOR_SCHEMES:
            problems.append(f"{where}: locator_scheme must be one of {', '.join(LOCATOR_SCHEMES)}")
        status, cls = s.get("status"), s.get("licence_class")
        if status and status not in STATUSES:
            problems.append(f"{where}: status must be one of {', '.join(STATUSES)}")
        if cls and cls not in LICENCE_CLASSES:
            problems.append(f"{where}: licence_class must be one of {', '.join(LICENCE_CLASSES)}")
        for key in ("url", "fetched_from"):
            if s.get(key) and classify_url(s[key], allow, deny) == "denied":
                problems.append(f"{where}: {key} is on the deny list ({host_of(s[key])})")
        if status == "certified":
            if cls != "operator-local" and not s.get("fetched_from"):
                problems.append(f"{where}: a certified source records the URL its text was fetched from")
            if not re.fullmatch(r"[0-9a-f]{64}", str(s.get("sha256", ""))):
                problems.append(f"{where}: a certified source records the SHA-256 of the text it was checked against")
            if s.get("fetched_from") and cls != "operator-local":
                found = classify_url(s["fetched_from"], allow, deny)
                if found not in ("unknown", cls) and found != "denied":
                    problems.append(f"{where}: fetched from a {found} host but recorded as {cls}")
        if status == "uncertified" and not uncertified_ok:
            problems.append(f"{where}: uncertified citations are not accepted by this project (needs-references)")
    return problems


# --- cache ----------------------------------------------------------------------------------------

def cache_paths(root: Path, sha256: str, ext: str = "pdf") -> tuple[Path, Path]:
    """(file, text extraction) for a fetched source."""
    folder = Path(root) / sha256[:2]
    return folder / f"{sha256}.{ext.lstrip('.')}", folder / f"{sha256}.txt"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, root: Path):  # pragma: no cover - build-plan step 4
    """Fetch a source into the cache, hash it, and extract its text. Not implemented yet: agents
    fetch sources themselves (allowed classes only, never pirate mirrors, never past a paywall)."""
    raise NotImplementedError("the sources service (build-plan step 4) is not implemented yet")


def ocr(path: Path):  # pragma: no cover - build-plan step 4
    """Text of an image-only scan (uv venv + RapidOCR). Not implemented yet."""
    raise NotImplementedError("OCR (build-plan step 4) is not implemented yet")


# --- the needs-references hand-off ----------------------------------------------------------------

@dataclass
class MissingReference:
    authors: str
    title: str
    year: str
    needed_for: str
    tried: list[str] = field(default_factory=list)
    note: str = ""


def parse_missing(data: dict) -> list[MissingReference]:
    """The `needs-references.toml` an agent writes in its scratch directory: [[missing]] tables."""
    out = []
    for item in data.get("missing", []):
        if not isinstance(item, dict):
            continue
        out.append(MissingReference(authors=str(item.get("authors", "")), title=str(item.get("title", "")),
                                    year=str(item.get("year", "")), needed_for=str(item.get("needed_for", "")),
                                    tried=[str(t) for t in item.get("tried", [])], note=str(item.get("note", ""))))
    return [m for m in out if m.title and m.needed_for]


def _template_body() -> str:
    text = (ISSUE_TEMPLATES / "needs-references.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip() + "\n"


def needs_references_comment(job_id: str, roadmap: str, items: list[MissingReference]) -> str:
    """The comment the lane posts when it parks a job in `needs-references` (from the template)."""
    def cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ").strip()[:300]
    rows = "\n".join(f"| {cell(m.authors)} | {cell(m.title)} | {cell(m.year)} | {cell(m.needed_for)} | "
                     f"{cell(', '.join(m.tried)) or 'none'} |" for m in items)
    return (_template_body().replace("__JOB__", job_id).replace("__ROADMAP__", roadmap)
            .replace("__ROWS__", rows or "| | | | | |"))
