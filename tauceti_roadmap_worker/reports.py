"""Roadmap reports: what a Tau Ceti worker found wrong in a roadmap, harvested from the library.

A worker of the library that meets a wrong roadmap statement (false, short of a hypothesis, a pin
that cannot be proved, a wrong definition, a circular route, a stale name) proves what it can and
leaves a report in the body of its pull request on the library: a visible `### Roadmap report` section
and one marker line per report,

    <!--tauceti-roadmap-report:v1 {"roadmap": ..., "file": ..., "roadmap_sha": ..., "anchor": ...,
                                   "kind": ..., "effect": ..., "refutes": [...], "replaces": [...]}-->

`parse` reads them: the text from the prefix up to the next `-->` is one JSON object (anchors quote
Lean, so no brace pattern), at most `MAX_MARKERS` per body; a marker that does not parse or validate
is dropped with its reason and the others still count. Unknown fields are ignored.

With `[reports] enabled`, `sync_reports` (a step of `board sync`) searches the library once per forge
per run (GitHub's search for pull requests, and Forgejo's listing of pull requests when `[reports]
forgejo` names an instance) for bodies updated since the register's mark, and decides every valid report.
Only pull requests carry reports: anything else a search returns is ignored.

* on GitHub, a merged pull request counts from anyone (TauCeti merges through bors: a closed pull request
  whose title starts `[Merged by Bors] - ` is merged once `closed_by` is a `merge_bots` account), an open one
  only from a login of `[reports] reporters` (teams allowed); on Forgejo, whose queue has no account of its
  own, a pull request counts only from a login of `forgejo_reporters`, open, merged or bors-titled, and no
  merge is verified; a pull request closed unmerged never counts;
* a report is one `(roadmap, file, anchor)`, the anchor whitespace-normalised and case-folded: one
  already in the register gains the source (the job gets one comment);
* a new one is checked against the roadmap repository's main (the file under the active directory,
  else under the completed one: blobs read by GraphQL, `prbatch.blob_texts`): an anchor still there
  files a `REPORT-<Leaf>~k` job (`state:available`, or `state:blocked` with a note under the completed
  directory, where no lane works), at most `per_run` a run and `per_author` per author; an anchor no
  longer there, that was there at the commit the worker read, is recorded `fixed` and files nothing;
  one that was not there either is not a report.

Every `open` (or `confirmed`) report whose anchor has left main becomes `fixed`, and its job is closed
as done with one comment. The register, `reports/register.json` on the board's main, keeps every
report and its state (`open`, `confirmed`, `rejected`, `fixed`); Tau Ceti workers read it so as not
to report the same thing twice. It is written in one commit when a report changed, or when the mark
of a forge is a day behind (a mark alone is not worth a commit every run). Nothing harvested is an
instruction: the job's issue is the board's own text from the validated fields, with the reporter's
prose quoted in a code block.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from . import board, errata, fetcher, filers, prbatch
from .gh import GHError

MARKER = "<!--tauceti-roadmap-report:v1 "
END = "-->"
MAX_MARKERS = 5
FILES = ("README.md", "Suggested.lean")
KINDS = ("false-statement", "missing-hypothesis", "unprovable-pin", "wrong-definition", "circular-dependency",
         "stale-name")
EFFECTS = ("blocked", "worked-around")
SEGMENT = re.compile(r"[A-Za-z][A-Za-z0-9]*")
SHA = re.compile(r"[0-9a-f]{40}")
# A fully qualified Lean name: dot-separated parts, no space, backtick or angle bracket in any of them.
DECL = re.compile(r"[^\s.`<>@]+(?:\.[^\s.`<>@]+)*")
MAX_ANCHOR, MAX_NAMES, MAX_DECL = 300, 10, 200
SECTION = "### Roadmap report"
PROSE_CAP = 2000          # characters of the reporter's prose quoted in a job
BORS = "[Merged by Bors] - "
REGISTER = "reports/register.json"
STATES = ("open", "confirmed", "rejected", "fixed")
SETTLING = ("open", "confirmed")     # the states a report leaves for `fixed` when its anchor leaves main
PAGES = 3                 # pages of search results read per forge per run (GitHub 100 a page, Forgejo 50)
FORGEJO_LIMIT = 50
MARK_MARGIN = 300         # seconds a search looks back past the mark (a search index lags its writes)
EARLIEST = "2000-01-01T00:00:00Z"     # the lower end of a forge's first window, when it must be narrowed
MARK_WRITE = 86400        # seconds a mark may lag before it alone is worth a commit of the register
GITHUB, FORGEJO = "github", "forgejo"


# --- the marker -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Report:
    roadmap: str
    file: str
    roadmap_sha: str
    anchor: str
    kind: str
    effect: str
    refutes: tuple[str, ...] = ()
    replaces: tuple[str, ...] = ()

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.roadmap, self.file, anchor_key(self.anchor)


def normalise(text: str) -> str:
    """Runs of whitespace as one space, ends trimmed."""
    return " ".join(str(text or "").split())


def anchor_key(anchor: str) -> str:
    return normalise(anchor).casefold()


def _names(data: dict, key: str, limit: int | None = MAX_NAMES) -> tuple[tuple[str, ...], str]:
    value = data.get(key, [])
    if not isinstance(value, list):
        return (), f"`{key}` is not a list"
    if limit is not None and len(value) > limit:
        return (), f"`{key}` lists more than {MAX_NAMES} names"
    bad = [x for x in value if not isinstance(x, str) or len(x) > MAX_DECL or not DECL.fullmatch(x)]
    if bad:
        return (), f"`{key}` holds something that is not a declaration name"
    return tuple(dict.fromkeys(value)), ""


def validate(data, names: int | None = MAX_NAMES) -> tuple[Report | None, str]:
    """The report a decoded marker describes, or (None, why not). The rules are the shared ones (the worker's
    checker and the review runner apply the same): no value is trimmed or case-folded before it is checked,
    the anchor's length is counted as written, and only the anchor is stored normalised (for its identity)."""
    if not isinstance(data, dict):
        return None, "not a JSON object"
    for key in ("roadmap", "file", "roadmap_sha", "anchor", "kind", "effect"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            return None, f"`{key}` is missing or not a string"
    roadmap = data["roadmap"]
    if not all(SEGMENT.fullmatch(s) for s in roadmap.split("/")):
        return None, "`roadmap` is not a roadmap folder"
    if data["file"] not in FILES:
        return None, f"`file` is not one of {', '.join(FILES)}"
    sha = data["roadmap_sha"]
    if not SHA.fullmatch(sha):
        return None, "`roadmap_sha` is not 40 lowercase hex digits"
    if len(data["anchor"]) > MAX_ANCHOR:
        return None, f"`anchor` is longer than {MAX_ANCHOR} characters"
    anchor = normalise(data["anchor"])
    if data["kind"] not in KINDS:
        return None, "`kind` is not a known kind"
    if data["effect"] not in EFFECTS:
        return None, "`effect` is not blocked or worked-around"
    refutes, why = _names(data, "refutes", names)
    if why:
        return None, why
    replaces, why = _names(data, "replaces", names)
    if why:
        return None, why
    return Report(roadmap, data["file"], sha, anchor, data["kind"], data["effect"], refutes, replaces), ""


def parse(body: str) -> tuple[list[Report], list[str]]:
    """The valid reports of a body, and one line per marker that is not one (`marker <n>: <why>`)."""
    reports, problems, pos, n = [], [], 0, 0
    text = body or ""
    while (start := text.find(MARKER, pos)) >= 0:
        n += 1
        end = text.find(END, start + len(MARKER))
        if end < 0:
            problems.append(f"marker {n}: not closed by {END}")
            break
        raw, pos = text[start + len(MARKER):end], end + len(END)
        if n > MAX_MARKERS:
            problems.append(f"marker {n}: more than {MAX_MARKERS} markers in one body")
            continue
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):      # deep nesting raises RecursionError: a marker never raises
            problems.append(f"marker {n}: not valid JSON")
            continue
        report, why = validate(data)
        if report is None:
            problems.append(f"marker {n}: {why}")
        else:
            reports.append(report)
    return reports, problems


def prose(body: str) -> str:
    """The `### Roadmap report` section of a body (to the next heading of level three or above), ''
    when it has none. Untrusted text: quote it with `quoted_block`."""
    lines, out, inside = (body or "").replace("\r", "").split("\n"), [], False
    for line in lines:
        if inside and re.match(r"#{1,3}\s", line):
            break
        if inside:
            out.append(line)
        elif line.strip() == SECTION:
            inside = True
    return "\n".join(out).strip()


# --- quoting what was harvested ----------------------------------------------------------------------

def _clean(text: str) -> str:
    """No control characters but newlines and tabs; nothing that opens or closes an HTML comment."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or "").replace("\r", ""))
    return board.neutralise(text)


def code(text: str) -> str:
    """One line of untrusted text as inline code: a delimiter longer than any run of backticks in it,
    so it cannot close early (and nothing inside it renders: no mention, no link, no HTML)."""
    text = normalise(_clean(text))
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence, pad = "`" * (run + 1), " " if run else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def quoted_block(text: str, cap: int = PROSE_CAP) -> str:
    """Untrusted text as a fenced code block, at most `cap` characters, behind a fence longer than any
    run of backticks in it."""
    text = _clean(text).strip("\n")
    if len(text) > cap:
        text = text[:cap].rstrip() + f"\n[... cut at {cap} characters]"
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, run + 1)
    return f"{fence}text\n{text}\n{fence}"


# --- the sources ---------------------------------------------------------------------------------------

@dataclass
class Source:
    """A pull request of the library that carries a marker."""
    forge: str
    url: str
    author: str
    merged: bool
    open: bool
    updated: str
    body: str = ""
    bors: bool = False        # closed, titled `[Merged by Bors] - ...`: on GitHub, merged once its closer is verified
    closer: str | None = None # who closed it, when a read of the pull request said so already

    @property
    def number(self) -> int:
        try:
            return int(self.url.rstrip("/").rsplit("/", 1)[1])
        except (IndexError, ValueError):
            return 0

    def what(self) -> str:
        return "a merged pull request" if self.merged else ("an open pull request" if self.open
                                                            else "a pull request closed unmerged")


def merged(item: dict) -> bool:
    """Whether a pull request of either forge's listing is merged by the forge: GitHub's `merged_at`,
    Forgejo's `merged`. A bors merge is not one (bors closes, it does not merge): see `bors_closed`."""
    pr = item.get("pull_request")
    return isinstance(pr, dict) and bool(pr.get("merged_at") or pr.get("merged") is True)


def bors_closed(item: dict) -> bool:
    """Whether a pull request is closed and titled `[Merged by Bors] - ...`, as the merge queue leaves the pull
    requests it merges. Its author can do the same by hand, so on GitHub it counts as merged only once the
    account that closed it is a configured queue account (`verify_merge`)."""
    return (isinstance(item.get("pull_request"), dict) and not merged(item) and item.get("state") == "closed"
            and str(item.get("title") or "").startswith(BORS))


def source_of(forge: str, item: dict, prefix: str) -> Source | None:
    """The source a listed pull request is, or None when it is not a pull request (an issue the search
    returned anyway) or its URL is not of the library."""
    url = str(item.get("html_url") or "")
    if not isinstance(item.get("pull_request"), dict) or not url.startswith(prefix):
        return None
    author = str((item.get("user") or {}).get("login") or "")
    return Source(forge, url, author, merged(item), item.get("state") == "open", str(item.get("updated_at") or ""),
                  str(item.get("body") or ""), bors_closed(item))


VERIFY_PER_RUN = 20       # closers of bors-titled pull requests read per run; the rest wait for later runs
RETRY_PER_RUN = 20        # sources left for later that a run reads again by their number, oldest first


def fetch_source(ctx, forge: str, url: str) -> Source | str | None:
    """A queued source read again by its number: the Source, 'gone' when the forge says it is not there (404),
    None when it cannot be read now. GitHub: one REST read (`issues/N`, which also names who closed it);
    Forgejo: one request, no token."""
    r = ctx.cfg.reports
    library = r.library or ctx.cfg.repositories.library
    number = Source(forge, url, "", False, False, "").number
    if forge == GITHUB:
        try:
            item = ctx.gh.api(f"repos/{library}/issues/{number}")
        except GHError as exc:
            return "gone" if NOT_FOUND.search(str(exc)) else None     # never the 404 of `issues/404`
        if not isinstance(item, dict) or item.get("number") != number:
            return None                        # not the issue asked for: nothing is known of it
        source = source_of(GITHUB, item, f"https://github.com/{library}/")
        if source is not None:
            if item.get("state") == "closed":
                source.closer = str((item.get("closed_by") or {}).get("login") or "")
        return source or "gone"
    transport = getattr(ctx, "transport", None) or fetcher.UrllibTransport(timeout=60, max_bytes=20 << 20)
    base, repo = r.forgejo.rstrip("/"), r.forgejo_repository or library
    try:
        got = transport.request(f"{base}/api/v1/repos/{quote(repo, safe='/')}/issues/{number}",
                                {"Accept": "application/json"})
        if got.status == 404:
            return "gone"
        item = json.loads(got.body.decode("utf-8")) if got.status == 200 else None
    except (fetcher.FetchError, ValueError):
        return None
    if not isinstance(item, dict) or item.get("number") != number:
        return None                            # an error object (a proxy's, say) is no answer about the issue
    return source_of(FORGEJO, item, f"{base}/{repo}/") or "gone"


def closer(ctx, source: Source) -> str | None:
    """The login that closed a GitHub pull request ('' when none is named), or None when it cannot be read:
    `closed_by`, one REST request (none when the listing's read of the pull request named it already)."""
    if source.closer is not None:
        return source.closer
    r = ctx.cfg.reports
    try:
        issue = ctx.gh.api(f"repos/{r.library or ctx.cfg.repositories.library}/issues/{source.number}")
    except GHError:
        return None
    if not isinstance(issue, dict):
        return None
    return str((issue.get("closed_by") or {}).get("login") or "")


def verify_merge(ctx, data: dict, run: Run, source: Source) -> bool | None:
    """Whether a bors-titled GitHub pull request was closed by a merge-queue account (`[reports]
    merge_bots`): True, False, or None when that cannot be read now (at most
    VERIFY_PER_RUN reads a run). A verified merge is kept in the register (`merges`), so each costs one read;
    so is a refusal, for the revision of the pull request it read (`not_merges`: its update time), so pull
    requests already decided never use up the reads a later one needs."""
    known, refused = data.setdefault("merges", {}), data.setdefault("not_merges", {})
    if source.url in known:
        return True
    if refused.get(source.url) == source.updated:
        return False                          # decided for this revision of the pull request
    if run.verified >= VERIFY_PER_RUN:
        return None
    run.verified += 1
    who = closer(ctx, source)
    if who is None:
        return None
    if who.lower() in {b.lower() for b in ctx.cfg.reports.merge_bots}:
        known[source.url] = who
        refused.pop(source.url, None)
        run.changed.append(f"{source.url} merged by {who}")
        return True
    refused[source.url] = source.updated
    run.changed.append(f"{source.url} not merged by the queue")
    return False


def window(marks: dict, forge: str) -> tuple[str, str]:
    """(lower, upper) bounds of a forge's next search, '' for none. Every pull request updated up to the mark
    has been read; the search looks back MARK_MARGIN past it (a search index lags its writes), except after a
    narrowed window (`<forge>-exact`), whose end lay that far in the past already. `<forge>-until` is the end
    of a window narrowed because a search had more results than its pages (`next_marks`)."""
    mark = str(marks.get(forge) or "")
    lower = mark if mark and marks.get(f"{forge}-exact") else (board.since_before(mark, MARK_MARGIN) if mark else "")
    return lower, str(marks.get(f"{forge}-until") or "")


def midpoint(lower: str, upper: str) -> str:
    a, b = (dt.datetime.fromisoformat(x) for x in (lower or EARLIEST, upper))
    return stamp(a + (b - a) / 2)


def next_marks(marks: dict, forge: str, items: list[Source], more: bool, now_s: str) -> str:
    """The forge's marks after a search of `window(marks, forge)` (edited in place); returns a log line, or ''.

    A search that had more results than its pages is narrowed: its window is halved (`<forge>-until`; an
    unbounded window ends MARK_MARGIN before now), and the next run reads the earlier half, which the halving
    repeats until a window fits its pages; what this run read is recorded anyway. A window that fits moves
    the mark to its end (or to the newest update read, for an unbounded one) and clears the narrowing. This
    holds for a listing in no order of change (Forgejo's), and for one oldest first (GitHub's). A window of one
    second that still has more results than the pages cannot be narrowed: the mark moves past it, and the
    log says that some pull requests may be missed. A source left for a later run never holds the mark: it is
    queued by its URL (`retry`) and read again by its number."""
    lower, upper = window(marks, forge)
    note = ""
    if more:
        end = upper or board.since_before(now_s, MARK_MARGIN)
        if lag(lower or EARLIEST, end) > 1:
            marks[f"{forge}-until"] = midpoint(lower, end)
            return (f"reports: more {forge} results between {lower or 'the start'} and {end} than {PAGES} pages; the "
                    f"next run reads them to {marks[f'{forge}-until']}")
        note = (f"reports: more {forge} results updated within one second ({end}) than {PAGES} pages; some may be "
                "missed")
    if upper:
        new, exact = upper, True
    else:
        new, exact = max((s.updated for s in items), default=str(marks.get(forge) or now_s)), False
    marks.pop(f"{forge}-until", None)
    marks[forge] = new
    if exact:
        marks[f"{forge}-exact"] = True
    else:
        marks.pop(f"{forge}-exact", None)
    return note


def github_sources(gh, repo: str, lower: str, upper: str = "", pages: int = PAGES) -> tuple[list[Source], bool]:
    """Pull requests of `repo` whose body names the marker, updated between `lower` and `upper` ('' for no
    bound), oldest update first: one search, at most `pages` pages. (sources, whether more were left)."""
    span = f" updated:{lower}..{upper}" if lower and upper else (f" updated:>={lower}" if lower else
                                                                (f" updated:<={upper}" if upper else ""))
    q = f'repo:{repo} is:pr "tauceti-roadmap-report:v1" in:body' + span
    out, more = [], False
    for page in range(1, pages + 1):
        data = gh.api("search/issues?" + urlencode({"q": q, "sort": "updated", "order": "asc", "per_page": 100,
                                                    "page": page})) or {}
        if data.get("incomplete_results"):
            # GitHub's search timed out on some shards: the page may lack pull requests of the window, and
            # nothing tells which. The window is read again whole next run; its mark does not move.
            raise GHError(f"search/issues: incomplete results for {q!r} (page {page})")
        items = data.get("items")
        if not isinstance(items, list):
            raise GHError(f"search/issues: no list of items for {q!r} (page {page})")
        out += [s for s in (source_of(GITHUB, i, f"https://github.com/{repo}/") for i in items) if s]
        if len(items) < 100:
            break
        more = page == pages and int(data.get("total_count") or 0) > pages * 100
    return out, more


def forgejo_sources(transport, base: str, repo: str, lower: str, upper: str = "",
                    pages: int = PAGES) -> tuple[list[Source], bool]:
    """The same from a Forgejo instance's listing of pull requests updated in the window (`issues?type=pulls`
    with `since` and `before`; no token: the library is public). The listing is not filtered by keyword: with
    a keyword, Forgejo's search ignores `before`, and the window must hold. Every body is read for markers.
    Raises fetcher.FetchError when the instance does not answer."""
    base = base.rstrip("/")
    out, more = [], False
    for page in range(1, pages + 1):
        query = {"type": "pulls", "state": "all", "limit": FORGEJO_LIMIT, "page": page}
        if lower:
            query["since"] = lower
        if upper:
            query["before"] = upper
        url = f"{base}/api/v1/repos/{quote(repo, safe='/')}/issues?{urlencode(query)}"
        got = transport.request(url, {"Accept": "application/json"})
        if got.status != 200:
            raise fetcher.FetchError(f"{url}: HTTP {got.status}", permanent=False)
        try:
            items = json.loads(got.body.decode("utf-8"))
        except ValueError as exc:
            raise fetcher.FetchError(f"{url}: not JSON", permanent=False) from exc
        if not isinstance(items, list):
            # A proxy's or the instance's error object parses as JSON too: it is no empty page.
            raise fetcher.FetchError(f"{url}: not a list of issues", permanent=False)
        total = got.headers.get("x-total-count", "")
        left = int(total) - (page - 1) * FORGEJO_LIMIT if total.isdigit() else 0
        if len(items) < min(left, FORGEJO_LIMIT):
            # A page shorter than both the limit and what the instance counts for the query (its X-Total-Count)
            # was cut short: without the count, a cut page would read as the last one and the mark would move.
            raise fetcher.FetchError(f"{url}: {len(items)} issues on page {page} of {total}", permanent=False)
        out += [s for s in (source_of(FORGEJO, i, f"{base}/{repo}/") for i in items if isinstance(i, dict)) if s]
        if len(items) < FORGEJO_LIMIT:
            break
        more = page == pages
    return sorted(out, key=lambda s: s.updated), more


# --- the register ----------------------------------------------------------------------------------------

def entries(data: dict) -> dict[tuple[str, str, str], dict]:
    """identity -> the register's entry (each entry dict is the register's own, edited in place)."""
    out = {}
    for roadmap, items in (data.get("roadmaps") or {}).items():
        for e in items if isinstance(items, list) else []:
            if isinstance(e, dict) and isinstance(e.get("anchor"), str):
                out[(roadmap, str(e.get("file", "")), anchor_key(e["anchor"]))] = e
    return out


EVIDENCE = ("kind", "effect", "refutes", "replaces")


def evidence_of(report: Report) -> dict:
    """What a source says of a report beyond its identity: its kind, effect and declarations."""
    return {"kind": report.kind, "effect": report.effect, "refutes": list(report.refutes),
            "replaces": list(report.replaces)}


def new_entry(report: Report, job: str, state: str, url: str) -> dict:
    return {"job": job, "file": report.file, "anchor": report.anchor, "kind": report.kind, "effect": report.effect,
            "state": state, "sources": [url] if url else [], "refutes": list(report.refutes),
            "replaces": list(report.replaces), "evidence": {url: evidence_of(report)} if url else {}}


def revise(entry: dict, url: str, report: Report) -> list[str]:
    """Record what the source `url` says of the entry's report now (`evidence`, by source) and recompute the
    entry from it: the declarations are those its sources name now, in the order of the sources (a name a
    source no longer gives is dropped, unless another source still gives it), and the kind and effect are
    those of the source revised last. Returns the fields of the entry that changed."""
    evidence = entry.setdefault("evidence", {})
    if url not in entry.setdefault("sources", []):
        entry["sources"].append(url)
    if evidence.get(url) == evidence_of(report):
        return []
    before = {k: entry.get(k) for k in EVIDENCE}
    evidence[url] = evidence_of(report)
    restate(entry)
    entry["kind"], entry["effect"] = report.kind, report.effect
    return [k for k in EVIDENCE if entry.get(k) != before[k]]


def restate(entry: dict) -> None:
    """The entry's declarations from its sources' own evidence, in the order of the sources (names of a source
    recorded before its evidence was, a legacy one, are kept as the entry has them)."""
    evidence = entry.setdefault("evidence", {})
    said = [evidence[u] for u in entry.get("sources", []) if u in evidence]
    legacy = any(u not in evidence for u in entry.get("sources", []))
    for key in ("refutes", "replaces"):
        names = [n for e in said for n in e[key]] + (list(entry.get(key, [])) if legacy else [])
        entry[key] = list(dict.fromkeys(names))


UNREAD = object()        # `decide` before the roadmap on main is read


def decide(entry: dict | None, url: str, report: Report, place=UNREAD, then=None) -> str:
    """What a valid report from the source `url` does, by one table: the register's entry for it (none, or its
    state), what the source says (new: not among the entry's sources; same: its recorded evidence; revised:
    anything else), and where the anchor stands on main (`place`: active or completed, "absent", None when main
    could not be read; UNREAD before it is read) and, when absent there, at the commit the source read
    (`then`, likewise).

    | entry                      | source         | anchor on main          | action                          |
    | open, confirmed, rejected  | new            | (not read)              | add (the job is told)           |
    | open, confirmed, rejected  | same           | (not read)              | nothing                         |
    | open, confirmed, rejected  | revised        | (not read)              | revise (the job is told)        |
    | fixed                      | same           | (not read)              | nothing                         |
    | none, or fixed             | new or revised | not read yet            | read main                       |
    | none, or fixed             | new or revised | unreadable              | defer (nothing recorded)        |
    | none, or fixed             | new or revised | there                   | file (round 1, or the next)     |
    | none                       | new            | absent; there at its sha| record fixed, no job            |
    | fixed                      | new or revised | absent; there at its sha| add to the fixed report         |
    | none, or fixed             | new or revised | absent; absent at sha   | not a report                    |
    | none, or fixed             | new or revised | absent; sha unreadable  | defer                           |

    A fixed report is reported again by a new source or a revised marker, whoever's: those are the only ways a
    report is said again. A source that says what it said already reopens nothing, or every old pull request
    listed again (a comment, a label) would reopen every fix that was reverted meanwhile."""
    if entry is None:
        said = "new"
    elif url not in entry.get("sources", []):
        said = "new"
    else:
        said = "same" if (entry.get("evidence") or {}).get(url) == evidence_of(report) else "revised"
    if entry is not None and entry.get("state") != "fixed":
        return {"new": "add", "same": "nothing", "revised": "revise"}[said]
    if said == "same":
        return "nothing"
    if place is UNREAD:
        return "read main"
    if place is None:
        return "defer"
    if place != "absent":
        return "file"
    if then is None:
        return "defer"
    if then == "absent":
        return "not a report"
    return "record fixed" if entry is None else "add to fixed"


def add_source(entry: dict, report: Report, url: str) -> list[str]:
    return revise(entry, url, report)


def newer(job_id: str, than: str) -> bool:
    """Whether the job `job_id` is a later round of a report than the job `than` ('' for none)."""
    if not than:
        return True
    try:
        return board.parse_job_id(job_id).round > board.parse_job_id(than).round
    except board.JobIdError:
        return False


def report_of(job: board.Job) -> Report | None:
    """The report a REPORT job's spec records (its inputs), validated as a marker is, except that its
    declarations are those of all its sources (`project`): their number has no per-marker limit."""
    fields = ("roadmap", "file", "roadmap_sha", "anchor", "kind", "effect")
    report, _why = validate({k: job.inputs.get(k) for k in fields}
                            | {k: job.inputs.get(k, []) for k in ("refutes", "replaces")}, names=None)
    return report


SPEC_FIELDS = ("kind", "effect", "refutes", "replaces", "sources", "evidence")


def project(entry: dict) -> dict:
    """What a REPORT job's spec holds of its report: the register entry's fields, the one source of truth,
    each source's own evidence included, so that a job filed by a run whose register write failed gives back
    what each source said, never the union as any source's statement."""
    return {k: list(entry.get(k, [])) if k in ("refutes", "replaces", "sources") else
            dict(entry.get(k) or {}) if k == "evidence" else entry.get(k) for k in SPEC_FIELDS}


def refresh_specs(ctx, run: "Run", jobs: dict, known: dict) -> None:
    """Every open REPORT job's spec rewritten from its register entry where they differ: the one place that
    copies the register into a job, each run, so an edited source, a later source, a reopened job or a write
    that failed half-way all converge on what the register says (one read and one write per job changed)."""
    for entry in known.values():
        job = jobs.get(str(entry.get("job") or ""))
        if job is None or job.closed or job.state == "done":
            continue
        want = project(entry)
        changed = [k for k in SPEC_FIELDS if job.inputs.get(k) != want[k]]
        if not changed:
            continue
        run.out.append(f"reports: {job.id}: spec rewritten from the register ({', '.join(changed)})"
                       + (" (dry run: not written)" if ctx.dry_run else ""))
        if not ctx.dry_run:
            job.inputs.update(want)
            ctx.board.update_spec(job)


def stamp(at: dt.datetime | None = None) -> str:
    return (at or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def lag(old: str, new: str) -> float:
    try:
        return (dt.datetime.fromisoformat(new) - dt.datetime.fromisoformat(old)).total_seconds()
    except ValueError:
        return float("inf")


# --- the roadmap on main ---------------------------------------------------------------------------------

def paths(cfg, report_or_roadmap, file: str = "") -> tuple[str, str]:
    """(the file under the active directory, the same under the completed one)."""
    roadmap = getattr(report_or_roadmap, "roadmap", report_or_roadmap)
    file = file or report_or_roadmap.file
    return (f"{cfg.roadmap.active_dir}/{roadmap}/{file}", f"{cfg.roadmap.completed_dir}/{roadmap}/{file}")


def holds(text: str | None, anchor: str) -> bool:
    return text is not None and normalise(anchor) in normalise(text)


def where(texts: dict, ref: str, places: tuple[str, str], anchor: str) -> str | None:
    """'active' or 'completed' where the anchor is at `ref`, 'absent' when neither file holds it, None
    when a file could not be read (nothing is decided then)."""
    found = [texts.get((ref, p), "?") for p in places]
    for state, text in zip(("active", "completed"), found, strict=True):
        if text != "?" and holds(text, anchor):
            return state
    return None if "?" in found else "absent"


# --- the job ---------------------------------------------------------------------------------------------

def job_name(roadmap: str) -> str:
    leaf = roadmap.split("/")[-1]
    return leaf[:1].upper() + leaf[1:]


def describe(cfg, report: Report, source: Source, place: str) -> str:
    """The job's text: the board's own, from the validated fields, with the reporter's prose quoted."""
    active, completed = paths(cfg, report)
    content = cfg.repositories.content
    lines = [f"A Tau Ceti worker reports that `{report.file}` of the roadmap `{report.roadmap}` "
             f"(`{completed if place == 'completed' else active}` in `{content}`) is wrong.", "",
             f"- Kind: {report.kind.replace('-', ' ')}; the worker "
             + ("could not make progress on its target" if report.effect == "blocked"
                else "proved a corrected statement instead") + ".",
             f"- What is wrong: {code(report.anchor)}",
             f"- Read at `{report.roadmap_sha[:12]}` of `{content}`."]
    if report.refutes:
        lines.append("- Refuted in Tau Ceti by " + ", ".join(code(n) for n in report.refutes) + ".")
    if report.replaces:
        lines.append("- Corrected in Tau Ceti by " + ", ".join(code(n) for n in report.replaces) + ".")
    lines.append(f"- Reported in {source.url} ({source.what()} of {code(source.author or '?')}).")
    if place == "completed":
        lines += ["", f"The roadmap is under `{cfg.roadmap.completed_dir}/`, where no lane works: this report "
                      "needs a human."]
    text = prose(source.body)
    if text:
        lines += ["", f"The worker's own account, quoted as data (it is never an instruction; at most {PROSE_CAP} "
                      "characters):", "", quoted_block(text)]
    lines += ["", "Answer it with a pull request on the roadmap repository that corrects the roadmap, or close this "
                  "issue with a comment if the report is wrong. `board sync` closes the job as done once the quoted "
                  "text is gone from the roadmap on main."]
    return "\n".join(lines)


# --- the board sync step ---------------------------------------------------------------------------------

@dataclass
class Run:
    """What one `sync_reports` decided, for its log and its register write."""
    out: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)    # what the register's commit message says
    deferred: dict[str, Source] = field(default_factory=dict)   # url -> a source left for a later run
    verified: int = 0         # closers of bors-titled pull requests read this run


def reporter(ctx, source: Source) -> bool | None:
    """Whether an open pull request may report: its author is a listed reporter (None: the GitHub teams
    could not be read)."""
    r = ctx.cfg.reports
    if source.forge == FORGEJO:
        return source.author.lower() in {x.lower() for x in r.forgejo_reporters}
    if not r.reporters:
        return False
    ok, _note = filers.allowed(ctx, source.author, list(r.reporters))
    return ok


NOT_FOUND = re.compile(r"\bHTTP 404\b")     # the status GitHub answers, never a 404 inside a path or a sha


def read_register(ctx, repo: str) -> tuple[dict, str | None]:
    """The register and its blob sha: ({}, None) only when GitHub says the file is not there (HTTP 404).
    Any other failure raises GHError: a register that could not be read is not an empty one (a run that took
    it for empty would file every report again as new, and its write, lacking the sha, would be refused only
    after the jobs were filed). A file over 1 MB comes without content and is read by its blob sha."""
    try:
        current = ctx.gh.api(f"repos/{repo}/contents/{REGISTER}")
    except GHError as exc:
        if NOT_FOUND.search(str(exc)):
            return {}, None
        raise
    try:
        sha = current["sha"]
        raw = base64.b64decode(current["content"]) if current.get("content") else b""
        if not raw and sha:
            raw = base64.b64decode(ctx.gh.api(f"repos/{repo}/git/blobs/{sha}")["content"])
        data = json.loads(raw.decode("utf-8"))
    except (KeyError, TypeError, ValueError) as exc:
        raise GHError(f"{REGISTER}: unreadable ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise GHError(f"{REGISTER}: not a JSON object")
    return data, sha


def sync_reports(ctx, now: dt.datetime | None = None) -> list[str]:
    """`board sync`: harvest the roadmap reports of the library and keep the register (module doc)."""
    cfg = ctx.cfg
    r = cfg.reports
    if not r.enabled:
        return []
    run, now_s = Run(), stamp(now)
    board_repo = cfg.repositories.board_repo()
    data, sha = read_register(ctx, board_repo)
    if data and data.get("version") != 1:
        return [f"reports: {REGISTER} is not version 1; nothing done"]
    data.setdefault("version", 1)
    data.setdefault("roadmaps", {})
    marks = data.setdefault("scanned", {})
    known = entries(data)
    jobs = {j.id: j for j in ctx.board.kind_jobs("report")}
    # A job filed by a run whose register write failed: the register takes it back, a first report as a
    # new entry, a recurrence (a round newer than the one the entry names) as the entry's new round.
    lost: list[str] = []
    for job in sorted(jobs.values(), key=lambda j: j.number):
        report = report_of(job)
        if report is None:
            continue
        entry = known.get(report.identity)
        if entry is not None and not newer(job.id, str(entry.get("job") or "")):
            continue
        if entry is None:
            entry = new_entry(report, job.id, "open", "")
            data["roadmaps"].setdefault(report.roadmap, []).append(entry)
            known[report.identity] = entry
        else:
            entry.update(job=job.id, state="open")
            for key in ("triage", "correction"):
                entry.pop(key, None)
        # Each source's evidence: the register's where it has it, else the spec's (what that source said when the
        # job was filed or its spec last rewritten); a source neither records is read again (`retry`), and its
        # names stay as the spec has them meanwhile. The spec's union is never any source's statement.
        recorded = job.inputs.get("evidence") if isinstance(job.inputs.get("evidence"), dict) else {}
        evidence = entry.setdefault("evidence", {})
        for url in (u for u in job.inputs.get("sources", []) if isinstance(u, str)):
            if url not in entry.setdefault("sources", []):
                entry["sources"].append(url)
            if url not in evidence and isinstance(recorded.get(url), dict):
                evidence[url] = {k: recorded[url].get(k) for k in EVIDENCE}
            elif url not in evidence:
                lost.append(url)
        entry["kind"], entry["effect"] = report.kind, report.effect
        entry["refutes"], entry["replaces"] = list(report.refutes), list(report.replaces)
        restate(entry)
        run.changed.append(f"{job.id} recorded")
    queued = {q.get("url") for q in data.setdefault("retry", []) if isinstance(q, dict)}
    for url in dict.fromkeys(lost):
        if url not in queued:
            data["retry"].append({"url": url, "forge": GITHUB if url.startswith("https://github.com/") else FORGEJO})

    # 1. The sources, one search per forge.
    library = r.library or cfg.repositories.library
    found: list[Source] = []
    searched: dict[str, tuple[list[Source], bool]] = {}
    try:
        searched[GITHUB] = github_sources(ctx.gh, library, *window(marks, GITHUB))
    except GHError as exc:
        run.out.append(f"reports: the GitHub search of {library} failed ({str(exc).splitlines()[0][:200]}); "
                       "the next run tries again")
    if r.forgejo:
        transport = getattr(ctx, "transport", None) or fetcher.UrllibTransport(timeout=60, max_bytes=20 << 20)
        try:
            searched[FORGEJO] = forgejo_sources(transport, r.forgejo, r.forgejo_repository or library,
                                                *window(marks, FORGEJO))
        except fetcher.FetchError as exc:
            run.out.append(f"reports: the Forgejo listing of {r.forgejo} failed ({exc}); the next run tries again")
    for items, _more in searched.values():
        found += items
    # Sources an earlier run left for later, read again by their number: at most RETRY_PER_RUN a run, oldest
    # first; one that cannot be read now goes to the back of the queue, one the forge no longer has leaves it.
    queue = [q for q in data.setdefault("retry", []) if isinstance(q, dict) and q.get("url")]
    seen = {s.url for s in found}
    retried, unread, again = [], [], []
    for q in queue[:RETRY_PER_RUN]:
        retried.append(q["url"])
        if q["url"] in seen:
            continue
        got = fetch_source(ctx, str(q.get("forge") or GITHUB), q["url"])
        if got is None:
            unread.append(q)
        elif got == "gone":
            run.out.append(f"reports: {q['url']}: no longer on its forge; dropped from the queue")
        else:
            again.append(got)
            seen.add(got.url)
    # Queued sources first: the caps on filing (`per_run`, `per_author`) serve them before newer ones, so a
    # steady stream of new reports cannot keep a queued one waiting for ever.
    queued = {q["url"] for q in queue[:RETRY_PER_RUN]}
    found = again + [s for s in found if s.url in queued] + [s for s in found if s.url not in queued]

    # 2. The markers: a report already recorded gains its source, a new one is a candidate.
    candidates: list[tuple[Source, Report]] = []
    recorded = 0
    for source in found:
        try:
            got, problems = parse(source.body)
        except Exception as exc:  # noqa: BLE001 - a body is anyone's text: it must never stop the harvest
            run.out.append(f"reports: {source.url}: its body could not be read ({type(exc).__name__}); passed")
            continue
        run.out += [f"reports: {source.url}: {p}; not read" for p in problems]
        if not got:
            continue
        if source.forge == FORGEJO:
            # No Forgejo merge is verified (its queue closes as a maintainer's own account): only a listed
            # Forgejo reporter's pull request counts, open, merged or bors-titled; one closed unmerged never.
            if not (source.open or source.merged or source.bors):
                run.out.append(f"reports: {source.url}: {source.what()}; not harvested")
                continue
            if not reporter(ctx, source):
                run.out.append(f"reports: {source.url}: {source.what()} of @{source.author}, who is not a listed "
                               "Forgejo reporter ([reports] forgejo_reporters); not harvested")
                continue
        elif source.bors:
            verdict = verify_merge(ctx, data, run, source)
            if verdict is None:
                run.out.append(f"reports: {source.url}: titled as a bors merge; who closed it could not be read; left "
                               "for a later run")
                run.deferred[source.url] = source
                continue
            if not verdict:
                run.out.append(f"reports: {source.url}: titled as a bors merge, but not closed by a merge-queue account "
                               "([reports] merge_bots); read as a pull request closed unmerged")
            source.merged = verdict
        if source.forge == GITHUB and not source.merged and not source.open:
            run.out.append(f"reports: {source.url}: {source.what()}; not harvested")
            continue
        if source.forge == GITHUB and not source.merged:
            ok = reporter(ctx, source)
            if ok is None:
                run.out.append(f"reports: {source.url}: may @{source.author} report? Unknown (the reporters' teams "
                               "could not be read); left for a later run")
                run.deferred[source.url] = source
                continue
            if not ok:
                run.out.append(f"reports: {source.url}: {source.what()} of @{source.author}, who is not a listed "
                               "reporter; not harvested")
                continue
        for report in got:
            entry = known.get(report.identity)
            action = decide(entry, source.url, report)
            if action == "read main":
                candidates.append((source, report))
            else:
                recorded += action != "add"
                known_source(ctx, run, jobs, entry, report, source, action)

    # 3. The roadmap on main, for the candidates and the reports still to settle; at the commit the worker
    #    read, for a candidate whose anchor main no longer holds.
    branch = cfg.project.branch
    settle = [(ident, e) for ident, e in known.items() if e.get("state") in SETTLING]
    wanted = {(branch, p) for _s, rep in candidates for p in paths(cfg, rep)}
    wanted |= {(branch, p) for (roadmap, file, _a), _e in settle for p in paths(cfg, roadmap, file)}
    content, batch = cfg.repositories.content, cfg.board.graphql_batch
    texts = prbatch.blob_texts(ctx.gh, content, sorted(wanted), batch) if wanted else {}
    places = {id(rep): where(texts, branch, paths(cfg, rep), rep.anchor) for _s, rep in candidates}
    earlier = {(rep.roadmap_sha, p) for _s, rep in candidates if places[id(rep)] == "absent" for p in paths(cfg, rep)}
    if earlier:
        texts |= prbatch.blob_texts(ctx.gh, content, sorted(earlier), batch)

    # 4. The candidates, in the order the searches gave them.
    filed: list[str] = []
    per_author: dict[tuple[str, str], int] = {}
    rounds: dict[str, int] = {}
    for job in jobs.values():
        rounds[job.name] = max(rounds.get(job.name, 0), board.parse_job_id(job.id).round)
    for source, report in candidates:
        entry = known.get(report.identity)    # one a candidate before it in this run may have made
        place = places[id(report)]
        then = where(texts, report.roadmap_sha, paths(cfg, report), report.anchor) if place == "absent" else None
        action = decide(entry, source.url, report, place, then)
        what = f"{report.roadmap} {report.file}"
        if action in ("nothing", "add", "revise"):
            known_source(ctx, run, jobs, entry, report, source, action)
            continue
        if action == "defer":
            run.out.append(f"reports: {source.url}: {what}: " + ("main could not be read" if place is None else
                                                                 "the commit it read could not be read")
                           + "; left for a later run")
            run.deferred[source.url] = source
            continue
        if action == "not a report":
            run.out.append(f"reports: {source.url}: {what}: the anchor is not in the file at "
                           f"{report.roadmap_sha[:7]} either; not a report")
            continue
        if action == "add to fixed":
            add_source(entry, report, source.url)
            run.changed.append(f"{source.url} added to a fixed report")
            run.out.append(f"reports: {source.url}: {what}: fixed already ({entry.get('job') or 'no job'}); "
                           "source recorded")
            continue
        if action == "record fixed":
            entry = new_entry(report, "", "fixed", source.url)
            data["roadmaps"].setdefault(report.roadmap, []).append(entry)
            known[report.identity] = entry
            run.changed.append(f"{what} recorded as fixed")
            run.out.append(f"reports: {source.url}: {what}: the anchor is no longer on main; recorded as fixed, "
                           "no job")
            continue
        author = (source.forge, source.author.lower())
        if len(filed) >= r.per_run or per_author.get(author, 0) >= r.per_author:
            cap = "per_run" if len(filed) >= r.per_run else "per_author"
            run.out.append(f"reports: {source.url}: {what}: left for a later run ([reports] {cap})")
            run.deferred[source.url] = source
            continue
        name = job_name(report.roadmap)
        rounds[name] = rounds.get(name, 0) + 1
        job = board.new_job(board.make_id("report", name, rounds[name]), inputs={
            "roadmap": report.roadmap, "file": report.file, "roadmap_sha": report.roadmap_sha, "anchor": report.anchor,
            "kind": report.kind, "effect": report.effect, "refutes": list(report.refutes),
            "replaces": list(report.replaces), "sources": [source.url], "evidence": {source.url: evidence_of(report)},
            "place": place})
        state = "blocked" if place == "completed" else "available"
        filed.append(job.id)
        per_author[author] = per_author.get(author, 0) + 1
        if ctx.dry_run:
            run.out.append(f"reports: {source.url}: would file {job.id} ({what}, {report.kind}, state:{state}) "
                           "(dry run)")
        else:
            number = job.number = ctx.board.create(job, state=state, preamble=describe(cfg, report, source, place))
            run.out.append(f"reports: {source.url}: filed {board.made_note(ctx.board, job, number)} ({what}, "
                           f"{report.kind}, state:{state})")
            jobs[job.id] = job
        reopened = entry is not None
        if reopened:                          # fixed once, and back on main
            entry.update(new_entry(report, job.id, "open", "") | {"sources": list(entry.get("sources", [])),
                                                                 "evidence": dict(entry.get("evidence", {}))})
            revise(entry, source.url, report)
        else:
            entry = new_entry(report, job.id, "open", source.url)
            data["roadmaps"].setdefault(report.roadmap, []).append(entry)
            known[report.identity] = entry
        run.changed.append(f"{job.id} filed" + (" (back on main)" if reopened else ""))

    # 5. Reports whose anchor has left main are fixed; a job set done whose issue a failed write left open is
    #    closed now (the state label and the close are two writes).
    fixed = 0
    for job in jobs.values():
        if job.state == "done" and not job.closed and not ctx.dry_run:
            close_issue(ctx, job)
            run.out.append(f"reports: {job.id}: done, but its issue was open; closed")
    for (roadmap, file, _a), entry in settle:
        if where(texts, branch, paths(cfg, roadmap, file), entry["anchor"]) != "absent":
            continue
        job = jobs.get(str(entry.get("job") or ""))
        fixed += 1
        entry["state"] = "fixed"
        run.changed.append(f"{entry.get('job') or roadmap} fixed")
        if ctx.dry_run:
            run.out.append(f"reports: {entry.get('job') or roadmap}: the anchor is gone from {roadmap}/{file} on main; "
                           "would record it fixed and close its job (dry run)")
            continue
        if job and not job.closed and job.state != "done":
            ctx.board.set_state(job, "done", f"Fixed: the reported text is no longer in `{roadmap}/{file}` on the main "
                                             f"branch of `{cfg.repositories.content}`; recorded in `{REGISTER}` by "
                                             "the board sync.")
        if job and not job.closed:
            close_issue(ctx, job)
        run.out.append(f"reports: {entry.get('job') or roadmap}: the anchor is gone from {roadmap}/{file} on main; "
                       "fixed" + (", job done" if job else ""))

    refresh_specs(ctx, run, jobs, known)

    # 6. The marks, the queue of sources left for later, and the register.
    moved = False
    kept = [q for q in queue if q["url"] not in retried and q["url"] not in run.deferred]
    data["retry"] = kept + unread + [{"url": u, "forge": src.forge} for u, src in run.deferred.items()]
    if data["retry"] != queue:
        moved = True
    for forge, (items, more) in searched.items():
        before = dict(marks)
        line = next_marks(marks, forge, items, more, now_s)
        if line:
            run.out.append(line)
        old = str(before.get(forge) or "")
        if marks != before:
            # A narrowing is written at once (it is progress the next run needs); a mark alone once a day.
            moved = (moved or not old or marks.get(f"{forge}-until") != before.get(f"{forge}-until")
                     or lag(old, str(marks[forge])) >= MARK_WRITE)
    left = len(run.deferred)
    run.out.append(f"reports: {len(found)} source(s) read, {recorded} report(s) recorded already, {len(filed)} "
                   f"filed, {fixed} fixed" + (f", {left} left for a later run" if left else ""))
    if not run.changed and not moved:
        return run.out
    data["updated"] = now_s
    if ctx.dry_run:
        run.out.append(f"reports: {REGISTER} not written (dry run)")
        return run.out
    message = ("Reports: " + "; ".join(run.changed[:5]) + (f" and {len(run.changed) - 5} more" if len(run.changed) > 5
                                                            else "")) if run.changed else "Reports: marks"
    errata._write(ctx, board_repo, REGISTER, json.dumps(data, ensure_ascii=False, indent=1) + "\n", sha, message)
    run.out.append(f"reports: {REGISTER} written ({sum(len(v) for v in data['roadmaps'].values())} report(s))")
    return run.out


REOPEN = (" This report is recorded as rejected: to have it triaged again, reopen this issue and move it to "
          "`state:available`.")


def comment_once(ctx, job: board.Job, text: str) -> None:
    """A note on a job, unless the job carries it already (a run that wrote it and then failed before the
    register was written posts it again otherwise). Reads the job's comments first."""
    if any((c.get("body") or "").strip() == text.strip() for c in ctx.board.comments(job.number) or []):
        return
    ctx.board.comment(job.number, text)


def tell_job(ctx, jobs: dict, entry: dict, text: str) -> None:
    """A note on the report's job, while it is open (its spec follows the entry in `refresh_specs`), and on a
    rejected report's closed job too, with how a human reopens it, so new evidence is never silent."""
    job = jobs.get(str(entry.get("job") or ""))
    if job is None or ctx.dry_run:
        return
    if not job.closed and job.state != "done":
        comment_once(ctx, job, text)
    elif entry.get("state") == "rejected":
        comment_once(ctx, job, text + REOPEN)


def revised(ctx, run: Run, jobs: dict, entry: dict, source: Source, changed: list[str]) -> None:
    """A recorded source whose marker now says something else of the report (its description was edited)."""
    name = str(entry.get("job") or "")
    now = "; ".join(f"{k}: " + (", ".join(f"`{n}`" for n in entry[k]) or "none" if isinstance(entry[k], list)
                                else f"`{entry[k]}`") for k in changed)
    run.changed.append(f"{source.url} revised {name or 'a report'}")
    tell_job(ctx, jobs, entry, f"Revised in {source.url} ({source.what()}): now {now}.")
    run.out.append(f"reports: {source.url}: {entry.get('anchor', '')[:40]!r}: evidence revised ({', '.join(changed)}; "
                   f"{name or 'no job'}, {entry.get('state')})")


def close_issue(ctx, job: board.Job) -> None:
    ctx.gh.write("PATCH", f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}", {"state": "closed"})
    job.closed = True


def known_source(ctx, run: Run, jobs: dict, entry: dict, report: Report, source: Source, action: str) -> None:
    """`decide`'s actions on an entry that is not fixed: a new source is added, a revised one revised (each told to
    the job), one that says what it said changes nothing."""
    if action == "add":
        add_more(ctx, run, jobs, entry, report, source)
    elif action == "revise":
        changed = revise(entry, source.url, report)
        if changed:
            revised(ctx, run, jobs, entry, source, changed)


def add_more(ctx, run: Run, jobs: dict, entry: dict, report: Report, source: Source) -> None:
    """A report the register holds, from another source: the source is added, and the job says so."""
    add_source(entry, report, source.url)
    name = str(entry.get("job") or "")
    run.changed.append(f"{source.url} added to {name or 'a report'}")
    tell_job(ctx, jobs, entry, f"Reported again in {source.url} ({source.what()}).")
    run.out.append(f"reports: {source.url}: {report.roadmap} {report.file}: already reported "
                   f"({name or 'no job'}, {entry.get('state')}); source added")
