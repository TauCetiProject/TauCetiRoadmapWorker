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

* a merged pull request counts from anyone (TauCeti merges through bors: a closed pull request whose
  title starts `[Merged by Bors] - ` is merged); an open pull request only from a login of
  `[reports] reporters` (GitHub, teams allowed) or `forgejo_reporters`; a pull request closed unmerged
  never;
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


def _names(data: dict, key: str) -> tuple[tuple[str, ...], str]:
    value = data.get(key, [])
    if not isinstance(value, list):
        return (), f"`{key}` is not a list"
    if len(value) > MAX_NAMES:
        return (), f"`{key}` lists more than {MAX_NAMES} names"
    bad = [x for x in value if not isinstance(x, str) or len(x) > MAX_DECL or not DECL.fullmatch(x)]
    if bad:
        return (), f"`{key}` holds something that is not a declaration name"
    return tuple(dict.fromkeys(value)), ""


def validate(data) -> tuple[Report | None, str]:
    """The report a decoded marker describes, or (None, why not)."""
    if not isinstance(data, dict):
        return None, "not a JSON object"
    for key in ("roadmap", "file", "roadmap_sha", "anchor", "kind", "effect"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            return None, f"`{key}` is missing or not a string"
    roadmap = data["roadmap"].strip().strip("/")
    if not all(SEGMENT.fullmatch(s) for s in roadmap.split("/")):
        return None, "`roadmap` is not a roadmap folder"
    if data["file"] not in FILES:
        return None, f"`file` is not one of {', '.join(FILES)}"
    sha = data["roadmap_sha"].strip().lower()
    if not SHA.fullmatch(sha):
        return None, "`roadmap_sha` is not a 40-hex commit"
    anchor = normalise(data["anchor"])
    if len(anchor) > MAX_ANCHOR:
        return None, f"`anchor` is longer than {MAX_ANCHOR} characters"
    if data["kind"] not in KINDS:
        return None, "`kind` is not a known kind"
    if data["effect"] not in EFFECTS:
        return None, "`effect` is not blocked or worked-around"
    refutes, why = _names(data, "refutes")
    if why:
        return None, why
    replaces, why = _names(data, "replaces")
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
        except ValueError:
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

    def what(self) -> str:
        return "a merged pull request" if self.merged else ("an open pull request" if self.open
                                                            else "a pull request closed unmerged")


def merged(item: dict) -> bool:
    """Whether a pull request of either forge's listing is merged: GitHub's `merged_at`, Forgejo's
    `merged`, or a bors merge (closed, title `[Merged by Bors] - ...`: bors closes, it does not merge)."""
    pr = item.get("pull_request")
    if not isinstance(pr, dict):
        return False
    if pr.get("merged_at") or pr.get("merged") is True:
        return True
    return item.get("state") == "closed" and str(item.get("title") or "").startswith(BORS)


def source_of(forge: str, item: dict, prefix: str) -> Source | None:
    """The source a listed pull request is, or None when it is not a pull request (an issue the search
    returned anyway) or its URL is not of the library."""
    url = str(item.get("html_url") or "")
    if not isinstance(item.get("pull_request"), dict) or not url.startswith(prefix):
        return None
    author = str((item.get("user") or {}).get("login") or "")
    return Source(forge, url, author, merged(item), item.get("state") == "open", str(item.get("updated_at") or ""), str(item.get("body") or ""))


def since_query(mark: str) -> str:
    return board.since_before(mark, MARK_MARGIN) if mark else ""


def github_sources(gh, repo: str, mark: str, pages: int = PAGES) -> tuple[list[Source], bool]:
    """Pull requests of `repo` whose body names the marker, updated since `mark` (less a
    margin), oldest update first: one search, at most `pages` pages. (sources, whether more were left)."""
    since = since_query(mark)
    q = f'repo:{repo} is:pr "tauceti-roadmap-report:v1" in:body' + (f" updated:>={since}" if since else "")
    out, more = [], False
    for page in range(1, pages + 1):
        data = gh.api("search/issues?" + urlencode({"q": q, "sort": "updated", "order": "asc", "per_page": 100,
                                                    "page": page})) or {}
        items = data.get("items") or []
        out += [s for s in (source_of(GITHUB, i, f"https://github.com/{repo}/") for i in items) if s]
        if len(items) < 100:
            break
        more = page == pages and int(data.get("total_count") or 0) > pages * 100
    return out, more


def forgejo_sources(transport, base: str, repo: str, mark: str, pages: int = PAGES) -> tuple[list[Source], bool]:
    """The same from a Forgejo instance's listing of pull requests (`issues?type=pulls`: its keyword search
    reads titles and bodies; no token: the library is public). Raises fetcher.FetchError when the instance does not answer."""
    base, since = base.rstrip("/"), since_query(mark)
    out, more = [], False
    for page in range(1, pages + 1):
        query = {"type": "pulls", "state": "all", "q": "tauceti-roadmap-report:v1", "limit": FORGEJO_LIMIT, "page": page}
        if since:
            query["since"] = since
        url = f"{base}/api/v1/repos/{quote(repo, safe='/')}/issues?{urlencode(query)}"
        got = transport.request(url, {"Accept": "application/json"})
        if got.status != 200:
            raise fetcher.FetchError(f"{url}: HTTP {got.status}", permanent=False)
        try:
            items = json.loads(got.body.decode("utf-8"))
        except ValueError as exc:
            raise fetcher.FetchError(f"{url}: not JSON", permanent=False) from exc
        items = items if isinstance(items, list) else []
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


def new_entry(report: Report, job: str, state: str, url: str) -> dict:
    return {"job": job, "file": report.file, "anchor": report.anchor, "kind": report.kind, "effect": report.effect,
            "state": state, "sources": [url], "refutes": list(report.refutes), "replaces": list(report.replaces)}


def add_source(entry: dict, report: Report, url: str) -> None:
    entry.setdefault("sources", []).append(url)
    for key in ("refutes", "replaces"):
        entry[key] = list(dict.fromkeys([*entry.get(key, []), *getattr(report, key)]))


def report_of(job: board.Job) -> Report | None:
    """The report a REPORT job's spec records (its inputs)."""
    fields = ("roadmap", "file", "roadmap_sha", "anchor", "kind", "effect")
    report, _why = validate({k: job.inputs.get(k) for k in fields}
                            | {k: job.inputs.get(k, []) for k in ("refutes", "replaces")})
    return report


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
    deferred: dict[str, list[str]] = field(default_factory=dict)    # forge -> update times left for later


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


def sync_reports(ctx, now: dt.datetime | None = None) -> list[str]:
    """`board sync`: harvest the roadmap reports of the library and keep the register (module doc)."""
    cfg = ctx.cfg
    r = cfg.reports
    if not r.enabled:
        return []
    run, now_s = Run(), stamp(now)
    board_repo = cfg.repositories.board_repo()
    data, sha = errata._read_json(ctx, board_repo, REGISTER)
    if data and data.get("version") != 1:
        return [f"reports: {REGISTER} is not version 1; nothing done"]
    data.setdefault("version", 1)
    data.setdefault("roadmaps", {})
    marks = data.setdefault("scanned", {})
    known = entries(data)
    jobs = {j.id: j for j in ctx.board.kind_jobs("report")}
    # A job filed by a run whose register write failed: the register takes it back.
    for job in sorted(jobs.values(), key=lambda j: j.number):
        report = report_of(job)
        if report and report.identity not in known:
            entry = new_entry(report, job.id, "open", "")
            entry["sources"] = [s for s in job.inputs.get("sources", []) if isinstance(s, str)]
            data["roadmaps"].setdefault(report.roadmap, []).append(entry)
            known[report.identity] = entry
            run.changed.append(f"{job.id} recorded")

    # 1. The sources, one search per forge.
    library = r.library or cfg.repositories.library
    found: list[Source] = []
    searched: dict[str, tuple[list[Source], bool]] = {}
    try:
        searched[GITHUB] = github_sources(ctx.gh, library, str(marks.get(GITHUB) or ""))
    except GHError as exc:
        run.out.append(f"reports: the GitHub search of {library} failed ({str(exc).splitlines()[0][:200]}); "
                       "the next run tries again")
    if r.forgejo:
        transport = getattr(ctx, "transport", None) or fetcher.UrllibTransport(timeout=60, max_bytes=20 << 20)
        try:
            searched[FORGEJO] = forgejo_sources(transport, r.forgejo, r.forgejo_repository or library,
                                                str(marks.get(FORGEJO) or ""))
        except fetcher.FetchError as exc:
            run.out.append(f"reports: the Forgejo listing of {r.forgejo} failed ({exc}); the next run tries again")
    for forge, (items, more) in searched.items():
        found += items
        if more:
            run.out.append(f"reports: more {forge} results than {PAGES} pages; " + (
                "the rest are read by the next run" if forge == GITHUB
                else "its mark stays where it was (Forgejo lists in no order of change)"))

    # 2. The markers: a report already recorded gains its source, a new one is a candidate.
    candidates: list[tuple[Source, Report]] = []
    recorded = 0
    for source in found:
        got, problems = parse(source.body)
        run.out += [f"reports: {source.url}: {p}; not read" for p in problems]
        if not got:
            continue
        if not source.merged and not source.open:
            run.out.append(f"reports: {source.url}: {source.what()}; not harvested")
            continue
        if not source.merged:
            ok = reporter(ctx, source)
            if ok is None:
                run.out.append(f"reports: {source.url}: may @{source.author} report? Unknown (the reporters' teams "
                               "could not be read); left for a later run")
                run.deferred.setdefault(source.forge, []).append(source.updated)
                continue
            if not ok:
                run.out.append(f"reports: {source.url}: {source.what()} of @{source.author}, who is not a listed "
                               "reporter; not harvested")
                continue
        for report in got:
            entry = known.get(report.identity)
            if entry is not None and source.url in entry.get("sources", []):
                recorded += 1
            elif entry is not None and entry.get("state") != "fixed":
                add_more(ctx, run, jobs, entry, report, source)
            else:
                candidates.append((source, report))

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
        entry = known.get(report.identity)
        if entry is not None and source.url in entry.get("sources", []):
            continue                          # two markers of one body with one identity
        place = places[id(report)]
        what = f"{report.roadmap} {report.file}"
        if entry is not None and entry.get("state") != "fixed":
            add_more(ctx, run, jobs, entry, report, source)       # filed by a candidate before it in this run
            continue
        if place is None:
            run.out.append(f"reports: {source.url}: {what}: main could not be read; left for a later run")
            run.deferred.setdefault(source.forge, []).append(source.updated)
            continue
        if place == "absent":
            then = where(texts, report.roadmap_sha, paths(cfg, report), report.anchor)
            if then is None:
                run.out.append(f"reports: {source.url}: {what}: the commit it read could not be read; left for a "
                               "later run")
                run.deferred.setdefault(source.forge, []).append(source.updated)
            elif then == "absent":
                run.out.append(f"reports: {source.url}: {what}: the anchor is not in the file at "
                               f"{report.roadmap_sha[:7]} either; not a report")
            elif entry is not None:
                add_source(entry, report, source.url)
                run.changed.append(f"{source.url} added to a fixed report")
                run.out.append(f"reports: {source.url}: {what}: fixed already ({entry.get('job') or 'no job'}); "
                               "source added")
            else:
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
            run.deferred.setdefault(source.forge, []).append(source.updated)
            continue
        name = job_name(report.roadmap)
        rounds[name] = rounds.get(name, 0) + 1
        job = board.new_job(board.make_id("report", name, rounds[name]), inputs={
            "roadmap": report.roadmap, "file": report.file, "roadmap_sha": report.roadmap_sha, "anchor": report.anchor,
            "kind": report.kind, "effect": report.effect, "refutes": list(report.refutes),
            "replaces": list(report.replaces), "sources": [source.url], "place": place})
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
            entry.update(new_entry(report, job.id, "open", "") | {"sources": [*entry.get("sources", []), source.url]})
        else:
            entry = new_entry(report, job.id, "open", source.url)
            data["roadmaps"].setdefault(report.roadmap, []).append(entry)
            known[report.identity] = entry
        run.changed.append(f"{job.id} filed" + (" (back on main)" if reopened else ""))

    # 5. Reports whose anchor has left main are fixed.
    fixed = 0
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
            ctx.gh.write("PATCH", f"repos/{board_repo}/issues/{job.number}", {"state": "closed"})
        run.out.append(f"reports: {entry.get('job') or roadmap}: the anchor is gone from {roadmap}/{file} on main; "
                       "fixed" + (", job done" if job else ""))

    # 6. The marks, and the register.
    moved = False
    for forge, (items, more) in searched.items():
        if more and forge == FORGEJO:
            continue                          # Forgejo lists in no order of change: what it left unread may be older
        late = run.deferred.get(forge)
        new = min(late) if late else max((s.updated for s in items), default=str(marks.get(forge) or now_s))
        if new and new != marks.get(forge):
            moved = moved or not marks.get(forge) or lag(str(marks[forge]), new) >= MARK_WRITE
            marks[forge] = new
    left = sum(len(v) for v in run.deferred.values())
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


def add_more(ctx, run: Run, jobs: dict, entry: dict, report: Report, source: Source) -> None:
    """A report the register holds, from another source: the source is added, and an open job says so."""
    add_source(entry, report, source.url)
    name = str(entry.get("job") or "")
    run.changed.append(f"{source.url} added to {name or 'a report'}")
    job = jobs.get(name)
    if job and not job.closed and job.state != "done" and not ctx.dry_run:
        ctx.board.comment(job.number, f"Reported again in {source.url} ({source.what()}).")
    run.out.append(f"reports: {source.url}: {report.roadmap} {report.file}: already reported "
                   f"({name or 'no job'}, {entry.get('state')}); source added")
