"""The index of open roadmap pull requests, searching it, and resolving a README's citations, against a
local repository standing in for the content repository (its `refs/pull/N/head` are the open heads)."""

import json
import subprocess

import pytest

from tauceti_roadmap_worker import cli, config, gitops, openroadmaps
from tauceti_roadmap_worker.paths import HERE

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="needs git")

MAIN = {
    "TauCetiRoadmap/Lattices/README.md": "# Roadmap: lattices\n\n## Layer 1: basics\n\n### 1.2 Poisson summation\n\n"
                                         "**L1.3** Theta functions.\n",
    "TauCetiRoadmap/Lattices/Suggested.lean": "theorem poisson_summation : True := trivial\n",
    "TauCetiRoadmap/Algebra/Groups/README.md": "# Roadmap: groups\n\n## B1. Actions\n\n1. orbits\n2. stabilizers\n",
}
PR_THETA = {"TauCetiRoadmap/ThetaSeries/README.md": "# Roadmap: theta series\n\n### 2.1 Lattice Poisson summation\n",
            "TauCetiRoadmap/ThetaSeries/Suggested.lean": "def thetaSeries : Nat := 0\n"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def commit(repo, files, message):
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    commit(origin, MAIN, "main")
    git(origin, "checkout", "-q", "-b", "theta")
    theta = commit(origin, PR_THETA, "theta series")
    git(origin, "update-ref", "refs/pull/7/head", theta)
    git(origin, "checkout", "-q", "main")
    monkeypatch.setattr(gitops, "url", lambda repo: str(origin))
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")

    class FakeGH:
        calls = 0

        def graphql(self, query, **variables):
            FakeGH.calls += 1
            nodes = [{"number": 7, "title": "Theta series", "isDraft": False, "headRefOid": theta,
                      "author": {"login": "alice"}, "files": {"nodes": [{"path": p} for p in PR_THETA]}},
                     {"number": 8, "title": "Docs only", "isDraft": False, "headRefOid": theta,
                      "author": {"login": "bob"}, "files": {"nodes": [{"path": "docs/x.md"}]}}]
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}
    return cfg, FakeGH()


def test_refresh_lists_open_roadmap_work_and_reuses_a_fresh_index(world):
    cfg, gh = world
    index = openroadmaps.refresh(gh, cfg)
    assert [p["pr"] for p in index["prs"]] == [7]                               # #8 touches no roadmap
    assert index["prs"][0]["roadmaps"] == ["ThetaSeries"] and index["prs"][0]["adds"] == ["ThetaSeries"]
    assert set(index["main_roadmaps"]) == {"Lattices", "Groups"}                 # nested folders count
    assert index["main_folders"]["Groups"] == "TauCetiRoadmap/Algebra/Groups"
    assert openroadmaps.refresh(gh, cfg) == index and gh.calls == 1             # fresh: no second listing
    assert openroadmaps.rows(index)[0].startswith("#7  ThetaSeries (new)  @alice")


def test_grep_searches_open_heads_first_then_main(world):
    cfg, gh = world
    index = openroadmaps.refresh(gh, cfg)
    lines = openroadmaps.grep(cfg.state_dir(), index, "Poisson summation", fixed=True)
    assert lines[0].startswith("#7 ThetaSeries (@alice): TauCetiRoadmap/ThetaSeries/README.md:3:")
    assert lines[1].startswith("main: TauCetiRoadmap/Lattices/README.md:5:")
    assert openroadmaps.grep(cfg.state_dir(), index, "thetaSeries", readme=False) == [
        "#7 ThetaSeries (@alice): TauCetiRoadmap/ThetaSeries/Suggested.lean:1: def thetaSeries : Nat := 0"]
    assert openroadmaps.grep(cfg.state_dir(), index, "summation", limit=1)[-1] == \
        "... 2 more matching lines: narrow the pattern"          # README, README, Lean


def test_resolver_reports_only_what_is_found_nowhere(world):
    cfg, gh = world
    index = openroadmaps.refresh(gh, cfg)
    readme = "\n".join([
        "Lattices §1.2 and Lattices L1.3 exist; Lattices §4.4 does not.",     # line 1
        "ThetaSeries §2.1 is at the open head; Groups B1.2 is the second item under B1.",  # line 2
        "VariationalMethods §2.3 is cited but exists nowhere; McNinch §2.2 is a source key.",  # line 3
        "Lattices Layer 1 exists, Lattices Layer 9 does not.",                 # line 4
        "Names: `TauCetiRoadmap.Lattices.poisson_summation`, `TauCetiRoadmap.Lattices.theta_invented`, "
        "`Real.pi`, `Foo.made_up_name`, `AddGroup.add_comm`.",                   # line 5
    ])
    citations = openroadmaps.citation_problems(cfg.state_dir(), index, readme, own="Mine")
    assert citations == ["1: Lattices §4.4: Lattices has no 4.4 (checked main)",
                         "3: VariationalMethods §2.3: no roadmap VariationalMethods on main or in an open pull request",
                         "4: Lattices Layer 9: Lattices has no Layer 9 (checked main)"]
    names = openroadmaps.name_problems(cfg.state_dir(), index, readme, "", {"Real", "pi", "mul_comm"})
    assert names == ["README 5: Foo.made_up_name: `made_up_name` occurs nowhere in the pinned Mathlib and Tau Ceti "
                     "sources (a generated name? check it with #check)",
                     "README 5: TauCetiRoadmap.Lattices.theta_invented: not in Lattices's Suggested.lean on main or "
                     "at an open head"]                                          # add_comm: the additive of mul_comm
    report = openroadmaps.report(citations, names, "", index, "README.md")
    assert "## Cross-roadmap citations found nowhere" in report and "- 3: VariationalMethods" in report


def test_library_tokens_are_read_once_per_pin(world, tmp_path):
    cfg, _gh = world
    packages = tmp_path / "packages"
    (packages / "mathlib" / "Mathlib").mkdir(parents=True)
    (packages / "mathlib" / "Mathlib" / "A.lean").write_text("theorem Nat.succ_le_iff : True := trivial\n")
    tokens = openroadmaps.library_tokens(cfg.state_dir(), packages)
    assert "succ_le_iff" in tokens
    (packages / "mathlib" / "Mathlib" / "A.lean").write_text("changed\n")      # same pin: the cache answers
    assert "succ_le_iff" in openroadmaps.library_tokens(cfg.state_dir(), packages)


def test_the_command(world, capsys):
    cfg, gh = world
    openroadmaps.refresh(gh, cfg)
    conf = str(HERE / "tauceti-roadmap.toml.example")
    assert cli.main(["--conf", conf, "roadmaps", "list"]) == 0
    assert "#7  ThetaSeries (new)" in capsys.readouterr().out
    assert cli.main(["--conf", conf, "roadmaps", "grep", "theta", "-i", "--readme-only"]) == 0
    assert "ThetaSeries/README.md:1:" in capsys.readouterr().out
    assert cli.main(["--conf", conf, "roadmaps", "show", "ThetaSeries", "--file", "Suggested.lean"]) == 0
    assert "def thetaSeries" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--conf", conf, "roadmaps", "show", "Nowhere"])


def test_a_lane_resolves_its_readme_and_lists_the_tools(world, tmp_path):
    from tauceti_roadmap_worker.board import new_job
    from tauceti_roadmap_worker.lanes import base, lane_for
    cfg, gh = world
    job = new_job("REV-PKG-Mine~2")
    lane = lane_for(job)
    board = tmp_path / "board"
    pkg = board / "packages" / "Mine"
    pkg.mkdir(parents=True)
    (pkg / "README.md").write_text("Lattices §4.4 is missing.\n")
    ws = base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "j" / "scratch", board=board, content=tmp_path,
                        workdir=board, report=tmp_path / "r.md", branch="b", job_id=job.id)
    ws.scratch.mkdir(parents=True)

    class Ctx:
        dry_run, conf, state = False, "/conf.toml", cfg.state_dir()

        def __init__(self):
            self.gh, self.cfg = gh, cfg

        def cli(self, *args):
            return "tauceti-roadmap " + " ".join(args)
    ctx = Ctx()
    lane.prepare_tools(job, ctx, ws)
    assert "Lattices has no 4.4" in (ws.scratch / "citations.md").read_text()
    text = lane.tools_text(ctx, ws)
    assert "**Open roadmap pull requests** (1, 1 of them new roadmaps" in text and "found 1 (`" in text
    ws.extra.clear()
    assert "not available this round" in lane.tools_text(ctx, ws)
    assert json.loads(openroadmaps.index_path(cfg.state_dir()).read_text())["prs"][0]["pr"] == 7


def test_a_pull_request_with_more_than_a_hundred_files_is_listed_whole(world):
    cfg, _gh = world

    class BigGH:
        def graphql(self, query, **variables):
            first = [{"path": f"docs/{i}.md"} for i in range(100)]
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"number": 9, "title": "Big", "isDraft": False, "headRefOid": "x", "author": {"login": "c"},
                 "files": {"totalCount": 101, "nodes": first}}]}}}

        def api(self, path, paginate=False):
            assert path == "repos/TauCetiProject/TauCetiRoadmap/pulls/9/files?per_page=100" and paginate
            return [{"filename": f"docs/{i}.md"} for i in range(100)] + [{"filename": "TauCetiRoadmap/Big/README.md"}]
    prs = openroadmaps.inventory(BigGH(), "TauCetiProject/TauCetiRoadmap", ("TauCetiRoadmap", "Completed"))
    assert prs[0]["roadmaps"] == ["Big"]


def test_a_name_on_several_lines_is_reported_at_its_first(world):
    cfg, gh = world
    index = openroadmaps.refresh(gh, cfg)
    readme = "`Foo.made_up_name` here\nand `Foo.made_up_name` again\n"
    assert openroadmaps.name_problems(cfg.state_dir(), index, readme, "", set())[0].startswith("README 1: Foo.made_up_name")
