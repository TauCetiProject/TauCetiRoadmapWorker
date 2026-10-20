"""Test doubles shared by the sources, references and intentions tests: canned HTTP responses, a
minimal PDF writer, and a GitHub/board pair that records every write."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from tauceti_roadmap_worker import config
from tauceti_roadmap_worker.fetcher import Client, FetchError, Fetcher, Response
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.paths import HERE


class FakeTransport:
    """URL -> Response (or an exception to raise). Unknown URLs are 404s. Every request is logged."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.requests: list[str] = []

    def add(self, url: str, body: bytes = b"", status: int = 200, headers: dict | None = None):
        self.routes[url] = Response(status, url, {k.lower(): v for k, v in (headers or {}).items()}, body)
        return self

    def redirect(self, url: str, to: str, status: int = 302):
        return self.add(url, b"", status, {"Location": to})

    def request(self, url: str, headers: dict) -> Response:
        self.requests.append(url)
        found = self.routes.get(url)
        if isinstance(found, Exception):
            raise found
        if found is None:
            return Response(404, url, {}, b"not found")
        return Response(found.status, url, dict(found.headers), found.body)


def transient(url: str) -> FetchError:
    return FetchError(f"{url}: connection reset", permanent=False)


def make_pdf(pages: list[str]) -> bytes:
    """A valid PDF with one line of Helvetica text per page ('' gives a page with no text layer)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = (f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else "").encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream")
        content = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def example_config(**sources_over) -> config.Config:
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(encoding="utf-8"), "example")
    for key, value in sources_over.items():
        setattr(cfg.sources, key, value)
    return cfg


def make_fetcher(tmp_path, transport: FakeTransport, **sources_over) -> Fetcher:
    cfg = example_config(**sources_over)
    client = Client(transport, allow=cfg.sources.allow, deny=cfg.sources.deny + [".pastebin.com"],
                    paywalled=cfg.sources.paywalled, pace={}, sleep=lambda s: None)
    return Fetcher(client, tmp_path / "cache", cfg.sources)


class FakeGH:
    """GitHub REST: contents of the packages repository, every write recorded."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})       # path in the packages repository -> text
        self.writes: list[tuple[str, str, dict]] = []
        self.dry_run = False

    def api(self, path: str, paginate: bool = False):
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name not in self.files:
                raise GHError(f"GET {path}: 404")
            return {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": f"blob-{len(self.files[name])}"}
        raise GHError(f"GET {path}: not faked")

    def write(self, method: str, path: str, payload: dict | None = None):
        self.writes.append((method, path, payload or {}))
        if method == "PUT" and "/contents/" in path:
            self.files[path.split("/contents/", 1)[1]] = base64.b64decode(payload["content"]).decode()
        return {"number": 99, "html_url": "https://example.invalid/99"}


class FakeBoard:
    def __init__(self, comments: dict[int, list[dict]] | None = None, jobs: list | None = None):
        self.threads = comments or {}
        self.jobs = jobs or []
        self.posted: list[tuple[int, str]] = []
        self.states: list[tuple[str, str]] = []
        self.created: list = []

    def comments(self, number: int) -> list[dict]:
        return list(self.threads.get(number, []))

    def comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))
        self.threads.setdefault(number, []).append({"id": 10_000 + len(self.posted), "body": body,
                                                    "user": {"login": "board-bot"}, "author_association": "MEMBER"})

    def set_state(self, job, state: str, comment: str = "") -> None:
        self.states.append((job.id, state))
        job.state = state

    def list_jobs(self, states=("available",), kinds=None):
        return [j for j in self.jobs if j.state in states and (not kinds or j.kind in kinds)]

    def all_jobs(self):
        return list(self.jobs)

    def create(self, job, state: str = "available", preamble: str = "") -> int:
        self.created.append((job, state, preamble))
        return 500 + len(self.created)       # like GitHub's listing, all_jobs() may not show it yet


def comment(cid: int, body: str, association: str = "MEMBER", login: str = "maintainer") -> dict:
    return {"id": cid, "body": body, "user": {"login": login}, "author_association": association,
            "html_url": f"https://github.com/o/board/issues/7#issuecomment-{cid}"}


def bot_context(cfg=None, gh=None, board=None, operator="maintainer"):
    return SimpleNamespace(cfg=cfg or example_config(), gh=gh or FakeGH(), board=board or FakeBoard(),
                           operator=operator, dry_run=False)


def arxiv_feed(entries: list[dict]) -> bytes:
    items = "".join(
        f"<entry><id>http://arxiv.org/abs/{e['id']}</id><title>{e['title']}</title>"
        f"<published>{e.get('year', '2021')}-01-04T00:00:00Z</published>"
        + "".join(f"<author><name>{a}</name></author>" for a in e.get("authors", []))
        + (f"<arxiv:doi>{e['doi']}</arxiv:doi>" if e.get("doi") else "") + "</entry>" for e in entries)
    return (f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
            f'xmlns:arxiv="http://arxiv.org/schemas/atom">{items}</feed>').encode()


def crossref(doi: str, title: str, authors: list[tuple[str, str]], year: int, preprints: list[str] = ()) -> bytes:
    msg = {"DOI": doi, "title": [title], "author": [{"given": g, "family": f} for g, f in authors],
           "issued": {"date-parts": [[year]]}, "container-title": ["J. Example Math."], "volume": "12",
           "relation": {"has-preprint": [{"id-type": "arxiv", "id": p} for p in preprints]}}
    return json.dumps({"status": "ok", "message": msg}).encode()


HAL_EMPTY = json.dumps({"response": {"numFound": 0, "docs": []}}).encode()
