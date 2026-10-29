"""Pull requests of the content repository read in batches through GraphQL, for `board sync`.

`board sync` reads every open roadmap pull request: the pull request itself (draft, labels, head),
its reviews, its issue comments and its inline review comments. By REST that is four requests per
pull request, every run, from the operator's hourly quota, which the workers share. `PullReader` asks
GraphQL for `[board]
graphql_batch` pull requests at a time (aliased `pullRequest(number:)` fields of one query: GraphQL
has its own hourly budget, and such a query costs a few points of it), and hands each back in the
shapes the REST reads give (`PullData`): the dicts `lanes.pr.events_from_api` and the label and
draft code read, field for field (`id`, `user.login`, `submitted_at`, `commit_id`, `created_at`,
`body`, `state`, `pull_request_review_id`, `path`, `line`, `original_line`; `draft`, `labels[].name`,
`head.sha`, `head.ref`, `head.repo.full_name`), so nothing downstream changes.

A pull request with more than a page of anything (`REVIEWS` reviews, `REVIEW_COMMENTS` inline
comments in one review, `COMMENTS` issue comments, `LABELS` labels) is read by REST, as is every pull
request of a batch whose query fails. A bot's login reads as REST has it (`github-actions[bot]`), and
a deleted account as `ghost`.

`verdict_texts` reads files of the board's main the same way (`object(expression:)` blobs): the
verdict records of finished pull-request reviews, which `board sync` reads every run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .gh import GHError

REVIEWS, REVIEW_COMMENTS, COMMENTS, LABELS = 50, 50, 60, 100

FRAGMENT = f"""
fragment PullFields on PullRequest {{
  number isDraft state headRefOid headRefName
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
        nodes {{ databaseId createdAt path line originalLine body author {{ __typename login }} }}
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
                     "repo": {"full_name": repo["nameWithOwner"]} if repo else None}}
    reviews, inline = [], []
    for r in (node.get("reviews") or {}).get("nodes") or []:
        reviews.append({"id": r.get("databaseId"), "user": login(r.get("author")), "state": r.get("state"),
                        "commit_id": (r.get("commit") or {}).get("oid"), "submitted_at": r.get("submittedAt"),
                        "body": r.get("body") or ""})
        inline += [{"id": c.get("databaseId"), "pull_request_review_id": r.get("databaseId"),
                    "user": login(c.get("author")), "created_at": c.get("createdAt"), "path": c.get("path"),
                    "line": c.get("line"), "original_line": c.get("originalLine"), "body": c.get("body") or ""}
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
