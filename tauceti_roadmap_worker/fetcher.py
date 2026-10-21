"""The sources service's network half: resolve a citation to a legal source, download it into the
cache, extract its line-numbered text, and say what to record in the manifest.

A citation is a DOI, an arXiv id, a URL, a file in one of the operator's local directories, or free
text ("Author, Title, Year"). Resolution tries, in order, and stops at the first text it can read:

1. the operator's own texts (`[sources] local_dirs`), matched by arXiv id, DOI or title words;
2. arXiv: the id given, the preprint Crossref lists for a DOI, or a title search;
3. HAL: the file deposited for the DOI or the title;
4. the DOI's landing page, when it is on a host of a licence class: its `citation_pdf_url`, else its
   only PDF link (Numdam, EuDML, GDZ, Project Euclid open content, publisher open archives);
5. a URL named explicitly (arXiv abstract pages and Numdam item pages are rewritten to their PDF).

The policy is enforced on every request and every redirect hop:

* a host on the deny list (the project's, the sandbox proxy's bundled list and the proxy operator's
  additions) is never contacted;
* a paywalled host (`[sources] paywalled`: Elsevier and Springer by default) is never contacted,
  and a DOI with a paywalled prefix is never resolved: a citation that resolves only there is
  recorded as `uncertified` with its DOI and locator;
* the bytes of a source are kept only when they come from a host of a licence class, or from a URL
  the caller named together with its class; metadata services (the arXiv API, Crossref, the HAL
  API, doi.org) are read but never recorded as where a text came from;
* one plain GET per hop with a declared User-Agent: no credentials, no cookies, no retries around a
  refusal; arXiv gets one request every three seconds from all the processes of a host that share
  the state directory (a lock file in the cache paces them together).

Everything goes through a `Transport`, so the tests run on canned responses.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import html.parser
import http.client
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse

from . import __version__, sources
from .sources import host_matches, host_of

REDIRECTS = (301, 302, 303, 307, 308)
RESOLVERS = ("doi.org", "dx.doi.org", "hdl.handle.net")
DOCUMENT_KINDS = ("pdf", "djvu", "ps", "text")
LOCAL_SUFFIXES = (".pdf", ".djvu", ".ps", ".txt", ".html", ".htm")


class FetchError(Exception):
    """A request that did not yield a document. `permanent` is False for network trouble worth retrying."""

    def __init__(self, message: str, *, permanent: bool = True, paywalled: bool = False, refused: bool = False):
        super().__init__(message)
        self.permanent, self.paywalled, self.refused = permanent, paywalled, refused


# --- transport and policy -------------------------------------------------------------------------

@dataclass
class Response:
    status: int
    url: str
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the client follows redirects itself, checking each hop
        return None


class UrllibTransport:
    """One GET, redirects not followed. Proxies come from the environment (the sandbox proxy)."""

    def __init__(self, timeout: int = 60, max_bytes: int = 200 << 20):
        self.timeout, self.max_bytes = timeout, max_bytes
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, url: str, headers: dict) -> Response:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                body = r.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise FetchError(f"{url}: larger than the {self.max_bytes >> 20} MB cap ([sources] max_megabytes)")
                return Response(r.status, url, {k.lower(): v for k, v in r.headers.items()}, body)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(1 << 16) if exc.fp else b""
            except (OSError, http.client.HTTPException):
                body = b""
            return Response(exc.code, url, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
        except http.client.InvalidURL as exc:
            raise FetchError(f"{url!r}: not a valid URL ({exc})") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            # connection trouble, a truncated body (IncompleteRead), a malformed status line
            raise FetchError(f"{host_of(url)}: {getattr(exc, 'reason', exc)}", permanent=False) from exc
        except ValueError as exc:            # urllib's "unknown url type" and the like
            raise FetchError(f"{url!r}: {exc}") from exc


class Client:
    """GETs under the sources policy: deny list, paywalls, redirects checked hop by hop, pacing."""

    def __init__(self, transport, *, allow: dict[str, list[str]], deny: list[str], paywalled: list[str],
                 user_agent: str = "", pace: dict[str, float] | None = None, sleep=time.sleep, clock=time.time,
                 pace_dir: Path | None = None):
        self.transport, self.allow, self.deny, self.paywalled = transport, allow, deny, paywalled
        self.user_agent = user_agent or f"tauceti-roadmap-worker/{__version__} (legal sources only)"
        self.pace = {".arxiv.org": 3.0} if pace is None else pace
        self.sleep, self.clock, self.last, self.pace_dir = sleep, clock, {}, pace_dir
        self.log: list[str] = []

    def classify(self, url: str) -> str:
        return sources.classify_url(url, self.allow, self.deny)

    def check(self, url: str) -> None:
        scheme, host = urlparse(url).scheme, host_of(url)
        if scheme not in ("http", "https") or not host:
            raise FetchError(f"refusing {url!r}: only http(s) URLs are fetched", refused=True)
        if any(host_matches(host, r) for r in self.deny):
            raise FetchError(f"{host} is on the deny list", refused=True)
        if any(host_matches(host, r) for r in self.paywalled):
            raise FetchError(f"{host} is paywalled (never fetched)", paywalled=True)

    def _wait(self, host: str) -> None:
        for rule, gap in self.pace.items():
            if not host_matches(host, rule):
                continue
            if self.pace_dir is None:
                wait = self.last.get(rule, -1e9) + gap - self.clock()
                if wait > 0:
                    self.sleep(wait)
                self.last[rule] = self.clock()
                continue
            # Shared with every process using this state directory: the time of the last request is
            # kept in a lock file, read and written under an exclusive lock.
            self.pace_dir.mkdir(parents=True, exist_ok=True)
            with open(self.pace_dir / f".pace{rule}", "a+", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    last = float(f.read().strip() or "-1e9")
                except ValueError:
                    last = -1e9
                wait = last + gap - self.clock()
                if wait > 0:
                    self.sleep(wait)
                f.seek(0)
                f.truncate()
                f.write(repr(self.clock()))

    def get(self, url: str, accept: str = "*/*") -> Response:
        for _ in range(10):
            self.check(url)
            host = host_of(url)
            self._wait(host)
            r = self.transport.request(url, {"User-Agent": self.user_agent, "Accept": accept})
            self.log.append(f"GET {url} -> {r.status}")
            if r.status in REDIRECTS and r.header("location"):
                url = urljoin(url, r.header("location"))
                continue
            if 200 <= r.status < 300:
                r.url = url
                return r
            if r.status in (401, 402):
                raise FetchError(f"{host}: HTTP {r.status} (login or payment required)", paywalled=True)
            if r.status in (404, 410):
                raise FetchError(f"{url}: not found (HTTP {r.status})")
            if r.status == 429 or r.status >= 500:
                raise FetchError(f"{host}: HTTP {r.status}", permanent=False)
            raise FetchError(f"{host}: HTTP {r.status}")
        raise FetchError(f"too many redirects from {url}")


def client_from_config(cfg, transport=None) -> Client:
    s = cfg.sources
    return Client(transport or UrllibTransport(max_bytes=s.max_megabytes << 20), allow=s.allow,
                  deny=sources.deny_list(s.deny), paywalled=s.paywalled, user_agent=s.user_agent,
                  pace_dir=sources.cache_root(cfg.state_dir()))


# --- citations ------------------------------------------------------------------------------------

ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?"
ARXIV_OLD = r"[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
ARXIV_ID = re.compile(rf"^(?:{ARXIV_NEW}|{ARXIV_OLD})$")
ARXIV_URL = re.compile(rf"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/({ARXIV_NEW}|{ARXIV_OLD})(?:\.pdf)?/?(?:[?#].*)?$")
DOI_URL = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\.\S+)$", re.I)
ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
YEAR = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


@dataclass
class Citation:
    kind: str            # doi | arxiv | url | file | text
    value: str
    authors: str = ""
    title: str = ""
    year: str = ""


def parse_citation(text: str, *, authors: str = "", title: str = "", year: str = "") -> Citation:
    raw = text.strip().strip("<>").strip()
    extra = {"authors": authors, "title": title, "year": str(year or "")}
    m = DOI_URL.match(raw)
    if m:
        return Citation("doi", unquote(m.group(1)).rstrip(".,;"), **extra)
    if raw.lower().startswith("doi:"):
        return Citation("doi", raw[4:].strip().rstrip(".,;"), **extra)
    if sources.DOI_RE.match(raw):
        return Citation("doi", raw.rstrip(".,;"), **extra)
    m = ARXIV_URL.match(raw)
    if m:
        return Citation("arxiv", m.group(1), **extra)
    candidate = raw[6:].strip() if raw.lower().startswith("arxiv:") else raw
    if ARXIV_ID.match(candidate):
        return Citation("arxiv", candidate, **extra)
    if re.match(r"^https?://", raw, re.I):
        return Citation("url", raw, **extra)
    if raw.startswith(("/", "~", "./")) or os.path.isfile(os.path.expanduser(raw)):
        return Citation("file", raw, **extra)
    quoted = re.search(r"[\"“«]([^\"”»]{8,})[\"”»]", raw)
    found_year = YEAR.findall(raw)
    return Citation("text", raw, authors=authors or leading_authors(raw),
                    title=title or (quoted.group(1).strip() if quoted else ""),
                    year=extra["year"] or (found_year[-1] if found_year else ""))


def leading_authors(text: str) -> str:
    """The author part of 'A. Author, B. Writer, Title, Year' or 'A. Author, "Title", ...': the
    leading comma-separated pieces that look like names (initials and at most three words)."""
    head = re.sub(r"\s+and\s+|\s*&\s*", ", ", re.split(r"[\"“«]", text, maxsplit=1)[0])
    names = []
    for piece in head.split(","):
        piece = piece.strip()
        tokens = piece.split()
        if not tokens or len(tokens) > 4 or not re.match(r"^[^\W\d_]", piece):
            break
        if not any(re.fullmatch(r"(?:[^\W\d_]\.-?)+", t) for t in tokens) and names:
            break                                     # a piece without initials after the names: the title
        if not any(re.fullmatch(r"(?:[^\W\d_]\.-?)+", t) for t in tokens) and len(tokens) > 1:
            break
        names.append(piece)
    return ", ".join(names)


def year_compatible(found: str, wanted: str) -> bool:
    """A hit from long after the cited year is a different text (a later edition, a reprint, a
    lecture course of the same title); preprints come before publication. Unknown years pass."""
    if not (str(found).isdigit() and str(wanted).isdigit()):
        return True
    return int(found) <= int(wanted) + 2


STOPWORDS = {"the", "of", "on", "and", "in", "for", "to", "with", "by", "its", "de", "la", "le", "les",
             "des", "du", "et", "sur", "und", "der", "die", "das", "von", "zur", "zum", "uber", "ein", "eine"}


def words(text: str, min_len: int = 1) -> list[str]:
    """Accent-folded lower-case words without stopwords. Single letters count ("Theorem A und
    Theorem B"); search queries pass min_len=2."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()
    return [w for w in re.findall(r"[a-z0-9]+", folded) if len(w) >= min_len and w not in STOPWORDS]


def title_matches(found: str, wanted: str, free_text: bool = False) -> bool:
    """Does a search hit's title match? With a known title, the word sets agree to 80%; with free
    text ("Author, Title, Year"), 85% of the hit's title words occur in the text."""
    f, w = set(words(found)), set(words(wanted))
    if len(f) < 2 or not w:
        return False
    if free_text:
        return len(f & w) / len(f) >= 0.85
    return len(f & w) / max(len(f), len(w)) >= 0.8


def surnames_match(found: list[str] | str, wanted: str) -> bool:
    """Some surname of the wanted authors occurs among the hit's authors ('' wanted: no constraint)."""
    want = {words(n)[-1] for n in sources._surnames(wanted) if words(n)}
    if not want:
        return True
    have = " ".join(found) if isinstance(found, list) else found
    return bool(want & set(words(have)))


# --- metadata services ----------------------------------------------------------------------------

ATOM, ARXIV_NS = "{http://www.w3.org/2005/Atom}", "{http://arxiv.org/schemas/atom}"


def parse_arxiv_feed(body: bytes) -> list[dict]:
    """Entries of an arXiv API Atom feed: id (with version), title, authors, year, doi, journal."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall(f"{ATOM}entry"):
        m = re.search(r"arxiv\.org/abs/(.+)$", e.findtext(f"{ATOM}id") or "")
        if not m:
            continue                      # the API reports a bad id as an entry titled "Error"
        out.append({"arxiv": m.group(1), "title": " ".join((e.findtext(f"{ATOM}title") or "").split()),
                    "authors": [" ".join((a.findtext(f"{ATOM}name") or "").split()) for a in e.findall(f"{ATOM}author")],
                    "year": (e.findtext(f"{ATOM}published") or "")[:4], "doi": e.findtext(f"{ARXIV_NS}doi") or "",
                    "venue": " ".join((e.findtext(f"{ARXIV_NS}journal_ref") or "").split())})
    return out


def arxiv_query_url(*, ids: str = "", title: str = "", authors: str = "", text: str = "", rows: int = 5) -> str:
    if ids:
        return "https://export.arxiv.org/api/query?" + urlencode({"id_list": ids, "max_results": 1})
    terms = []
    if title:
        terms.append(" AND ".join(f"ti:{w}" for w in words(title, 2)[:10]))
    elif text:
        terms.append(" AND ".join(f"all:{w}" for w in words(YEAR.sub(" ", text), 2)[:8]))
    surnames = sources._surnames(authors)
    if surnames and words(surnames[0]):
        terms.append(f"au:{words(surnames[0])[-1]}")
    return "https://export.arxiv.org/api/query?" + urlencode({"search_query": " AND ".join(t for t in terms if t),
                                                              "max_results": rows})


def crossref_meta(msg: dict) -> dict:
    """Title, authors, year, DOI, venue and listed arXiv preprints of a Crossref work."""
    def year_of(key):
        parts = (msg.get(key) or {}).get("date-parts") or [[None]]
        return str(parts[0][0] or "") if parts and parts[0] else ""
    authors = ", ".join(" ".join(x for x in (a.get("given", ""), a.get("family", "")) if x).strip()
                        for a in msg.get("author", []) if a.get("family") or a.get("given"))
    preprints = []
    for rel in (msg.get("relation") or {}).get("has-preprint", []):
        rid = str(rel.get("id", ""))
        if rel.get("id-type") != "arxiv" and "arxiv" not in rid.lower():
            continue
        m = ARXIV_DOI.match(rid) or re.search(rf"({ARXIV_NEW}|{ARXIV_OLD})", rid)
        if m:
            preprints.append(m.group(1))
    venue = " ".join(x for x in [(msg.get("container-title") or [""])[0], str(msg.get("volume", "") or "")] if x)
    return {"title": " ".join(((msg.get("title") or [""])[0]).split()), "authors": authors,
            "year": year_of("issued") or year_of("published-print") or year_of("published-online"),
            "doi": msg.get("DOI", ""), "venue": venue, "preprints": preprints}


def hal_query_url(*, doi: str = "", title: str = "", rows: int = 5) -> str:
    q = f'doiId_s:"{doi}"' if doi else "title_t:(" + " AND ".join(words(title, 2)[:10]) + ")"
    return "https://api.archives-ouvertes.fr/search/?" + urlencode(
        {"q": q, "fl": "title_s,authFullName_s,producedDateY_i,fileMain_s,uri_s,doiId_s", "wt": "json", "rows": rows})


def parse_hal(body: bytes) -> list[dict]:
    try:
        docs = json.loads(body)["response"]["docs"]
    except (ValueError, KeyError, TypeError):
        return []
    return [{"title": (d.get("title_s") or [""])[0], "authors": ", ".join(d.get("authFullName_s") or []),
             "year": str(d.get("producedDateY_i") or ""), "file": d.get("fileMain_s") or "",
             "url": d.get("uri_s") or "", "doi": d.get("doiId_s") or ""} for d in docs if isinstance(d, dict)]


class _PageLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v or "" for k, v in attrs}
        if tag == "meta" and a.get("name") and a.get("content"):
            self.meta.setdefault(a["name"].lower(), a["content"])
        elif tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])


def landing_pdf(page: str, base: str) -> tuple[str, str]:
    """(PDF URL, '') from a landing page's `citation_pdf_url`, else from its only PDF link; ('', why)
    when there is none or several (never a guess)."""
    p = _PageLinks()
    p.feed(page)
    if p.meta.get("citation_pdf_url"):
        return urljoin(base, p.meta["citation_pdf_url"]), ""
    links = list(dict.fromkeys(urljoin(base, h) for h in p.hrefs
                               if urlparse(h).path.lower().endswith(".pdf") or "/download/pdf/" in h))
    if len(links) == 1:
        return links[0], ""
    return "", ("no PDF link" if not links else f"{len(links)} PDF links (name the right one)")


def direct_pdf_url(url: str) -> str:
    """The PDF behind an arXiv abstract page or a Numdam item page; other URLs unchanged."""
    m = ARXIV_URL.match(url)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}"
    m = re.match(r"^https?://(?:www\.)?numdam\.org/item/([^?#]+?)/?$", url)
    if m and not m.group(1).lower().endswith(".pdf"):
        return f"https://www.numdam.org/item/{m.group(1)}.pdf"
    return url


# --- local texts ----------------------------------------------------------------------------------

def local_files(dirs: list[str]) -> list[Path]:
    out = []
    for d in dirs:
        root = Path(os.path.expanduser(d))
        if root.is_dir():
            out += sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in LOCAL_SUFFIXES)
    return out


def within(path: Path, dirs: list[str]) -> bool:
    path = path.resolve()
    return any(path.is_relative_to(Path(os.path.expanduser(d)).resolve()) for d in dirs)


def local_match(files: list[Path], *, arxiv: str = "", doi: str = "", title: str = "", year: str = "") -> Path | None:
    """A local text named after the arXiv id, the DOI, or (most of) the title words and the year."""
    def stem(p: Path) -> str:
        return p.stem.lower()
    if arxiv:
        base = re.sub(r"v\d+$", "", arxiv).lower()
        keys = {base, base.replace("/", "-"), base.replace("/", "_"), base.replace("/", "")}
        hit = next((p for p in files if any(k in stem(p) for k in keys)), None)
        if hit:
            return hit
    if doi:
        slug = re.sub(r"[^a-z0-9]+", "", doi.lower().split("/", 1)[-1])
        hit = next((p for p in files if len(slug) >= 6 and slug in re.sub(r"[^a-z0-9]+", "", stem(p))), None)
        if hit:
            return hit
    want = set(words(title))
    if len(want) >= 2:
        for p in files:
            have = set(words(p.stem.replace("_", " ").replace("-", " ")))
            if len(want & have) / len(want) >= 0.8 and (not year or year in p.stem):
                return p
    return None


def local_by_hash(dirs: list[str], sha256: str, index: Path) -> Path | None:
    """The local text with this hash; hashes are kept in `index` by (path, size, mtime)."""
    try:
        known = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    changed, found = False, None
    for p in local_files(dirs):
        st = p.stat()
        key = str(p)
        if known.get(key, {}).get("stamp") != [st.st_size, st.st_mtime_ns]:
            known[key] = {"stamp": [st.st_size, st.st_mtime_ns], "sha256": sources.sha256_file(p)}
            changed = True
        if known[key]["sha256"] == sha256:
            found = p
            break
    if changed:
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps(known, indent=1) + "\n", encoding="utf-8")
    return found


def local_by_name(dirs: list[str], name: str) -> Path | None:
    """The local text with this file name (case-insensitive) in the configured directories."""
    want = (name or "").strip().lower()
    return next((p for p in local_files(dirs) if p.name.lower() == want), None) if want else None


def local_source(dirs: list[str], name: str, sha256: str, index: Path) -> Path | None:
    """The operator's copy of a local text: by its hash when one is recorded (whatever the file is
    called here), else by its file name."""
    if sha256 and sources.SHA_RE.match(sha256):
        return local_by_hash(dirs, sha256, index)
    return local_by_name(dirs, name)


# --- fetching -------------------------------------------------------------------------------------

@dataclass
class Candidate:
    url: str = ""
    via: str = ""
    landing: bool = False        # an HTML page to look into for the document
    local: Path | None = None
    licence_class: str = ""      # forced class: named by the caller, or operator-local
    canonical: str = ""          # the URL a reader should follow (abstract page, DOI)


@dataclass
class Outcome:
    status: str                  # fetched | uncertified | failed
    entry: dict = field(default_factory=dict)
    file: Path | None = None
    text: Path | None = None
    tried: list[str] = field(default_factory=list)
    note: str = ""
    transient: bool = False


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


class Fetcher:
    def __init__(self, client: Client, cache: Path, cfg_sources, now=_now):
        self.client, self.cache, self.cfg, self.now = client, Path(cache), cfg_sources, now

    # -- storing ------------------------------------------------------------------------------------
    def keep(self, data: bytes, kind: str, origin: str, expected: str = "") -> tuple[Path, Path, str]:
        """Cache the bytes under their hash (verified), extract the line-numbered text beside them."""
        path = sources.store_bytes(self.cache, data, sources.EXTENSION.get(kind, "bin"), expected)
        sha = path.name.split(".", 1)[0]
        extraction = sources.extract(path, kind, ocr=self.cfg.ocr, ocr_dpi=self.cfg.ocr_dpi,
                                     min_chars=self.cfg.ocr_min_chars)
        return path, sources.write_text(self.cache, sha, extraction, origin), extraction.note

    # -- resolution ---------------------------------------------------------------------------------
    def _json(self, url: str):
        return json.loads(self.client.get(url, accept="application/json").body)

    def _arxiv(self, url: str) -> list[dict]:
        return parse_arxiv_feed(self.client.get(url, accept="application/atom+xml").body)

    def _local(self, meta: dict) -> list[Candidate]:
        hit = local_match(local_files(self.cfg.local_dirs), arxiv=meta.get("arxiv", ""), doi=meta.get("doi", ""),
                          title=meta.get("title", ""), year=str(meta.get("year", "")))
        return [Candidate(local=hit, via=f"local text {hit.name}", licence_class="operator-local")] if hit else []

    @staticmethod
    def _arxiv_candidate(arxiv_id: str) -> Candidate:
        return Candidate(f"https://arxiv.org/pdf/{arxiv_id}", "arXiv", canonical=f"https://arxiv.org/abs/{arxiv_id}")

    def candidates(self, cit: Citation, meta: dict, notes: list[str], search: bool = True):
        """Where the text might be, cheapest first; metadata lookups happen only when reached.
        `search=False` skips the title searches (a DOI found by a search that already ran)."""
        def guard(what, call, default):
            try:
                return call()
            except FetchError as exc:
                notes.append(f"{what}: {exc}")
                if not exc.permanent:
                    meta["_transient"] = True
                return default
            except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                notes.append(f"{what}: an unexpected answer ({type(exc).__name__})")
                return default

        if cit.kind == "file":
            path = Path(os.path.expanduser(cit.value))
            if not within(path, self.cfg.local_dirs):
                notes.append(f"{cit.value}: a local text must be in one of [sources] local_dirs")
                return
            yield Candidate(local=path, via=f"local text {path.name}", licence_class="operator-local")
            return
        if cit.kind == "url":
            m = ARXIV_URL.match(cit.value)
            if m:
                meta["arxiv"] = m.group(1)
            yield Candidate(direct_pdf_url(cit.value), "the given URL", landing=True, canonical=cit.value)
            return
        doi = cit.value if cit.kind == "doi" else ""
        arxiv = cit.value if cit.kind == "arxiv" else ""
        if doi and ARXIV_DOI.match(doi):
            arxiv, doi = ARXIV_DOI.match(doi).group(1), ""
        if arxiv:
            hits = guard("arXiv API", lambda: self._arxiv(arxiv_query_url(ids=arxiv)), [])
            if hits:
                meta.update({k: v for k, v in hits[0].items() if k != "authors"}, authors=", ".join(hits[0]["authors"]))
            meta["arxiv"] = meta.get("arxiv") or arxiv
            yield from self._local(meta)
            yield self._arxiv_candidate(meta["arxiv"])
            return
        preprints: list[str] = []
        if doi:
            meta["doi"] = doi
            work = guard("Crossref", lambda: crossref_meta(self._json(f"https://api.crossref.org/works/{quote(doi, safe='/:;()')}")["message"]), None)
            if work:
                preprints = work.pop("preprints")
                meta.update({k: v for k, v in work.items() if v and not meta.get(k)})
        else:
            meta.update({k: v for k, v in (("title", cit.title), ("authors", cit.authors), ("year", cit.year)) if v})
        yield from self._local(meta)
        for p in preprints:
            meta.setdefault("arxiv", p)
            yield self._arxiv_candidate(p)
        if doi:
            for h in guard("HAL", lambda: parse_hal(self.client.get(hal_query_url(doi=doi), accept="application/json").body), []):
                if h["file"]:
                    yield Candidate(h["file"], "HAL", canonical=h["url"])
            prefix = doi.split("/", 1)[0]
            if prefix in self.cfg.paywalled_doi_prefixes:
                meta["_paywalled"] = f"DOI prefix {prefix} belongs to a paywalled publisher (not resolved)"
            else:
                yield Candidate(f"https://doi.org/{doi}", "the DOI's landing page", landing=True,
                                canonical=f"https://doi.org/{doi}")
        title, free = meta.get("title", ""), not meta.get("title")
        query = title or (cit.value if cit.kind == "text" else "")
        if not query or not search:
            return
        url = arxiv_query_url(title=title, authors=meta.get("authors", ""), text="" if title else query)
        for hit in guard("arXiv search", lambda: self._arxiv(url), []):
            if self._hit_matches(hit["title"], hit["authors"], hit["year"], query, free, meta):
                for key, value in (("title", hit["title"]), ("year", hit["year"]), ("authors", ", ".join(hit["authors"]))):
                    if not meta.get(key):
                        meta[key] = value
                meta["arxiv"] = hit["arxiv"]
                yield self._arxiv_candidate(hit["arxiv"])
                break
        for h in guard("HAL search", lambda: parse_hal(self.client.get(hal_query_url(title=query), accept="application/json").body), []):
            if h["file"] and self._hit_matches(h["title"], h["authors"], h["year"], query, free, meta):
                yield Candidate(h["file"], "HAL", canonical=h["url"])
                break
        if cit.kind == "text" and not doi:
            params = {"query.bibliographic": query, "rows": 3}
            items = guard("Crossref search", lambda: self._json("https://api.crossref.org/works?" + urlencode(params))
                          .get("message", {}).get("items", []), [])
            for item in items:
                work = crossref_meta(item)
                if work["doi"] and self._hit_matches(work["title"], work["authors"], work["year"], query, free, meta):
                    yield from self.candidates(Citation("doi", work["doi"], meta.get("authors", ""), work["title"],
                                                        meta.get("year", "")), meta, notes, search=False)
                    break

    @staticmethod
    def _hit_matches(title: str, authors, year: str, query: str, free: bool, meta: dict) -> bool:
        """A search hit is the cited work: the title matches, an author's surname occurs, and the
        year does not contradict the citation."""
        return (title_matches(title, query, free) and surnames_match(authors, meta.get("authors", ""))
                and year_compatible(year, str(meta.get("year", ""))))

    def _try(self, cand: Candidate, licence_class: str, tried: list[str], paywall: list[str], meta: dict):
        """(data, kind, final URL, licence class) of a candidate, or None with the reason recorded."""
        if cand.local:
            try:
                data = sources.unwrap(cand.local.read_bytes())
            except OSError as exc:
                tried.append(f"{cand.via}: {exc}")
                return None
            kind = sources.sniff(data)
            if kind not in DOCUMENT_KINDS + ("html",):
                tried.append(f"{cand.via}: not a document")
                return None
            return data, kind, "", "operator-local"
        # A class named by the caller vouches for the URL the caller named, on that URL's host only:
        # never for discovered URLs, a redirect to another host, or a link to another host.
        named = licence_class if cand.via == "the given URL" else ""

        def vouched(url: str) -> str:
            return cand.licence_class or (named if host_of(url) == host_of(cand.url) else "")
        forced = vouched(cand.url)
        pre = self.client.classify(cand.url)
        if not forced and not cand.landing and pre == "unknown":
            tried.append(f"{cand.via} {cand.url}: {host_of(cand.url)} is in no licence class")
            return None
        try:
            r = self.client.get(cand.url)
            r.body = sources.unwrap(r.body)                 # `paper.ps.gz` from an author's page
            kind = sources.sniff(r.body, r.header("content-type"))
            if kind == "html" and cand.landing:
                cls = vouched(r.url) or self.client.classify(r.url)
                if cls in ("unknown", "denied"):
                    tried.append(f"{cand.via}: the landing page {r.url} is on a host in no licence class "
                                 "(fetch its PDF explicitly with --licence-class once you have checked it is open)")
                    return None
                pdf, why = landing_pdf(r.body.decode("utf-8", errors="replace"), r.url)
                if not pdf:
                    if cand.via == "the given URL":
                        return r.body, "html", r.url, cls      # a web page named as the source (a Stacks tag)
                    tried.append(f"{cand.via} {r.url}: {why}")
                    return None
                landing_url = r.url
                r = self.client.get(pdf)
                r.body = sources.unwrap(r.body)
                kind = sources.sniff(r.body, r.header("content-type"))
                if kind == "html":
                    paywall.append(f"{pdf} (linked from {landing_url}) returned a web page, not the text: "
                                   "login or subscription required")
                    return None
        except FetchError as exc:
            (paywall if exc.paywalled else tried).append(f"{cand.via} {cand.url}: {exc}")
            if not exc.permanent:
                meta["_transient"] = True
            return None
        if kind not in DOCUMENT_KINDS:
            tried.append(f"{cand.via} {r.url}: returned {'a web page' if kind == 'html' else 'an unrecognised file'}, "
                         "not a document")
            return None
        if kind == "ps" and not sources.ghostscript():
            tried.append(f"{cand.via} {r.url}: a PostScript file, which needs Ghostscript (gs) to read: install "
                         "ghostscript on this host and fetch again")
            return None
        known = self.client.classify(r.url)
        cls = known if known not in ("unknown", "denied") else vouched(r.url) or known
        if cls in ("unknown", "denied"):
            tried.append(f"{cand.via} {r.url}: {host_of(r.url)} is in no licence class (pass --licence-class "
                         "after checking that it is a legal source)")
            return None
        return r.body, kind, r.url, cls

    def fetch(self, cit: Citation, *, licence_class: str = "") -> Outcome:
        meta: dict = {}
        notes: list[str] = []
        tried: list[str] = []
        paywall: list[str] = []
        seen: set[str] = set()
        for cand in self.candidates(cit, meta, notes):
            if cand.url and cand.url in seen:
                continue
            seen.add(cand.url)
            got = self._try(cand, licence_class, tried, paywall, meta)
            if not got:
                continue
            data, kind, final, cls = got
            path, txt, note = self.keep(data, kind, final or cand.via)
            now = self.now()
            canonical = (cand.canonical or (f"https://doi.org/{meta['doi']}" if meta.get("doi") else "")
                         or (f"https://arxiv.org/abs/{meta['arxiv']}" if meta.get("arxiv") else "") or final)
            entry = self._entry(meta, url=canonical, fetched_from=final, sha256=path.name.split(".", 1)[0],
                                licence_class=cls, status="certified", fetched_at=now.isoformat().replace("+00:00", "Z"),
                                accessed=now.date().isoformat())
            if cand.via == "arXiv":
                version = re.search(r"v(\d+)$", cand.url)
                entry["arxiv"] = cand.url.rsplit("/pdf/", 1)[-1]
                entry["version_note"] = f"arXiv v{version.group(1)}" if version else "arXiv, latest version"
            return Outcome("fetched", entry, path, txt, tried + [f"{cand.via}: fetched"], note)
        transient = bool(meta.get("_transient"))
        if meta.get("_paywalled"):
            paywall.append(meta["_paywalled"])
        if paywall and not transient:
            entry = self._entry(meta, url=(f"https://doi.org/{meta['doi']}" if meta.get("doi") else
                                           (cit.value if cit.kind == "url" else "")),
                                licence_class=sources.NO_TEXT, status="uncertified",
                                version_note="paywalled; cited by its locator without the text")
            return Outcome("uncertified", entry, tried=notes + tried + paywall,
                           note="the citation resolves only to a paywalled page")
        return Outcome("failed", self._entry(meta), tried=notes + tried + paywall, transient=transient,
                       note="network trouble: try again later" if transient else "no legal source found")

    @staticmethod
    def _entry(meta: dict, **fields) -> dict:
        year = str(meta.get("year", ""))
        entry = {"authors": meta.get("authors", ""), "title": meta.get("title", ""),
                 "year": int(year) if year.isdigit() else year, "venue": meta.get("venue", ""),
                 "doi": meta.get("doi", ""), "arxiv": meta.get("arxiv", "")}
        entry.update(fields)
        return {k: v for k, v in entry.items() if v not in ("", None)}

    # -- texts the manifest already records ---------------------------------------------------------
    def materialize(self, manifest: dict, offline: bool = False) -> list[Available]:
        """Make sure every certified text of a manifest is in the cache (downloading it again from
        where it was fetched, or finding it among the local texts, and checking its hash)."""
        out = []
        for s in manifest.get("source", []):
            sid, sha = str(s.get("id", "?")), str(s.get("sha256", ""))
            label = f"{s.get('authors', '')}, {s.get('title', '')}, {s.get('year', '')}".strip(", ")
            if s.get("status") == "local":
                out.append(self.local_text(sid, label, str(s.get("local_name", ""))))
                continue
            if s.get("status") != "certified" or not sources.SHA_RE.match(sha):
                continue
            file, txt = sources.cached_file(self.cache, sha), sources.cache_paths(self.cache, sha)[1]
            if file and txt.is_file():
                if sources.unreadable(txt) and self.ocr_ready():
                    # Extracted before OCR could run here: read the scan again.
                    data = file.read_bytes()
                    _, txt, _ = self.keep(data, sources.sniff(data), s.get("fetched_from", ""), sha)
                out.append(self.readable(sid, label, txt))
                continue
            try:
                if file:
                    data = file.read_bytes()
                    _, txt, _ = self.keep(data, sources.sniff(data), s.get("fetched_from", ""), sha)
                elif offline:
                    out.append(Available(sid, label, None, "not in the cache (dry run: not fetched)"))
                    continue
                elif s.get("licence_class") == "operator-local" and not s.get("fetched_from"):
                    local = local_by_hash(self.cfg.local_dirs, sha, self.cache / "local-index.json")
                    if not local:
                        # Not gone: another operator holds it, and it never travels; no request.
                        out.append(Available(sid, label, None, needs_local(s.get("local_name") or sid)))
                        continue
                    data = sources.unwrap(local.read_bytes())
                    _, txt, _ = self.keep(data, sources.sniff(data), f"local text {local.name}", sha)
                elif s.get("fetched_from"):
                    r = self.client.get(s["fetched_from"])
                    r.body = sources.unwrap(r.body)
                    _, txt, _ = self.keep(r.body, sources.sniff(r.body, r.header("content-type")), s["fetched_from"], sha)
                else:
                    out.append(Available(sid, label, None, "no fetched_from to download it from", True))
                    continue
            except sources.HashMismatch as exc:
                out.append(Available(sid, label, None, f"the text at {s.get('fetched_from') or 'its source'} changed ({exc})", True))
                continue
            except FetchError as exc:
                out.append(Available(sid, label, None, str(exc), exc.permanent))
                continue
            out.append(self.readable(sid, label, txt))
        return out

    def ocr_ready(self) -> bool:
        return bool(self.cfg.ocr) and not sources.ocr_status()

    @staticmethod
    def readable(sid: str, label: str, txt: Path, sha256: str = "") -> Available:
        """The text, or, for a scan this host could not read (image-only, OCR off or missing), a
        permanent problem: the job asks for it again under that name rather than as missing."""
        if sources.unreadable(txt):
            return Available(sid, label, None, sources.UNREADABLE, True, sha256)
        return Available(sid, label, txt, sha256=sha256)

    def local_text(self, sid: str, label: str, name: str) -> Available:
        """A `local` entry: the operator's file of that name, hashed and kept in the cache."""
        local = local_by_name(self.cfg.local_dirs, name)
        if not local:
            return Available(sid, label, None, needs_local(name or sid))
        data = sources.unwrap(local.read_bytes())
        path, txt, _ = self.keep(data, sources.sniff(data), f"local text {local.name}")
        return self.readable(sid, label, txt, path.name.split(".", 1)[0])


def needs_local(name: str) -> str:
    return f"needs local source {name} (an operator-local text this host does not hold)"


@dataclass
class Available:
    id: str
    label: str
    text: Path | None
    problem: str = ""
    permanent: bool = False
    sha256: str = ""         # a `local` text: the hash of the file read, for the manifest
