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


def test_a_pull_request_whose_head_ref_github_lacks_is_left_out_not_fatal(world, monkeypatch):
    cfg, gh = world
    listing = gh.graphql

    def with_a_headless_one(query, **variables):
        data = listing(query, **variables)
        nodes = data["repository"]["pullRequests"]["nodes"]
        nodes.append({"number": 9, "title": "No head ref", "isDraft": False, "headRefOid": "0" * 40,
                      "author": {"login": "dan"},
                      "files": {"nodes": [{"path": "TauCetiRoadmap/Headless/README.md"}]}})
        return data
    monkeypatch.setattr(gh, "graphql", with_a_headless_one, raising=False)
    index = openroadmaps.refresh(gh, cfg, force=True)
    assert [p["pr"] for p in index["prs"]] == [7] and index["unfetched"] == [9]
    assert openroadmaps.read(cfg.state_dir(), index, openroadmaps.pr_ref(7), "ThetaSeries").startswith("# Roadmap: theta")
    # Any other fetch failure still raises.
    monkeypatch.setattr(gitops, "url", lambda repo: "/nonexistent/origin")
    monkeypatch.setattr(openroadmaps, "mirror", lambda state: state / "fresh-mirror" / "content.git")
    with pytest.raises(gitops.GitError):
        openroadmaps.refresh(gh, cfg, force=True)


def headless_pull_request(tmp_path, number, files):
    """In the fixture's origin: a branch with `files`, and GitHub's test merge of it into main under
    refs/pull/<number>/merge, with no refs/pull/<number>/head. Returns the head commit."""
    origin = tmp_path / "origin"
    main = git(origin, "rev-parse", "main").strip()
    git(origin, "checkout", "-q", "-b", f"headless{number}", main)
    head = commit(origin, files, f"headless {number}")
    git(origin, "checkout", "-q", "--detach", main)
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "test merge", head)
    git(origin, "update-ref", f"refs/pull/{number}/merge", "HEAD")
    git(origin, "checkout", "-q", "main")
    return head


def test_a_missing_head_ref_is_read_from_the_test_merge_when_it_has_the_listed_head(world, tmp_path, monkeypatch):
    cfg, gh = world
    files = {"TauCetiRoadmap/Headless/README.md": "# Roadmap: headless\n"}
    head = headless_pull_request(tmp_path, 9, files)
    listing = gh.graphql
    listed_head = {"sha": head}

    def with_the_headless_one(query, **variables):
        data = listing(query, **variables)
        data["repository"]["pullRequests"]["nodes"].append(
            {"number": 9, "title": "No head ref", "isDraft": False, "headRefOid": listed_head["sha"],
             "author": {"login": "dan"}, "files": {"nodes": [{"path": path} for path in files]}})
        return data
    monkeypatch.setattr(gh, "graphql", with_the_headless_one, raising=False)
    index = openroadmaps.refresh(gh, cfg, force=True)
    assert [p["pr"] for p in index["prs"]] == [7, 9] and index["unfetched"] == []
    assert openroadmaps.read(cfg.state_dir(), index, openroadmaps.pr_ref(9), "Headless") == "# Roadmap: headless\n"
    mirror = openroadmaps.mirror(cfg.state_dir())
    assert git(mirror, "for-each-ref", "--format=%(refname)", "refs/open-merge/") == ""        # the scratch ref is gone
    # A test merge of another head than the one GitHub lists is not used: the pull request is left out.
    listed_head["sha"] = "1" * 40
    index = openroadmaps.refresh(gh, cfg, force=True)
    assert [p["pr"] for p in index["prs"]] == [7] and index["unfetched"] == [9]
    # The read-only checkout of such a pull request comes from the same place.
    dest = tmp_path / "checkout"
    assert gitops.checkout_pr("o/r", dest, 9) == head
    assert (dest / "TauCetiRoadmap/Headless/README.md").read_text() == "# Roadmap: headless\n"
    with pytest.raises(gitops.GitError):
        gitops.checkout_pr("o/r", dest, 99)                                                    # neither ref: still an error


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


def test_the_listing_says_how_each_file_changes_and_main_says_which_folders_hold_a_readme():
    class GH:
        def graphql(self, query, **variables):
            assert "changeType" in query
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"number": 7, "title": "Targets", "isDraft": False, "headRefOid": "h", "author": {"login": "c"},
                 "files": {"totalCount": 2, "nodes": [
                     {"path": "TauCetiRoadmap/Fam/Group/GroupTargets.lean", "changeType": "ADDED"},
                     {"path": "TauCetiRoadmap/Old/Suggested.lean", "changeType": "DELETED"}]}},
                {"number": 8, "title": "Docs", "isDraft": False, "headRefOid": "h", "author": {"login": "c"},
                 "files": {"totalCount": 1, "nodes": [{"path": "docs/x.md", "changeType": "MODIFIED"}]}}]}}}

        def api(self, path, paginate=False):
            assert path == "repos/o/r/git/trees/main?recursive=1"
            return {"truncated": False, "tree": [
                {"path": "TauCetiRoadmap/Fam/README.md", "type": "blob"},
                {"path": "TauCetiRoadmap/Fam/Group/README.md", "type": "blob"},
                {"path": "TauCetiRoadmap/Fam/Group", "type": "tree"},
                {"path": "Completed/Done/README.md", "type": "blob"}, {"path": "README.md", "type": "blob"}]}
    # Only a pull request with a roadmap file by name is listed by default; `any_file` lists the others
    # under the directories too, every entry with its files and how each changes.
    assert [p["pr"] for p in openroadmaps.inventory(GH(), "o/r", ("TauCetiRoadmap",))] == [7]
    [entry] = openroadmaps.inventory(GH(), "o/r", ("TauCetiRoadmap",), any_file=True)
    assert entry["roadmaps"] == ["Old"] and entry["files"] == [
        {"path": "TauCetiRoadmap/Fam/Group/GroupTargets.lean", "change": "added"},
        {"path": "TauCetiRoadmap/Old/Suggested.lean", "change": "removed"}]
    assert openroadmaps.readme_folders(GH(), "o/r", "main", "TauCetiRoadmap") == {"Fam", "Fam/Group"}


def test_a_long_listing_read_by_rest_names_the_old_path_of_a_renamed_file():
    class GH:
        def api(self, path, paginate=False):
            return [{"filename": "Completed/Foo/README.md", "status": "renamed",
                     "previous_filename": "TauCetiRoadmap/Foo/README.md"}, {"filename": "x.md", "status": "added"}]
    pr = {"number": 3, "files": {"totalCount": 101, "nodes": [{"path": "x.md", "changeType": "ADDED"}]}}
    assert openroadmaps.changed_files(GH(), "o/r", pr) == [
        {"path": "Completed/Foo/README.md", "change": "renamed", "previous": "TauCetiRoadmap/Foo/README.md"},
        {"path": "x.md", "change": "added"}]


def test_a_name_on_several_lines_is_reported_at_its_first(world):
    cfg, gh = world
    index = openroadmaps.refresh(gh, cfg)
    readme = "`Foo.made_up_name` here\nand `Foo.made_up_name` again\n"
    assert openroadmaps.name_problems(cfg.state_dir(), index, readme, "", set())[0].startswith("README 1: Foo.made_up_name")


# --- aggregate pull requests: what they carry is found and read, and they stay out of `prs` ----------

MEMBER_FOO = {"TauCetiRoadmap/Foo/README.md": "# Roadmap: foo\n\n### 1.1 Weil pairing\n",
              "TauCetiRoadmap/Foo/Suggested.lean": "def foo : Nat := 0\n"}
ONLY_BAR = {"TauCetiRoadmap/Fam/Bar/README.md": "# Roadmap: bar\n\n### 1.1 Weil pairing on curves\n",
            "TauCetiRoadmap/Fam/Bar/Suggested.lean": "def bar : Nat := 0\n"}


@pytest.fixture
def aggregated(world, tmp_path, monkeypatch):
    """The world, plus #12 by alice adding Foo, and the aggregate #99 by bob carrying Foo and the nested
    Fam/Bar, whose own pull request was closed in the aggregate's favour (so only #99 carries Bar)."""
    cfg, gh = world
    origin = tmp_path / "origin"
    git(origin, "checkout", "-q", "-b", "foo", "main")
    foo = commit(origin, MEMBER_FOO, "foo")
    git(origin, "update-ref", "refs/pull/12/head", foo)
    git(origin, "checkout", "-q", "-b", "aggregate", "main")
    whole = commit(origin, {**MEMBER_FOO, **ONLY_BAR}, "foo and bar")
    git(origin, "update-ref", "refs/pull/99/head", whole)
    git(origin, "checkout", "-q", "main")
    listing = gh.graphql

    def with_the_aggregate(query, **variables):
        data = listing(query, **variables)
        data["repository"]["pullRequests"]["nodes"] += [
            {"number": 12, "title": "Foo", "isDraft": False, "headRefOid": foo, "author": {"login": "alice"},
             "files": {"nodes": [{"path": p} for p in MEMBER_FOO]}},
            {"number": 99, "title": "Foo and Bar in one piece", "isDraft": False, "headRefOid": whole,
             "author": {"login": "bob"}, "files": {"nodes": [{"path": p} for p in {**MEMBER_FOO, **ONLY_BAR}]}}]
        return data
    monkeypatch.setattr(gh, "graphql", with_the_aggregate, raising=False)
    cfg.prreview.aggregate_prs = [99]
    return cfg, gh


def test_an_aggregate_is_indexed_apart_and_what_it_alone_carries_is_found_and_read(aggregated):
    cfg, gh = aggregated
    index = openroadmaps.refresh(gh, cfg, force=True)
    assert [p["pr"] for p in index["prs"]] == [7, 12] and index["unfetched"] == []     # `prs` as before
    [entry] = index["aggregates"]
    assert entry["pr"] == 99 and entry["aggregate"] is True and entry["roadmaps"] == ["Foo", "Bar"]
    assert entry["folders"]["Bar"] == "TauCetiRoadmap/Fam/Bar" and entry["adds"] == ["Foo", "Bar"]
    mirror = openroadmaps.mirror(cfg.state_dir())
    assert "refs/open/99" in git(mirror, "for-each-ref", "--format=%(refname)", "refs/open/").split()
    # Where each roadmap can be read: main and the pull requests first, then the aggregate.
    assert openroadmaps.sources_for(index, "Bar") == [("#99 (aggregate)", "refs/open/99")]
    assert openroadmaps.sources_for(index, "Foo") == [("#12", "refs/open/12"), ("#99 (aggregate)", "refs/open/99")]
    assert openroadmaps.sources_for(index, "Bar", aggregates=False) == []
    # A nested roadmap only the aggregate carries is read in its folder there.
    assert openroadmaps.read(cfg.state_dir(), index, "refs/open/99", "Bar").startswith("# Roadmap: bar")
    assert openroadmaps.read(cfg.state_dir(), index, "refs/open/99", "Bar", "Suggested.lean") == "def bar : Nat := 0\n"
    # The listing: one line for the aggregate after the others, Bar marked as carried only there.
    lines = openroadmaps.rows(index)
    assert lines[1].startswith("#12  Foo (new)  @alice") and len(lines) == 3
    assert lines[2].startswith("#99 (aggregate)  Foo, Bar (only here)  @bob  ")
    # grep: Bar is searched in the aggregate; Foo only in its member, so no hit is reported twice.
    assert openroadmaps.grep(cfg.state_dir(), index, "Weil pairing", fixed=True) == [
        "#12 Foo (@alice): TauCetiRoadmap/Foo/README.md:3: ### 1.1 Weil pairing",
        "#99 (aggregate) Bar (@bob): TauCetiRoadmap/Fam/Bar/README.md:3: ### 1.1 Weil pairing on curves"]
    # The resolver finds Bar and its ids and Lean names at the aggregate's head.
    readme = "Bar §1.1 exists; Bar §9.9 does not.\nNames: `TauCetiRoadmap.Bar.bar`, `TauCetiRoadmap.Bar.nope`.\n"
    assert openroadmaps.citation_problems(cfg.state_dir(), index, readme, own="Mine") == [
        "1: Bar §9.9: Bar has no 9.9 (checked #99 (aggregate))"]
    assert openroadmaps.name_problems(cfg.state_dir(), index, readme, "", None) == [
        "README 2: TauCetiRoadmap.Bar.nope: not in Bar's Suggested.lean on main or at an open head"]
    assert "2 open roadmap pull requests and 1 aggregate at their heads" in openroadmaps.report([], [], "", index, "x")


def test_an_aggregate_whose_head_cannot_be_fetched_is_left_out(aggregated, monkeypatch):
    cfg, gh = aggregated
    listing = gh.graphql

    def with_a_headless_aggregate(query, **variables):
        data = listing(query, **variables)
        data["repository"]["pullRequests"]["nodes"].append(
            {"number": 98, "title": "Headless aggregate", "isDraft": False, "headRefOid": "0" * 40,
             "author": {"login": "carol"}, "files": {"nodes": [{"path": "TauCetiRoadmap/Gone/README.md"}]}})
        return data
    monkeypatch.setattr(gh, "graphql", with_a_headless_aggregate, raising=False)
    cfg.prreview.aggregate_prs = [98, 99]
    index = openroadmaps.refresh(gh, cfg, force=True)
    assert [p["pr"] for p in index["aggregates"]] == [99] and index["unfetched"] == []
    assert [p["pr"] for p in index["prs"]] == [7, 12]


def test_no_aggregate_leaves_the_index_as_it_was(aggregated):
    cfg, gh = aggregated
    cfg.prreview.aggregate_prs = []
    index = openroadmaps.refresh(gh, cfg, force=True)
    assert [p["pr"] for p in index["prs"]] == [7, 12, 99] and "aggregates" not in index
    assert openroadmaps.sources_for(index, "Bar") == [("#99", "refs/open/99")]


def test_the_command_lists_and_shows_what_an_aggregate_carries(aggregated, tmp_path, capsys):
    from tauceti_roadmap_worker.board import new_job
    from tauceti_roadmap_worker.lanes import base, lane_for
    cfg, gh = aggregated
    index = openroadmaps.refresh(gh, cfg, force=True)
    conf = str(HERE / "tauceti-roadmap.toml.example")
    assert cli.main(["--conf", conf, "roadmaps", "list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("2 open roadmap pull requests and 1 aggregate (") and "#99 (aggregate)  Foo, Bar (only here)" in out
    assert cli.main(["--conf", conf, "roadmaps", "show", "Bar"]) == 0
    assert capsys.readouterr().out.startswith("<!-- Bar/README.md at #99 (aggregate) -->\n# Roadmap: bar")
    assert cli.main(["--conf", conf, "roadmaps", "show", "Foo"]) == 0
    assert capsys.readouterr().out.startswith("<!-- Foo/README.md at #12 -->")
    assert cli.main(["--conf", conf, "roadmaps", "show", "Foo", "--pr", "99", "--file", "Suggested.lean"]) == 0
    assert capsys.readouterr().out == "<!-- Foo/Suggested.lean at #99 (aggregate) -->\ndef foo : Nat := 0\n"
    assert cli.main(["--conf", conf, "roadmaps", "grep", "curves"]) == 0
    assert capsys.readouterr().out.startswith("#99 (aggregate) Bar (@bob): TauCetiRoadmap/Fam/Bar/README.md:3:")
    with pytest.raises(SystemExit, match="no roadmap Bar on main"):     # overlap facts: per pull request
        cli.main(["--conf", conf, "roadmaps", "overlaps", "Bar"])
    # A brief whose index has an aggregate says how to read what it carries; one with none does not.
    lane = lane_for(new_job("REV-PKG-Mine~2"))
    ws = base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "j" / "scratch", board=tmp_path, content=tmp_path,
                        workdir=tmp_path, report=tmp_path / "r.md", branch="b", job_id="REV-PKG-Mine~2")

    class Ctx:
        def cli(self, *args):
            return "tauceti-roadmap " + " ".join(args)
    ws.extra["open_index"] = index
    assert "listed as an aggregate carries roadmaps of a family in one piece" in lane.tools_text(Ctx(), ws)
    ws.extra["open_index"] = {**index, "aggregates": []}
    assert "aggregate" not in lane.tools_text(Ctx(), ws)
