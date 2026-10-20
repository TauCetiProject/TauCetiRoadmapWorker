"""The `needs-references` hand-off, end to end.

Worker side. When an agent cannot read a source legally, or the worker cannot fetch a text the
manifest records, the job's scratch directory gets `needs-references.toml`:

    [[missing]]
    id = "Kie67"                       # the key the README cites it by (derived when absent)
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

    attach: KEY <attachment link> [sha256: <hex>]   a file attached to the comment (GitHub attachment
                                                    links only); with one item, a bare link will do
    source: KEY <DOI | arXiv id | URL>               an alternative legal source, fetched by the service
    waive: KEY[, KEY ...]                           cite by locator alone

Attachments are downloaded into the cache and verified (a stated `sha256:` must match; a hash the
manifest recorded is compared and a difference noted), named sources go through the same policy as
`sources fetch`, and waivers are recorded as `status = "waived"`. The roadmap's `sources.toml` on
the packages repository is updated, and when every requested key is answered the job returns to
`state:available`, where the next worker finds the texts in the cache (downloading them again from
the recorded URL and checking the hash).
"""

from __future__ import annotations

import base64
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import board, fetcher, sources
from .gh import GHError
from .paths import ISSUE_TEMPLATES

REQUEST_MARK = "tauceti-roadmap-needs-references"
PROGRESS_MARK = "tauceti-roadmap-references-progress"
REQUEST_RE = re.compile(r"<!--\s*" + REQUEST_MARK + r"\s*(\{.*?\})\s*-->", re.S)
PROGRESS_RE = re.compile(r"<!--\s*" + PROGRESS_MARK + r"\s*(\{.*?\})\s*-->", re.S)
ATTACHMENT = re.compile(r"https://github\.com/(?:user-attachments/(?:files|assets)/[^\s)\]>\"'<]+"
                        r"|[\w.-]+/[\w.-]+/files/\d+/[^\s)\]>\"'<]+)")
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


def parse_missing(data: dict, existing_keys: set[str] | None = None) -> list[MissingReference]:
    """The [[missing]] tables of a needs-references.toml; items without a title or a `needed_for`
    are dropped, and items without a key get one derived from authors and year."""
    out, taken = [], set(existing_keys or set())
    for item in data.get("missing", []):
        if not isinstance(item, dict):
            continue
        m = MissingReference(authors=str(item.get("authors", "")), title=str(item.get("title", "")),
                             year=str(item.get("year", "")), needed_for=str(item.get("needed_for", "")),
                             tried=[str(t) for t in item.get("tried", [])], note=str(item.get("note", "")),
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
    text = (ISSUE_TEMPLATES / "needs-references.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip() + "\n"


def _marker(mark: str, data: dict) -> str:
    # '<' and '>' only occur inside JSON strings, so escaping them keeps the comment closed.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- {mark} {payload} -->"


def request_comment(job_id: str, roadmap: str, items: list[MissingReference]) -> str:
    """The comment the lane posts when it parks a job: the template's table for humans, the items in a
    marker for the intake."""
    def cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ").replace("<!--", "&lt;!--").strip()[:300]
    rows = "\n".join(f"| `{cell(m.key)}` | {cell(m.authors)} | {cell(m.title)} | {cell(m.year)} | {cell(m.needed_for)} | "
                     f"{cell(', '.join(m.tried)) or 'none'} |" for m in items)
    body = (_template_body().replace("__JOB__", job_id).replace("__ROADMAP__", roadmap)
            .replace("__ROWS__", rows or "| | | | | | |"))
    return body + "\n" + _marker(REQUEST_MARK, {"job": job_id, "roadmap": roadmap,
                                                "items": [asdict(m) for m in items]}) + "\n"


def decode_request(body: str) -> dict | None:
    """{job, roadmap, items: [MissingReference]} from the last request marker in a comment, or None."""
    found = REQUEST_RE.findall(body or "")
    if not found:
        return None
    try:
        data = json.loads(found[-1])
        items = [MissingReference(**{k: v for k, v in i.items() if k in MissingReference.__dataclass_fields__})
                 for i in data.get("items", [])]
    except (ValueError, TypeError, AttributeError):
        return None
    return {"job": str(data.get("job", "")), "roadmap": str(data.get("roadmap", "")), "items": items}


# --- answers --------------------------------------------------------------------------------------

@dataclass
class Answer:
    key: str
    action: str              # attach | source | waive
    value: str = ""          # the attachment link, or the citation of the alternative source
    sha256: str = ""         # stated for an attachment
    comment: int = 0
    url: str = ""            # the comment's link, recorded as the entry's resolution


def _markdown_url(text: str) -> str:
    m = re.search(r"\]\((https?://[^)\s]+)\)", text)
    return m.group(1) if m else text.strip().strip("<>")


def parse_answers(body: str, keys: list[str]) -> tuple[list[Answer], list[str]]:
    """Answers in one comment, and notes on lines that name an unknown key or an unassigned link."""
    canon = {k.lower(): k for k in keys}
    answers, notes = [], []
    for raw in (body or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", raw).strip()
        m = re.match(rf"(?i)^(attach|source|waive)\s*:\s*({KEY}(?:\s*,\s*{KEY})*)\s*(.*)$", line)
        if m:
            action, rest = m.group(1).lower(), m.group(3)
            named = [k.strip() for k in m.group(2).split(",")] if action == "waive" else [m.group(2)]
            for k in named:
                if k.lower() not in canon:
                    notes.append(f"`{k}` is not a key of this request")
                    continue
                if action == "waive":
                    answers.append(Answer(canon[k.lower()], "waive"))
                elif action == "source":
                    if rest.strip():
                        answers.append(Answer(canon[k.lower()], "source", _markdown_url(rest)))
                    else:
                        notes.append(f"`source: {k}` names no source")
                else:
                    link = ATTACHMENT.search(rest)
                    sha = SHA.search(rest)
                    if link:
                        answers.append(Answer(canon[k.lower()], "attach", link.group(0), sha.group(1).lower() if sha else ""))
                    else:
                        notes.append(f"`attach: {k}` carries no GitHub attachment link")
            continue
        for link in ATTACHMENT.findall(line):
            stem = Path(link.rsplit("/", 1)[-1]).stem.lower()
            sha = SHA.search(line)
            key = canon.get(stem) or (keys[0] if len(keys) == 1 else "")
            if key:
                answers.append(Answer(key, "attach", link, sha.group(1).lower() if sha else ""))
            else:
                notes.append(f"an attachment ({link.rsplit('/', 1)[-1]}) names no key: write `attach: KEY <link>`")
    return answers, notes


def collect_answers(comments: list[dict], keys: list[str], trusted: list[str], skip_logins: list[str]) -> tuple[dict, list[str], set[int]]:
    """The latest answer per key in comments from trusted associations (the board's own notes and
    anyone else's comments are skipped), the notes, and the ids of the comments read."""
    latest, notes, read = {}, [], set()
    for c in comments:
        login = (c.get("user") or {}).get("login", "")
        body = c.get("body") or ""
        if login in skip_logins or REQUEST_MARK in body or PROGRESS_MARK in body:
            continue
        if c.get("author_association") not in trusted:
            continue
        answers, found_notes = parse_answers(body, keys)
        if answers or found_notes:
            read.add(int(c.get("id") or 0))
        for a in answers:
            a.comment, a.url = int(c.get("id") or 0), c.get("html_url", "")
            latest[a.key] = a
        notes += found_notes
    return latest, notes, read


# --- the board side -------------------------------------------------------------------------------

@dataclass
class Resolution:
    key: str
    ok: bool
    detail: str
    entry: dict | None = None


def _base_entry(item: MissingReference, existing: dict | None) -> dict:
    entry = dict(existing or {})
    entry["id"] = item.key
    for k in ("authors", "title", "year", "doi", "locator_scheme", "locator"):
        value = getattr(item, k)
        if value and not entry.get(k):
            entry[k] = int(value) if k == "year" and str(value).isdigit() else value
    return entry


def resolve(answer: Answer, item: MissingReference, existing: dict | None, fet: fetcher.Fetcher,
            uncertified_ok: bool) -> Resolution:
    """Carry out one answer: fetch and verify the file, or fetch the named source, or record the waiver."""
    base = _base_entry(item, existing)
    if answer.action == "waive":
        base.update(status="waived", licence_class=sources.NO_TEXT, resolution=answer.url)
        for k in ("fetched_from", "fetched_at", "sha256"):
            base.pop(k, None)
        return Resolution(item.key, True, "waived: cited by its locator alone", base)
    if answer.action == "source":
        out = fet.fetch(fetcher.parse_citation(answer.value))
        if out.status == "fetched":
            entry = {**base, **{k: v for k, v in out.entry.items() if k not in ("authors", "title", "year") or not base.get(k)}}
            entry.update(resolution=answer.url)
            return Resolution(item.key, True, f"fetched from {out.entry.get('fetched_from') or 'a local text'} "
                                              f"(sha256 {out.entry['sha256'][:12]}…)", entry)
        if out.status == "uncertified":
            entry = {**base, **out.entry, "id": item.key, "resolution": answer.url}
            return Resolution(item.key, uncertified_ok, "the named source is paywalled: recorded as uncertified"
                              + ("" if uncertified_ok else ", which this project does not accept"), entry)
        return Resolution(item.key, False, "the named source could not be fetched: " + "; ".join(out.tried[-3:]))
    if not ATTACHMENT.fullmatch(answer.value):
        return Resolution(item.key, False, "only GitHub attachment links are downloaded")
    try:
        r = fet.client.get(answer.value)
    except fetcher.FetchError as exc:
        return Resolution(item.key, False, f"the attachment could not be downloaded: {exc}")
    kind = sources.sniff(r.body, r.header("content-type"))
    if kind not in fetcher.DOCUMENT_KINDS:
        return Resolution(item.key, False, "the link returned " + ("a web page" if kind == "html" else "an unrecognised file")
                          + ", not the text (attachments of a private board need a signed-in download: attach it "
                            "to a public issue or name a legal source)")
    try:
        path, _txt, note = fet.keep(r.body, kind, answer.value, expected=answer.sha256)
    except sources.HashMismatch as exc:
        return Resolution(item.key, False, f"the attachment does not match the stated hash: {exc}")
    sha = path.name.split(".", 1)[0]
    now = fet.now()
    base.update(status="certified", licence_class="operator-local", fetched_from=answer.value,
                fetched_at=now.isoformat().replace("+00:00", "Z"), sha256=sha, accessed=now.date().isoformat(),
                resolution=answer.url)
    base.setdefault("url", answer.value)
    recorded = item.sha256 or str((existing or {}).get("sha256", ""))
    detail = f"attached file verified (sha256 {sha[:12]}…)"
    if recorded and recorded != sha:
        base["version_note"] = (f"the attached text differs from the one recorded earlier (sha256 {recorded[:12]}…): "
                                "its locators must be checked again")
        detail += "; it differs from the text recorded earlier, so its locators must be checked again"
    if note:
        detail += f"; {note}"
    return Resolution(item.key, True, detail, base)


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
    at = max((i for i, c in enumerate(comments) if decode_request(c.get("body") or "")), default=-1)
    if at < 0:
        return f"{job.id}: parked without a request marker; a maintainer releases it by hand"
    request = decode_request(comments[at]["body"])
    items = {m.key: m for m in request["items"]}
    after = comments[at + 1:]
    s = ctx.cfg.sources
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
    answers, notes, read = collect_answers(after, list(items), s.answer_associations, ctx.cfg.board.bot_logins)
    fresh = {k: a for k, a in answers.items() if a.comment not in handled}
    if not fresh and not (read - handled):
        waiting = [k for k in items if k not in done]
        return f"{job.id}: waiting for an answer on {', '.join(waiting) or 'nothing'}"
    manifest, blob = board_manifest(ctx, job.name)
    results = []
    for key, answer in fresh.items():
        res = resolve(answer, items[key], sources.find_source(manifest, id=key), fet, s.uncertified_ok)
        results.append(res)
        if res.entry:
            sources.upsert_source(manifest, res.entry)
        if res.ok:
            done[key] = res.detail
    changed = [r.key for r in results if r.entry]
    missing = [k for k in items if k not in done]
    lines = [f"Intake: answers to the references request of `{job.id}`:", ""]
    lines += [f"- `{r.key}`: {r.detail}" + ("" if r.ok else " (still missing)") for r in results]
    lines += [f"- note: {n}" for n in notes]
    if missing:
        lines += ["", "Still needed: " + ", ".join(f"`{k}`" for k in missing) + ". Answer with `attach: KEY <link>`, "
                  "`source: KEY <DOI, arXiv id or URL>` or `waive: KEY`."]
    else:
        lines += ["", "Every requested source is answered; the job is available again."]
    lines += ["", _marker(PROGRESS_MARK, {"handled": sorted(handled | read), "resolved": done})]
    if not yes:
        return f"{job.id}: would record {', '.join(changed) or 'nothing'}" + (
            f"; still missing {', '.join(missing)}" if missing else "; would release the job") + " (dry run; pass --yes)"
    if changed:
        write_board_manifest(ctx, job.name, manifest, blob,
                             f"Sources: answers to the references request of {job.id} ({', '.join(changed)})")
    ctx.board.comment(job.number, "\n".join(lines))
    if not missing:
        ctx.board.set_state(job, board.transition("needs-references", "references-resolved"))
        return f"{job.id}: references resolved; available again"
    return f"{job.id}: recorded {', '.join(changed) or 'nothing'}; still missing {', '.join(missing)}"


def resume_all(ctx, fet: fetcher.Fetcher, yes: bool = False) -> list[str]:
    out = []
    for job in ctx.board.list_jobs(("needs-references",)):
        try:
            out.append(resume_job(ctx, job, fet, yes))
        except GHError as exc:
            out.append(f"{job.id}: {exc}")
    return out
