import json
from pathlib import Path
from tauceti_roadmap_worker.gh import GH


def _gh(tmp_path, answers):
    """A GH whose `_run` plays scripted `gh api -i` answers (headers + body) and records the args."""
    gh = GH(); gh.cache_dir = tmp_path / "cache"; gh.read_delays = (); seen = []

    def run(args, stdin=None):
        seen.append(args); gh.calls += 1
        answer = answers.pop(0)
        if answer.startswith("HTTP/2.0 304"):          # gh exits 1 on a 304 and reports it on stderr
            from tauceti_roadmap_worker.gh import GHError
            raise GHError("gh api -i: HTTP 304 (https://api.github.com/...)")
        return answer
    gh._run = run
    return gh, seen


def test_a_plain_get_stores_the_etag_and_a_304_answers_from_the_cache(tmp_path):
    body = json.dumps({"number": 7, "title": "x"})
    gh, seen = _gh(tmp_path, [f"HTTP/2.0 200 OK\r\nEtag: W/\"abc\"\r\nX-Other: 1\r\n\r\n{body}",
                              "HTTP/2.0 304 Not Modified\r\nEtag: W/\"abc\"\r\n\r\n"])
    assert gh.api("repos/o/r/pulls/7") == {"number": 7, "title": "x"}
    assert seen[0] == ["api", "-i", "repos/o/r/pulls/7"] and gh.calls == 1 and gh.cache_hits == 0
    assert gh.api("repos/o/r/pulls/7") == {"number": 7, "title": "x"}      # the 304: body from the cache
    assert seen[1] == ["api", "-i", "repos/o/r/pulls/7", "-H", 'If-None-Match: W/"abc"']
    assert gh.calls == 1 and gh.cache_hits == 1                                # a 304 costs nothing
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1


def test_a_paginated_listing_reads_pages_with_their_own_etags_and_stops_at_a_short_page(tmp_path):
    page1 = json.dumps([{"n": i} for i in range(100)]); page2 = json.dumps([{"n": 100}])
    gh, seen = _gh(tmp_path, [f"HTTP/2.0 200 OK\r\nEtag: \"p1\"\r\n\r\n{page1}", f"HTTP/2.0 200 OK\r\nEtag: \"p2\"\r\n\r\n{page2}",
                              "HTTP/2.0 304 Not Modified\r\n\r\n", f"HTTP/2.0 200 OK\r\nEtag: \"p2b\"\r\n\r\n{json.dumps([{'n': 100}, {'n': 101}])}"])
    items = gh.api("repos/o/r/issues?state=open", paginate=True)
    assert len(items) == 101 and seen[0][2] == "repos/o/r/issues?state=open&per_page=100&page=1"
    assert seen[1][2] == "repos/o/r/issues?state=open&per_page=100&page=2" and gh.calls == 2
    items = gh.api("repos/o/r/issues?state=open", paginate=True)             # page 1 unchanged, page 2 grew
    assert len(items) == 102 and gh.cache_hits == 1 and gh.calls == 3


def test_without_a_cache_dir_or_with_jq_the_old_path_is_taken(tmp_path, monkeypatch):
    gh = GH(); gh.read_delays = (); seen = []
    gh._run = lambda args, stdin=None: (seen.append(args), gh.__setattr__("calls", gh.calls + 1), "[]")[2]
    assert gh.api("repos/o/r/pulls", paginate=True) == [] and seen[0] == ["api", "--paginate", "repos/o/r/pulls"]
    gh.cache_dir = tmp_path
    assert gh.api("repos/o/r/pulls", jq=".[].number") == "[]" and seen[1] == ["api", "repos/o/r/pulls", "--jq", ".[].number"]
