"""One GitHub blip never costs a whole board run (regression: hosted run 39580415820 exited 3 on "gh api
.../pulls/165: HTTP 503" while the intake read ~60 open board pull requests one GET at a time)."""

import json

import pytest

from tauceti_roadmap_worker import boardpr, intake
from tauceti_roadmap_worker.board import encode_submission
from tauceti_roadmap_worker.gh import GH, GHError

from .fakes import FakeBoard, bot_context


def test_reads_are_retried_after_a_transient_failure(monkeypatch):
    sleeps, calls = [], []
    gh = GH(sleep=sleeps.append)

    def run(args, stdin=None):
        calls.append(args)
        if len(calls) < 3:
            raise GHError("gh api repos/o/r/pulls/165: gh: HTTP 503")
        return '{"number": 165}'
    monkeypatch.setattr(gh, "_run", run)
    assert gh.api("repos/o/r/pulls/165") == {"number": 165} and sleeps == [2, 5]
    calls.clear()
    monkeypatch.setattr(gh, "_run", lambda args, stdin=None: (_ for _ in ()).throw(GHError("gh: HTTP 404: Not Found")))
    with pytest.raises(GHError, match="404"):
        gh.raw_file("o/r", "x.md")                                              # not transient: no retry
    always = GH(sleep=lambda s: None)
    monkeypatch.setattr(always, "_run", lambda args, stdin=None: (_ for _ in ()).throw(GHError("gh: HTTP 502")))
    with pytest.raises(GHError, match="502"):
        always.api("repos/o/r/pulls")                                           # three retries, then it stands


def pull(n, labels=(), head="h"):
    return {"number": n, "body": "Refs #1\n\n" + encode_submission(f"PKG-P{n}", "w1", "alice", "codex"),
            "labels": [{"name": label} for label in labels], "head": {"sha": head}, "updated_at": "2026-10-21T09:50:00Z"}


class ListGH:
    def __init__(self, pulls):
        self.pulls = pulls

    def api(self, path, paginate=False):
        if "pulls?state=open" in path:
            return self.pulls
        raise AssertionError(f"no GET per pull request: {path}")


def test_the_intake_reads_the_listing_once_and_goes_on_past_an_error(tmp_path, monkeypatch):
    pulls = [pull(164, ["needs-review"]), pull(165), pull(166)]
    seen = []

    def take(ctx, n, yes=False, pr=None):
        seen.append((n, pr is not None))
        if n == 165:
            raise GHError("gh api repos/o/jobs/pulls/165/files: gh: HTTP 503")
        return f"#{n}: under review on the board (read by the review scan)" if n == 164 else f"#{n}: not merged: x"
    monkeypatch.setattr(intake, "intake", take)
    ctx = bot_context(gh=ListGH(pulls))
    ctx.state = tmp_path
    lines = []
    assert intake.intake_open(ctx, yes=True, log=lines.append) == [165]
    assert seen == [(164, True), (165, True), (166, True)]                      # the listing's objects, no GET
    assert "#165: skipped: GitHub did not answer" in lines[1] and lines[2] == "#166: not merged: x"
    # The next run: unchanged pull requests with a stable outcome are not read again; #165 is.
    seen.clear()
    lines.clear()
    assert intake.intake_open(ctx, yes=True, log=lines.append) == [165]
    assert seen == [(165, True)] and lines[0].startswith("#164: unchanged since the last run")
    cached = json.loads((tmp_path / "intake-prs.json").read_text())
    assert set(cached) == {"164", "166"}
    # A new push (head) is read again.
    pulls[2]["head"]["sha"] = "h2"
    seen.clear()
    intake.intake_open(ctx, yes=True, log=lines.append)
    assert (166, True) in seen


def test_the_review_scan_goes_on_past_an_error():
    class ScanGH:
        def api(self, path, paginate=False):
            if "pulls?state=open" in path:
                return [{"number": n, "head": {"sha": "h"}, "labels": [{"name": "needs-review"}]} for n in (12, 13)]
            if "/issues/12/comments" in path:
                raise GHError("gh: HTTP 503")
            if "/issues/13/comments" in path:
                return []
            raise AssertionError(path)
    from tauceti_roadmap_worker.board import new_job
    jobs = []
    for n in (12, 13):
        job = new_job(f"REV-PKG-P{n}", inputs={"board_pr": n})
        job.number, job.state = 100 + n, "available"
        jobs.append(job)
    out = boardpr.scan(bot_context(gh=ScanGH(), board=FakeBoard(jobs=jobs)))
    assert out[0].startswith("#12: skipped: GitHub did not answer")


def test_a_board_run_ends_with_its_api_budget(monkeypatch):
    """The hosted workflow ran 61 times in an hour on comment events; each run now says what it used."""
    from tauceti_roadmap_worker import cli
    gh = GH(sleep=lambda s: None)
    headers = "HTTP/2.0 200 OK\nX-Ratelimit-Limit: 1000\nX-Ratelimit-Remaining: 150\nX-Ratelimit-Reset: 1792576800\n\n123\n"
    outputs = iter(['[{"n": 1}]\n[{"n": 2}]\n[{"n": 3}]', headers])
    monkeypatch.setattr(GH, "_run", lambda self, args, stdin=None: (setattr(self, "calls", self.calls + 1),
                                                                   next(outputs))[1])
    assert len(gh.api("repos/o/jobs/pulls?state=open", paginate=True)) == 3 and gh.calls == 3     # one per page
    lines = cli.api_budget(gh, "o/jobs")
    assert lines[0] == "GitHub API calls this run: 3 (rate limit remaining 150/1000, resets 10:00Z)"
    assert lines[1].startswith("WARN: only 150 GitHub API requests left until 10:00Z")
    roomy = type("G", (), {"calls": 12, "rate_limit": lambda self, repo: {"remaining": 900, "limit": 1000, "reset": 0}})()
    assert cli.api_budget(roomy, "o/jobs") == ["GitHub API calls this run: 12 (rate limit remaining 900/1000, resets 00:00Z)"]
    blind = type("G", (), {"calls": 4, "rate_limit": lambda self, repo: {}})()
    assert cli.api_budget(blind, "o/jobs") == ["GitHub API calls this run: 4 (rate limit not readable)"]
