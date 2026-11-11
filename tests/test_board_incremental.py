"""The board's incremental reads: one full listing a run, then only what changed since."""
from __future__ import annotations

from pathlib import Path

from tauceti_roadmap_worker import board, config
from tauceti_roadmap_worker.board import encode_spec, new_job, since_before
from tauceti_roadmap_worker.gh import GH
from tauceti_roadmap_worker.paths import HERE

LISTING = "issues?labels=roadmap-job&state=all&per_page=100"


def issue(number: int, job_id: str, state: str, at: str, closed: bool = False) -> dict:
    job = new_job(job_id)
    return {"number": number, "title": board.title_for(job), "body": encode_spec(job), "updated_at": at,
            "state": "closed" if closed else "open",
            "labels": [{"name": "roadmap-job"}, {"name": f"kind:{job.kind}"}, {"name": f"state:{state}"}]}


class FakeGH:
    def __init__(self, issues):
        self.issues, self.paths, self.dry_run, self.fence = {i["number"]: i for i in issues}, [], False, None

    def api(self, path, paginate=False):
        self.paths.append(path)
        assert "/issues?" in path, path
        rows = sorted(self.issues.values(), key=lambda i: -i["number"])
        if "&since=" in path:
            since = path.split("&since=", 1)[1]
            rows = [i for i in rows if i["updated_at"] >= since]
        if ",kind:" in path:
            kind = path.split(",kind:", 1)[1].split("&", 1)[0]
            rows = [i for i in rows if {"name": f"kind:{kind}"} in i["labels"]]
        return [dict(i) for i in rows]

    def write(self, method, path, payload=None):
        number = max(self.issues) + 1
        created = {"number": number, "title": payload["title"], "body": payload["body"], "state": "open",
                   "updated_at": "2026-11-11T05:10:00Z", "labels": [{"name": n} for n in payload["labels"]]}
        self.hidden = created             # the listing does not show it yet
        return created


def a_board(issues, incremental=True):
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")
    gh = FakeGH(issues)
    b = board.Board(cfg, gh=gh)
    b.incremental = incremental
    return b, gh


def test_the_since_of_a_listing_is_the_newest_change_less_a_margin():
    assert since_before("2026-11-11T05:00:10Z") == "2026-11-11T04:55:10Z"
    assert since_before("2026-11-11T00:01:00Z", 120) == "2026-11-10T23:59:00Z"
    assert since_before("") == "" and since_before(None) == "" and since_before("yesterday") == ""


def test_a_run_reads_the_whole_board_once_and_then_only_what_changed():
    b, gh = a_board([issue(1, "PKG-Foo", "done", "2026-11-11T01:00:00Z", closed=True),
                     issue(2, "REV-PKG-Foo", "available", "2026-11-11T05:00:00Z")])
    assert [(j.id, j.state) for j in b.all_jobs()] == [("REV-PKG-Foo", "available"), ("PKG-Foo", "done")]
    assert gh.paths == [f"repos/{b.repo}/{LISTING}"]
    # A worker claims the review and the intake's next read sees it, and a job filed meanwhile, without
    # reading the first job again.
    gh.issues[2] = issue(2, "REV-PKG-Foo", "claimed", "2026-11-11T05:03:00Z")
    gh.issues[3] = issue(3, "PKG-Bar", "available", "2026-11-11T05:04:00Z")
    assert [(j.id, j.state) for j in b.all_jobs()] == [("PKG-Bar", "available"), ("REV-PKG-Foo", "claimed"),
                                                        ("PKG-Foo", "done")]
    assert gh.paths[-1] == f"repos/{b.repo}/{LISTING}&since=2026-11-11T04:55:00Z" and len(gh.paths) == 2
    # Nothing changed: the same jobs, as new objects (a caller may have changed the ones it was given).
    first, second = b.all_jobs(), b.all_jobs()
    assert [j.spec() for j in first] == [j.spec() for j in second] and first[0] is not second[0]
    assert gh.paths[-1].endswith("&since=2026-11-11T04:59:00Z")


def test_a_change_inside_the_margin_that_a_lagging_listing_showed_late_is_still_read():
    b, gh = a_board([issue(1, "PKG-Foo", "available", "2026-11-11T05:00:00Z")])
    b.all_jobs()
    gh.issues[2] = issue(2, "PKG-Bar", "available", "2026-11-11T05:02:00Z")
    b.all_jobs()                                # the listing showed #2 but not yet #1's change of 05:01
    gh.issues[1] = issue(1, "PKG-Foo", "claimed", "2026-11-11T05:01:00Z")
    assert {j.id: j.state for j in b.all_jobs()} == {"PKG-Foo": "claimed", "PKG-Bar": "available"}


def test_without_the_flag_every_read_is_the_whole_listing():
    b, gh = a_board([issue(1, "PKG-Foo", "available", "2026-11-11T05:00:00Z")], incremental=False)
    b.all_jobs(), b.all_jobs()
    b.kind_jobs("package")
    assert gh.paths == [f"repos/{b.repo}/{LISTING}"] * 2 + [
        f"repos/{b.repo}/issues?labels=roadmap-job,kind:package&state=all&per_page=100"]
    assert b._seen is None


def test_issues_without_a_timestamp_are_read_in_full_every_time():
    rows = [issue(1, "PKG-Foo", "available", "")]
    b, gh = a_board(rows)
    b.all_jobs(), b.all_jobs()
    assert gh.paths == [f"repos/{b.repo}/{LISTING}"] * 2


def test_the_listing_by_kind_comes_from_the_issues_held_once_the_board_was_read():
    b, gh = a_board([issue(1, "PKG-Foo", "done", "2026-11-11T01:00:00Z", closed=True),
                     issue(2, "REV-PKG-Foo", "available", "2026-11-11T05:00:00Z")])
    assert [j.id for j in b.kind_jobs("review")] == ["REV-PKG-Foo"]      # not read yet: one listing by label
    assert ",kind:review" in gh.paths[-1] and b._seen is None
    b.all_jobs()
    assert [j.id for j in b.kind_jobs("review")] == ["REV-PKG-Foo"] and [j.id for j in b.kind_jobs("package")] == ["PKG-Foo"]
    assert all(",kind:" not in p for p in gh.paths[1:]) and all("&since=" in p for p in gh.paths[2:])
    assert ",kind:review&state=open" in (b.kind_jobs("review", "open"), gh.paths[-1])[1]   # open only: a listing


def test_a_job_the_run_created_is_held_before_the_listing_shows_it():
    b, gh = a_board([issue(1, "PKG-Foo", "done", "2026-11-11T05:00:00Z", closed=True)])
    b.all_jobs()
    review = new_job("REV-PKG-Foo")
    number = b.create(review)
    assert number == 2 and review.number == 2
    assert {j.id: j.state for j in b.all_jobs()} == {"PKG-Foo": "done", "REV-PKG-Foo": "available"}
    assert b.create(new_job("REV-PKG-Foo")) == 2 and b.reused == {"REV-PKG-Foo": 2}     # and is not created twice


def test_an_existing_job_is_found_among_the_issues_held():
    b, gh = a_board([issue(1, "REV-PKG-Foo", "done", "2026-11-11T05:00:00Z", closed=True)])
    b.all_jobs()
    assert b.existing(new_job("REV-PKG-Foo")) == 1 and b.existing(new_job("REV-PKG-Bar")) == 0
    assert all(",kind:" not in p for p in gh.paths)


def test_a_moving_since_never_goes_through_the_etag_cache(tmp_path: Path):
    asked = []

    class Plain(GH):
        def _read(self, args):
            asked.append(args)
            return "[]"

        def _cached_pages(self, path):
            raise AssertionError(f"cached: {path}")
    gh = Plain(cache_dir=tmp_path)
    assert gh.api("repos/o/r/issues?labels=roadmap-job&state=all&per_page=100&since=2026-11-11T04:55:00Z",
                  paginate=True) == []
    assert asked[0][:2] == ["api", "--paginate"] and not list(tmp_path.iterdir())
    try:
        gh.api("repos/o/r/issues/7/comments?per_page=100&since=2026-11-11T04:55:00Z", paginate=True)
    except AssertionError as exc:               # a thread read `since` a fixed moment keeps its cache entry
        assert "cached" in str(exc)
    else:
        raise AssertionError("the comments thread did not go through the cache")
