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
"""

from __future__ import annotations

import base64
import json
import re
import tomllib
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


def request_comment(job_id: str, roadmap: str, items: list[MissingReference]) -> str:
    """The comment the lane posts when it parks a job: the template's table for humans, the items in a
    marker for the intake."""
    def cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ").replace("<!--", "&lt;!--").strip()[:300]
    rows = "\n".join(f"| `{cell(m.key)}` | {cell(m.authors)} | {cell(m.title)} | {cell(m.year)} | {cell(m.needed_for)} | "
                     f"{cell('; '.join(tried_list(m.tried))) or 'none'} |" for m in items)
    body = (_template_body().replace("__JOB__", job_id).replace("__ROADMAP__", roadmap)
            .replace("__ROWS__", rows or "| | | | | | |"))
    return body + "\n" + _marker(REQUEST_MARK, {"job": job_id, "roadmap": roadmap,
                                                "items": [asdict(m) for m in items]}) + "\n"


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
    """{job, roadmap, items: [MissingReference]} from the last request marker in a comment, or None
    when there is none or it is malformed (no items, an item that is not well formed)."""
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
    return {"job": data["job"], "roadmap": str(data.get("roadmap", "")), "items": items}


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
                notes.append(f"`source: {a.key}` from @{login} is ignored: sources are named by the project's target "
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
    transient: bool = False      # network trouble: the answer is tried again on the next run


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


def resume_job(ctx, job: board.Job, fet: fetcher.Fetcher, yes: bool = False) -> str:
    """Answer a parked job from its comments; release it when every requested key is resolved."""
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
    fresh = {k: a for k, a in answers.items() if a.comment not in handled}
    if not fresh and not (read - handled):
        waiting = [k for k in items if k not in done]
        if waiting:
            return f"{job.id}: waiting for an answer on {', '.join(waiting)}"
        # Everything was answered on an earlier run whose release did not go through: release now.
        if not yes:
            return f"{job.id}: every key answered; would release the job (dry run; pass --yes)"
        ctx.board.set_state(job, board.transition("needs-references", "references-resolved"))
        return f"{job.id}: references resolved earlier; available again"
    manifest, blob = board_manifest(ctx, job.name)
    results, retry = [], set()
    for key, answer in fresh.items():
        res = resolve(answer, items[key], sources.find_source(manifest, id=key), fet, s.uncertified_ok)
        results.append(res)
        if res.entry:
            sources.upsert_source(manifest, res.entry, replace=True)   # the entry already starts from the old one
        if res.ok:
            done[key] = res.detail
        if res.transient:
            retry.add(answer.comment)
    changed = [r.key for r in results if r.entry]
    missing = [k for k in items if k not in done]
    lines = [f"Intake: answers to the references request of `{job.id}`:", ""]
    lines += [f"- `{r.key}`: {r.detail}" + ("" if r.ok else " (tried again on the next run)" if r.transient
                                            else " (still missing)") for r in results]
    lines += [f"- note: {n}" for n in notes]
    if missing:
        lines += ["", "Still needed: " + ", ".join(f"`{k}`" for k in missing) + ". Answer with "
                  "`source: KEY <DOI, arXiv id or URL> [licence: <class>]`, `local: KEY <file name>` or `waive: KEY`."]
    else:
        lines += ["", "Every requested source is answered; the job is available again."]
    lines += ["", _marker(PROGRESS_MARK, {"handled": sorted((handled | read) - retry), "resolved": done})]
    if not yes:
        return f"{job.id}: would record {', '.join(changed) or 'nothing'}" + (
            f"; still missing {', '.join(missing)}" if missing else "; would release the job") + " (dry run; pass --yes)"
    if changed:
        write_board_manifest(ctx, job.name, manifest, blob,
                             f"Sources: answers to the references request of {job.id} ({', '.join(changed)})")
    record_local_needs(ctx, job, results)
    ctx.board.comment(job.number, "\n".join(lines))
    if not missing:
        ctx.board.set_state(job, board.transition("needs-references", "references-resolved"))
        return f"{job.id}: references resolved; available again"
    return f"{job.id}: recorded {', '.join(changed) or 'nothing'}; still missing {', '.join(missing)}"


def local_needs(job: board.Job) -> list[dict]:
    """The local texts a job may be resumed with only by an operator who holds them:
    [{key, name, sha256}] from its spec."""
    return [n for n in job.inputs.get("local_sources", []) if isinstance(n, dict) and n.get("name")]


def record_local_needs(ctx, job: board.Job, results: list[Resolution]) -> None:
    """Keep the job's spec in step with its answers: a `local:` answer adds its file to the job's
    requirements, any other answer for the key drops it. Written before the job is released."""
    needs = {n["key"]: n for n in local_needs(job)}
    before = dict(needs)
    for r in results:
        if r.ok and r.entry and r.entry.get("local_name"):
            needs[r.key] = {"key": r.key, "name": r.entry["local_name"], "sha256": r.entry.get("sha256", "")}
        elif r.ok:
            needs.pop(r.key, None)
    if needs == before:
        return
    if needs:
        job.inputs["local_sources"] = list(needs.values())
    else:
        job.inputs.pop("local_sources", None)
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
