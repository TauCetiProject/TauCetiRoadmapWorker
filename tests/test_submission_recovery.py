"""A submission that stops part-way is finished, not redone (design review R05, failure B: a fix on a
board pull request pushed its commit, its completion comment then failed with HTTP 503, the kept run
recorded only `at`, `error` and `tries`, and every later attempt stopped at "nothing to push" without
ever posting the comment, so the board never saw the fix).

GitHub is played by local repositories (the board's, standing for the canonical repository and the
operator's fork at once, and the content repository's; `refs/pull/N/head` follows the pull request's
branch as GitHub's does) and an API over them whose writes can be refused or lose their answer. A
new Context and a new lane stand for a new worker process: only the state directory carries over.

- the pending record names the commit, its branch and pull request, the head its push expects and
  the effects still owed, from before the push on;
- after a stop past the push (a refused comment, a crash) the next attempt only comments and marks
  the job submitted: no agent run, no new commit;
- a comment or pull request whose answer was lost is found again, never made twice;
- a finished submission replays to the same state;
- a kept run superseded by someone else's push, a closed pull request or a re-filed job is dropped."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import agents, gitops, prreviews
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import Context, lane_for
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import fix as lane_fix

from .fakes import FakeBoard, FakeClaims, example_config
from .test_checkout import needs_git

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
BOARD, CONTENT = "TauCetiProject/TauCetiRoadmapJobs", "TauCetiProject/TauCetiRoadmap"
PR_BRANCH, ROADMAP_BRANCH = "w1/PKG-Foo", "roadmap/foo"
REVIEW = "Review (chatgpt) of ccccccc\n\nVerdict: needs_changes\n\n1. **F01** (P1) the design names no supplier\n"


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout.strip()


class Hub:
    """GitHub over local repositories: board pull request #12 on alice's branch `w1/PKG-Foo`, content
    pull request #905 on `roadmap/foo`, their comments, and the submission pull requests opened on the
    board. `fail(path, *faults)` plans the next writes to a path: `refuse` (HTTP 503, nothing done),
    `lose` (done, then HTTP 504), `crash` (done, then the process dies)."""

    def __init__(self, tmp_path):
        self.tmp, self.board, self.content = tmp_path, tmp_path / "board.git", tmp_path / "content.git"
        for bare in (self.board, self.content):
            subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        seed = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        (seed / "packages" / "Foo" / "design").mkdir(parents=True)
        (seed / "packages" / "Foo" / "design" / "README.md").write_text("# Foo design\n")
        (seed / "TauCetiRoadmap" / "Foo").mkdir(parents=True)
        (seed / "TauCetiRoadmap" / "Foo" / "README.md").write_text("# Foo\n")
        sh(seed, "add", "-A")
        sh(seed, "commit", "-q", "-m", "main")
        sh(seed, "push", "-q", str(self.board), "HEAD:refs/heads/main")
        sh(seed, "push", "-q", str(self.content), "HEAD:refs/heads/main", f"HEAD:refs/heads/{ROADMAP_BRANCH}")
        (seed / "packages" / "Foo" / "design" / "README.md").write_text("# Foo design\n\nround 1\n")
        sh(seed, "commit", "-q", "-am", "design round 1")
        sh(seed, "push", "-q", str(self.board), f"HEAD:refs/heads/{PR_BRANCH}")
        sh(self.board, "symbolic-ref", "refs/pull/12/head", f"refs/heads/{PR_BRANCH}")
        sh(self.content, "symbolic-ref", "refs/pull/905/head", f"refs/heads/{ROADMAP_BRANCH}")
        self.base = sh(seed, "rev-parse", "HEAD")
        self.comments = {12: [], 905: []}
        self.opened, self.faults, self.state = [], {}, {12: "open", 905: "open"}

    def head(self, number=12):
        bare, branch = (self.board, PR_BRANCH) if number == 12 else (self.content, ROADMAP_BRANCH)
        return sh(bare, "rev-parse", f"refs/heads/{branch}")

    def push_elsewhere(self):
        """Someone else pushes a commit onto the board pull request's branch."""
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", "-b", PR_BRANCH, str(self.board), str(other)], check=True)
        (other / "packages" / "Foo" / "design" / "README.md").write_text("# Foo design\n\nsomeone else's\n")
        sh(other, "commit", "-q", "-am", "elsewhere")
        sh(other, "push", "-q", "origin", f"HEAD:refs/heads/{PR_BRANCH}")
        return sh(other, "rev-parse", "HEAD")

    def fail(self, path, *faults):
        self.faults.setdefault(path, []).extend(faults)

    def fault(self, path):
        for key, planned in self.faults.items():
            if key in path and planned:
                return planned.pop(0)
        return None

    def pr(self, number, repo, branch):
        return {"number": number, "state": self.state[number], "user": {"login": "alice"},
                "head": {"sha": self.head(number), "ref": branch, "repo": {"full_name": repo}}}

    def api(self, path, paginate=False):
        if path == f"repos/{BOARD}/pulls/12":
            return self.pr(12, "alice/TauCetiRoadmapJobs", PR_BRANCH)
        if path == f"repos/{CONTENT}/pulls/905":
            return self.pr(905, "alice/TauCetiRoadmap", ROADMAP_BRANCH)
        if "/comments" in path:
            return [dict(c) for c in self.comments[int(path.split("/issues/")[1].split("/")[0])]]
        if path.startswith(f"repos/{BOARD}/pulls?head="):
            return [p for p in self.opened if path.startswith(f"repos/{BOARD}/pulls?head={p['head']['label']}&")]
        if path in ("repos/alice/TauCetiRoadmapJobs", "repos/alice/TauCetiRoadmap"):
            return {"fork": True}
        raise GHError(f"GET {path}: not faked")

    def raw_file(self, repo, path, ref=""):
        return REVIEW

    def write(self, method, path, payload=None):
        fault = self.fault(path)
        if fault == "refuse":
            raise GHError(f"gh api -X {method} {path}: HTTP 503: Service Unavailable")
        if path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            made = {"id": 900 + len(self.comments[number]), "body": payload["body"], "user": {"login": "alice"},
                    "html_url": f"https://github.com/o/r/pull/{number}#c{len(self.comments[number])}"}
            self.comments[number].append(made)
        elif path == f"repos/{BOARD}/pulls":
            if any(p["head"]["label"] == payload["head"] for p in self.opened):
                raise GHError(f"gh api -X POST {path}: HTTP 422: A pull request already exists for {payload['head']}.")
            number = 77 + len(self.opened)
            made = {"number": number, "html_url": f"https://github.com/{BOARD}/pull/{number}",
                    "head": {"label": payload["head"]}, "body": payload["body"]}
            self.opened.append(made)
        else:
            raise GHError(f"{method} {path}: not faked")
        if fault == "lose":
            raise GHError(f"gh api -X {method} {path}: HTTP 504: Gateway Timeout")
        if fault == "crash":
            raise KeyboardInterrupt
        return made


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = Hub(tmp_path)
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("TAUCETI_ROADMAP_LAKE_PACKAGES", raising=False)
    monkeypatch.setattr(gitops, "url", lambda repo: str(hub.content if repo.endswith("/TauCetiRoadmap") else hub.board))
    monkeypatch.setattr(lane_base.Lane, "sleep", staticmethod(lambda s: None))
    return hub


def process(hub):
    """A fresh worker process: its own Context and board, the same state directory."""
    return Context(cfg=example_config(), gh=hub, board=FakeBoard(), claims=FakeClaims(), worker_id="alice-1",
                   operator="alice", agent="claude", model="fable")


def fix_job(number=27):
    job = new_job("FIX-DESIGN-Foo~2", inputs={"board_pr": 12, "operator": "alice", "review": "REV-DESIGN-Foo"})
    job.number = number
    return job


def first_attempt(ctx, job):
    """The run up to its submission, as the agent left it: the design repaired and the report written."""
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    (ws.package(ctx.cfg, "Foo") / "design" / "README.md").write_text("# Foo design\n\nround 2: supplier named\n")
    ws.report.write_text("## F01 (P1) — applied\nThe supplier is named.\n")
    return lane.deliver(job, ctx, ws)


@pytest.fixture
def no_agent(monkeypatch):
    monkeypatch.setattr(agents, "run_agent", lambda *a, **k: pytest.fail("the agent ran again"))


def fix_comments(hub, job):
    return [c for c in hub.comments[12] if prreviews.worker_marker(c["body"]).get("job") == job.id]


@needs_git
def test_a_pushed_fix_whose_comment_failed_is_only_commented_by_the_next_attempt(hub, no_agent):
    job = fix_job()
    hub.fail("/issues/12/comments", "refuse", "refuse", "refuse")
    ctx = process(hub)
    result = first_attempt(ctx, job)
    pushed = hub.head()
    assert result.status == "failed" and "still owed: comment, submitted; try 1 of 3" in result.detail
    record = lane_base.read_pending(ctx, job)
    assert record["board"] == {"sha": pushed, "base": hub.base, "branch": PR_BRANCH,
                               "repo": "alice/TauCetiRoadmapJobs", "pr": 12}
    assert (record["done"], record["outstanding"], record["issue"]) == (["push"], ["comment", "submitted"], 27)
    assert sh(hub.board, "rev-parse", f"{pushed}^") == hub.base and ctx.board.states == []

    ctx = process(hub)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted" and "(push done; try 2)" in result.detail
    assert hub.head() == pushed                                         # no new commit, no second push
    [comment] = fix_comments(hub, job)
    assert comment["body"].startswith(f"Fix `{job.id}` pushed `{pushed[:7]}`")
    assert prreviews.worker_marker(comment["body"])["head"] == pushed
    assert ctx.board.states == [(job.id, "submitted")] and lane_base.read_pending(ctx, job) is None
    assert not (ctx.wdir / "jobs" / "FIX-DESIGN-Foo_r2" / lane_base.PENDING_DIR).exists()


@needs_git
def test_a_lost_comment_answer_is_found_and_not_posted_again(hub):
    job = fix_job()
    hub.fail("/issues/12/comments", "lose")
    ctx = process(hub)
    result = first_attempt(ctx, job)
    assert result.status == "submitted" and len(fix_comments(hub, job)) == 1
    assert result.url.endswith("#c0") and lane_base.read_pending(ctx, job) is None


def crash_in_push(monkeypatch, after):
    """The process dies in its first push to the pull request's branch, before or after pushing."""
    real, calls = gitops.push_branch, []

    def push_branch(wt, fork, branch, expected=None):
        calls.append(branch)
        if after or len(calls) > 1:
            real(wt, fork, branch, expected=expected)
        if len(calls) == 1:
            raise KeyboardInterrupt
    monkeypatch.setattr(gitops, "push_branch", push_branch)


@needs_git
@pytest.mark.parametrize("where", ["before the push", "after the push", "after the comment"])
def test_a_crash_at_any_step_leaves_a_record_a_new_process_finishes(hub, monkeypatch, no_agent, where):
    job = fix_job()
    if where == "after the comment":
        hub.fail("/issues/12/comments", "crash")
    else:
        crash_in_push(monkeypatch, after=where == "after the push")
    with pytest.raises(KeyboardInterrupt):
        first_attempt(process(hub), job)
    record = lane_base.read_pending(process(hub), job)
    assert record["outstanding"][-2:] == ["comment", "submitted"] and record["board"]["base"] == hub.base

    ctx = process(hub)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    head = hub.head()
    assert sh(hub.board, "rev-parse", f"{head}^") == hub.base         # one fix commit on the pull request
    assert "round 2: supplier named" in sh(hub.board, "show", f"{head}:packages/Foo/design/README.md")
    [comment] = fix_comments(hub, job)
    assert prreviews.worker_marker(comment["body"])["head"] == head
    assert ctx.board.states == [(job.id, "submitted")] and lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_finished_submission_replays_to_the_same_state(hub, monkeypatch, no_agent):
    job = fix_job()
    clear = lane_base.clear_pending
    monkeypatch.setattr(lane_base, "clear_pending", lambda ctx, job: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):                               # done, but dead before the record went
        first_attempt(process(hub), job)
    monkeypatch.setattr(lane_base, "clear_pending", clear)
    pushed = hub.head()
    assert lane_base.read_pending(process(hub), job)["outstanding"] == []
    ctx = process(hub)
    assert lane_for(job).run(job, ctx).status == "submitted"
    assert hub.head() == pushed and len(fix_comments(hub, job)) == 1
    assert ctx.board.states == [(job.id, "submitted")] and lane_base.read_pending(ctx, job) is None


@needs_git
@pytest.mark.parametrize("change", ["pushed on top", "pushed before", "closed", "filed again"])
def test_a_superseded_run_is_dropped_instead_of_finished(hub, monkeypatch, no_agent, change):
    job = fix_job()
    if change == "pushed before":
        crash_in_push(monkeypatch, after=False)
        with pytest.raises(KeyboardInterrupt):
            first_attempt(process(hub), job)
    else:
        hub.fail("/issues/12/comments", "refuse", "refuse", "refuse")
        first_attempt(process(hub), job)
    kept = lane_base.read_pending(process(hub), job)["board"]["sha"]
    other = hub.push_elsewhere() if change.startswith("pushed") else hub.head()
    hub.state[12] = "closed" if change == "closed" else "open"
    job = fix_job(28 if change == "filed again" else 27)

    ctx = process(hub)
    result = lane_for(job).run(job, ctx)
    why = {"pushed on top": f"#12 is at {other[:7]}, past the {kept[:7]} the run pushed",
           "pushed before": f"#12 is at {other[:7]}, no longer the {hub.base[:7]} the run was built on",
           "closed": "#12 is closed", "filed again": "the job is now #28, the run was for #27"}[change]
    assert result.status == "skipped" and result.detail == (f"dropped the run kept after its submission stopped: "
                                                            f"{why}; the next round runs the job afresh")
    assert lane_base.read_pending(ctx, job) is None and fix_comments(hub, job) == []
    assert hub.head() == other and ctx.board.states == []


def test_a_record_from_before_the_effects_were_kept_still_names_its_content_commit():
    entry = {"tries": 1, "error": "push rejected", "at": "2026-10-24T00:00:00+00:00", "sha": "a" * 40,
             "base": "b" * 40, "branch": "roadmap/foo"}
    assert lane_base.kept_commit(entry, "content") == {"sha": "a" * 40, "base": "b" * 40, "branch": "roadmap/foo"}
    assert lane_base.kept_commit(entry, "board") == {}


# --- a red-team fix on a roadmap pull request: the content commit, then the board submission -----------

@needs_git
def test_a_content_fix_whose_board_submission_failed_is_submitted_without_a_second_push_or_comment(hub, monkeypatch,
                                                                                                    no_agent):
    job = new_job("FIX-RT-Foo~1", inputs={"pr": 905, "operator": "alice", "reviews": ["RT-Foo~1a"]})
    job.number = 31
    monkeypatch.setattr(lane_fix, "open_roadmap_prs", lambda ctx: [
        {"pr": 905, "name": "Foo", "head": hub.head(905), "branch": ROADMAP_BRANCH, "owner": "alice"}])

    def check(self, job_, ctx_, ws):
        ws.extra["gate"] = SimpleNamespace(counts={"build": 0, "depcheck": 0, "readme-ids-missing": 0}, ok=True)
        return []
    monkeypatch.setattr(lane_fix.RedTeamFixLane, "check", check)
    real_push = gitops.push
    refused = []

    def push(wt, fork, branch, dry_run=False, expected=None):
        if fork == "alice/TauCetiRoadmapJobs" and len(refused) < 3:
            refused.append(branch)
            raise gitops.PushError("git push: ! [remote rejected] (failed)")
        return real_push(wt, fork, branch, dry_run=dry_run, expected=expected)
    monkeypatch.setattr(gitops, "push", push)

    ctx = process(hub)
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    (ws.workdir / "TauCetiRoadmap" / "Foo" / "README.md").write_text("# Foo\n\nrepaired\n")
    sh(ws.workdir, "commit", "-q", "-am", "Foo: red-team repairs")
    ws.report.write_text("## R01 (P1) — applied\nRepaired.\n")
    assert lane.validate(job, ctx, ws) == []
    result = lane.deliver(job, ctx, ws)
    repaired = hub.head(905)
    assert result.status == "failed" and "still owed: push, pull-request, submitted" in result.detail
    record = lane_base.read_pending(ctx, job)
    assert record["content"]["sha"] == repaired and record["content"]["pr"] == 905
    assert record["done"] == ["content-push", "content-comment"] and len(hub.comments[905]) == 1

    hub.fail(f"repos/{BOARD}/pulls", "lose")                     # the pull request is opened, its answer lost
    ctx = process(hub)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    assert hub.head(905) == repaired and len(hub.comments[905]) == 1          # no second push, no second comment
    [opened] = hub.opened
    assert result.url == opened["html_url"] and ctx.board.states == [(job.id, "submitted")]
    report = sh(hub.board, "show", f"refs/heads/alice-1/FIX-RT-Foo_r1:packages/Foo/reports/{job.id}.md")
    assert report.startswith("## R01")
    assert lane_base.read_pending(ctx, job) is None
    assert not sh(ctx.wdir / "content", "for-each-ref", "refs/tauceti-pending/")      # the kept commit's ref went
