"""Two live defects: GitHub's cron skipped the board's scheduled runs (an accepted pull request sat
mergeable for 20 minutes), and an accepted design (#63) could not merge because the intake had
rewritten its sources.toml on main with answers while the branch changed it too."""

import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import boardpr, gitops, sources, worker
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import LaneResult

from .fakes import FakeBoard, example_config
from .test_checkout import needs_git


class DispatchGH:
    def __init__(self, fail=False):
        self.runs, self.fail = [], fail

    def cli_write(self, *args):
        if self.fail:
            raise GHError("gh workflow run: HTTP 403: Resource not accessible by integration")
        self.runs.append(args)
        return ""


def test_a_round_that_changes_the_board_asks_for_a_board_run(monkeypatch):
    monkeypatch.setattr(worker, "_last_dispatch", [0.0])
    gh, lines, now = DispatchGH(), [], [1000.0]
    ctx = SimpleNamespace(cfg=example_config(), gh=gh, dry_run=False)
    submitted = LaneResult("PKG-Foo", "submitted", "")
    assert worker.request_board_run(ctx, submitted, lines.append, clock=lambda: now[0])
    assert gh.runs == [("workflow", "run", "board.yml", "-R", ctx.cfg.repositories.board_repo())]
    assert lines == ["board run requested"]
    now[0] += 30
    assert not worker.request_board_run(ctx, LaneResult("RT-Foo~1a", "failed", ""), lines.append, clock=lambda: now[0])
    now[0] += 60                                                                 # a burst collapses to one request
    assert worker.request_board_run(ctx, LaneResult("DESIGN-Foo", "needs-references", ""), lines.append,
                                    clock=lambda: now[0])
    assert not worker.request_board_run(SimpleNamespace(cfg=ctx.cfg, gh=gh, dry_run=False),
                                        LaneResult("", "idle", ""), lines.append, clock=lambda: now[0] + 999)
    ctx.cfg.board.dispatch = False
    assert not worker.request_board_run(ctx, submitted, lines.append, clock=lambda: now[0] + 999)
    ctx.cfg.board.dispatch = True
    failing = SimpleNamespace(cfg=ctx.cfg, gh=DispatchGH(fail=True), dry_run=False)
    assert not worker.request_board_run(failing, submitted, lines.append, clock=lambda: now[0] + 999)   # ignored


def test_manifests_merge_by_key_and_answers_win():
    pr = {"schema": "tauceti-roadmap-sources/v1", "roadmap": "K3",
          "source": [{"id": "A", "title": "the author's"}, {"id": "B", "title": "the author's B", "status": "uncertified"}]}
    main = {"schema": "tauceti-roadmap-sources/v1", "roadmap": "K3",
            "source": [{"id": "B", "status": "local", "local_name": "b.txt", "resolution": "https://x/1"},
                       {"id": "A", "title": "main's A without an answer"}, {"id": "C", "title": "only on main"}]}
    merged = sources.merge_manifests(pr, main)
    by_id = {e["id"]: e for e in merged["source"]}
    assert by_id["A"]["title"] == "the author's" and by_id["B"]["status"] == "local" and "C" in by_id
    assert [e["id"] for e in merged["source"]] == ["A", "B", "C"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@x", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def manifest(*entries):
    data = sources.new_manifest("K3")
    data["source"] = list(entries)
    return sources.dump_manifest(data)


@pytest.fixture
def board_origin(tmp_path, monkeypatch):
    """A board repository (bare) whose main and pull request #63 both changed packages/K3/sources.toml."""
    origin, seed = tmp_path / "board.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    pkg = seed / "packages" / "K3"
    pkg.mkdir(parents=True)
    (pkg / "sources.toml").write_text(manifest({"id": "A", "title": "A"}))
    (pkg / "README.md").write_text("# K3\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "base")
    git(seed, "push", "-q", str(origin), "main")
    git(seed, "checkout", "-q", "-b", "design")
    (pkg / "sources.toml").write_text(manifest({"id": "A", "title": "A"}, {"id": "N", "title": "new in the design"}))
    (pkg / "README.md").write_text("# K3\n\nThe design.\n")
    git(seed, "commit", "-q", "-am", "design")
    git(seed, "push", "-q", str(origin), "HEAD:refs/pull/63/head")
    head = git(seed, "rev-parse", "HEAD")
    git(seed, "checkout", "-q", "main")
    (pkg / "sources.toml").write_text(manifest({"id": "A", "title": "A", "status": "waived", "resolution": "https://x/2"}))
    git(seed, "commit", "-q", "-am", "answers")
    git(seed, "push", "-q", str(origin), "main")
    monkeypatch.setattr(gitops, "url", lambda repo: str(origin))
    return origin, seed, head


class PrGH:
    def __init__(self):
        self.writes = []

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        if path.endswith("/merge"):
            raise GHError("gh api -X PUT: gh: Pull Request has merge conflicts (HTTP 405)")
        return {}


@needs_git
def test_a_conflict_in_sources_toml_only_is_merged_by_key(board_origin, tmp_path):
    origin, _seed, head = board_origin
    gh = PrGH()
    ctx = SimpleNamespace(cfg=example_config(), gh=gh, state=tmp_path / "state", operator="board-bot", board=FakeBoard())
    pr = {"number": 63, "head": {"sha": head}, "title": "[Design] K3: submission", "user": {"login": "alice"}}
    done, sha = boardpr.squash_with_main(ctx, 63, pr)
    assert done
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(origin), str(check)], check=True)
    assert git(check, "rev-parse", "HEAD") == sha and "The design." in (check / "packages/K3/README.md").read_text()
    entries = {e["id"]: e for e in sources.load_manifest(check / "packages/K3/sources.toml")["source"]}
    assert entries["A"]["status"] == "waived" and "N" in entries                 # the answer kept, the design's entry added
    assert git(check, "log", "-1", "--format=%an") == "alice"
    assert ("PATCH", "repos/" + ctx.cfg.repositories.packages_repo() + "/pulls/63", {"state": "closed"}) in gh.writes


@needs_git
def test_any_other_conflict_blocks_the_job_for_a_human(board_origin, tmp_path, monkeypatch):
    origin, seed, head = board_origin
    (seed / "packages/K3/README.md").write_text("# K3\n\nChanged on main too.\n")
    git(seed, "commit", "-q", "-am", "main readme")
    git(seed, "push", "-q", str(origin), "main")
    before = git(seed, "rev-parse", "HEAD")
    gh = PrGH()
    review = new_job("REV-DESIGN-K3~6", inputs={"board_pr": 63})
    review.number, review.state = 70, "available"
    board = FakeBoard(jobs=[review])
    ctx = SimpleNamespace(cfg=example_config(), gh=gh, state=tmp_path / "state", operator="board-bot", board=board)
    pr = {"number": 63, "head": {"sha": head}, "title": "t", "user": {"login": "alice"}, "body": ""}
    monkeypatch.setattr(boardpr, "merge_problems", lambda *a, **k: [])
    why = boardpr.merge(ctx, 63, pr, [review], None, blocker=review)
    assert why == "merge failed (conflicts with main in packages/K3/README.md)"
    assert board.states == [("REV-DESIGN-K3~6", "blocked")]
    check = tmp_path / "check2"
    subprocess.run(["git", "clone", "-q", str(origin), str(check)], check=True)
    assert git(check, "rev-parse", "HEAD") == before                             # main untouched
