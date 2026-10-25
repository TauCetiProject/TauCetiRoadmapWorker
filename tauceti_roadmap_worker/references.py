"""The `needs-references` hand-off, end to end.

Worker side. When an agent cannot read a source legally, or the worker cannot fetch a text the
manifest records, the job's scratch directory gets `needs-references.toml`:

    [[missing]]
    id = "Ket67"                       # the key the README cites it by (derived when absent)
    authors = "A. Author"
    title = "On the thing"
    year = 1967
    needed_for = "Theorem 2.3 (the comparison map), Layer 2"
    tried = ["publisher page (paywalled)", "arXiv (no preprint)"]
    doi = "10.1234/x"                  # optional
    locator_scheme = "theorem"         # optional
    locator = "Satz 2.3"               # optional
    sha256 = "<64 hex>"                # optional: the text the manifest recorded, when it was lost

The lane posts the request rendered from `.github/ISSUE_TEMPLATE/needs-references.md`, with the
items in a machine-readable marker, and parks the job in `state:needs-references`.

Board side (`board intake`, `board references`). For each parked job the intake reads the comments
after the latest request, from the author associations `[sources] answer_associations` allows (the
text of anyone else is ignored, and every comment is data: only the three answer forms below are
read), one line at a time:

    source: KEY <DOI | arXiv id | URL>               an alternative legal source, fetched by the service
    local: KEY <file name> [sha256: <hex>]          a text operators hold in `[sources] local_dirs`
    waive: KEY[, KEY ...]                           cite by locator alone

Sources never enter a repository or an issue: an attachment is never downloaded, and a comment that
carries one gets a note asking for one of the three answers. A named source goes through the same
policy as `sources fetch`; a local text is recorded as `operator-local` with its file name (status
`local`, or `certified` when the hash is given), and the job records the requirement, so that only
an operator who holds the file resumes it; a waiver is recorded as `status = "waived"`. The
roadmap's `sources.toml` on the packages repository is updated, and when every requested key is
answered the job returns to `state:available`.

A `source:` answer whose fetch failed for network trouble is tried again on later runs, at most
`[sources] answer_retries` times with growing spacing (the attempts are recorded in the job's spec
under `reference_retries`), with one note on the first failure and none in between; when the last
attempt fails, or the failure is not network trouble, the request is raised again for the keys
still missing and the answer is forgotten. A new answer for the key replaces a pending retry.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import tomllib
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import board, fetcher, sources
from .config import LICENCE_CLASSES
from .gh import GHError
from .paths import ISSUE_TEMPLATES

_TEMPLATES: Path | None = None   # a worker's startup snapshot (build.snapshot)


def use_templates(root: Path | None) -> None:
    global _TEMPLATES
    _TEMPLATES = Path(root) if root else None

REQUEST_MARK = "tauceti-roadmap-needs-references"
PROGRESS_MARK = "tauceti-roadmap-references-progress"
REQUEST_RE = re.compile(r"<!--\s*" + REQUEST_MARK + r"\s*(\{.*?\})\s*-->", re.S)
PROGRESS_RE = re.compile(r"<!--\s*" + PROGRESS_MARK + r"\s*(\{.*?\})\s*-->", re.S)
RETRY_MINUTES = 10            # a failed `source:` answer waits this long before its first retry, twice as long each time
RETRIES = "reference_retries"  # the job spec's record of the retries pending: key -> {comment, failed, at}
STAMP = "%Y-%m-%dT%H:%M:%SZ"
# Recognised only to say that attachments are not used: a source never enters an issue.
ATTACHMENT = re.compile(r"https://github\.com/(?:user-attachments/(?:files|assets)/[^\s)\]>\"'<]+"
                        r"|[\w.-]+/[\w.-]+/files/\d+/[^\s)\]>\"'<]+)")
NO_ATTACHMENTS = ("attachments are not used (sources never enter a repository or an issue): answer with "
                  "`local: KEY <file name>`, `source: KEY <DOI, arXiv id or URL>` or `waive: KEY`")
FILE_NAME = re.compile(r"^[^/\\]+$")
KEY = r"[^\s,:;`*]+"
SHA = re.compile(r"\bsha256\s*[:=]\s*([0-9a-fA-F]{64})\b")


# --- the request ----------------------------------------------------------------------------------

@dataclass
class MissingReference:
    authors: str
    title: str
    year: str
    needed_for: str
    tried: list[str] = field(default_factory=list)
    note: str = ""
    key: str = ""
    doi: str = ""
    url: str = ""
    locator_scheme: str = ""
    locator: str = ""
    sha256: str = ""


def tried_list(value) -> list[str]:
    """`tried` as a list of strings: an agent may write one string instead of a list (then it was
    iterated character by character: "A, u, t, h, o, r"), and a list that a string was split into
    (every item one character) is joined back."""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(t) for t in value]
        if len(items) > 1 and all(len(t) <= 1 for t in items):
            items = ["".join(items)]
    else:
        items = []
    return [t.strip() for t in items if t.strip()]


def parse_missing(data: dict, existing_keys: set[str] | None = None) -> list[MissingReference]:
    """The [[missing]] tables of a needs-references.toml; items without a title or a `needed_for`
    are dropped, and items without a key get one derived from authors and year."""
    out, taken = [], set(existing_keys or set())
    for item in data.get("missing", []):
        if not isinstance(item, dict):
            continue
        m = MissingReference(authors=str(item.get("authors", "")), title=str(item.get("title", "")),
                             year=str(item.get("year", "")), needed_for=str(item.get("needed_for", "")),
                             tried=tried_list(item.get("tried", [])), note=str(item.get("note", "")),
                             key=str(item.get("id") or item.get("key") or ""), doi=str(item.get("doi", "")),
                             url=str(item.get("url", "")), locator_scheme=str(item.get("locator_scheme", "")),
                             locator=str(item.get("locator", "")), sha256=str(item.get("sha256", "")).lower())
        if not (m.title and m.needed_for):
            continue
        if not m.key:
            m.key = sources.citation_key(m.authors, m.year, taken)
        taken.add(m.key)
        out.append(m)
    return out


SETTLED = "settled"


def settled_status(entry: dict, uncertified_ok: bool) -> str:
    """'waived', 'uncertified' or 'local' when a manifest entry is settled (final until a human
    answers it again with `source:` or `local:`), else ''. A `local:` answer is settled: a worker whose
    operator holds the file reads it, and one whose operator does not never takes the job."""
    status = str(entry.get("status", ""))
    if status == "waived" or (status == "uncertified" and uncertified_ok):
        return status
    return "local" if status == "local" or entry.get("licence_class") == "operator-local" else ""


# --- the identity of a source ----------------------------------------------------------------------
#
# A source's identity is not its manifest key: an agent that renames a settled work (`HR-SIEVE`,
# then `HR-1974`; `KM-16RANK-published`, then `KM-KERNEL-PUBLISHED`) must not cost a further human
# answer. Two citations name the same work when they share a DOI, an arXiv id, a normalised title
# together with the first author's surname, or (the first author, the year within one, and a title
# the other contains or starts with) - a bibliographic line as a title, a preprint and its published
# version.

_MATH_FOLD = str.maketrans({"√": " sqrt ", "≡": " congruent ", "∞": " infinity ", "∑": " sum ", "∏": " prod ",
                            "π": " pi ", "ζ": " zeta ", "ℓ": " l ", "′": "", "ʹ": ""})
_PREFIX_WORDS = 6      # a shared leading run of this many title words, with author and year, is the same work


def normalise_title(title) -> str:
    """Lowercase ASCII words: diacritics and punctuation stripped, Unicode math folded (ℚ -> q,
    √ -> sqrt, ≡ -> congruent), so that 'ℚ(√-2p)' and 'Q(sqrt(-2p))' read the same."""
    text = unicodedata.normalize("NFKD", str(title or "").translate(_MATH_FOLD))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def normalise_doi(doi) -> str:
    text = str(doi or "").strip().lower()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text)
    return text.rstrip("/.") if text.startswith("10.") else ""


def arxiv_of(*values) -> str:
    """The arXiv id (without its version) in the first of `values` that carries one."""
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        m = fetcher.ARXIV_URL.match(text) or fetcher.ARXIV_DOI.match(text)
        found = m.group(1) if m else text.removeprefix("arXiv:").removeprefix("arxiv:").strip()
        if fetcher.ARXIV_ID.match(found):
            return re.sub(r"v\d+$", "", found).lower()
    return ""


def first_surname(authors) -> str:
    names = sources._surnames(str(authors or ""))
    return normalise_title(names[0]).replace(" ", "") if names else ""


def _year(value) -> int | None:
    found = re.findall(r"\d{4}", str(value or ""))
    return int(found[-1]) if found else None


def _fields(x) -> dict:
    """The identifying fields of a request item or a manifest entry."""
    if isinstance(x, MissingReference):
        return {"id": x.key, "doi": x.doi, "arxiv": arxiv_of(x.url, x.doi), "title": x.title, "authors": x.authors,
                "year": x.year}
    return {"id": str(x.get("id", "")), "doi": str(x.get("doi", "")),
            "arxiv": arxiv_of(x.get("arxiv"), x.get("url"), x.get("doi")), "title": str(x.get("title", "")),
            "authors": str(x.get("authors", "")), "year": x.get("year", "")}


def _title_overlap(a: str, b: str) -> bool:
    """`a` and `b` normalised: one contains the other (two words or more) as a run of words, or they
    share their first `_PREFIX_WORDS` words."""
    wa, wb = a.split(), b.split()
    if not wa or not wb:
        return False
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(short) >= 2 and any(long_[i:i + len(short)] == short for i in range(len(long_) - len(short) + 1)):
        return True
    return len(short) >= _PREFIX_WORDS and wa[:_PREFIX_WORDS] == wb[:_PREFIX_WORDS]


def same_source(a, b) -> bool:
    """Whether a request item and a manifest entry (or two of either) cite the same work: by key,
    by DOI, by arXiv id, by normalised title with the first author's surname, or by the first
    author, the year (within one: a preprint and its published version) and a title the other
    contains or starts with."""
    x, y = _fields(a), _fields(b)
    if x["id"] and x["id"].lower() == y["id"].lower():
        return True
    if normalise_doi(x["doi"]) and normalise_doi(x["doi"]) == normalise_doi(y["doi"]):
        return True
    if x["arxiv"] and x["arxiv"] == y["arxiv"]:
        return True
    ta, tb = normalise_title(x["title"]), normalise_title(y["title"])
    sa, sb = first_surname(x["authors"]), first_surname(y["authors"])
    same_author = sa == sb if sa and sb else True
    if ta and ta == tb and same_author:
        return True
    ya, yb = _year(x["year"]), _year(y["year"])
    if sa and sb and sa == sb and ya is not None and yb is not None and abs(ya - yb) <= 1:
        return _title_overlap(ta, tb)
    return False


def drop_settled(items: list[MissingReference], manifests: list[dict], uncertified_ok: bool
                 ) -> tuple[list[MissingReference], list[tuple[str, str, str]]]:
    """The request items to keep, and (key, status, manifest key) of those a manifest already settled
    (waived, local, or uncertified when the project accepts it): a settled citation is never requested
    again, under its key or any other (`same_source`)."""
    entries = [e for m in manifests for e in m.get("source", []) if isinstance(e, dict)]
    kept, dropped = [], []
    for item in items:
        hit = next(((st, str(e.get("id", ""))) for e in entries
                    if same_source(item, e) and (st := settled_status(e, uncertified_ok))), None)
        if hit:
            dropped.append((item.key, hit[0], hit[1]))
        else:
            kept.append(item)
    return kept, dropped


REREQUESTS = 1    # a text a human answered may be asked for again this many times; the next is auto-settled


def answered_entry(item: MissingReference, manifests: list[dict]) -> dict | None:
    """The manifest entry a human answered (it carries a `resolution`) for the work `item` cites."""
    for m in manifests:
        for e in m.get("source", []):
            if isinstance(e, dict) and e.get("resolution") and same_source(item, e):
                return e
    return None


def settled_entry(item: MissingReference, manifest: dict, uncertified_ok: bool) -> dict | None:
    """A settled manifest entry (waived, local, or uncertified when accepted) for the work `item`
    cites, under its key or another, or None."""
    for e in manifest.get("source", []):
        if isinstance(e, dict) and same_source(item, e) and settled_status(e, uncertified_ok):
            return e
    return None


def apply_settled(item: MissingReference, entry: dict) -> Resolution:
    """The earlier answer for `entry` applied to the requested key as well, so the citation under
    the new key keeps working while the roadmap converges on one key per source."""
    old = str(entry.get("id", ""))
    copied = {k: v for k, v in entry.items() if k != "cited_for"}
    copied["id"] = item.key
    for k in ("locator_scheme", "locator"):
        if getattr(item, k) and not copied.get(k):
            copied[k] = getattr(item, k)
    status = str(entry.get("status", ""))
    return Resolution(item.key, True, f"the same work as `{old}`, settled earlier ({status}): the earlier answer "
                                      f"applies; one key per source: cite it as `{old}`", copied)


def drop_capped(items: list[MissingReference], manifests: list[dict], earlier: list[list[MissingReference]]
                ) -> tuple[list[MissingReference], list[tuple[str, str, str]]]:
    """The items to keep, and (key, status, manifest key) of those not requested again: a text a human
    already answered that this job's earlier requests listed more than `REREQUESTS` times (by
    identity, whatever the key) is settled with the earlier answer instead of a further request."""
    kept, dropped = [], []
    for item in items:
        entry = answered_entry(item, manifests)
        asked = sum(any(same_source(item, m) for m in request) for request in earlier)
        if entry is not None and asked > REREQUESTS:
            dropped.append((item.key, str(entry.get("status", "")), str(entry.get("id", ""))))
        else:
            kept.append(item)
    return kept, dropped


def settle_line(dropped: list[tuple[str, str, str]]) -> str:
    """The round-log words for keys settled without a request, after 'settled in the manifest':
    `: key (status)` for keys the manifest has, `as <manifest key>: <key> (status)` for renamed ones."""
    plain = [f"{k} ({st})" for k, st, old in dropped if k.lower() == old.lower()]
    renamed = [f" as {old}: {k} ({st})" for k, st, old in dropped if k.lower() != old.lower()]
    return ";".join(([": " + ", ".join(plain)] if plain else []) + renamed)


def earlier_requests(comments: list[dict], job_id: str, trusted: list[str], bot_logins: list[str]
                     ) -> list[list[MissingReference]]:
    """The items of every needs-references request this job posted on its issue (by the board's
    bots, an operator or an allowed author association), oldest first. A request the intake raised
    again after a failed answer is the same request, not a further one."""
    out = []
    for c in comments:
        if (c.get("user") or {}).get("login") not in bot_logins and c.get("author_association") not in trusted:
            continue
        request = decode_request(c.get("body") or "")
        if request and request["job"] == job_id and not request["again"]:
            out.append(list(request["items"]))
    return out


def request_count(comments: list[dict], job_id: str, trusted: list[str], bot_logins: list[str]) -> int:
    """How many needs-references requests this job has posted on its issue (see `earlier_requests`)."""
    return len(earlier_requests(comments, job_id, trusted, bot_logins))


def read_missing(path: Path) -> list[MissingReference]:
    try:
        return parse_missing(tomllib.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError):
        return []


def dump_missing(items: list[MissingReference]) -> str:
    blocks = []
    for m in items:
        data = {"id": m.key, **{k: v for k, v in asdict(m).items() if k != "key"}}
        blocks.append("[[missing]]\n" + "".join(f"{k} = {sources.toml_value(v)}\n" for k, v in data.items()
                                                 if v not in ("", [])))
    return "\n".join(blocks)


def add_missing(path: Path, item: MissingReference) -> None:
    """Add an item to a needs-references.toml (merging `tried` into an item with the same key or title)."""
    items = read_missing(path)
    for m in items:
        if m.key.lower() == item.key.lower() or m.title.lower() == item.title.lower():
            m.tried = list(dict.fromkeys(m.tried + item.tried))
            break
    else:
        if not item.key:
            item.key = sources.citation_key(item.authors, item.year, {m.key for m in items})
        items.append(item)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dump_missing(items), encoding="utf-8")


def _template_body() -> str:
    text = ((_TEMPLATES or ISSUE_TEMPLATES) / "needs-references.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip() + "\n"


def _marker(mark: str, data: dict) -> str:
    return f"<!-- {mark} {board.comment_json(data)} -->"


def request_comment(job_id: str, roadmap: str, items: list[MissingReference], again: bool = False) -> str:
    """The comment the lane posts when it parks a job: the template's table for humans, the items in a
    marker for the intake. `again`: the intake raising the request again for the keys still missing."""
    def cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ").replace("<!--", "&lt;!--").strip()[:300]
    rows = "\n".join(f"| `{cell(m.key)}` | {cell(m.authors)} | {cell(m.title)} | {cell(m.year)} | {cell(m.needed_for)} | "
                     f"{cell('; '.join(tried_list(m.tried))) or 'none'} |" for m in items)
    body = (_template_body().replace("__JOB__", job_id).replace("__ROADMAP__", roadmap)
            .replace("__ROWS__", rows or "| | | | | | |"))
    return body + "\n" + _marker(REQUEST_MARK, {"job": job_id, "roadmap": roadmap, "items": [asdict(m) for m in items],
                                                **({"again": True} if again else {})}) + "\n"


def _item(raw) -> MissingReference | None:
    """A request item, or None when it is malformed (every field a string, `tried` a list of
    strings or one string, a usable key and a title)."""
    if not isinstance(raw, dict):
        return None
    fields = {}
    for name in MissingReference.__dataclass_fields__:
        value = raw.get(name, [] if name == "tried" else "")
        if name == "tried":
            if isinstance(value, list) and not all(isinstance(t, str) for t in value):
                return None
            if not isinstance(value, (list, str)):
                return None
            value = tried_list(value)
        elif isinstance(value, int) and not isinstance(value, bool) and name == "year":
            value = str(value)
        elif not isinstance(value, str):
            return None
        fields[name] = value
    item = MissingReference(**fields)
    return item if re.fullmatch(KEY, item.key) and item.title else None


def decode_request(body: str) -> dict | None:
    """{job, roadmap, items: [MissingReference], again} from the last request marker in a comment, or
    None when there is none or it is malformed (no items, an item that is not well formed)."""
    found = REQUEST_RE.findall(body or "")
    if not found:
        return None
    try:
        data = json.loads(found[-1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        return None
    items = [_item(raw) for raw in data["items"]]
    if any(item is None for item in items) or not isinstance(data.get("job"), str):
        return None
    return {"job": data["job"], "roadmap": str(data.get("roadmap", "")), "items": items,
            "again": data.get("again") is True}


def latest_request(comments: list[dict], job_id: str, trusted: list[str], bot_logins: list[str]) -> tuple[int, dict | None]:
    """(index, request) of the latest well-formed request for this job posted by the board or an
    allowed author association; (-1, None) when there is none. Anyone else's marker is data."""
    for i in range(len(comments) - 1, -1, -1):
        c = comments[i]
        if (c.get("user") or {}).get("login") not in bot_logins and c.get("author_association") not in trusted:
            continue
        request = decode_request(c.get("body") or "")
        if request and request["job"] == job_id:
            return i, request
    return -1, None


# --- answers --------------------------------------------------------------------------------------

@dataclass
class Answer:
    key: str
    action: str              # source | local | waive
    value: str = ""          # the citation of the alternative source, or the local file name
    sha256: str = ""         # stated for a local file
    comment: int = 0
    url: str = ""            # the comment's link, recorded as the entry's resolution
    login: str = ""          # who answered
    licence: str = ""        # source: the licence class a trailing `licence: <class>` names


def _markdown_url(text: str) -> str:
    m = re.search(r"\]\((https?://[^)\s]+)\)", text)
    return m.group(1) if m else text.strip().strip("<>")


def parse_answers(body: str, keys: list[str]) -> tuple[list[Answer], list[str]]:
    """Answers in one comment, and notes on lines that name an unknown key, an unusable file name or
    an attachment (which is never used)."""
    canon = {k.lower(): k for k in keys}
    answers, notes = [], []
    for raw in (body or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", raw).strip()
        m = re.match(rf"(?i)^(attach|source|local|waive)\s*:\s*({KEY}(?:\s*,\s*{KEY})*)\s*(.*)$", line)
        if m:
            action, rest = m.group(1).lower(), m.group(3)
            if action == "attach":
                notes.append(f"`attach:` {NO_ATTACHMENTS}")
                continue
            named = [k.strip() for k in m.group(2).split(",")] if action == "waive" else [m.group(2)]
            for k in named:
                if k.lower() not in canon:
                    notes.append(f"`{k}` is not a key of this request")
                    continue
                if action == "waive":
                    answers.append(Answer(canon[k.lower()], "waive"))
                elif action == "source":
                    lic = re.search(r"(?i)\s+licen[cs]e\s*:\s*`?([\w-]+)`?\s*$", rest)
                    licence = lic.group(1).lower() if lic else ""
                    rest = rest[:lic.start()] if lic else rest
                    if licence and licence not in LICENCE_CLASSES:
                        notes.append(f"`source: {k}`: `licence: {licence}` is not a licence class "
                                     f"({', '.join(LICENCE_CLASSES)}); it is recorded as answered by its author")
                        licence = ""
                    if rest.strip():
                        answers.append(Answer(canon[k.lower()], "source", _markdown_url(rest), licence=licence))
                    else:
                        notes.append(f"`source: {k}` names no source")
                else:
                    sha = SHA.search(rest)
                    name = SHA.sub("", rest).strip().strip("`\"'")
                    if ATTACHMENT.search(rest):
                        notes.append(f"`local: {k}` {NO_ATTACHMENTS}")
                    elif name and FILE_NAME.match(name):
                        answers.append(Answer(canon[k.lower()], "local", name, sha.group(1).lower() if sha else ""))
                    else:
                        notes.append(f"`local: {k}` needs a file name (no directory): the file in `[sources] local_dirs`")
            continue
        if ATTACHMENT.search(line):
            notes.append(f"an attachment: {NO_ATTACHMENTS}")
    return answers, notes


def collect_answers(comments: list[dict], keys: list[str], trusted: list[str], skip_logins: list[str],
                    answerer=None) -> tuple[dict, list[str], set[int]]:
    """The latest answer per key, the notes, and the ids of the comments read. `local:` and `waive:`
    come from trusted associations or allowed answerers; `source:` only from an allowed answerer
    (`answerer(login)`; without the callback, from trusted associations as before), and anyone
    else's is noted and ignored. The board's own notes and other comments are skipped."""
    latest, notes, read = {}, [], set()
    for c in comments:
        login = (c.get("user") or {}).get("login", "")
        body = c.get("body") or ""
        if login in skip_logins or REQUEST_MARK in body or PROGRESS_MARK in body:
            continue
        member = c.get("author_association") in trusted
        if not member and not (answerer and answerer(login)):
            continue
        answers, found_notes = parse_answers(body, keys)
        if answers or found_notes:
            read.add(int(c.get("id") or 0))
        for a in answers:
            if a.action == "source" and answerer is not None and not answerer(login):
                notes.append(f"`source: {a.key}` from `{login}` is ignored: sources are named by the project's target "
                             "filers and operators (or the logins `[board] reference_answerers` lists)")
                continue
            a.comment, a.url, a.login = int(c.get("id") or 0), c.get("html_url", ""), login
            latest[a.key] = a
        notes += found_notes
    return latest, notes, read


def answerer_check(ctx):
    """`answerer(login)`: whether a login may answer with `source:` (cached per run). `[board]
    reference_answerers` when set (logins and teams), else the target filers and the operators
    (`[board] operators`, default the running login, and the logins the claims namespace records)."""
    from . import filers, mergeorder
    cfg, cache = ctx.cfg, {}
    entries = list(cfg.board.reference_answerers)
    has_state = getattr(ctx, "state", None) is not None     # teams and the claims namespace are read through it
    operators: set[str] | None = None

    def member(entry_list: list[str], who: str) -> bool:
        if any(e.lower() == who for e in entry_list if not e.startswith("team:")):
            return True
        return has_state and any(who in (filers.team_members(ctx, e[5:])[0] or set())
                                 for e in entry_list if e.startswith("team:"))

    def allowed(login: str) -> bool:
        nonlocal operators
        who = (login or "").lower()
        if not who:
            return False
        if who not in cache:
            if entries:
                cache[who] = member(entries, who)
            else:
                if operators is None:
                    operators = mergeorder.operators(ctx) if has_state else {
                        x.lower() for x in (cfg.board.operators or [getattr(ctx, "operator", "")]) if x}
                cache[who] = who in operators or member(list(cfg.board.target_filers), who)
        return cache[who]
    return allowed


# --- the board side -------------------------------------------------------------------------------

@dataclass
class Resolution:
    key: str
    ok: bool
    detail: str
    entry: dict | None = None
    transient: bool = False      # network trouble: the answer is tried again on a later run


def _base_entry(item: MissingReference, existing: dict | None) -> dict:
    """The entry an answer starts from: the manifest's (identity only: its old provenance goes) or
    the request's."""
    entry = {k: v for k, v in (existing or {}).items() if k not in sources.PROVENANCE}
    entry["id"] = item.key
    for k in ("authors", "title", "year", "doi", "locator_scheme", "locator"):
        value = getattr(item, k)
        if value and not entry.get(k):
            entry[k] = int(value) if k == "year" and str(value).isdigit() else value
    return entry


def resolve(answer: Answer, item: MissingReference, existing: dict | None, fet: fetcher.Fetcher,
            uncertified_ok: bool) -> Resolution:
    """Carry out one answer: fetch the named source, record the local text, or record the waiver.
    Nothing is ever downloaded from an issue."""
    base = _base_entry(item, existing)
    if answer.action == "waive":
        base.update(status="waived", licence_class=sources.NO_TEXT, resolution=answer.url)
        return Resolution(item.key, True, "waived: cited by its locator alone", base)
    if answer.action == "source":
        # The answerer vouches for the source they name: on its host, a class the project does not
        # list is theirs to give (a denied host is still refused).
        vouch = answer.licence or (f"answered-by:{answer.login}" if answer.login else "")
        out = fet.fetch(fetcher.parse_citation(answer.value), licence_class=vouch)
        if out.status == "fetched":
            entry = {**base, **{k: v for k, v in out.entry.items() if k not in ("authors", "title", "year") or not base.get(k)}}
            entry.update(resolution=answer.url)
            return Resolution(item.key, True, f"fetched from {out.entry.get('fetched_from') or 'a local text'} "
                                              f"(sha256 {out.entry['sha256'][:12]}…)", entry)
        if out.status == "uncertified":
            entry = {**base, **out.entry, "id": item.key, "resolution": answer.url}
            return Resolution(item.key, uncertified_ok, "the named source is paywalled: recorded as uncertified"
                              + ("" if uncertified_ok else ", which this project does not accept"), entry)
        return Resolution(item.key, False, "the named source could not be fetched: " + "; ".join(out.tried[-3:]),
                          transient=out.transient)
    if answer.action == "local":
        # The text stays on the operators' machines: the board records its name (and hash, if given);
        # the lane that resumes the job finds it in `[sources] local_dirs` and reads it there.
        for key in sources.PROVENANCE:
            base.pop(key, None)
        base.update(licence_class="operator-local", local_name=answer.value, resolution=answer.url)
        if answer.sha256:
            base.update(status="certified", sha256=answer.sha256)
        else:
            base.update(status="local")
        recorded = item.sha256 or str((existing or {}).get("sha256", ""))
        detail = f"held locally as `{answer.value}`" + (f" (sha256 {answer.sha256[:12]}…)" if answer.sha256 else "")
        if recorded and answer.sha256 and recorded != answer.sha256:
            base["version_note"] = (f"the local text differs from the one recorded earlier (sha256 {recorded[:12]}…): "
                                    "its locators must be checked again")
            detail += "; it differs from the text recorded earlier, so its locators must be checked again"
        return Resolution(item.key, True, detail + "; only an operator who holds it resumes the job", base)
    return Resolution(item.key, False, f"unknown answer {answer.action!r}")


def board_manifest(ctx, name: str) -> tuple[dict, str | None]:
    """The roadmap's sources.toml on the packages repository's main branch, and its blob sha."""
    path = f"{board.package_dir(ctx.cfg.board.packages_dir, name)}/sources.toml"
    try:
        current = ctx.gh.api(f"repos/{ctx.cfg.repositories.packages_repo()}/contents/{path}")
    except GHError:
        return sources.new_manifest(name), None
    try:
        return tomllib.loads(base64.b64decode(current["content"]).decode("utf-8")), current["sha"]
    except (KeyError, ValueError, tomllib.TOMLDecodeError):
        return sources.new_manifest(name), current.get("sha")


def write_board_manifest(ctx, name: str, data: dict, sha: str | None, message: str) -> None:
    path = f"{board.package_dir(ctx.cfg.board.packages_dir, name)}/sources.toml"
    payload = {"message": message, "content": base64.b64encode(sources.dump_manifest(data).encode()).decode()}
    if sha:
        payload["sha"] = sha
    ctx.gh.write("PUT", f"repos/{ctx.cfg.repositories.packages_repo()}/contents/{path}", payload)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def retry_due(record: dict, now: dt.datetime) -> dt.datetime:
    """When a `source:` answer that failed for network trouble is tried again: RETRY_MINUTES after
    its first failure, twice as long after each later one (a record without a readable time or
    count is due now)."""
    try:
        last = dt.datetime.strptime(str(record.get("at", "")), STAMP).replace(tzinfo=dt.timezone.utc)
        failed = max(int(record.get("failed", 1)), 1)
    except (ValueError, TypeError):
        return now
    return last + dt.timedelta(minutes=RETRY_MINUTES * 2 ** (failed - 1))


def pending_retries(job: board.Job, items: dict, answers: dict, done: dict) -> dict[str, dict]:
    """The `source:` answers whose fetch failed for network trouble and are still to be tried again,
    key -> {comment, failed, at}, from the job's spec. A record whose key is answered again (by a
    later comment), resolved, or no longer requested is dropped: the new answer takes over."""
    out = {}
    stored = job.inputs.get(RETRIES)
    for key, rec in (stored.items() if isinstance(stored, dict) else ()):
        if not isinstance(rec, dict) or key not in items or key in done or key not in answers:
            continue
        if answers[key].action != "source" or answers[key].comment != rec.get("comment"):
            continue
        out[key] = dict(rec)
    return out


def resume_job(ctx, job: board.Job, fet: fetcher.Fetcher, yes: bool = False) -> str:
    """Answer a parked job from its comments; release it when every requested key is resolved.
    A `source:` answer that failed for network trouble is tried again on later runs (see
    `pending_retries`); when its last attempt fails, or an answer fails for any other reason, the
    request is raised again for the keys still missing."""
    comments = ctx.board.comments(job.number)
    s = ctx.cfg.sources
    at, request = latest_request(comments, job.id, s.request_associations, ctx.cfg.board.bot_logins)
    if request is None:
        return f"{job.id}: parked without a request the board can read; a maintainer releases it by hand"
    items = {m.key: m for m in request["items"]}
    after = comments[at + 1:]
    handled: set[int] = set()
    done: dict[str, str] = {}
    for c in after:
        mark = PROGRESS_RE.findall(c.get("body") or "")
        if mark and (c.get("author_association") in s.answer_associations
                     or (c.get("user") or {}).get("login") in ctx.cfg.board.bot_logins):
            try:
                data = json.loads(mark[-1])
                handled |= {int(x) for x in data.get("handled", [])}
                done.update({str(k): str(v) for k, v in data.get("resolved", {}).items()})
            except (ValueError, TypeError):
                pass
    answers, notes, read = collect_answers(after, list(items), s.answer_associations, ctx.cfg.board.bot_logins,
                                           answerer_check(ctx))
    now = _now()
    attempts = s.answer_retries + 1                      # the first try and the retries
    retries = pending_retries(job, items, answers, done)
    due = {k for k, r in retries.items() if retry_due(r, now) <= now}
    fresh = {k: a for k, a in answers.items() if a.comment not in handled or k in due}
    manifest, blob = board_manifest(ctx, job.name)
    # A requested key that names a work the manifest already settled under another key (the same
    # DOI, arXiv id or title: a rename) needs no human answer: the earlier answer is applied to it.
    settled = {k: e for k, m in items.items() if k not in done and k not in fresh
               and (e := settled_entry(m, manifest, s.uncertified_ok)) is not None}
    if not fresh and not (read - handled) and not settled:
        waiting = [k for k in items if k not in done]
        if waiting:
            later = [f"`{k}` at {retry_due(r, now).strftime(STAMP)} (attempt {r['failed'] + 1} of {attempts})"
                     for k, r in retries.items()]
            return (f"{job.id}: waiting for an answer on {', '.join(waiting)}"
                    + (f"; the source named for {', '.join(later)} is tried again" if later else ""))
        # Everything was answered on an earlier run whose release did not go through: release now.
        if not yes:
            return f"{job.id}: every key answered; would release the job (dry run; pass --yes)"
        ctx.board.set_state(job, board.transition("needs-references", "references-resolved"))
        return f"{job.id}: references resolved earlier; available again"
    results = [apply_settled(items[key], entry) for key, entry in settled.items()]
    for res in results:
        sources.upsert_source(manifest, res.entry, replace=True)
        done[res.key] = res.detail
    for key, answer in fresh.items():
        res = resolve(answer, items[key], sources.find_source(manifest, id=key), fet, s.uncertified_ok)
        results.append(res)
        if res.entry:
            sources.upsert_source(manifest, res.entry, replace=True)   # the entry already starts from the old one
        if res.ok:
            done[key] = res.detail
            retries.pop(key, None)
        elif res.transient:
            retries[key] = {"comment": answer.comment, "failed": retries.get(key, {}).get("failed", 0) + 1,
                            "at": now.strftime(STAMP)}
    changed = [r.key for r in results if r.entry]
    missing = [k for k in items if k not in done]
    exhausted = {k for k, r in retries.items() if r["failed"] >= attempts}
    permanent = {r.key for r in results if not r.ok and not r.transient}
    again = exhausted | permanent                        # these answers are forgotten: the request is raised again
    first = {r.key for r in results if r.transient and r.key in retries and retries[r.key]["failed"] == 1}
    # A run that only tried a pending retry again, and failed again, says nothing on the issue.
    quiet = not again and not notes and not (read - handled) and all(r.transient and r.key not in first for r in results)

    def line(r: Resolution) -> str:
        if r.ok:
            return f"- `{r.key}`: {r.detail}"
        if r.key in exhausted:
            n = retries[r.key]["failed"]
            return f"- `{r.key}`: {r.detail} (given up after {n} attempt{'s' if n != 1 else ''})"
        if r.transient:
            return (f"- `{r.key}`: {r.detail}; it is tried again up to {s.answer_retries} times, at least "
                    f"{RETRY_MINUTES} minutes apart; to replace it, answer again with `source: {r.key} <DOI, arXiv id "
                    f"or URL>`, `local: {r.key} <file name>` or `waive: {r.key}`")
        return f"- `{r.key}`: {r.detail} (not tried again)"
    lines = [f"Intake: answers to the references request of `{job.id}`:", ""]
    lines += [line(r) for r in results]
    lines += [f"- note: {n}" for n in notes]
    if again:
        keys = ", ".join(f"`{k}`" for k in sorted(again))
        lines += ["", f"The request is raised again below for every key still missing; the answer{'s' if len(again) > 1 else ''} "
                  f"for {keys} {'are' if len(again) > 1 else 'is'} forgotten: answer again (another `source:`, `local:` or "
                  "`waive:`).", "",
                  request_comment(job.id, request["roadmap"], [items[k] for k in missing], again=True).rstrip()]
        retries = {}
    elif missing:
        lines += ["", "Still needed: " + ", ".join(f"`{k}`" for k in missing) + ". Answer with "
                  "`source: KEY <DOI, arXiv id or URL> [licence: <class>]`, `local: KEY <file name>` or `waive: KEY`."]
        lines += ["", _marker(PROGRESS_MARK, {"handled": sorted(handled | read), "resolved": done})]
    else:
        lines += ["", "Every requested source is answered; the job is available again."]
        lines += ["", _marker(PROGRESS_MARK, {"handled": sorted(handled | read), "resolved": done})]
    if not yes:
        return f"{job.id}: would record {', '.join(changed) or 'nothing'}" + (
            f"; would raise the request again for {', '.join(missing)}" if again else
            f"; still missing {', '.join(missing)}" if missing else "; would release the job") + " (dry run; pass --yes)"
    if changed:
        write_board_manifest(ctx, job.name, manifest, blob,
                             f"Sources: answers to the references request of {job.id} ({', '.join(changed)})")
    record_local_needs(ctx, job, results, retries)
    if not quiet:
        ctx.board.comment(job.number, "\n".join(lines))
    if not missing:
        ctx.board.set_state(job, board.transition("needs-references", "references-resolved"))
        return f"{job.id}: references resolved; available again"
    if again:
        return (f"{job.id}: recorded {', '.join(changed) or 'nothing'}; the request is raised again for "
                f"{', '.join(missing)} (the source named for {', '.join(sorted(again))} could not be fetched)")
    if quiet:
        tried = ", ".join(f"`{k}` (attempt {retries[k]['failed']} of {attempts}, next at "
                          f"{retry_due(retries[k], now).strftime(STAMP)})" for k in fresh)
        return f"{job.id}: the source named for {tried} could not be fetched again; still missing {', '.join(missing)}"
    return f"{job.id}: recorded {', '.join(changed) or 'nothing'}; still missing {', '.join(missing)}"


def local_needs(job: board.Job) -> list[dict]:
    """The local texts a job may be resumed with only by an operator who holds them:
    [{key, name, sha256}] from its spec."""
    return [n for n in job.inputs.get("local_sources", []) if isinstance(n, dict) and n.get("name")]


def record_local_needs(ctx, job: board.Job, results: list[Resolution], retries: dict | None = None) -> None:
    """Keep the job's spec in step with its answers: a `local:` answer adds its file to the job's
    requirements, any other answer for the key drops it; `retries` (when given) replaces the record
    of the `source:` answers still to be tried again. Written before the job is released."""
    needs = {n["key"]: n for n in local_needs(job)}
    before = dict(needs)
    for r in results:
        if r.ok and r.entry and r.entry.get("local_name"):
            needs[r.key] = {"key": r.key, "name": r.entry["local_name"], "sha256": r.entry.get("sha256", "")}
        elif r.ok:
            needs.pop(r.key, None)
    stored = job.inputs.get(RETRIES) if isinstance(job.inputs.get(RETRIES), dict) else {}
    if needs == before and (retries is None or retries == stored):
        return
    if needs:
        job.inputs["local_sources"] = list(needs.values())
    else:
        job.inputs.pop("local_sources", None)
    if retries is not None:
        if retries:
            job.inputs[RETRIES] = retries
        else:
            job.inputs.pop(RETRIES, None)
    ctx.board.update_spec(job)


def resume_all(ctx, fet: fetcher.Fetcher, yes: bool = False) -> list[str]:
    """Every parked job, each on its own: one job's trouble never stops the others."""
    out = []
    for job in ctx.board.list_jobs(("needs-references",)):
        try:
            out.append(resume_job(ctx, job, fet, yes))
        except Exception as exc:  # noqa: BLE001 - reported per job; the intake goes on with the rest
            out.append(f"{job.id}: {type(exc).__name__}: {exc}")
    return out
