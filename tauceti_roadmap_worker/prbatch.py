"""Pull requests of the content repository read in batches through GraphQL, for `board sync`.

`board sync` reads every open roadmap pull request: the pull request itself (draft, labels, head),
its reviews, its issue comments and its inline review comments. By REST that is four requests per
pull request, every run, from the operator's hourly quota, which the workers share. `PullReader` asks
GraphQL for `[board]
graphql_batch` pull requests at a time (aliased `pullRequest(number:)` fields of one query: GraphQL
has its own hourly budget, and such a query costs a few points of it), and hands each back in the
shapes the REST reads give (`PullData`): the dicts `lanes.pr.events_from_api` and the label and
draft code read, field for field (`id`, `user.login`, `submitted_at`, `commit_id`, `created_at`,
`body`, `state`, `pull_request_review_id`, `in_reply_to_id`, `path`, `line`, `original_line`; `draft`,
`labels[].name`, `head.sha`, `head.ref`, `head.repo.full_name`, `base.ref`), so nothing downstream
changes.

A pull request with more than a page of anything (`REVIEWS` reviews, `REVIEW_COMMENTS` inline
comments in one review, `COMMENTS` issue comments, `LABELS` labels) is read by REST, as is every pull
request of a batch whose query fails. A bot's login reads as REST has it (`github-actions[bot]`), and
a deleted account as `ghost`.

`verdict_texts` reads files of the board's main the same way (`object(expression:)` blobs): the
verdict records of finished pull-request reviews, which `board sync` reads every run. `tree_ids` reads
the git object ids of folders at given commits, by which `board sync` tells a head that only moved
(a rebase, a merge of the base branch) from one whose roadmap folders changed.

What a commit holds under a path never changes, so `tree_ids`, `tree_entries` and `blob_texts` keep
their answers in an object cache when one is set (`use_cache`: an sqlite file; the board commands
keep `git-objects.sqlite` in the project's state directory). A pair the cache holds is answered from
it; the queries ask for the other pairs only, and when none is left nothing is asked at all. The cache
keeps only what cannot change: a commit named by its full sha (40 lowercase hex digits, never an
abbreviation or a branch), an answer that is not None (a commit GitHub does not have yet may come), a
whole text (a GraphQL text not cut short, or the file read by REST), and no pair of a batch whose
query failed. Each row records when it was last used, and a row unused for `CACHE_DAYS` days is
dropped when the cache is opened. Whatever goes wrong with the file (it cannot be opened, another
process holds its lock longer than `CACHE_TIMEOUT` seconds, it is corrupt, the disk is full) leaves the
read or write concerned to GitHub: the cache never raises and never changes an answer. Without a
`use_cache` call there is no cache, and nothing is written.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .gh import GHError

REVIEWS, REVIEW_COMMENTS, COMMENTS, LABELS = 50, 50, 60, 100

FRAGMENT = f"""
fragment PullFields on PullRequest {{
  number isDraft state headRefOid headRefName baseRefName
  headRepository {{ nameWithOwner }}
  author {{ __typename login }}
  labels(first: {LABELS}) {{ pageInfo {{ hasNextPage }} nodes {{ name }} }}
  reviews(first: {REVIEWS}) {{
    pageInfo {{ hasNextPage }}
    nodes {{
      databaseId state submittedAt body
      author {{ __typename login }}
      commit {{ oid }}
      comments(first: {REVIEW_COMMENTS}) {{
        pageInfo {{ hasNextPage }}
        nodes {{ databaseId createdAt path line originalLine body author {{ __typename login }} replyTo {{ databaseId }} }}
      }}
    }}
  }}
  comments(first: {COMMENTS}) {{
    pageInfo {{ hasNextPage }}
    nodes {{ databaseId createdAt body author {{ __typename login }} }}
  }}
}}"""


@dataclass
class PullData:
    """A pull request as its REST reads give it: `pulls/N`, `pulls/N/reviews`, `issues/N/comments`
    and `pulls/N/comments`."""
    pull: dict
    reviews: list[dict]
    comments: list[dict]
    inline: list[dict]


def query(numbers: list[int]) -> str:
    """One GraphQL query for the pull requests `numbers` of a repository (`$owner`, `$name`)."""
    fields = " ".join(f"p{int(n)}: pullRequest(number: {int(n)}) {{ ...PullFields }}" for n in numbers)
    return f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}" + FRAGMENT


def login(actor: dict | None) -> dict:
    """REST's `user` of a GraphQL `author`: a bot's login with its `[bot]` suffix, `ghost` for a deleted
    account."""
    if not actor:
        return {"login": "ghost"}
    name = actor.get("login") or "ghost"
    return {"login": f"{name}[bot]" if actor.get("__typename") == "Bot" and not name.endswith("[bot]") else name}


def _more(connection: dict | None) -> bool:
    return bool(((connection or {}).get("pageInfo") or {}).get("hasNextPage"))


def complete(node: dict) -> bool:
    """Whether the query gave every review, inline comment, issue comment and label of the pull request."""
    reviews = node.get("reviews") or {}
    return not (_more(reviews) or _more(node.get("comments")) or _more(node.get("labels"))
                or any(_more(r.get("comments")) for r in reviews.get("nodes") or []))


def from_node(node: dict) -> PullData:
    """The REST shapes of one pull request node of `query`."""
    state = (node.get("state") or "").upper()
    repo = node.get("headRepository")
    pull = {"number": node.get("number"), "draft": bool(node.get("isDraft")),
            "state": "open" if state == "OPEN" else "closed", "merged": state == "MERGED",
            "user": login(node.get("author")),
            "labels": [{"name": x.get("name", "")} for x in (node.get("labels") or {}).get("nodes") or []],
            "head": {"sha": node.get("headRefOid") or "", "ref": node.get("headRefName") or "",
                     "repo": {"full_name": repo["nameWithOwner"]} if repo else None},
            "base": {"ref": node.get("baseRefName") or ""}}
    reviews, inline = [], []
    for r in (node.get("reviews") or {}).get("nodes") or []:
        reviews.append({"id": r.get("databaseId"), "user": login(r.get("author")), "state": r.get("state"),
                        "commit_id": (r.get("commit") or {}).get("oid"), "submitted_at": r.get("submittedAt"),
                        "body": r.get("body") or ""})
        inline += [{"id": c.get("databaseId"), "pull_request_review_id": r.get("databaseId"),
                    "user": login(c.get("author")), "created_at": c.get("createdAt"), "path": c.get("path"),
                    "line": c.get("line"), "original_line": c.get("originalLine"), "body": c.get("body") or "",
                    "in_reply_to_id": (c.get("replyTo") or {}).get("databaseId")}
                   for c in (r.get("comments") or {}).get("nodes") or []]
    comments = [{"id": c.get("databaseId"), "user": login(c.get("author")), "created_at": c.get("createdAt"),
                 "body": c.get("body") or ""} for c in (node.get("comments") or {}).get("nodes") or []]
    return PullData(pull, reviews, comments, sorted(inline, key=lambda c: c["id"] or 0))


def rest(gh, repo: str, number: int) -> PullData:
    """The four REST reads of one pull request."""
    return PullData(gh.api(f"repos/{repo}/pulls/{number}") or {},
                    gh.api(f"repos/{repo}/pulls/{number}/reviews?per_page=100", paginate=True) or [],
                    gh.api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True) or [],
                    gh.api(f"repos/{repo}/pulls/{number}/comments?per_page=100", paginate=True) or [])


def chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class PullReader:
    """The pull requests `numbers` of `repo`, read `batch` at a time by GraphQL when it is created (0:
    none), and the rest by REST when asked for (`get`): those with more than a page of anything, and
    those of a batch whose query failed. `queries` and `rest_reads` count what it did."""

    def __init__(self, gh, repo: str, numbers, batch: int):
        self.gh, self.repo, self.found, self.queries, self.rest_reads = gh, repo, {}, 0, 0
        wanted = sorted({int(n) for n in numbers})
        if batch <= 0 or not wanted or not hasattr(gh, "graphql"):
            return
        owner, name = repo.split("/", 1)
        for chunk in chunks(wanted, batch):
            self.queries += 1
            try:
                data = gh.graphql(query(chunk), owner=owner, name=name)
            except GHError:
                continue                  # these pull requests are read by REST
            nodes = (data or {}).get("repository") or {}
            for n in chunk:
                node = nodes.get(f"p{n}")
                if node and complete(node):
                    self.found[n] = from_node(node)

    def get(self, number: int) -> PullData:
        """Everything of the pull request: the batch's, else its four REST reads."""
        number = int(number)
        if number not in self.found:
            self.rest_reads += 1
            self.found[number] = rest(self.gh, self.repo, number)
        return self.found[number]

    def pull(self, number: int) -> dict:
        """The pull request alone (`pulls/N`): the batch's, else one REST read."""
        if int(number) in self.found:
            return self.found[int(number)].pull
        self.rest_reads += 1
        return self.gh.api(f"repos/{self.repo}/pulls/{int(number)}") or {}

    def comments(self, number: int) -> list[dict]:
        """Its issue comments alone (`issues/N/comments`): the batch's, else one paginated REST read."""
        if int(number) in self.found:
            return self.found[int(number)].comments
        self.rest_reads += 1
        return self.gh.api(f"repos/{self.repo}/issues/{int(number)}/comments?per_page=100", paginate=True) or []

    def summary(self) -> str:
        return (f"pull requests read: {len(self.found) - self.rest_reads} by GraphQL in {self.queries} "
                f"quer{'y' if self.queries == 1 else 'ies'}, {self.rest_reads} by REST")


def pull_texts(gh, repo: str, numbers, batch: int) -> dict[int, dict | None]:
    """{title, body} of each pull request `numbers` of `repo`: `batch` at a time by GraphQL (aliased
    `pullRequest(number:)` fields asking for those two only), a number a query leaves out or fails for
    by one REST read (`pulls/N`), and None for one REST cannot read either."""
    wanted, out = sorted({int(n) for n in numbers}), {}
    if batch > 0 and wanted and hasattr(gh, "graphql"):
        owner, name = repo.split("/", 1)
        for chunk in chunks(wanted, batch):
            fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ title body }}" for n in chunk)
            try:
                data = gh.graphql("query($owner: String!, $name: String!) { repository(owner: $owner, "
                                  f"name: $name) {{ {fields} }} }}", owner=owner, name=name)
            except GHError:
                continue
            nodes = (data or {}).get("repository") or {}
            for n in chunk:
                node = nodes.get(f"p{n}")
                if isinstance(node, dict) and "body" in node:
                    out[n] = {"title": node.get("title") or "", "body": node.get("body") or ""}
    for n in wanted:
        if n not in out:
            try:
                data = gh.api(f"repos/{repo}/pulls/{n}") or {}
                out[n] = {"title": data.get("title") or "", "body": data.get("body") or ""}
            except GHError:
                out[n] = None
    return out


def verdict_texts(gh, repo: str, paths: list[str], batch: int, ref: str = "main") -> dict[str, str | None]:
    """The texts of files on `ref` of `repo`, `batch` paths per GraphQL query (blobs named by
    `object(expression:)`): path -> its text, or None for a file that is not there. A path of a batch
    whose query failed is left out (the caller reads it by REST)."""
    out: dict[str, str | None] = {}
    if batch <= 0 or not paths or not hasattr(gh, "graphql"):
        return out
    owner, name = repo.split("/", 1)
    wanted = sorted(set(paths))
    for chunk in chunks(wanted, max(batch, 1) * 4):
        fields = " ".join(f"f{i}: object(expression: {json.dumps(f'{ref}:{p}')}) {{ ... on Blob {{ text }} }}"
                          for i, p in enumerate(chunk))
        try:
            data = gh.graphql(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                              f"{{ {fields} }} }}", owner=owner, name=name)
        except GHError:
            continue
        nodes = (data or {}).get("repository") or {}
        for i, p in enumerate(chunk):
            if f"f{i}" in nodes:          # null: no such file
                blob = nodes[f"f{i}"]
                out[p] = blob.get("text") if isinstance(blob, dict) else None
    return out


# --- the object cache of tree_ids, tree_entries and blob_texts ------------------------------------------

CACHE_DAYS = 21           # a row of the object cache unused for this many days is dropped when it is opened
CACHE_TIMEOUT = 1.0       # seconds a read or write of the cache waits for another process's lock
FULL_SHA = re.compile(r"[0-9a-f]{40}")


def cacheable(commit, path) -> bool:
    """Whether the answer for (`commit`, `path`) can never change: a commit named by its full sha."""
    return isinstance(commit, str) and FULL_SHA.fullmatch(commit) is not None and isinstance(path, str)


def _answer(kind: str, value) -> bool:
    """Whether `value` is an answer of `kind` the cache keeps: an id or a text, or a folder's entries
    {name: id}; never None."""
    if kind == "tree_entries":
        return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
    return isinstance(value, str)


class ObjectCache:
    """The answers of `tree_ids` (kind `tree_id`), `tree_entries` (`tree_entries`) and `blob_texts`
    (`blob_text`) in the sqlite file `path`, one row per (kind, repository, commit, path), its value
    as JSON text and `used` the time it was last used (stored, or read: once per opening). `db` is None
    when the file could not be opened; a read or write that fails is done without the cache. `hits`
    and `reads` count the pairs it answered and those the functions asked GitHub for."""

    def __init__(self, path, now: float | None = None):
        self.path = Path(path)
        self.now = time.time() if now is None else now
        self.db: sqlite3.Connection | None = None
        self.hits = self.reads = 0
        self.touched: set[tuple] = set()
        db = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=CACHE_TIMEOUT)
            db.execute("CREATE TABLE IF NOT EXISTS objects (kind TEXT NOT NULL, repo TEXT NOT NULL, "
                       "sha TEXT NOT NULL, path TEXT NOT NULL, value TEXT NOT NULL, used REAL NOT NULL, "
                       "PRIMARY KEY (kind, repo, sha, path))")
            db.commit()
        except (OSError, sqlite3.Error, ValueError):
            if db is not None:
                _close(db)
            return
        self.db = db
        self._write("DELETE FROM objects WHERE used < ?", [(self.now - CACHE_DAYS * 86400,)])

    def get(self, kind: str, repo: str, pairs) -> dict:
        """(commit, path) -> the answer the cache holds, for those of `pairs` it holds."""
        found: dict = {}
        if self.db is None:
            return found
        try:
            for commit, path in pairs:
                if not cacheable(commit, path):
                    continue
                rows = self.db.execute("SELECT value FROM objects WHERE kind = ? AND repo = ? AND sha = ? AND path = ?",
                                       (kind, repo, commit, path)).fetchall()
                try:
                    value = json.loads(rows[0][0]) if rows and isinstance(rows[0][0], str) else None
                except ValueError:
                    continue
                if _answer(kind, value):
                    found[(commit, path)] = value
        except (sqlite3.Error, ValueError):       # (ValueError: a text sqlite cannot take, as UnicodeEncodeError)
            pass
        fresh = [(kind, repo, *pair) for pair in found if (kind, repo, *pair) not in self.touched]
        if fresh:
            self.touched.update(fresh)
            self._write("UPDATE objects SET used = ? WHERE kind = ? AND repo = ? AND sha = ? AND path = ?",
                        [(self.now, *key) for key in fresh])
        return found

    def put(self, kind: str, repo: str, answers: dict) -> None:
        """Keeps those of `answers` ((commit, path) -> answer) that can never change."""
        rows = [(kind, repo, commit, path, json.dumps(value), self.now) for (commit, path), value in answers.items()
                if cacheable(commit, path) and _answer(kind, value)]
        if rows and self._write("INSERT OR REPLACE INTO objects (kind, repo, sha, path, value, used) "
                                "VALUES (?, ?, ?, ?, ?, ?)", rows):
            self.touched.update(row[:4] for row in rows)

    def _write(self, sql: str, rows: list[tuple]) -> bool:
        """One write of `rows`, all or none; False when it failed."""
        if self.db is None:
            return False
        try:
            self.db.executemany(sql, rows)
            self.db.commit()
            return True
        except (sqlite3.Error, ValueError):
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            return False

    def close(self) -> None:
        if self.db is not None:
            _close(self.db)
            self.db = None


def _close(db: sqlite3.Connection) -> None:
    try:
        db.close()
    except sqlite3.Error:
        pass


_cache: ObjectCache | None = None        # the process's object cache (`use_cache`), when one is set


def use_cache(path) -> ObjectCache | None:
    """Sets the process's object cache to the sqlite file `path` (created, with its folder, when
    missing), or to none (`None`), closing the one set before. Returns the cache set: None when there
    is none, a file that cannot be opened leaving none."""
    global _cache
    if _cache is not None:
        _cache.close()
    _cache = None
    if path is not None:
        cache = ObjectCache(path)
        _cache = cache if cache.db is not None else None
    return _cache


def cache_lines() -> list[str]:
    """What the object cache did, for the end of a board run: nothing without a cache."""
    return [f"git objects: {_cache.hits} cached, {_cache.reads} read"] if _cache is not None else []


def _cached(kind: str, repo: str, wanted, out: dict) -> list[tuple[str, str]]:
    """The pairs of `wanted` left to read, sorted, once `out` holds the answers the object cache has."""
    pairs = sorted(set(wanted))
    cache = _cache
    if cache is None:
        return pairs
    out.update(cache.get(kind, repo, pairs))
    todo = [pair for pair in pairs if pair not in out]
    cache.hits += len(pairs) - len(todo)
    cache.reads += len(todo)
    return todo


def _keep(kind: str, repo: str, out: dict, pairs) -> None:
    """Stores in the object cache the answers `out` has for `pairs` (read by a query that succeeded)."""
    if _cache is not None:
        _cache.put(kind, repo, {pair: out[pair] for pair in pairs if pair in out})


def tree_ids(gh, repo: str, wanted: list[tuple[str, str]], batch: int) -> dict[tuple[str, str], str | None]:
    """The git object id of each path at each commit (`wanted`: (commit, path) pairs), read as
    `object(expression: "<commit>:<path>") { oid }`, four times `batch` pairs per GraphQL query. A
    folder's id is its tree's, so two commits hold the same files under a path exactly when its two
    ids agree. None for a path, or a commit, that is not there; a pair of a batch whose query failed
    is left out (the caller then knows nothing about it, which is not the same as a difference). A
    pair the object cache holds (`use_cache`) is answered from it."""
    out: dict[tuple[str, str], str | None] = {}
    if not wanted or not hasattr(gh, "graphql"):
        return out
    owner, name = repo.split("/", 1)
    for chunk in chunks(_cached("tree_id", repo, wanted, out), max(batch, 1) * 4):
        fields = " ".join(f"t{i}: object(expression: {json.dumps(f'{sha}:{path}')}) {{ oid }}"
                          for i, (sha, path) in enumerate(chunk))
        try:
            data = gh.graphql(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                              f"{{ {fields} }} }}", owner=owner, name=name)
        except GHError:
            continue
        nodes = (data or {}).get("repository") or {}
        for i, pair in enumerate(chunk):
            if f"t{i}" in nodes:          # null: no such path at that commit
                node = nodes[f"t{i}"]
                out[pair] = str(node["oid"]) if isinstance(node, dict) and node.get("oid") else None
        _keep("tree_id", repo, out, chunk)
    return out


def tree_entries(gh, repo: str, wanted: list[tuple[str, str]], batch: int) -> dict[tuple[str, str], dict | None]:
    """The entries of each folder at each commit (`wanted`: (commit, path) pairs), as {name: git object
    id}, read as `object(expression: "<commit>:<path>") { ... on Tree { entries { name oid } } }`, four
    times `batch` pairs per GraphQL query: two commits hold the same file under the folder exactly when
    the ids of its entry agree. None for a path, or a commit, that is not there (or is not a folder); a
    pair of a batch whose query failed is left out (nothing is known about it). A pair the object cache
    holds (`use_cache`) is answered from it."""
    out: dict[tuple[str, str], dict | None] = {}
    if not wanted or not hasattr(gh, "graphql"):
        return out
    owner, name = repo.split("/", 1)
    for chunk in chunks(_cached("tree_entries", repo, wanted, out), max(batch, 1) * 4):
        fields = " ".join(f"e{i}: object(expression: {json.dumps(f'{sha}:{path}')}) "
                          "{ ... on Tree { entries { name oid } } }" for i, (sha, path) in enumerate(chunk))
        try:
            data = gh.graphql(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                              f"{{ {fields} }} }}", owner=owner, name=name)
        except GHError:
            continue
        nodes = (data or {}).get("repository") or {}
        for i, pair in enumerate(chunk):
            if f"e{i}" in nodes:
                node = nodes[f"e{i}"]
                entries = node.get("entries") if isinstance(node, dict) else None
                out[pair] = ({str(e.get("name")): str(e.get("oid")) for e in entries if isinstance(e, dict)}
                             if isinstance(entries, list) else None)
        _keep("tree_entries", repo, out, chunk)
    return out


def blob_texts(gh, repo: str, wanted: list[tuple[str, str]], batch: int) -> dict[tuple[str, str], str | None]:
    """The text of each file at each commit (`wanted`: (commit, path) pairs), four times `batch` pairs
    per GraphQL query (`object(expression:) { ... on Blob { text isTruncated } }`); a text GraphQL cut
    short is read whole by REST. None for a file that is not there (or not text); a pair of a batch
    whose query failed, and one REST could not read, is left out. A pair the object cache holds
    (`use_cache`) is answered from it; one of a batch whose query failed is not kept there, even when
    REST read it."""
    out: dict[tuple[str, str], str | None] = {}
    if not wanted:
        return out
    pending = _cached("blob_text", repo, wanted, out)
    failed: set[tuple[str, str]] = set()
    if hasattr(gh, "graphql"):
        owner, name = repo.split("/", 1)
        for chunk in chunks(pending, max(batch, 1) * 4):
            fields = " ".join(f"b{i}: object(expression: {json.dumps(f'{sha}:{path}')}) "
                              "{ ... on Blob { text isTruncated } }" for i, (sha, path) in enumerate(chunk))
            try:
                data = gh.graphql(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, "
                                  f"name: $name) {{ {fields} }} }}", owner=owner, name=name)
            except GHError:
                failed.update(chunk)
                continue
            nodes = (data or {}).get("repository") or {}
            for i, pair in enumerate(chunk):
                if f"b{i}" not in nodes:
                    continue
                node = nodes[f"b{i}"]
                if not isinstance(node, dict):
                    out[pair] = None
                elif not node.get("isTruncated"):
                    out[pair] = node.get("text") if isinstance(node.get("text"), str) else None
    for pair in pending:
        if pair not in out and hasattr(gh, "raw_file"):
            try:
                out[pair] = gh.raw_file(repo, pair[1], ref=pair[0])
            except GHError:
                continue
    _keep("blob_text", repo, out, [pair for pair in pending if pair not in failed])
    return out
