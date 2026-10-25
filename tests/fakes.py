"""Test doubles shared by the sources, references, intentions, board and worker tests: canned HTTP
responses, a minimal PDF writer, a GitHub/board pair that records every write, the board's job
issues with their labels, and a claims namespace."""

from __future__ import annotations

import base64
import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

from tauceti_roadmap_worker import board, claims, config
from tauceti_roadmap_worker.fetcher import Client, FetchError, Fetcher, Response
from tauceti_roadmap_worker.gh import GHError, check_fence
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
        self.fence = None

    def api(self, path: str, paginate: bool = False):
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name not in self.files:
                raise GHError(f"GET {path}: 404")
            return {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": f"blob-{len(self.files[name])}"}
        raise GHError(f"GET {path}: not faked")

    def write(self, method: str, path: str, payload: dict | None = None):
        check_fence(self)
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
        self.specs: list[tuple[str, dict]] = []
        self.labels: list[tuple[str, str]] = []
        self.created: list = []

    def comments(self, number: int) -> list[dict]:
        return list(self.threads.get(number, []))

    def comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))
        self.threads.setdefault(number, []).append({"id": 10_000 + len(self.posted), "body": body,
                                                    "user": {"login": "board-bot"}, "author_association": "MEMBER"})

    def set_state(self, job, state: str, comment: str = "", expect: str = "") -> bool:
        if expect and job.state != expect:
            return False
        self.states.append((job.id, state))
        job.state = state
        return True

    def current_state(self, job) -> str:
        return job.state

    def update_spec(self, job) -> None:
        self.specs.append((job.id, job.spec()))

    def find(self, job_id, jobs=None):
        return next((j for j in (jobs if jobs is not None else self.jobs) if j.id == job_id), None)

    def add_label(self, job, label: str) -> None:
        self.labels.append((job.id, label))

    def list_jobs(self, states=("available",), kinds=None):
        return [j for j in self.jobs if j.state in states and (not kinds or j.kind in kinds)]

    def all_jobs(self):
        return list(self.jobs)

    def kind_jobs(self, kind: str, issue_state: str = "all"):
        return [j for j in self.jobs if j.kind == kind]

    def labelled_by(self, job, label: str) -> str:
        return ""

    def create(self, job, state: str = "available", preamble: str = "") -> int:
        self.created.append((job, state, preamble))
        return 500 + len(self.created)       # like GitHub's listing, all_jobs() may not show it yet


class FakeIssuesGH:
    """GitHub REST for the board's job issues: listings filtered by labels and state, label adds
    (answered with the issue's labels, as GitHub does), label removals (an error for a label the
    issue lacks, or for every removal while `fail_removals` is set) and comments."""

    def __init__(self, issues: list[dict]):
        self.issues = {i["number"]: {"state": "open", **i} for i in issues}
        # The labels an issue starts with were added in the order listed.
        self.events = {n: [{"event": "labeled", "label": dict(label)} for label in i["labels"]]
                       for n, i in self.issues.items()}
        self.writes: list[tuple[str, str, dict]] = []
        self.comments: dict[int, list[str]] = {}     # issue -> comment bodies, read back as the bot's
        self.pulls: list[dict] = []                  # the open pull requests of the packages repository
        self.fail_removals = False
        self.dry_run = False
        self.fence = None

    def labels(self, number: int) -> list[str]:
        return [label["name"] for label in self.issues[number]["labels"]]

    def states(self, number: int) -> list[str]:
        return board.state_labels(self.labels(number))

    def api(self, path: str, paginate: bool = False):
        url = urlsplit(path)
        parts, query = url.path.split("/"), parse_qs(url.query)
        if parts[3:] == ["pulls"]:
            want_state = (query.get("state") or ["open"])[0]
            return [copy.deepcopy(p) for p in self.pulls if p.get("state", "open") == want_state]
        if len(parts) == 5 and parts[3] == "pulls":
            return copy.deepcopy(next(p for p in self.pulls if p["number"] == int(parts[4])))
        if len(parts) == 6 and parts[3] == "issues" and parts[5] == "comments":
            return [{"id": 10_000 + k, "body": body, "user": {"login": "board-bot"},
                     "created_at": "2026-10-21T14:00:00Z"}
                    for k, body in enumerate(self.comments.get(int(parts[4]), []))]
        if parts[3:] == ["issues"]:
            want = set((query.get("labels") or [""])[0].split(",")) - {""}
            state = (query.get("state") or ["open"])[0]
            return [copy.deepcopy(i) for n, i in self.issues.items()
                    if want <= set(self.labels(n)) and state in ("all", i["state"])]
        if len(parts) == 6 and parts[3] == "issues" and parts[5] == "labels":
            return copy.deepcopy(self.issues[int(parts[4])]["labels"])
        if len(parts) == 6 and parts[3] == "issues" and parts[5] == "events":
            return copy.deepcopy(self.events[int(parts[4])])
        if len(parts) == 5 and parts[3] == "issues":
            return copy.deepcopy(self.issues[int(parts[4])])
        raise GHError(f"GET {path}: not faked")

    def add_label(self, number: int, name: str) -> None:
        """A label added from outside (another writer)."""
        self.issues[number]["labels"].append({"name": name})
        self.events[number].append({"event": "labeled", "label": {"name": name}})

    def write(self, method: str, path: str, payload: dict | None = None):
        check_fence(self)
        self.writes.append((method, path, payload or {}))
        parts = path.split("/")
        if parts[3:] == ["issues"] and method == "POST":       # a new issue, numbered after the last
            number = max(self.issues, default=0) + 1
            self.issues[number] = {"number": number, "state": "open", "title": payload.get("title", ""),
                                   "body": payload.get("body", ""), "created_at": f"2026-10-21T13:{number % 60:02d}:00Z",
                                   "labels": [{"name": name} for name in payload.get("labels", [])]}
            self.events[number] = [{"event": "labeled", "label": dict(label)} for label in self.issues[number]["labels"]]
            return copy.deepcopy(self.issues[number])
        number = int(parts[4])
        if len(parts) > 5 and parts[5] == "labels" and method == "POST":
            for name in payload["labels"]:
                if name not in self.labels(number):
                    self.add_label(number, name)
            return copy.deepcopy(self.issues[number]["labels"])
        if len(parts) > 6 and parts[5] == "labels" and method == "DELETE":
            name = unquote(parts[6])
            if self.fail_removals or name not in self.labels(number):
                raise GHError(f"DELETE {path}: 404")
            self.issues[number]["labels"] = [x for x in self.issues[number]["labels"] if x["name"] != name]
            return copy.deepcopy(self.issues[number]["labels"])
        if len(parts) == 6 and parts[5] == "comments":
            self.comments.setdefault(number, []).append(payload["body"])
            return {"id": len(self.writes)}
        if len(parts) == 5 and method == "PATCH":
            self.issues[number].update(payload)
            return copy.deepcopy(self.issues[number])
        raise GHError(f"{method} {path}: not faked")


def job_issue(number: int, job_id: str, *states: str, namespace: str = "roadmap-job", **spec) -> dict:
    """A board issue carrying the job `job_id` (spec fields as keywords) with the given state labels."""
    job = board.new_job(job_id, **spec)
    labels = [namespace, f"kind:{job.kind}"] + [f"state:{s}" for s in states]
    return {"number": number, "title": board.title_for(job), "body": board.encode_spec(job),
            "labels": [{"name": label} for label in labels]}


class FakeClaims:
    """A claims namespace: every acquire succeeds; `live` keys read as live leases of another worker,
    `leases` as the lease records given."""

    def __init__(self, live: tuple[str, ...] = (), leases: dict | None = None, owner: str = "alice/w1"):
        self.live = set(live)
        self.leases = dict(leases or {})
        self.owner = owner
        self.acquired: list[str] = []
        self.released: list[str] = []

    def live_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in {*self.live, *self.leases} if k.startswith(prefix))

    def reachable(self) -> bool:
        return getattr(self, "readable", True)

    def acquire(self, key: str, ttl: int = claims.JOB_TTL, extra: dict | None = None) -> int:
        self.acquired.append(key)
        return claims.ACQUIRED

    def renew(self, key: str, ttl: int = claims.JOB_TTL) -> int:
        return claims.ACQUIRED

    def release(self, key: str, force: bool = False) -> None:
        self.released.append(key)

    def read(self, key: str) -> dict | None:
        if key in self.leases:
            return self.leases[key]
        return {"owner": "bob/w2", "expires_at": 4_102_444_800} if key in self.live else None

    def record_author(self, job_id: str, agent: str = "", model: str = "") -> int:
        self.authors = {**getattr(self, "authors", {}), job_id: {"operator": self.owner.split("/")[0], "agent": agent,
                                                                  "model": model}}
        return claims.ACQUIRED

    def recorded_authors(self, job_ids: list[str]) -> dict[str, dict]:
        return {jid: self.authors[jid] for jid in job_ids if jid in getattr(self, "authors", {})}


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
