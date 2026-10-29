"""`board sync` reads the open roadmap pull requests by GraphQL, `[board] graphql_batch` at a time, in
the shapes the REST reads give (`prbatch.PullReader`), and the verdicts of finished reviews as blobs
(`prbatch.verdict_texts`); REST reads what a page does not hold and what a failed query left."""

import copy
import re
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import config, intake, prbatch, replies
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes.pr import events_from_api

from .fakes import FakeBoard, example_config

REPO = example_config().repositories.content
SHA = "abc1234" + "0" * 33


# --- one pull request as REST gives it, and as the GraphQL query does ---------------------------------

def rest_sample(number=905):
    """REST's answers for one pull request, with the fields REST adds that nothing reads."""
    pull = {"number": number, "draft": True, "state": "open", "merged": False, "user": {"login": "alice"},
            "labels": [{"name": "awaiting-author", "color": "fff"}, {"name": "roadmap-worker"}],
            "head": {"sha": SHA, "ref": "roadmap/foo", "repo": {"full_name": "alice/TauCetiRoadmap"}},
            "html_url": f"https://github.com/{REPO}/pull/{number}"}
    reviews = [
        {"id": 71, "user": {"login": "kim"}, "state": "CHANGES_REQUESTED", "commit_id": SHA,
         "submitted_at": "2026-10-24T10:00:00Z", "body": "Changes needed: the hypothesis on p is missing.\r\n",
         "author_association": "MEMBER", "html_url": "x"},
        {"id": 72, "user": {"login": "alice"}, "state": "COMMENTED", "commit_id": SHA,
         "submitted_at": "2026-10-24T11:00:00Z", "body": ""},
        {"id": 73, "user": {"login": "ghost"}, "state": "APPROVED", "commit_id": None,
         "submitted_at": "2026-10-24T12:00:00Z", "body": "Looks right."}]
    inline = [
        {"id": 500, "pull_request_review_id": 71, "user": {"login": "kim"}, "created_at": "2026-10-24T09:59:00Z",
         "path": "TauCetiRoadmap/Foo/Suggested.lean", "line": None, "original_line": 4,
         "body": "Here: `foo` needs `[Fact p.Prime]`.", "diff_hunk": "@@"},
        {"id": 502, "pull_request_review_id": 72, "user": {"login": "alice"}, "created_at": "2026-10-24T11:00:00Z",
         "path": "TauCetiRoadmap/Foo/README.md", "line": 12, "original_line": 12, "body": "Noted; fixing it."}]
    comments = [
        {"id": 600, "user": {"login": "github-actions[bot]"}, "created_at": "2026-10-24T09:00:00Z",
         "body": "Merge order: ...", "author_association": "NONE"},
        {"id": 601, "user": {"login": "kim"}, "created_at": "2026-10-24T10:30:00Z",
         "body": "Request changes: the locator of Thm 2 is off by one page."}]
    return pull, reviews, comments, inline


def actor(login):
    """GraphQL's `author` for a REST login: a bot without its suffix, a deleted account as null."""
    if login == "ghost":
        return None
    if login.endswith("[bot]"):
        return {"__typename": "Bot", "login": login[:-len("[bot]")]}
    return {"__typename": "User", "login": login}


def graphql_node(pull, reviews, comments, inline, more=()):
    """The query's node for that pull request, with `hasNextPage` set on the connections in `more`."""
    def page(key):
        return {"hasNextPage": key in more}
    return {
        "number": pull["number"], "isDraft": pull["draft"], "state": "MERGED" if pull.get("merged") else pull["state"].upper(),
        "headRefOid": pull["head"]["sha"], "headRefName": pull["head"]["ref"],
        "headRepository": {"nameWithOwner": pull["head"]["repo"]["full_name"]} if pull["head"]["repo"] else None,
        "author": actor(pull["user"]["login"]),
        "labels": {"pageInfo": page("labels"), "nodes": [{"name": x["name"]} for x in pull["labels"]]},
        "reviews": {"pageInfo": page("reviews"), "nodes": [
            {"databaseId": r["id"], "state": r["state"], "submittedAt": r["submitted_at"], "body": r["body"],
             "author": actor(r["user"]["login"]), "commit": {"oid": r["commit_id"]} if r["commit_id"] else None,
             "comments": {"pageInfo": page(f"review-{r['id']}"), "nodes": [
                 {"databaseId": c["id"], "createdAt": c["created_at"], "path": c["path"], "line": c["line"],
                  "originalLine": c["original_line"], "body": c["body"], "author": actor(c["user"]["login"])}
                 for c in inline if c["pull_request_review_id"] == r["id"]]}}
            for r in reviews]},
        "comments": {"pageInfo": page("comments"), "nodes": [
            {"databaseId": c["id"], "createdAt": c["created_at"], "body": c["body"], "author": actor(c["user"]["login"])}
            for c in comments]}}


class BatchGH:
    """GitHub for the content repository's pull requests (`pulls`: number -> REST answers), by REST and
    by GraphQL; `more`: number -> the connections that have another page; `failing`: queries that
    fail. Every REST read and GraphQL query is counted."""

    def __init__(self, pulls, more=None, failing=0):
        self.pulls, self.more, self.failing = pulls, dict(more or {}), failing
        self.rest, self.queries, self.writes = [], [], []

    def api(self, path, paginate=False):
        self.rest.append(path)
        m = re.fullmatch(rf"repos/{REPO}/(pulls|issues)/(\d+)(/reviews|/comments)?(\?per_page=100)?", path)
        if not m or int(m.group(2)) not in self.pulls:
            raise GHError(f"GET {path}: 404")
        pull, reviews, comments, inline = self.pulls[int(m.group(2))]
        found = {("pulls", None): pull, ("pulls", "/reviews"): reviews, ("issues", "/comments"): comments,
                 ("pulls", "/comments"): inline}[(m.group(1), m.group(3))]
        return copy.deepcopy(found)

    def graphql(self, query, **variables):
        self.queries.append(query)
        if self.failing:
            self.failing -= 1
            raise GHError("graphql: [{'message': 'Something went wrong'}]")
        assert variables == {"owner": "TauCetiProject", "name": "TauCetiRoadmap"}
        numbers = [int(n) for n in re.findall(r"p(\d+): pullRequest\(number: \1\)", query)]
        return {"repository": {f"p{n}": graphql_node(*self.pulls[n], more=self.more.get(n, ())) if n in self.pulls
                               else None for n in numbers}}

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        return {}


def test_a_pull_request_read_by_graphql_has_the_shapes_of_its_rest_reads():
    sample = rest_sample()
    gh = BatchGH({905: sample})
    reader = prbatch.PullReader(gh, REPO, [905], 15)
    got = reader.get(905)
    pull, reviews, comments, inline = sample
    assert gh.rest == [] and len(gh.queries) == 1 and reader.summary() == "pull requests read: 1 by GraphQL in 1 query, 0 by REST"
    # What the label, draft and gate code reads of the pull request.
    for key in ("number", "draft", "state", "user", "head"):
        assert got.pull[key] == pull[key], key
    assert [x["name"] for x in got.pull["labels"]] == [x["name"] for x in pull["labels"]]
    # Every field the timeline reads, list for list, the bot's and the deleted account's logins included.
    for mine, theirs, keys in ((got.reviews, reviews, ("id", "user", "state", "commit_id", "submitted_at", "body")),
                               (got.comments, comments, ("id", "user", "created_at", "body")),
                               (got.inline, inline, ("id", "pull_request_review_id", "user", "created_at", "path",
                                                     "line", "original_line", "body"))):
        assert [{k: x[k] for k in keys} for x in mine] == [{k: x[k] for k in keys} for x in theirs]
    assert events_from_api(got.reviews, got.comments, got.inline) == events_from_api(reviews, comments, inline)
    # A review's text keeps its line ends: the answered records' versions are digests of it.
    assert replies.text_version(got.reviews[0]["body"]) == replies.text_version(reviews[0]["body"])


@pytest.mark.parametrize("more", [("reviews",), ("comments",), ("labels",), ("review-71",)])
def test_a_pull_request_with_more_than_a_page_of_anything_is_read_by_rest(more):
    gh = BatchGH({905: rest_sample(), 906: rest_sample(906)}, more={905: more})
    reader = prbatch.PullReader(gh, REPO, [906, 905], 15)
    assert reader.get(906).reviews and gh.rest == []
    got = reader.get(905)
    assert gh.rest == [f"repos/{REPO}/pulls/905", f"repos/{REPO}/pulls/905/reviews?per_page=100",
                       f"repos/{REPO}/issues/905/comments?per_page=100", f"repos/{REPO}/pulls/905/comments?per_page=100"]
    assert got.pull["html_url"].endswith("/pull/905") and reader.rest_reads == 1


def test_the_pull_requests_are_split_into_batches_and_a_failed_query_falls_back_to_rest():
    numbers = list(range(1, 33))
    gh = BatchGH({n: rest_sample(n) for n in numbers}, failing=1)
    reader = prbatch.PullReader(gh, REPO, reversed(numbers), 15)
    assert [len(re.findall(r"pullRequest\(number:", q)) for q in gh.queries] == [15, 15, 2]
    assert sorted(reader.found) == numbers[15:]                      # the first query failed
    for n in numbers:
        assert reader.get(n).pull["number"] == n
    assert len(gh.rest) == 15 * 4 and reader.summary() == "pull requests read: 17 by GraphQL in 3 queries, 15 by REST"
    # The parts alone: one REST read each when the batch does not hold the pull request.
    assert prbatch.PullReader(gh, REPO, [5], 0).comments(5) == rest_sample(5)[2] and gh.rest[-1].endswith("/comments?per_page=100")
    off = prbatch.PullReader(gh, REPO, numbers, 0)
    assert off.queries == 0 and off.pull(3)["number"] == 3 and gh.rest[-1] == f"repos/{REPO}/pulls/3"


# --- board sync with the batch reader ------------------------------------------------------------------

def roadmap_prs(numbers):
    return [{"pr": n, "name": f"Foo{n}", "head": SHA, "branch": "roadmap/foo", "owner": "alice"} for n in numbers]


def test_board_sync_reads_the_pull_requests_by_graphql_and_says_the_same(tmp_path, monkeypatch):
    numbers = list(range(901, 921))
    monkeypatch.setattr(intake, "open_roadmap_prs", lambda ctx: roadmap_prs(numbers))
    outs = {}
    for batch in (15, 0):
        gh = BatchGH({n: rest_sample(n) for n in numbers}, more={905: ("comments",)})
        cfg = example_config()
        cfg.board.graphql_batch = batch
        ctx = SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(), state=tmp_path)
        outs[batch] = intake.sync_pull_requests(ctx), gh
    (fast, gh), (slow, rest_gh) = outs[15], outs[0]
    assert fast[:-1] == slow[:-1] and "#901 Foo901: awaiting-review" in fast
    assert fast[-1] == "pull requests read: 19 by GraphQL in 2 queries, 1 by REST"
    assert slow[-1] == "pull requests read: 0 by GraphQL in 0 queries, 20 by REST"
    assert len(gh.rest) == 4 and {p.split("/")[4].split("?")[0] for p in gh.rest} == {"905"} and len(rest_gh.rest) == 80
    assert gh.writes == rest_gh.writes


# --- the verdicts of finished reviews --------------------------------------------------------------------

def test_the_verdicts_are_read_as_blobs_and_a_missing_one_is_none():
    class BlobGH:
        def __init__(self):
            self.queries = []

        def graphql(self, query, **variables):
            self.queries.append(query)
            paths = re.findall(r'f\d+: object\(expression: "main:([^"]+)"\)', query)
            return {"repository": {f"f{i}": ({"text": f"text of {p}"} if "Foo" in p else None)
                                   for i, p in enumerate(paths)}}
    gh = BlobGH()
    paths = [f"packages/Foo{n}/reports/PRREVIEW-Foo{n}~1.json" for n in range(70)] + ["packages/Bar/x.json"]
    texts = prbatch.verdict_texts(gh, "TauCetiProject/TauCetiRoadmapJobs", paths, 15)
    assert len(gh.queries) == 2 and texts["packages/Bar/x.json"] is None
    assert texts["packages/Foo3/reports/PRREVIEW-Foo3~1.json"] == "text of packages/Foo3/reports/PRREVIEW-Foo3~1.json"
    assert prbatch.verdict_texts(gh, "o/r", paths, 0) == {} and len(gh.queries) == 2


def test_the_label_rule_reads_the_verdicts_it_needs_in_one_query(monkeypatch):
    from .test_prreview import ContentGH, node, review_job, verdict_file
    H1 = "1" * 40
    jobs = [review_job(40 + n, 1, 10 + n, H1, "done", name=f"Foo{n}", folder=f"Foo{n}") for n in range(3)]
    files = {}
    for job in jobs:
        files.update(verdict_file(job, "accepted", H1))

    class VerdictGH(ContentGH):
        blob_queries, raw_reads = 0, 0

        def graphql(self, query, **variables):
            if "object(expression:" not in query:
                return super().graphql(query, **variables)
            self.blob_queries += 1
            paths = re.findall(r'f\d+: object\(expression: "main:([^"]+)"\)', query)
            return {"repository": {f"f{i}": {"text": self.files[p]} if p in self.files else None
                                   for i, p in enumerate(paths)}}

        def raw_file(self, repo, path, ref=""):
            self.raw_reads += 1
            return super().raw_file(repo, path, ref)
    nodes = [node(10 + n, H1, paths=(f"TauCetiRoadmap/Foo{n}/README.md",)) for n in range(3)]
    gh = VerdictGH(nodes, files=files)
    cfg = example_config()
    cfg.prreview.enabled = True
    out = intake.sync_pr_reviews(SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(jobs=list(jobs)), operator="bot",
                                                 dry_run=False))
    assert [line for line in out if "ai-approved added" in line] == [
        f"#{10 + n}: ai-approved added (the AI review of {H1[:7]} accepts it)" for n in range(3)]
    assert (gh.blob_queries, gh.raw_reads) == (1, 0)
    cfg.board.graphql_batch = 0
    gh = VerdictGH(nodes, files=files)
    intake.sync_pr_reviews(SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(jobs=list(jobs)), operator="bot", dry_run=False))
    assert (gh.blob_queries, gh.raw_reads) == (0, 3)


def test_the_batch_size_is_configurable_and_never_negative():
    assert config.Config().board.graphql_batch == 15
    assert config.parse('[project]\nname = "P"\n[board]\ngraphql_batch = 0\n', "x.toml").board.graphql_batch == 0
    with pytest.raises(config.ConfigError, match="board.graphql_batch must be 0 or more"):
        config.parse('[project]\nname = "P"\n[board]\ngraphql_batch = -1\n', "x.toml")
