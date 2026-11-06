"""Overlaps between open roadmap pull requests, against a local repository standing in for the content
repository: a revision of a roadmap on main that renumbers a section and renames a heading other roadmaps
cite, a second revision of the same roadmap that conflicts with it, and a new roadmap citing both ids."""

import subprocess

import pytest

from tauceti_roadmap_worker import cli, config, gitops, openroadmaps, overlaps
from tauceti_roadmap_worker.paths import HERE

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="needs git")

LATTICES = "TauCetiRoadmap/Lattices/README.md"
MAIN = {
    LATTICES: "# Roadmap: lattices\n\n## Layer 1: basics\n\n### 1.2 Poisson summation\n\n### 1.3 Theta functions\n",
    "TauCetiRoadmap/Groups/README.md": "# Roadmap: groups\n\nUses Lattices §1.3 and [the basics]"
                                       "(../Lattices/README.md#layer-1-basics).\n",
}
RENUMBER = {LATTICES: "# Roadmap: lattices\n\n## Layer 1: foundations\n\n### 1.2 Poisson summation\n\n"
                      "### 1.4 Theta functions\n"}
CONFLICT = {LATTICES: "# Roadmap: lattices\n\n## Layer 1: basics\n\n### 1.2 Poisson summation\n\n"
                      "### 1.3 Theta series\n"}
THETA = {"TauCetiRoadmap/ThetaSeries/README.md": "# Roadmap: theta series\n\nFrom Lattices §1.2 and Lattices §1.3.\n"}


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
    heads = {}
    for number, files in ((7, RENUMBER), (9, CONFLICT), (8, THETA)):
        git(origin, "checkout", "-q", "-b", f"pr{number}", "main")
        heads[number] = commit(origin, files, f"pr {number}")
        git(origin, "update-ref", f"refs/pull/{number}/head", heads[number])
    git(origin, "checkout", "-q", "main")
    monkeypatch.setattr(gitops, "url", lambda repo: str(origin))
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")

    class FakeGH:
        def graphql(self, query, **variables):
            nodes = [{"number": n, "title": f"PR {n}", "isDraft": False, "headRefOid": heads[n],
                      "author": {"login": login}, "files": {"nodes": [{"path": p} for p in files]}}
                     for n, login, files in ((7, "alice", RENUMBER), (8, "bob", THETA), (9, "carol", CONFLICT))]
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}
    return cfg, openroadmaps.refresh(FakeGH(), cfg)


def test_github_heading_anchors():
    text = "# Top\n## B1. Principal bundles\n```\n# not a heading\n```\n## Top\n<a id=\"kept\"></a>\n"
    assert overlaps.slug("Layer 2: `Unramified` extensions — and Frobenius") == \
        "layer-2-unramified-extensions--and-frobenius"
    assert overlaps.anchors(text) == {"top", "b1-principal-bundles", "top-1", "kept"}


def test_citations_read_ids_and_heading_links_but_not_the_roadmap_itself():
    found = overlaps.citations("Lattices §1.3 and Lattices Layer 1; see [x](../../A/Lattices/README.md#layer-1-basics)"
                               " and Groups §2.\n", {"Lattices", "Groups"}, citer="Groups")
    assert [(c.cited, c.kind, c.id, c.line) for c in found] == [
        ("Lattices", "section", "1.3", 1), ("Lattices", "layer", "1", 1), ("Lattices", "anchor", "layer-1-basics", 1)]


def test_removals_are_the_cited_ids_a_revision_drops(world):
    cfg, index = world
    found = overlaps.removals(cfg.state_dir(), index)
    got = sorted((r.pr, r.citation.label(), r.citation.citer, r.citation.where) for r in found)
    assert got == [(7, "Lattices #layer-1-basics", "Groups", "main"),
                   (7, "Lattices §1.3", "Groups", "main"),
                   (7, "Lattices §1.3", "ThetaSeries", "#8")]       # §1.2 survives; #9 keeps 1.3 and the heading


def test_shared_folders_say_whether_the_heads_merge(world):
    cfg, index = world
    [lattices] = overlaps.shared(cfg.state_dir(), index)
    assert (lattices.roadmap, lattices.prs) == ("Lattices", (7, 9))
    paths = lattices.conflicts[(7, 9)]
    if paths is None:
        pytest.skip("this git cannot merge-tree --write-tree")
    assert paths == [LATTICES]


def test_report_for_a_citing_roadmap_and_for_the_revision(world):
    cfg, index = world
    state = cfg.state_dir()
    text, facts = overlaps.report(state, index, "ThetaSeries", THETA["TauCetiRoadmap/ThetaSeries/README.md"], pr=8)
    assert facts == 1
    assert "- Lattices §1.3 (line 3): #7 (@alice) changes Lattices and has no such id at its head" in text
    assert "drops and other roadmaps cite" not in text                  # #8 changes no roadmap on main

    text, facts = overlaps.report(state, index, "Lattices", RENUMBER[LATTICES], pr=7)
    assert "- #9 (@carol) PR 9" in text and "#7 and #9 conflict in" in text
    assert "- Lattices #layer-1-basics: cited by Groups (main line 3)" in text
    assert "- Lattices §1.3: cited by Groups (main line 3); ThetaSeries (#8 line 3)" in text
    assert facts == 3                                                   # one other pull request, two dropped ids


def test_report_all_and_the_command(world, capsys):
    cfg, index = world
    text = overlaps.report_all(cfg.state_dir(), index)
    assert "- Lattices (`TauCetiRoadmap/Lattices`): #7, #9" in text
    assert "- #7 (@alice):" in text
    assert "  - Lattices §1.3: cited by Groups (main line 3); ThetaSeries (#8 line 3)" in text
    conf = str(HERE / "tauceti-roadmap.toml.example")
    assert cli.main(["--conf", conf, "roadmaps", "overlaps"]) == 0
    assert capsys.readouterr().out == text
    assert cli.main(["--conf", conf, "roadmaps", "overlaps", "Lattices", "--pr", "7"]) == 0
    assert "## Ids of Lattices that this pull request drops and other roadmaps cite" in capsys.readouterr().out
    assert cli.main(["--conf", conf, "roadmaps", "overlaps", "ThetaSeries"]) == 0      # read at #8, its only copy
    assert "#7 (@alice) changes Lattices" in capsys.readouterr().out


def test_neighbours_rank_open_roadmaps_by_shared_vocabulary():
    filler = [("main", f"M{i}", f"roadmap {i} words common text") for i in range(12)]
    texts = filler + [("#5", "Theta", "theta modular weight lattice poisson summation"),
                      ("#6", "Sheaves", "sheaf cohomology derived category"),
                      ("main", "Forms", "theta modular weight forms")]
    near = overlaps.neighbours(texts, "theta modular lattice poisson", own="Mine")
    assert [(where, name) for _s, where, name in near] == [("#5", "Theta")]          # main is not listed
    assert overlaps.neighbours(texts, "theta modular lattice poisson", own_pr=5) == []
    assert [name for _s, _w, name in overlaps.neighbours(texts, "sheaf derived")] == ["Sheaves"]


def test_a_lane_writes_the_overlaps_of_its_roadmap(world, tmp_path):
    from tauceti_roadmap_worker.board import new_job
    from tauceti_roadmap_worker.lanes import base, lane_for
    cfg, _index = world
    job = new_job("REV-PKG-Mine~2")
    lane = lane_for(job)
    board = tmp_path / "board"
    pkg = board / "packages" / "Mine"
    pkg.mkdir(parents=True)
    (pkg / "README.md").write_text("Builds on Lattices §1.3.\n")
    ws = base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "j" / "scratch", board=board, content=tmp_path,
                        workdir=board, report=tmp_path / "r.md", branch="b", job_id=job.id)
    ws.scratch.mkdir(parents=True)

    class Ctx:
        dry_run, conf, state, gh = True, "/conf.toml", cfg.state_dir(), None

        def __init__(self):
            self.cfg = cfg

        def cli(self, *args):
            return "tauceti-roadmap " + " ".join(args)
    ctx = Ctx()
    lane.prepare_tools(job, ctx, ws)
    assert "Lattices §1.3 (line 1): #7 (@alice)" in (ws.scratch / "overlaps.md").read_text()
    text = lane.tools_text(ctx, ws)
    assert "- **Overlaps**: `tauceti-roadmap roadmaps overlaps <Name>`" in text and "the worker found 1 (`" in text
