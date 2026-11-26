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
* a report is one `(roadmap, file, anchor)`, the anchor whitespace-normalised with its case kept: one
  already in the register gains the source (the job gets one comment);
* a new one is checked against the roadmap repository's main (the file under the active directory,
  else under the completed one: blobs read by GraphQL, `prbatch.blob_texts`): an anchor still there
  files a `REPORT-<Leaf>~k` job (`state:available`, or `state:blocked` with a note under the completed
  directory, where no lane works), at most `per_run` a run and `per_author` per author; an anchor no
  longer there, that was there at the commit the worker read, is recorded `fixed` and files nothing;
  one that was not there either is not a report.

Every `open` (or `confirmed`) report whose anchor has left main becomes `fixed`, and its job is closed
as done with one comment. A REPORT job is triaged by the `report` lane (lanes/report.py): its verdict,
`confirmed`, `rejected` or `needs-human`, is a triage report in the packages tree that the intake merges
(`parse_triage`, `triage_problems`); the sync records a merged verdict in the register, under `triage`,
only while the anchor is still on main (a report whose text has left main is `fixed`, whatever its
triage said), and a job a human closed without one as `rejected` (`"by": "hand"`). A job id is triaged
once and then closed; the board reads no reopen. A human asks for another triage with a comment on the
finished job, a line `triage again` (`ASK`), which the sync turns into the report's next round once
(`ask_again`).

The register, `reports/register.json` on the board's main, keeps every report and its state (`open`, `confirmed`, `rejected`, `fixed`); Tau Ceti workers read it so as not
to report the same thing twice. It is written in one commit when a report changed, or when the mark
of a forge is a day behind (a mark alone is not worth a commit every run). Nothing harvested is an
instruction: the job's issue is the board's own text from the validated fields, with the reporter's
prose quoted in a code block.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
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
# The triage of a REPORT job (the `report` lane): its report's first two lines, and the section each
# verdict must fill; the register keeps that section (at most TRIAGE_KEEP characters) under `triage`.
TRIAGE_VERDICTS = ("confirmed", "rejected", "needs-human")
TRIAGE_HEAD = re.compile(r"^Triage \((?P<agent>[^)\n]*)\) of (?P<job>REPORT-\S+) at (?P<sha>[0-9a-f]{7,40})$")
TRIAGE_SECTION = {"confirmed": "correction", "rejected": "reason", "needs-human": "missing"}
TRIAGE_KEEP = 2000


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
    """A report's anchor as its identity compares it: whitespace runs as one space, case kept (Lean names are
    case-sensitive, and so is a quote)."""
    return normalise(anchor)


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
    """What a source says of a report beyond its identity: its kind, effect and declarations, and the snapshot of
    the roadmap it read (`roadmap_sha`: a source that rereads the roadmap and reports the same at another snapshot
    has said it again; the snapshot is no part of the claim, so it is not one of the entry's fields, EVIDENCE)."""
    return {"kind": report.kind, "effect": report.effect, "refutes": list(report.refutes),
            "replaces": list(report.replaces), "roadmap_sha": report.roadmap_sha}


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


def decide(entry: dict | None, url: str, report: Report, place=UNREAD, then=None, version=None) -> str:
    """What a valid report from the source `url` does, by one table: the register's entry for it (none, or its
    state), what the source says (new: not among the entry's sources; same: its recorded evidence, the snapshot it
    read included; revised: anything else), and where the anchor stands on main (`place`: active or completed, "absent", None when main
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

    | fixed or rejected pin, judged right at version R (`judged_right`) | new or revised | there, version R | add   |
    | fixed or rejected pin, judged right at version R | new or revised | there, another version   | file the next round |
    | fixed or rejected pin, judged right at version R | new or revised | there, version unread    | defer         |
    | rejected pin, judged right at version R          | new or revised | absent                   | add or revise |

    A pin (a Suggested.lean report) a triage found right is looked at again by its file's version, not by its
    name, which a correction keeps: a late report of the old snapshot changes nothing, a new version is the next
    round. A fixed report is reported again by a new source or a revised marker, whoever's: those are the only ways a
    report is said again. A source that says what it said already reopens nothing, or every old pull request
    listed again (a comment, a label) would reopen every fix that was reverted meanwhile."""
    if entry is None:
        said = "new"
    elif url not in entry.get("sources", []):
        said = "new"
    else:
        said = "same" if (entry.get("evidence") or {}).get(url) == evidence_of(report) else "revised"
    right = judged_right(entry) if entry is not None else None
    settled = entry is not None and entry.get("state") in ("fixed", "rejected") and right is not None
    if entry is not None and entry.get("state") != "fixed" and not (settled and said != "same"):
        return {"new": "add", "same": "nothing", "revised": "revise"}[said]
    if said == "same":
        return "nothing"
    if settled and place not in (UNREAD, None, "absent"):
        # Last judged right at version `right`: at it nothing has changed since; at another, the pin may be
        # wrong again, whatever its name says.
        if version is None:
            return "defer"
        if version == right:
            return "add to fixed" if entry["state"] == "fixed" else {"new": "add", "revised": "revise"}[said]
        return "file"
    if settled and entry["state"] == "rejected" and place == "absent":
        return {"new": "add", "revised": "revise"}[said]
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


def judged_right(entry: dict) -> str | None:
    """The version of a Suggested.lean pin's file a triage last judged it right at, while that judgement stands:
    read from the entry's verdicts in the order they were recorded (`triages`, then `triage`). The last one
    recorded decides, a stale one (`judged`: never recorded as a verdict) counting for nothing: a rejection (one
    recorded as a correction included) gives the version it read; a `confirmed` or `needs-human`, or a job closed
    by hand (nobody judged a version), gives none."""
    if entry.get("file") != "Suggested.lean":
        return None
    history = [t for t in [*entry.get("triages", []), entry.get("triage")] if isinstance(t, dict) and "judged" not in t]
    last = history[-1] if history else {}
    return last.get("statement") or None if last.get("verdict") == "rejected" and not last.get("by") else None


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
    copies the register into a job, each run, so an edited source, a later source or a write
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


def statement_on(cfg, oids: dict, ref: str, report_or_entry, roadmap: str = "") -> str | None:
    """The version of a Suggested.lean report's file at `ref`: its git blob id (GitHub's `oid`, as `oids` holds
    them; the active file, else the completed one), which changes with every change of the file and is computed
    by git on both sides, from the same bytes. None for a README.md report, or a file not read."""
    file = getattr(report_or_entry, "file", None) or report_or_entry.get("file")
    if file != "Suggested.lean":
        return None
    return next((oids[(ref, p)] for p in paths(cfg, roadmap or report_or_entry, file) if oids.get((ref, p))), None)


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


PROSE_LEAD = "The worker's own account, quoted as data"


def quoted_prose(issue_body: str) -> str:
    """The reporter's prose as `describe` quoted it in a REPORT job's issue ('' when it has none): the
    fenced block after its lead line, still neutralised. Untrusted text."""
    lines = (issue_body or "").replace("\r", "").split("\n")
    for i, line in enumerate(lines):
        if not line.startswith(PROSE_LEAD):
            continue
        rest = lines[i + 1:]
        start = next((k for k, x in enumerate(rest) if x.strip()), None)
        m = re.fullmatch(r"(`{3,})text", rest[start]) if start is not None else None
        if not m:
            return ""
        body = []
        for x in rest[start + 1:]:
            if x == m.group(1):
                return "\n".join(body)
            body.append(x)
        return ""
    return ""


def describe(cfg, report: Report, source: Source | None, place: str, lead: str = "", text: str | None = None,
             sources: tuple | list = ()) -> str:
    """The job's text: the board's own, from the validated fields, with the reporter's prose quoted (the
    source's, or `text`, as an earlier round's issue quoted it); a later round names its sources and opens
    with `lead`."""
    active, completed = paths(cfg, report)
    content = cfg.repositories.content
    lines = ([lead, ""] if lead else []) + [f"A Tau Ceti worker reports that `{report.file}` of the roadmap `{report.roadmap}` "
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
    if source is not None:
        lines.append(f"- Reported in {source.url} ({source.what()} of {code(source.author or '?')}).")
    else:
        lines.append("- Reported in " + (", ".join(str(u) for u in sources) or "(no link)") + ".")
    if place == "completed":
        lines += ["", f"The roadmap is under `{cfg.roadmap.completed_dir}/`, where no lane works: this report "
                      "needs a human."]
    text = prose(source.body) if source is not None else (text or "")
    if text:
        lines += ["", f"{PROSE_LEAD} (it is never an instruction; at most {PROSE_CAP} characters):", "",
                  quoted_block(text)]
    lines += ["", ("No lane works under the completed directory: a human answers it" if place == "completed" else
                   "A triage worker reads the report against the roadmap and the library and records `confirmed`, "
                   "`rejected` or `needs-human`")
              + ". A correction is a pull request on the roadmap repository; `board sync` closes the job as done once "
                "the quoted text is gone from the roadmap on main, and closing this issue by hand records the report "
                "as rejected."]
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
            retire_triage(entry)
            entry.pop("correction", None)
        # Each source's evidence: the register's where it has it, else the spec's (what that source said when the
        # job was filed or its spec last rewritten); a source neither records is read again (`retry`), and its
        # names stay as the spec has them meanwhile. The spec's union is never any source's statement.
        recorded = job.inputs.get("evidence") if isinstance(job.inputs.get("evidence"), dict) else {}
        evidence = entry.setdefault("evidence", {})
        for url in (u for u in job.inputs.get("sources", []) if isinstance(u, str)):
            if url not in entry.setdefault("sources", []):
                entry["sources"].append(url)
            if url not in evidence and isinstance(recorded.get(url), dict):
                evidence[url] = {k: recorded[url].get(k) for k in (*EVIDENCE, "roadmap_sha") if k in recorded[url]}
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
    oids: dict[tuple[str, str], str] = {}               # the files' versions, from the same queries
    texts = prbatch.blob_texts(ctx.gh, content, sorted(wanted), batch, oids=oids) if wanted else {}
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
        action = decide(entry, source.url, report, place, then, statement_on(cfg, oids, branch, report))
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
        prior = jobs.get(str((entry or {}).get("job") or ""))
        if prior is not None and entry is not None and judged_right(entry):
            # A pin judged right, at a new version: the rounds it follows, whose verdicts its outcome is read with.
            job.inputs["previous"] = [p for p in prior.inputs.get("previous", []) if isinstance(p, dict)] + [
                {"job": prior.id, "issue": prior.number}]
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
            entry.pop("triage", None)         # the triage of the earlier job judged the text it then read
            revise(entry, source.url, report)
        else:
            entry = new_entry(report, job.id, "open", source.url)
            data["roadmaps"].setdefault(report.roadmap, []).append(entry)
            known[report.identity] = entry

        run.changed.append(f"{job.id} filed" + (" (back on main)" if reopened else ""))

    # 5. Reports whose anchor has left main are fixed. From here on each step takes its entries from the register as
    #    they are when it acts (`known`, by a state predicate): no list of entries is carried from one step to the
    #    next (`settle` only says what to read), so what one step changed, a later one sees.
    fixed = 0
    for (roadmap, file, _a), entry in known.items():
        if entry.get("state") not in SETTLING or where(texts, branch, paths(cfg, roadmap, file),
                                                         entry["anchor"]) != "absent":
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

    # 6. Triage verdicts, only for reports still open whose anchor main still holds; then the finished jobs
    #    left open (closed), and the requests to triage again (the report's next round).
    triaged = record_triage(ctx, run, jobs, known, texts, branch, oids)
    restated(ctx, run, jobs, known, texts, oids, branch)
    close_finished(ctx, run, jobs, known)
    ask_again(ctx, run, jobs, known)

    refresh_specs(ctx, run, jobs, known)

    # 7. The marks, the queue of sources left for later, and the register.
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
                   f"filed, {fixed} fixed" + (f", {triaged} triaged" if triaged else "")
                   + (f", {left} left for a later run" if left else ""))
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


LATER_NOTES = ("Reported again in ", "Revised in ")     # the board's notes on a job, read by the triage lane
ASK_AGAIN = (" To have this report triaged again, comment on this issue (or edit your comment) with a line `triage "
             "again` and what the next round should know: `board sync` files its next triage round, which reads this "
             "issue.")
ASK = re.compile(r"^[ \t]*triage again\b", re.I | re.M)     # a request for another triage, on a line of its own


def comment_once(ctx, job: board.Job, text: str) -> None:
    """A note on a job, unless the job carries it already (a run that wrote it and then failed before the
    register was written posts it again otherwise). Reads the job's comments first."""
    if any((c.get("body") or "").strip() == text.strip() for c in ctx.board.comments(job.number) or []):
        return
    ctx.board.comment(job.number, text)


def tell_job(ctx, jobs: dict, entry: dict, text: str) -> None:
    """A note on the report's job, while it is open (its spec follows the entry in `refresh_specs`), and on the
    finished job of a report not fixed, with how a human asks for another triage, so new evidence is never
    silent (a verdict recorded is never undone automatically)."""
    job = jobs.get(str(entry.get("job") or ""))
    if job is None or ctx.dry_run:
        return
    if not job.closed and job.state != "done":
        comment_once(ctx, job, text)
    elif entry.get("state") != "fixed":       # a verdict recorded stays: the job hears of the change, and a human
        comment_once(ctx, job, text + ASK_AGAIN)  # asks for another triage if it matters


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


# --- the triage of a REPORT job ----------------------------------------------------------------------------

def sections(text: str) -> dict[str, str]:
    """The `## ` sections of a report, by the first word of the lower-cased heading, outside code fences."""
    out: dict[str, list[str]] = {}
    current, fenced = None, False
    for line in (text or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = out.setdefault((line[3:].strip().lower().split() or [""])[0], [])
        elif current is not None:
            current.append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


EVIDENCE_FIELDS = ("file", "anchor", "kind", "effect", "refutes", "replaces", "statement")   # what a verdict judges


def evidence_digest(fields: dict) -> str:
    """The digest of the report a verdict judges: its file, anchor (as `anchor_key` compares it), kind, effect and
    declarations, from a register entry or a REPORT job's spec alike, and for a Suggested.lean report the digest
    of the pinned file it names (`statement`: its version, `statement_on`; its name alone says nothing). Not its
    sources: a later source that says the same changes nothing a triage decided."""
    data = {k: anchor_key(str(fields.get(k) or "")) if k == "anchor" else
            [str(x) for x in fields.get(k) or []] if k in ("refutes", "replaces") else str(fields.get(k) or "")
            for k in EVIDENCE_FIELDS}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def parse_triage(text: str) -> dict[str, str]:
    """A triage report's first line (agent, job, head read), verdict and sections."""
    first = (text or "").split("\n", 1)[0].strip().strip("*").strip()
    head = TRIAGE_HEAD.match(first)
    m = re.search(r"^Verdict:\s*`?([a-z-]+)`?\s*$", text or "", re.M)
    out = {key: head[group] if head else "" for key, group in (("agent", "agent"), ("job", "job"), ("head", "sha"))}
    out["verdict"] = m.group(1) if m else ""
    e = re.search(r"^Evidence:\s*([0-9a-f]{12})\s*$", text or "", re.M)
    out["digest"] = e.group(1) if e else ""
    return sections(text) | out              # a section never stands in for a header field


def triage_problems(text: str, job_id: str, head: str = "") -> list[str]:
    """Why a triage report would not be taken in: its first line names another job (or, with `head`,
    another commit of main), its verdict is not one of TRIAGE_VERDICTS, or the section the verdict needs
    (and, for a verdict that settles the report, `## Evidence`) is missing or empty."""
    t, problems = parse_triage(text), []
    if t["job"] != job_id or (head and not head.startswith(t["head"] or "-")):
        problems.append(f"report: first line must be `Triage (<agent>) of {job_id} at {head[:7] or '<sha7 of main>'}`")
    if t["verdict"] not in TRIAGE_VERDICTS:
        problems.append("report: no `Verdict: confirmed | rejected | needs-human` line")
        return problems
    need = TRIAGE_SECTION[t["verdict"]]
    if not t.get(need):
        problems.append(f"report: a `{t['verdict']}` verdict fills `## {need.capitalize()}`")
    if t["verdict"] != "needs-human" and not t.get("evidence"):
        problems.append(f"report: a `{t['verdict']}` verdict gives its `## Evidence`")
    if len(t.get("correction", "")) > 2 * TRIAGE_KEEP:
        problems.append(f"report: `## Correction` is longer than {2 * TRIAGE_KEEP} characters: state the correction, "
                        "not the rewritten section")
    return problems


def triage_path(cfg, job: board.Job) -> str:
    return f"{board.package_dir(cfg.board.packages_dir, job.name)}/reports/{job.id}.md"


def triage_record(cfg, job: board.Job, text: str) -> dict:
    """What the register keeps of a triage: its job, verdict, report (in the packages repository) and head
    read, and the section of its verdict (the correction a confirmed report asks for)."""
    t = parse_triage(text)
    key = TRIAGE_SECTION.get(t["verdict"], "")
    body = t.get(key, "")
    return {"job": job.id, "verdict": t["verdict"], "report": triage_path(cfg, job), "head": t["head"],
            key: body if len(body) <= TRIAGE_KEEP else body[:TRIAGE_KEEP] + " [...]"}


def record_triage(ctx, run: Run, jobs: dict, known: dict, texts: dict, branch: str, oids: dict) -> int:
    """`sync_reports`: the verdicts of triaged REPORT jobs into the register. A report still `open`, whose
    anchor main still holds (read this run), takes the verdict of its job's triage once the job is finished
    (`done`) or closed: the triage on the board's main is that job's one triage (a job id is triaged once),
    whatever state label a late write left, and only for the evidence it judged: a triage whose `Evidence`
    digest is not the entry's now (`evidence_digest`: a source revised the report after the brief was written)
    is not recorded, and the report's next round is filed. `confirmed` and `rejected` settle the report; `needs-human` is
    recorded with the report staying `open`. A job closed or set done with no triage on main (a human did it)
    makes the report `rejected`, `"by": "hand"`. Returns how many reports changed."""
    cfg, wanted, todo = ctx.cfg, {}, []
    for (roadmap, file, _a), entry in known.items():
        job = jobs.get(str(entry.get("job") or ""))
        if entry.get("state") != "open" or job is None:
            continue
        if where(texts, branch, paths(cfg, roadmap, file), entry["anchor"]) not in ("active", "completed"):
            continue                          # gone (fixed above) or not read: nothing is decided
        if job.state != "done" and not job.closed:
            continue
        if (entry.get("triage") or {}).get("job") == job.id:
            continue                          # its needs-human verdict is recorded already
        todo.append((roadmap, entry, job))
        wanted[("main", triage_path(cfg, job))] = None
    if not todo:
        return 0
    found = prbatch.blob_texts(ctx.gh, cfg.repositories.packages_repo(), sorted(wanted), cfg.board.graphql_batch)
    changed = 0
    for roadmap, entry, job in todo:
        key = ("main", triage_path(cfg, job))
        if key not in found:
            continue                          # not read: the next run tries again
        statement = statement_on(cfg, oids, branch, entry, roadmap)
        if entry.get("file") == "Suggested.lean" and statement is None:
            continue                          # the pin's file version was not read: nothing is decided
        text = found[key]
        verdict = parse_triage(text)["verdict"] if text else ""
        judged, now = (parse_triage(text)["digest"] if text else ""), evidence_digest({**entry, "statement": statement})
        if verdict in TRIAGE_VERDICTS and judged != now:
            # The job's one triage judged the report as it stood when the brief was written, and its evidence was
            # revised since: that id was never triaged for the report as it is, so the next round is.
            line = f"{job.id}: triage {verdict} judged evidence since revised; not recorded"
            if ctx.dry_run:
                run.out.append(f"reports: {line} (dry run: no round filed)")
                continue
            new = next_round(ctx, jobs, entry, job, lead=(
                f"Triaged again: the triage of `{job.id}` (`{verdict}`, `{triage_path(cfg, job)}`) judged the report as "
                "it stood before it was revised. This round reads the report as its sources say it now, and the "
                "earlier rounds' triages and what people wrote on their issues."))
            if new is None:
                continue                      # its issue could not be read: the next run tries again
            retire_triage(entry)
            entry.setdefault("triages", []).append({"job": job.id, "verdict": verdict, "report": triage_path(cfg, job),
                                                    "judged": judged, "now": now})
            entry.update(job=new.id, state="open")
            entry.pop("asked_seen", None)
            comment_once(ctx, job, f"Not recorded: this triage judged the report as it stood when its brief was "
                                   f"written (evidence `{judged or 'none'}`), and its sources revised it since (now "
                                   f"`{now}`). It is triaged again as #{new.number} (`{new.id}`).")
            changed += 1
            run.changed.append(f"{new.id} filed (evidence revised)")
            run.out.append(f"reports: {line}, filed {new.id} (#{new.number})")
            continue
        record = triage_record(cfg, job, text) | ({"statement": statement} if statement else {})
        if verdict == "needs-human":
            entry["triage"] = record
            line = f"{job.id}: triage needs a human; recorded, the report stays open"
        elif verdict == "rejected" and corrected(entry, job, statement):
            # A pin confirmed wrong, corrected since (its statement changed), and now right as written: fixed.
            entry["state"], entry["triage"] = "fixed", record | {"corrected": True}
            line = f"{job.id}: triage rejected the corrected pin; recorded as fixed"
        elif verdict in ("confirmed", "rejected"):
            entry["state"], entry["triage"] = verdict, record
            line = f"{job.id}: triage {verdict}; recorded"
        else:
            entry["state"] = "rejected"
            entry["triage"] = {"job": job.id, "verdict": "rejected", "by": "hand"}
            line = f"{job.id}: closed by hand with no triage verdict; recorded as rejected"
        changed += 1
        run.changed.append(line.split(";")[0])
        run.out.append(f"reports: {line}" + (" (dry run: not written)" if ctx.dry_run else ""))
    return changed


def corrected(entry: dict, job: board.Job, statement: str | None) -> bool:
    """Whether a `rejected` verdict of `job` on the pinned statement `statement` records a correction (the report
    is `fixed`) rather than an error that never was: read from the entry's own history, never from context a
    round carries. True when, among the verdicts recorded since the report was last filed afresh (the rounds the
    job's spec names, `previous`, and the job itself), one is a `confirmed` on another statement. A README.md
    report has no statement digest: a rejection after a confirmation is a human's overturn, and stays `rejected`;
    so is one of a pin whose statement is the one confirmed. A stale verdict (`judged`) or one by hand counts
    for nothing."""
    if entry.get("file") != "Suggested.lean" or not statement:
        return False
    rounds = {job.id, *(str(p.get("job") or "") for p in job.inputs.get("previous", []) if isinstance(p, dict))}
    return any(t.get("job") in rounds and t.get("verdict") == "confirmed" and not t.get("by") and "judged" not in t
               and t.get("statement") and t["statement"] != statement
               for t in [*entry.get("triages", []), entry.get("triage") or {}] if isinstance(t, dict))


def restated(ctx, run: Run, jobs: dict, known: dict, texts: dict, oids: dict, branch: str) -> None:
    """`sync_reports`: a Suggested.lean report whose verdict (`confirmed`, or `needs-human` with the report open) is
    recorded for its current job, and whose pinned statement on main is no longer the one that verdict judged (a
    correction that kept the declaration's name), is triaged again: its next round is filed. Nothing is assumed
    fixed (whether its rejection then records a correction is read from the history: `corrected`), and no verdict
    stands on a version it never read. The entry's state as it is now (an entry this sync fixed is left fixed),
    and main as this run read it for the anchor check, no other read; a version not read decides nothing."""
    for (roadmap, file, _a), entry in known.items():
        job, triage = jobs.get(str(entry.get("job") or "")), entry.get("triage") or {}
        if (entry.get("file") != "Suggested.lean" or entry.get("state") not in ("open", "confirmed") or job is None
                or triage.get("job") != job.id or triage.get("by") or triage.get("verdict") not in ("confirmed", "needs-human")
                or where(texts, branch, paths(ctx.cfg, roadmap, file), entry["anchor"]) not in ("active", "completed")):
            continue                          # the state now, and the anchor on main: a removal comes first
        judged = triage.get("statement")
        now = statement_on(ctx.cfg, oids, branch, entry, roadmap)
        if not judged or now is None or now == judged:
            continue
        line = f"{job.id}: the pinned statement changed since it was judged"
        if ctx.dry_run:
            run.out.append(f"reports: {line} (dry run: no round filed)")
            continue
        new = next_round(ctx, jobs, entry, job, lead=(
            f"Triaged again: the statement of `{entry['anchor']}` changed on main since the triage of `{job.id}` "
            f"judged it (`{triage['verdict']}`). This round reads it as it now stands."))
        if new is None:
            continue
        retire_triage(entry)
        entry.update(job=new.id, state="open")
        entry.pop("asked_seen", None)
        run.changed.append(f"{new.id} filed (statement changed)")
        run.out.append(f"reports: {line}; filed {new.id} (#{new.number})")


def retire_triage(entry: dict) -> None:
    """The entry's triage moves to its history (`triages`): it judged an earlier round."""
    if entry.get("triage"):
        entry.setdefault("triages", []).append(entry.pop("triage"))


def close_finished(ctx, run: Run, jobs: dict, known: dict) -> None:
    """`sync_reports`: a REPORT job is triaged once and then closed. An open issue of a job that is done, or
    whose verdict the register records (a close that failed, or a human's reopen, which the board does not
    read), is closed; one that was not done is told how to ask for another triage."""
    by_job = {str(e.get("job") or ""): e for e in known.values()}
    for job in sorted(jobs.values(), key=lambda j: j.number):
        if job.closed or ctx.dry_run:
            continue
        recorded = ((by_job.get(job.id) or {}).get("triage") or {}).get("job") == job.id
        if job.state != "done" and not recorded:
            continue                          # running, or waiting on its merged triage (reconcile_merged)
        if job.state != "done":
            comment_once(ctx, job, f"This job is finished: its verdict is recorded in `{REGISTER}`, so it is closed "
                                   "again." + ASK_AGAIN)
        close_issue(ctx, job)
        run.out.append(f"reports: {job.id}: finished, but its issue was open; closed")


def ask_again(ctx, run: Run, jobs: dict, known: dict) -> None:
    """`sync_reports`: requests for another triage. The register entry's current job, once finished (done or
    closed) with its verdict recorded, is read for a comment with a line `triage again` (`ASK`) from a person
    of `[sources] answer_associations`; the first one files the report's next round, `REPORT-<Leaf>~(k+1)`,
    with that comment's id in its spec (`asked`) and the earlier rounds named (`previous`). The entry moves
    to the new round, so the old job's comments are never read for a request again: each round is asked for
    once. A job's comments are read only when its issue's `updated_at` (from the listing) is not the one of the
    last read (`asked_seen`): a new comment and an edited one both move it; comments that cannot be read decide nothing."""
    cfg = ctx.cfg
    bots = {b.lower() for b in cfg.board.bot_logins}
    trusted = set(cfg.sources.answer_associations)
    for entry in known.values():
        job = jobs.get(str(entry.get("job") or ""))
        if job is None or entry.get("state") == "fixed" or not (job.closed or job.state == "done"):
            continue
        if (entry.get("triage") or {}).get("job") != job.id:
            continue                          # its verdict is not recorded yet
        seen = [job.id, job.updated]
        if entry.get("asked_seen") == seen:
            continue
        try:
            comments = ctx.board.comments(job.number) or []
        except GHError:
            continue
        request = next((c for c in comments if ASK.search(str(c.get("body") or ""))
                        and c.get("author_association") in trusted
                        and str((c.get("user") or {}).get("login") or "").lower() not in bots), None)
        if request is None:
            entry["asked_seen"] = seen
            run.changed.append(f"{job.id} comments read")
            continue
        if ctx.dry_run:
            run.out.append(f"reports: {job.id}: a human asked for another triage; would file its next round (dry run)")
            continue
        triage = entry.get("triage") or {}
        new = next_round(ctx, jobs, entry, job, asked=int(request.get("id") or 0), lead=(
            f"Triaged again, as a human asked on #{job.number} ({request.get('html_url') or 'a comment'}), after the "
            f"triage of `{job.id}` (`{triage.get('verdict', '?')}`, `{triage.get('report', triage_path(cfg, job))}`)."
            " This round reads the earlier rounds' triages and what people wrote on their issues."))
        if new is None:
            continue
        retire_triage(entry)
        entry.pop("correction", None)
        entry.pop("asked_seen", None)
        entry.update(job=new.id, state="open")
        run.changed.append(f"{new.id} filed (asked again)")
        comment_once(ctx, job, f"Triaged again as #{new.number} (`{new.id}`), as asked in "
                               f"{request.get('html_url') or 'a comment here'}.")
        run.out.append(f"reports: {job.id}: a human asked for another triage; filed {new.id} (#{new.number})")


def next_round(ctx, jobs: dict, entry: dict, job: board.Job, lead: str, asked: int = 0) -> board.Job | None:
    """The report's next REPORT round after `job`, filed `state:available` (the board files a title once, and
    a round filed by a run whose register write failed is taken back from its spec, so nothing is filed
    twice). Its spec is the register entry's, with the rounds before it (`previous`) and the comment that
    asked for it (`asked`, when a human did); its issue opens with `lead` and quotes the reporter's prose as `job`'s does (None when that issue cannot
    be read: nothing is filed)."""
    try:
        issue = ctx.gh.api(f"repos/{ctx.cfg.repositories.board_repo()}/issues/{job.number}")
    except GHError:
        return None
    if not isinstance(issue, dict) or issue.get("number") != job.number:
        return None
    name = job.name
    k = max(board.parse_job_id(j.id).round for j in jobs.values() if j.name == name) + 1
    earlier = [p for p in job.inputs.get("previous", []) if isinstance(p, dict)] + [{"job": job.id,
                                                                                      "issue": job.number}]
    inherited = {key: v for key, v in job.inputs.items() if key != "asked"}     # the request is this round's own
    new = board.new_job(board.make_id("report", name, k), inputs={**inherited, **project(entry), "previous": earlier,
                                                                  **({"asked": asked} if asked else {})})
    report = report_of(new)
    if report is None:
        return None
    new.number = ctx.board.create(new, state="available", preamble=describe(
        ctx.cfg, report, None, str(new.inputs.get("place") or "active"), lead=lead,
        text=quoted_prose(str(issue.get("body") or "")), sources=list(entry.get("sources", []))))
    jobs[new.id] = new
    return new
