"""Sources: the per-roadmap manifest, licence classes, the cache and its line-numbered texts, and OCR.

This module is the offline half of the sources service: everything here works without a network.
`fetcher.py` resolves citations and downloads; `references.py` is the `needs-references` hand-off.

The manifest is `packages/<Name>/sources.toml` (see docs/SOURCES.md):

    schema = "tauceti-roadmap-sources/v1"
    roadmap = "<Name>"

    [[source]]
    id = "AB21"                       # the key the README's References section uses
    authors = "A. Author, B. Author"
    title = "An example paper"
    year = 2021
    venue = "J. Example Math. 12"     # optional
    doi = "10.1234/example.5678"      # optional
    arxiv = "2101.00001v2"            # optional
    locator_scheme = "theorem"        # theorem | page | section | tag | chapter | equation
    locator = "Theorem 2.3"           # optional: the locators the roadmap cites
    url = "https://arxiv.org/abs/2101.00001v2"            # where a reader finds it
    fetched_from = "https://arxiv.org/pdf/2101.00001v2"   # the exact bytes hashed ('' if not fetched)
    fetched_at = "2026-10-20T09:15:00Z"
    sha256 = "<64 hex>"               # of those bytes
    licence_class = "open-repository" # see LICENCE_CLASSES, or "none" when no text was read
    status = "certified"              # certified | uncertified | waived
    accessed = "2026-10-20"
    version_note = "arXiv v2; numbering agrees with the published version"
    resolution = ""                   # set by the board: the comment that attached, named or waived it
    cited_for = ["1.2", "Layer 3"]

Cache: `<state>/sources/<sha256>.<ext>` with its line-numbered text extraction beside it as
`<sha256>.txt`, so that reviewers can quote locators by line. Files are named by content, so two
roadmaps citing the same text share one copy and a recorded hash always finds its bytes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html.parser
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import LICENCE_CLASSES
from .paths import SANDBOX

SCHEMA = "tauceti-roadmap-sources/v1"
LOCATOR_SCHEMES = ("theorem", "page", "section", "tag", "chapter", "equation")
STATUSES = ("certified", "uncertified", "waived")
NO_TEXT = "none"   # licence class of a citation whose text was not read (uncertified or waived)
REQUIRED = ("id", "authors", "title", "year", "locator_scheme", "licence_class", "status")
REQUIRED_WAIVED = ("id", "authors", "title", "year", "status")
FIELD_ORDER = ("id", "authors", "title", "year", "venue", "doi", "arxiv", "locator_scheme", "locator", "url",
               "fetched_from", "fetched_at", "sha256", "licence_class", "status", "accessed", "version_note",
               "resolution", "cited_for")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


# --- licence classes ------------------------------------------------------------------------------

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


def read_host_list(text: str) -> list[str]:
    """Rules of a deny-list file: one host per line, '#' comments, a leading dot covers subdomains."""
    return [r for r in (line.split("#", 1)[0].strip().lower() for line in text.splitlines()) if r]


def deny_list(configured: list[str], env: dict | None = None) -> list[str]:
    """The project's deny list plus the sandbox proxy's: the bundled sandbox/deny.txt (exfiltration
    channels and pirate mirrors) and the operator's own additions beside the proxy's logs. The
    service never contacts a host the proxy would refuse, sandboxed or not."""
    env = os.environ if env is None else env
    rules = [r.lower() for r in configured]
    proxy_dir = env.get("ALLOWPROXY_DIR") or str(Path(env.get("HOME") or Path.home()) / ".local/state/tauceti/allowproxy")
    for path in (SANDBOX / "deny.txt", Path(proxy_dir) / "deny.txt"):
        try:
            rules += read_host_list(path.read_text(encoding="utf-8"))
        except OSError:
            pass
    return list(dict.fromkeys(rules))


# --- the manifest ---------------------------------------------------------------------------------

def load_manifest(path: Path) -> dict:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def new_manifest(roadmap: str) -> dict:
    return {"schema": SCHEMA, "roadmap": roadmap, "source": []}


def _is_iso_time(value) -> bool:
    if isinstance(value, dt.datetime):
        return True
    try:
        dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


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
        status, cls = s.get("status"), s.get("licence_class")
        for key in REQUIRED_WAIVED if status == "waived" else REQUIRED:
            if key not in s or s[key] in ("", None):
                problems.append(f"{where}: missing {key}")
        if s.get("id") in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(s.get("id"))
        if s.get("locator_scheme") and s["locator_scheme"] not in LOCATOR_SCHEMES:
            problems.append(f"{where}: locator_scheme must be one of {', '.join(LOCATOR_SCHEMES)}")
        if status and status not in STATUSES:
            problems.append(f"{where}: status must be one of {', '.join(STATUSES)}")
        if cls and cls not in LICENCE_CLASSES + (NO_TEXT,):
            problems.append(f"{where}: licence_class must be one of {', '.join(LICENCE_CLASSES + (NO_TEXT,))}")
        if s.get("doi") and not DOI_RE.match(str(s["doi"])):
            problems.append(f"{where}: doi must look like 10.NNNN/suffix (no resolver prefix)")
        if s.get("fetched_at") and not _is_iso_time(s["fetched_at"]):
            problems.append(f"{where}: fetched_at must be an ISO 8601 time")
        for key in ("url", "fetched_from"):
            if s.get(key) and classify_url(s[key], allow, deny) == "denied":
                problems.append(f"{where}: {key} is on the deny list ({host_of(s[key])})")
        if status == "certified":
            if cls == NO_TEXT:
                problems.append(f"{where}: a certified source names the licence class of the text it was read in")
            if cls != "operator-local" and not s.get("fetched_from"):
                problems.append(f"{where}: a certified source records the URL its text was fetched from")
            if not SHA_RE.match(str(s.get("sha256", ""))):
                problems.append(f"{where}: a certified source records the SHA-256 of the text it was checked against")
            if s.get("fetched_from") and cls not in ("operator-local", NO_TEXT):
                found = classify_url(s["fetched_from"], allow, deny)
                if found not in ("unknown", cls) and found != "denied":
                    problems.append(f"{where}: fetched from a {found} host but recorded as {cls}")
        if status == "uncertified" and not uncertified_ok:
            problems.append(f"{where}: uncertified citations are not accepted by this project (needs-references)")
    return problems


def find_source(data: dict, *, id: str = "", doi: str = "", arxiv: str = "", sha256: str = "") -> dict | None:
    """The manifest entry with this id, else the one with this DOI, arXiv id (any version) or hash."""
    entries = data.get("source", [])
    for s in entries:
        if id and str(s.get("id", "")).lower() == id.lower():
            return s
    base = re.sub(r"v\d+$", "", arxiv)
    for s in entries:
        if doi and str(s.get("doi", "")).lower() == doi.lower():
            return s
        if base and re.sub(r"v\d+$", "", str(s.get("arxiv", ""))) == base:
            return s
        if sha256 and s.get("sha256") == sha256:
            return s
    return None


def upsert_source(data: dict, entry: dict) -> str:
    """Add `entry` to the manifest or update the entry with its id; 'added' or 'updated'. Fields of the
    new entry win, except that `cited_for` lists are merged and empty values never erase."""
    data.setdefault("source", [])
    current = find_source(data, id=entry.get("id", ""))
    if current is None:
        data["source"].append({k: v for k, v in entry.items() if v not in ("", None, [])})
        return "added"
    for key, value in entry.items():
        if value in ("", None, []):
            continue
        if key == "cited_for":
            current[key] = list(dict.fromkeys([*current.get(key, []), *value]))
        else:
            current[key] = value
    return "updated"


def _surnames(authors: str) -> list[str]:
    def initials(piece: str) -> bool:
        return all(re.fullmatch(r"(?:[^\W\d_]\.?-?)+", tok) and len(tok.replace(".", "").replace("-", "")) <= 3
                   for tok in piece.split())
    out = []
    for part in re.split(r"\s*;\s*|\s+and\s+|\s*&\s*", authors.strip()):
        pieces = [p.strip() for p in part.split(",") if p.strip()]
        i = 0
        while i < len(pieces):
            if i + 1 < len(pieces) and " " not in pieces[i] and initials(pieces[i + 1]):
                out.append(pieces[i])          # "Surname, A. B."
                i += 2
                continue
            words = [w for w in pieces[i].split() if w.rstrip(".").lower() not in ("jr", "sr", "ii", "iii")]
            if words:
                out.append(words[-1])          # "A. B. Surname"
            i += 1
    return [re.sub(r"[^\w]", "", s) for s in out if re.sub(r"[^\w]", "", s)]


def citation_key(authors: str, year, existing: set[str] | None = None) -> str:
    """An alpha-style key: `Sam73` for one author, `BLR90` for two or three, `ABC+21` for more;
    a, b, ... are appended until it is not in `existing` (compared case-insensitively)."""
    names = _surnames(authors or "") or ["Anon"]
    yy = (re.findall(r"\d{4}", str(year or "")) or [""])[-1][2:]
    if len(names) == 1:
        base = names[0][:3].capitalize()
    else:
        base = "".join(n[0].upper() for n in names[:3]) + ("+" if len(names) > 3 else "")
    key, taken, n = base + yy, {e.lower() for e in existing or set()}, 0
    while key.lower() in taken:
        key, n = base + yy + chr(ord("a") + n), n + 1
    return key


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_string(key)


def _toml_string(s: str) -> str:
    out = []
    for ch in s:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch in "\b\t\n\f\r":
            out.append({"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _ordered(entry: dict) -> list[tuple[str, object]]:
    known = [(k, entry[k]) for k in FIELD_ORDER if k in entry]
    return known + sorted((k, v) for k, v in entry.items() if k not in FIELD_ORDER)


def dump_manifest(data: dict) -> str:
    """The manifest as TOML: top-level values, then one [[source]] table per entry in a fixed field
    order. Comments of a hand-written file are not kept."""
    lines = []
    tables = []
    for key, value in data.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            tables.append((key, value))
        elif isinstance(value, list) and not value and key == "source":
            continue
        else:
            lines.append(f"{_toml_key(key)} = {toml_value(value)}")
    for key, entries in tables:
        for entry in entries:
            lines += ["", f"[[{_toml_key(key)}]]"]
            lines += [f"{_toml_key(k)} = {toml_value(v)}" for k, v in _ordered(entry)]
    return "\n".join(lines) + "\n"


def save_manifest(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_manifest(data), encoding="utf-8")
    os.replace(tmp, path)


# --- cache ----------------------------------------------------------------------------------------

def cache_root(state: Path) -> Path:
    return Path(state) / "sources"


def cache_paths(root: Path, sha256: str, ext: str = "pdf") -> tuple[Path, Path]:
    """(file, line-numbered text extraction) for a cached source."""
    return Path(root) / f"{sha256}.{ext.lstrip('.')}", Path(root) / f"{sha256}.txt"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class HashMismatch(Exception):
    pass


def store_bytes(root: Path, data: bytes, ext: str, expected: str = "") -> Path:
    """Write `data` into the cache under its hash and verify the written file. With `expected`, the
    data must have that SHA-256 (a recorded text, or the hash a human stated for an attachment)."""
    sha = sha256_bytes(data)
    if expected and sha != expected.lower():
        raise HashMismatch(f"SHA-256 {sha} differs from the expected {expected.lower()}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path, _ = cache_paths(root, sha, ext)
    if path.is_file() and sha256_file(path) == sha:
        return path
    tmp = root / f".{sha}.part"
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if sha256_file(path) != sha:
        path.unlink(missing_ok=True)
        raise HashMismatch(f"{path.name}: the written file does not hash to {sha}")
    return path


def cached_file(root: Path, sha256: str) -> Path | None:
    """The cached bytes of a hash (any extension except the text extraction), or None."""
    for path in sorted(Path(root).glob(f"{sha256}.*")):
        if path.suffix != ".txt":
            return path
    return None


def sniff(data: bytes, content_type: str = "") -> str:
    """The kind of a downloaded body: pdf, djvu, ps, html, text or bin (by content, not by name)."""
    head = data[:1024]
    if b"%PDF-" in head:
        return "pdf"
    if head.startswith(b"AT&TFORM"):
        return "djvu"
    if head.startswith(b"%!PS"):
        return "ps"
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html")) or b"<html" in low[:512] or "html" in content_type.lower():
        return "html"
    if b"\x00" not in data[:8192]:
        try:
            data[:8192].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "bin"


EXTENSION = {"pdf": "pdf", "djvu": "djvu", "ps": "ps", "html": "html", "text": "text", "bin": "bin"}


# --- text extraction ------------------------------------------------------------------------------

@dataclass
class Extraction:
    pages: list[str] = field(default_factory=list)
    method: str = "none"
    note: str = ""

    @property
    def chars(self) -> int:
        return sum(len("".join(p.split())) for p in self.pages)


def image_only(pages: list[str], min_chars: int = 250) -> bool:
    """More than half of the pages have almost no text layer (an image-only scan, perhaps with a
    text cover sheet, as digitisation portals add)."""
    if not pages:
        return True
    empty = sum(1 for p in pages if len("".join(p.split())) < min_chars)
    return empty * 2 > len(pages)


def _split_pages(text: str) -> list[str]:
    """Pages of extractor output: a form feed ends each page, except that an empty last page shows
    up as a bare newline; only a truly empty tail is the terminator."""
    pages = text.split("\f")
    if len(pages) > 1 and pages[-1] == "":
        pages.pop()
    return pages


def pdf_pages_pdftotext(path: Path) -> list[str] | None:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return _split_pages(out.stdout.decode("utf-8", errors="replace"))


def pdf_pages_pypdf(path: Path) -> list[str] | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return [page.extract_text() or "" for page in PdfReader(str(path)).pages]
    except Exception:  # noqa: BLE001 - a malformed PDF is data, not a crash
        return None


class _TextOfHtml(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(text: str) -> str:
    parser = _TextOfHtml()
    parser.feed(text)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def extract(path: Path, kind: str, *, ocr: bool = False, ocr_dpi: int = 300, min_chars: int = 250) -> Extraction:
    """Text of a cached source, page by page. PDFs go through `pdftotext -layout` (else pypdf); an
    image-only PDF goes through OCR when it is enabled and installed, and says so otherwise."""
    path = Path(path)
    if kind == "pdf":
        pages, method = pdf_pages_pdftotext(path), "pdftotext -layout"
        if pages is None:
            pages, method = pdf_pages_pypdf(path), "pypdf"
        if pages is None:
            return Extraction([], "none", "no PDF text extractor: install poppler-utils (pdftotext) or the `pdf` extra")
        result = Extraction(pages, method)
        if image_only(pages, min_chars):
            scanned = sum(1 for p in pages if len("".join(p.split())) < min_chars)
            if not ocr:
                result.note = (f"image-only scan ({scanned} of {len(pages)} pages have no text layer): enable "
                               "[sources] ocr to recognise it")
                return result
            try:
                return Extraction(ocr_pdf_pages(path, dpi=ocr_dpi), f"OCR (RapidOCR, {ocr_dpi} dpi)",
                                  f"image-only scan ({scanned} of {len(pages)} pages had no text layer)")
            except OcrUnavailable as exc:
                result.note = f"image-only scan; {exc}"
        return result
    raw = path.read_bytes()
    if kind == "html":
        return Extraction([html_to_text(raw.decode("utf-8", errors="replace"))], "html")
    if kind == "text":
        return Extraction([raw.decode("utf-8", errors="replace")], "plain text")
    if kind == "djvu" and shutil.which("djvutxt"):
        try:
            out = subprocess.run(["djvutxt", str(path)], capture_output=True, timeout=600)
            if out.returncode == 0:
                return Extraction(_split_pages(out.stdout.decode("utf-8", errors="replace")), "djvutxt")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return Extraction([], "none", f"no text extractor for {kind} files")


def numbered_text(pages: list[str], header: list[str]) -> str:
    """The text file reviewers cite: every line carries its number, pages are marked
    `=== page N ===`, and the header says where the text came from."""
    lines = [f"# {h}" for h in header]
    for i, page in enumerate(pages, 1):
        lines.append(f"=== page {i} ===")
        lines += [line.rstrip() for line in page.splitlines()]
    width = max(6, len(str(len(lines))))
    return "".join(f"{n:>{width}}  {line}\n" for n, line in enumerate(lines, 1))


def write_text(root: Path, sha256: str, extraction: Extraction, origin: str = "") -> Path:
    _, txt = cache_paths(root, sha256)
    header = [f"Text of {sha256} ({extraction.method}; {len(extraction.pages)} pages)."]
    if origin:
        header.append(f"From {origin}")
    if extraction.note:
        header.append(f"Note: {extraction.note}")
    header.append("Cite a passage as <source id> line N (the numbers at the left of each line).")
    txt.write_text(numbered_text(extraction.pages, header), encoding="utf-8")
    return txt


# --- OCR (the uv venv + RapidOCR recipe) ----------------------------------------------------------

OCR_HINT = ("Install the optional `ocr` extra (RapidOCR with pypdfium2) into the tool's environment, "
            "`uv tool install --force 'tauceti-roadmap-worker[ocr] @ git+https://github.com/TauCetiProject/"
            "TauCetiRoadmapWorker'`, or `uv pip install -e '.[ocr]'` in a checkout. If OpenCV then fails to load "
            "libGL, replace opencv-python by opencv-python-headless in the same environment.")


class OcrUnavailable(RuntimeError):
    pass


def _ocr_modules():
    try:
        import numpy
        import pypdfium2
    except ImportError as exc:
        raise OcrUnavailable(f"OCR is not installed ({exc.name or exc} is missing). {OCR_HINT}") from exc
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as first:
        try:
            from rapidocr import RapidOCR
        except ImportError:
            raise OcrUnavailable(f"OCR is not installed ({first}). {OCR_HINT}") from first
    return numpy, pypdfium2, RapidOCR


def ocr_status() -> str:
    """'' when OCR can run here, else why not and how to install it."""
    try:
        _ocr_modules()
        return ""
    except OcrUnavailable as exc:
        return str(exc)


def _ocr_items(result) -> list[tuple[list, str]]:
    """(box, text) pairs from either RapidOCR API: rapidocr_onnxruntime returns ([box, text, score]
    lists, elapsed); rapidocr 2+ returns an object with .boxes and .txts."""
    if isinstance(result, tuple):
        return [(item[0], item[1]) for item in (result[0] or [])]
    boxes, texts = getattr(result, "boxes", None), getattr(result, "txts", None)
    if boxes is None or texts is None:
        return []
    return [(b.tolist() if hasattr(b, "tolist") else b, t) for b, t in zip(boxes, texts, strict=False)]


def group_ocr_lines(items: list[tuple[list, str]], scale: float = 1.0) -> list[str]:
    """Recognised boxes as text lines: sorted top to bottom, then left to right, and joined into one
    line while their tops stay within 14 pixels (at 300 dpi) of each other."""
    boxes = sorted(items, key=lambda it: (round(it[0][0][1] / (12 * scale)), it[0][0][0]))
    lines, current, current_y = [], [], None
    for box, text in boxes:
        y = box[0][1]
        if current_y is not None and abs(y - current_y) > 14 * scale:
            lines.append(" ".join(current))
            current = []
        current.append(text)
        current_y = y
    if current:
        lines.append(" ".join(current))
    return lines


def ocr_pdf_pages(path: Path, dpi: int = 300, first: int = 1, last: int | None = None) -> list[str]:
    """OCR of PDF pages first..last (1-based, inclusive): render with pypdfium2, recognise with RapidOCR."""
    numpy, pdfium, RapidOCR = _ocr_modules()
    pdf = pdfium.PdfDocument(str(path))
    engine = RapidOCR()
    pages = []
    for i in range(first - 1, min(last or len(pdf), len(pdf))):
        image = pdf[i].render(scale=dpi / 72).to_pil().convert("RGB")
        pages.append("\n".join(group_ocr_lines(_ocr_items(engine(numpy.array(image))), scale=dpi / 300)))
    return pages
