"""`board sync` reads the open roadmap pull requests by GraphQL, `[board] graphql_batch` at a time, in
the shapes the REST reads give (`prbatch.PullReader`), and the verdicts of finished reviews as blobs
(`prbatch.verdict_texts`); REST reads what a page does not hold and what a failed query left. The git
objects at full commit shas (`tree_ids`, `tree_entries`, `blob_texts`) are read once into the object
cache of a board run (`prbatch.use_cache`)."""

import copy
import hashlib
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import cli, config, intake, prbatch, replies
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
            "base": {"ref": "roadmap/parent", "sha": "f" * 40}, "html_url": f"https://github.com/{REPO}/pull/{number}"}
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
        "headRefOid": pull["head"]["sha"], "headRefName": pull["head"]["ref"], "baseRefName": pull["base"]["ref"],
        "headRepository": {"nameWithOwner": pull["head"]["repo"]["full_name"]} if pull["head"]["repo"] else None,
        "author": actor(pull["user"]["login"]),
        "labels": {"pageInfo": page("labels"), "nodes": [{"name": x["name"]} for x in pull["labels"]]},
        "reviews": {"pageInfo": page("reviews"), "nodes": [
            {"databaseId": r["id"], "state": r["state"], "submittedAt": r["submitted_at"], "body": r["body"],
             "author": actor(r["user"]["login"]), "commit": {"oid": r["commit_id"]} if r["commit_id"] else None,
             "comments": {"pageInfo": page(f"review-{r['id']}"), "nodes": [
                 {"databaseId": c["id"], "createdAt": c["created_at"], "path": c["path"], "line": c["line"],
                  "originalLine": c["original_line"], "body": c["body"], "author": actor(c["user"]["login"]),
                  "replyTo": {"databaseId": c["in_reply_to_id"]} if c.get("in_reply_to_id") else None}
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
    assert got.pull["base"]["ref"] == pull["base"]["ref"] == "roadmap/parent"            # a stacked pull request
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


def test_a_review_state_and_an_inline_thread_read_by_graphql_are_those_rest_gives():
    """What tells a review that points out errors (`replies.unanswered_errors`): its state, and which
    inline comment a reply in a thread answers (`in_reply_to_id`, GraphQL's `replyTo`)."""
    pull, reviews, comments, inline = rest_sample()
    inline = inline + [{"id": 503, "pull_request_review_id": 72, "in_reply_to_id": 500, "user": {"login": "alice"},
                        "created_at": "2026-10-24T11:01:00Z", "path": "TauCetiRoadmap/Foo/Suggested.lean",
                        "line": None, "original_line": 4, "body": "Added."}]
    got = prbatch.PullReader(BatchGH({905: (pull, reviews, comments, inline)}), REPO, [905], 15).get(905)
    assert [c.get("in_reply_to_id") for c in got.inline] == [None, None, 500]
    mine, theirs = events_from_api(got.reviews, got.comments, got.inline), events_from_api(reviews, comments, inline)
    assert mine == theirs and [e.reply_to for e in mine if e.ref.startswith("inline-")] == [0, 0, 500]
    assert [e.ref for e in mine if e.changes_requested] == ["review-71"]


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
    assert [line for line in out if "ai-found-no-errors added" in line] == [
        f"#{10 + n}: ai-found-no-errors added (the AI review of {H1[:7]} accepts it)" for n in range(3)]
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


def test_titles_and_descriptions_are_read_in_batches_and_what_a_query_leaves_out_by_rest():
    class TextGH:
        def __init__(self):
            self.queries, self.rest = [], []

        def graphql(self, query, **variables):
            self.queries.append(query)
            if "p3:" in query:
                raise GHError("graphql: 502")
            numbers = [int(n) for n in re.findall(r"p(\d+): pullRequest\(number: \1\) \{ title body \}", query)]
            return {"repository": {f"p{n}": {"title": f"T{n}", "body": f"B{n}"} for n in numbers if n != 2}}

        def api(self, path, paginate=False):
            self.rest.append(path)
            n = int(path.rsplit("/", 1)[1])
            if n == 4:
                raise GHError(f"GET {path}: 502")
            return {"title": f"rest {n}", "body": None}

    gh = TextGH()
    got = prbatch.pull_texts(gh, REPO, [1, 2, 3, 4, 1], 2)
    assert len(gh.queries) == 2                                   # [1, 2] and [3, 4]; the second fails
    assert got == {1: {"title": "T1", "body": "B1"}, 2: {"title": "rest 2", "body": ""},
                   3: {"title": "rest 3", "body": ""}, 4: None}
    assert gh.rest == [f"repos/{REPO}/pulls/{n}" for n in (2, 3, 4)]


# --- the object cache of the git objects at full commit shas ---------------------------------------------

H1, H2 = "a1" * 20, "b2" * 20
FOLDER, README = "TauCetiRoadmap/Foo", "TauCetiRoadmap/Foo/README.md"
FOLDERS, READMES = [(H1, FOLDER), (H2, FOLDER)], [(H1, README), (H2, README)]


def git_objects():
    """(commit, path) -> a folder's entries {name: id}, or a file's text."""
    return {(H1, FOLDER): {"README.md": "c" * 40, "Suggested.lean": "d" * 40},
            (H2, FOLDER): {"README.md": "e" * 40, "Suggested.lean": "d" * 40},
            (H1, README): "# Foo\n\n## Statement\n\nThe theorem.\n",
            (H2, README): "# Foo\n\n## Proof\n\nThe proof.\n\n## Statement\n\nThe theorem.\n"}


def oid(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def asked(query):
    """The (commit, path) pairs a query asks for."""
    return {tuple(json.loads(e).split(":", 1)) for e in re.findall(r'\w+: object\(expression: ("[^"]*")\)', query)}


class ObjectGH:
    """GitHub's git objects (`objects`: see `git_objects`), by GraphQL (`object(expression:)` fields:
    `t` an id, `e` a folder's entries, `b` a file's text, cut short past `cut` characters) and by REST
    (`raw_file`); `failing`: queries that fail. Every query and REST read is recorded."""

    def __init__(self, objects, failing=0, cut=10 ** 6):
        self.objects, self.failing, self.cut = dict(objects), failing, cut
        self.queries, self.raw = [], []

    def graphql(self, query, **variables):
        self.queries.append(query)
        if self.failing:
            self.failing -= 1
            raise GHError("graphql: [{'message': 'Something went wrong'}]")
        out = {}
        for alias, expression in re.findall(r'(\w+): object\(expression: ("[^"]*")\)', query):
            obj = self.objects.get(tuple(json.loads(expression).split(":", 1)))
            if obj is None:
                out[alias] = None
            elif alias[0] == "t":
                out[alias] = {"oid": oid(obj)}
            elif alias[0] == "e":
                out[alias] = {"entries": [{"name": n, "oid": o} for n, o in obj.items()]} if isinstance(obj, dict) else {}
            else:
                out[alias] = {"text": obj[:self.cut], "isTruncated": len(obj) > self.cut} if isinstance(obj, str) else {}
        return {"repository": out}

    def raw_file(self, repo, path, ref=""):
        self.raw.append((ref, path))
        if not isinstance(self.objects.get((ref, path)), str):
            raise GHError(f"gh api: repos/{repo}/contents/{path}: Not Found (HTTP 404)")
        return self.objects[(ref, path)]


def read_all(gh, repo=REPO):
    """The three reads: the ids and the entries of the folder, and its README, at both commits."""
    return (prbatch.tree_ids(gh, repo, FOLDERS, 15),
            prbatch.tree_entries(gh, repo, [(H2, FOLDER), (H1, FOLDER), (H2, FOLDER)], 15),
            prbatch.blob_texts(gh, repo, READMES, 15))


def read_all_answers():
    objects = git_objects()
    return ({pair: oid(objects[pair]) for pair in FOLDERS}, {pair: objects[pair] for pair in FOLDERS},
            {pair: objects[pair] for pair in READMES})


@pytest.fixture
def cache_off():
    """Whatever object cache the test sets is switched off when it ends: none leaks into another test."""
    yield
    prbatch.use_cache(None)


@pytest.fixture
def cache(tmp_path, cache_off):
    """The object cache in the test's own state directory."""
    found = prbatch.use_cache(tmp_path / "state" / "git-objects.sqlite")
    assert found is not None
    return found


def test_a_second_read_of_the_same_git_objects_asks_github_nothing(cache):
    gh = ObjectGH(git_objects())
    assert read_all(gh) == read_all_answers() and len(gh.queries) == 3
    again = read_all(gh)
    assert again == read_all_answers() and len(gh.queries) == 3 and gh.raw == []
    assert {type(v) for v in again[0].values()} == {type(v) for v in again[2].values()} == {str}
    assert prbatch.cache_lines() == ["git objects: 6 cached, 6 read"]


def test_only_the_pairs_the_cache_lacks_are_asked_for(cache):
    objects = git_objects()
    new = [(H1, f"TauCetiRoadmap/Bar{n}") for n in range(8)]
    objects.update({pair: {"README.md": f"{n:040x}"} for n, pair in enumerate(new)})
    gh = ObjectGH(objects)
    assert prbatch.tree_ids(gh, REPO, FOLDERS, 1) == read_all_answers()[0] and len(gh.queries) == 1
    got = prbatch.tree_ids(gh, REPO, [(H2, FOLDER)] + new + [(H1, FOLDER)], 1)
    assert got == {pair: oid(objects[pair]) for pair in FOLDERS + new}
    assert [asked(q) for q in gh.queries[1:]] == [set(new[:4]), set(new[4:])]      # four pairs a query (batch 1)
    # what the cache holds of one kind answers nothing of another, nor of another repository
    assert prbatch.tree_entries(gh, REPO, FOLDERS, 1) == read_all_answers()[1] and asked(gh.queries[-1]) == set(FOLDERS)
    assert prbatch.tree_ids(gh, "someone/TauCetiRoadmap", FOLDERS, 1) == read_all_answers()[0]
    assert asked(gh.queries[-1]) == set(FOLDERS) and len(gh.queries) == 5


def test_what_may_still_change_is_never_cached(cache):
    """A commit not named by its full sha (an abbreviation, a branch, capitals, one digit too many),
    and an answer None (a commit or a path not there, a file read as a folder or the other way round),
    are asked for again every time."""
    objects = git_objects()
    names = ["a1a1a1a", "main", H1.upper(), H1 + "0"]
    for name in names:
        objects.update({(name, FOLDER): objects[(H1, FOLDER)], (name, README): objects[(H1, README)]})
    gone = "9" * 40
    for read, path, other in ((prbatch.tree_ids, FOLDER, README), (prbatch.tree_entries, FOLDER, README),
                              (prbatch.blob_texts, README, FOLDER)):
        gh = ObjectGH(objects)
        wanted = [(name, path) for name in names] + [(gone, path), (H1, "TauCetiRoadmap/Gone")]
        if read is not prbatch.tree_ids:
            wanted.append((H1, other))
        first = read(gh, REPO, wanted, 15)
        assert first[(names[0], path)] is not None and first[(gone, path)] is None
        assert sum(v is None for v in first.values()) == len(wanted) - len(names)
        assert read(gh, REPO, wanted, 15) == first
        assert len(gh.queries) == 2 and asked(gh.queries[0]) == asked(gh.queries[1]) == set(wanted), read.__name__


def test_a_pair_of_a_failed_query_is_not_cached(cache):
    """Not even a text REST then read whole."""
    for read, wanted in ((prbatch.tree_ids, FOLDERS), (prbatch.tree_entries, FOLDERS), (prbatch.blob_texts, READMES)):
        gh = ObjectGH(git_objects(), failing=1)
        answers = {pair: (oid(git_objects()[pair]) if read is prbatch.tree_ids else git_objects()[pair])
                   for pair in wanted}
        assert read(gh, REPO, wanted, 15) == ({} if read is not prbatch.blob_texts else answers)
        assert read(gh, REPO, wanted, 15) == answers and len(gh.queries) == 2            # asked for again
        assert read(gh, REPO, wanted, 15) == answers and len(gh.queries) == 2            # now from the cache
        assert len(gh.raw) == (2 if read is prbatch.blob_texts else 0)


def test_a_text_graphql_cuts_short_is_cached_whole_once_rest_read_it(cache):
    gh = ObjectGH(git_objects(), cut=12)
    whole = read_all_answers()[2]
    assert prbatch.blob_texts(gh, REPO, READMES, 15) == whole and sorted(gh.raw) == READMES
    assert prbatch.blob_texts(gh, REPO, READMES, 15) == whole and len(gh.raw) == 2 and len(gh.queries) == 1
    # one REST could not read is left out, and asked for again
    other = "c3" * 20
    gh = ObjectGH({(other, README): git_objects()[(H2, README)]}, cut=12)
    gh.raw_file = lambda repo, path, ref="": (_ for _ in ()).throw(GHError("gh api: HTTP 502: Bad Gateway"))
    assert prbatch.blob_texts(gh, REPO, [(other, README)], 15) == {}
    assert prbatch.blob_texts(gh, REPO, [(other, README)], 15) == {} and len(gh.queries) == 2


def test_folder_entries_come_back_from_the_file_as_they_were_read(tmp_path, cache_off):
    path = tmp_path / "state" / "git-objects.sqlite"
    gh = ObjectGH(git_objects())
    prbatch.use_cache(path)
    first = read_all(gh)
    prbatch.use_cache(None)
    prbatch.use_cache(path)                  # another run: the file is all it has of the first
    again = read_all(gh)
    assert again == first == read_all_answers() and len(gh.queries) == 3
    assert prbatch.cache_lines() == ["git objects: 6 cached, 0 read"]
    entries = again[1]
    assert {type(v) for v in entries.values()} == {dict}
    assert {type(x) for v in entries.values() for item in v.items() for x in item} == {str}
    assert list(entries[(H1, FOLDER)].items()) == [("README.md", "c" * 40), ("Suggested.lean", "d" * 40)]


def test_a_cache_file_that_cannot_be_used_changes_nothing(tmp_path, cache_off):
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database at all " * 200)
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder\n")
    for path in (corrupt, blocker / "git-objects.sqlite", tmp_path):        # (the last: a folder)
        assert prbatch.use_cache(path) is None and prbatch.cache_lines() == []
        gh = ObjectGH(git_objects())
        assert read_all(gh) == read_all(gh) == read_all_answers() and len(gh.queries) == 6
    assert corrupt.read_bytes() == b"not a database at all " * 200


def test_a_cache_that_fails_in_use_changes_nothing(cache):
    """A file locked, corrupt or on a full disk once the run has opened it."""
    gh = ObjectGH(git_objects())
    assert read_all(gh) == read_all_answers() and len(gh.queries) == 3

    class Broken:
        def execute(self, *args):
            raise sqlite3.DatabaseError("database disk image is malformed")

        def executemany(self, *args):
            raise sqlite3.OperationalError("database or disk is full")

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            pass
    cache.db.close()
    cache.db = Broken()
    assert read_all(gh) == read_all(gh) == read_all_answers() and len(gh.queries) == 9


def test_a_cache_another_process_holds_is_done_without(tmp_path, cache_off, monkeypatch):
    monkeypatch.setattr(prbatch, "CACHE_TIMEOUT", 0.05)
    path = tmp_path / "git-objects.sqlite"
    assert prbatch.use_cache(path) is not None
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    gh = ObjectGH(git_objects())
    assert read_all(gh) == read_all(gh) == read_all_answers() and len(gh.queries) == 6     # nothing kept
    other.execute("ROLLBACK")
    other.close()
    assert read_all(gh) == read_all(gh) == read_all_answers() and len(gh.queries) == 9     # kept, then read


def test_rows_unused_for_three_weeks_are_dropped_when_the_cache_is_opened(tmp_path):
    path, day, start = tmp_path / "git-objects.sqlite", 86400, 1_000_000_000.0
    assert prbatch.CACHE_DAYS == 21
    first = prbatch.ObjectCache(path, now=start)
    first.put("tree_id", REPO, {(H1, FOLDER): "1" * 40, (H2, FOLDER): "2" * 40})
    first.close()
    later = prbatch.ObjectCache(path, now=start + 10 * day)
    assert later.get("tree_id", REPO, [(H1, FOLDER)]) == {(H1, FOLDER): "1" * 40}         # used again
    later.close()
    last = prbatch.ObjectCache(path, now=start + 22 * day)
    db = sqlite3.connect(path)
    assert db.execute("SELECT sha, used FROM objects").fetchall() == [(H1, start + 10 * day)]
    db.close()
    assert last.get("tree_id", REPO, FOLDERS) == {(H1, FOLDER): "1" * 40}
    last.close()


def test_without_use_cache_nothing_is_cached_or_written(tmp_path, monkeypatch):
    monkeypatch.setattr(prbatch.sqlite3, "connect", lambda *a, **k: pytest.fail("an object cache was opened"))
    for name in ("HOME", "XDG_STATE_HOME", "TAUCETI_PROJECT_STATE"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.chdir(tmp_path)
    assert prbatch.cache_lines() == []
    gh = ObjectGH(git_objects())
    assert read_all(gh) == read_all(gh) == read_all_answers() and len(gh.queries) == 6
    assert list(tmp_path.iterdir()) == []


def test_a_board_run_reads_git_objects_through_the_cache_in_its_state_directory(tmp_path, monkeypatch, capsys,
                                                                              cache_off):
    cfg = example_config()
    gh = ObjectGH(git_objects())

    def board_run(args, cfg, ctx):           # two steps that read the same trees
        for _ in range(2):
            assert prbatch.tree_ids(ctx.gh, REPO, FOLDERS, 15) == read_all_answers()[0]
        return 0
    monkeypatch.setattr(cli, "_load", lambda args: cfg)
    monkeypatch.setattr(cli, "_bot_context", lambda args, cfg, dry: SimpleNamespace(gh=gh, board=SimpleNamespace()))
    monkeypatch.setattr(cli, "api_budget", lambda gh, repo: ["GitHub API calls this run: 1 (rate limit not readable)"])
    monkeypatch.setattr(cli, "board_run", board_run)
    args = cli.parser().parse_args(["board", "sync"])
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    for lines in (["git objects: 2 cached, 2 read"], ["git objects: 4 cached, 0 read"]):
        assert cli.board_cmd(args) == 0
        assert capsys.readouterr().out.splitlines() == ["GitHub API calls this run: 1 (rate limit not readable)"] + lines
        assert prbatch.cache_lines() == []                              # off again once the run ends
    assert (tmp_path / "state" / "git-objects.sqlite").is_file() and len(gh.queries) == 1
    # a state directory the cache cannot be kept in: the run reads as it would without one
    (tmp_path / "elsewhere").write_text("not a folder\n")
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "elsewhere"))
    assert cli.board_cmd(args) == 0
    assert capsys.readouterr().out.splitlines() == ["GitHub API calls this run: 1 (rate limit not readable)"]
    assert len(gh.queries) == 3
