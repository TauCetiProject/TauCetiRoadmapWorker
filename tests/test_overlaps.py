"""Overlaps between open roadmap pull requests, against local repositories standing in for the content
repository: a revision of a roadmap on main that renumbers a section and renames a heading other roadmaps
cite, a second revision of the same roadmap that conflicts with it, a new roadmap citing both ids, and the
cases that must not count as a removal (a head that fell behind main) or must (a deleted README)."""

import subprocess

import pytest

from tauceti_roadmap_worker import cli, config, depgraph, gitops, mergeorder, openroadmaps, overlaps
from tauceti_roadmap_worker.paths import HERE

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="needs git")

LATTICES = "TauCetiRoadmap/Lattices/README.md"
LATTICES_LEAN = "TauCetiRoadmap/Lattices/Suggested.lean"
GROUPS = "TauCetiRoadmap/Groups/README.md"
MAIN = {
    LATTICES: "# Roadmap: lattices\n\n## Layer 1: basics\n\n### 1.2 Poisson summation\n\n### 1.3 Theta functions\n",
    LATTICES_LEAN: "theorem poisson : True := trivial\n",
    GROUPS: "# Roadmap: groups\n\nUses Lattices §1.3 and [the basics](../Lattices/README.md#layer-1-basics).\n",
}
# The renumbering keeps a citation of another roadmap's §1.3: a mention, not a definition of its own §1.3.
RENUMBER = {LATTICES: "# Roadmap: lattices\n\n## Layer 1: foundations\n\n### 1.2 Poisson summation\n\n"
                      "### 1.4 Theta functions\n\nCompare ModularForms §1.3.\n"}
CONFLICT = {LATTICES: "# Roadmap: lattices\n\n## Layer 1: basics\n\n### 1.2 Poisson summation\n\n"
                      "### 1.3 Theta series\n"}
THETA = {"TauCetiRoadmap/ThetaSeries/README.md": "# Roadmap: theta series\n\nFrom Lattices §1.2 and Lattices §1.3.\n"}
CONF = str(HERE / "tauceti-roadmap.toml.example")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def commit(repo, files, message):
    for path, text in files.items():
        if text is None:
            git(repo, "rm", "-q", path)
            continue
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


def build(tmp_path, monkeypatch, mains, prs):
    """An origin whose main takes the commits `mains` in turn, and open pull requests (number, login,
    the main commit they branch from, the files they write: None deletes one, an (old, new) key moves
    one), indexed as `roadmaps refresh` indexes them."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    points = [commit(origin, files, f"main {i}") for i, files in enumerate(mains)]
    listed = []
    for number, login, base, files in prs:
        git(origin, "checkout", "-q", "-b", f"pr{number}", points[base])
        for key in files:
            if isinstance(key, tuple):
                (origin / key[1]).parent.mkdir(parents=True, exist_ok=True)
                git(origin, "mv", *key)
        head = commit(origin, {k: v for k, v in files.items() if not isinstance(k, tuple)}, f"pr {number}")
        git(origin, "update-ref", f"refs/pull/{number}/head", head)
        paths = [(k[1] if isinstance(k, tuple) else k, "DELETED" if v is None else "MODIFIED")
                 for k, v in files.items()]
        listed.append({"number": number, "title": f"PR {number}", "isDraft": False, "headRefOid": head,
                       "author": {"login": login},
                       "files": {"nodes": [{"path": p, "changeType": c} for p, c in paths]}})
    git(origin, "checkout", "-q", "main")
    monkeypatch.setattr(gitops, "url", lambda repo: str(origin))
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")

    class FakeGH:
        def graphql(self, query, **variables):
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": listed}}}
    return cfg, openroadmaps.refresh(FakeGH(), cfg)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch, [MAIN],
                 [(7, "alice", 0, RENUMBER), (9, "carol", 0, CONFLICT), (8, "bob", 0, THETA)])


def test_github_heading_anchors():
    text = "# Top\n## B1. Principal bundles\n```\n# not a heading\n```\n## Top\n<a id=\"kept\"></a>\n"
    assert overlaps.slug("Layer 2: `Unramified` extensions — and Frobenius") == \
        "layer-2-unramified-extensions--and-frobenius"
    assert overlaps.anchors(text) == {"top", "b1-principal-bundles", "top-1", "kept"}


def test_an_id_is_defined_by_a_heading_label_or_numbered_item_not_by_a_mention():
    assert openroadmaps.defined("### 1.3 Theta", "1.3") and openroadmaps.defined("### Target 1.3: theta", "1.3")
    assert openroadmaps.defined("- **B1.2** stabilizers", "B1.2")
    assert openroadmaps.defined("## B1. Actions\n\n1. orbits\n2. stabilizers\n", "B1.2")
    assert openroadmaps.defined("### 1.1 Spinors and their dictionary (G1.1)", "G1.1")
    assert not openroadmaps.defined("## 2.1 Uses ModularForms §1.3", "1.3")
    assert not openroadmaps.defined(RENUMBER[LATTICES], "1.3")            # ModularForms §1.3 is a mention
    assert openroadmaps.present(RENUMBER[LATTICES], "1.3")                # which `present` accepts
    assert openroadmaps.defined("## Layer 3: wild", "3", layer=True)
    assert not openroadmaps.defined("## Layer 30: wild", "3", layer=True)


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


def test_an_id_main_gained_after_the_branch_is_not_dropped(tmp_path, monkeypatch):
    later = {LATTICES: MAIN[LATTICES] + "\n### 1.5 Lattice sums\n", GROUPS: MAIN[GROUPS] + "Also Lattices §1.5.\n"}
    cfg, index = build(tmp_path, monkeypatch, [MAIN, later],
                       [(11, "dave", 0, {LATTICES_LEAN: "theorem poisson' : True := trivial\n"})])
    assert overlaps.revisions(cfg.state_dir(), index) == []                 # its README is the merge base's
    assert overlaps.removals(cfg.state_dir(), index) == []
    text, facts = overlaps.report(cfg.state_dir(), index, "Groups", later[GROUPS])
    assert facts == 0 and "#11" not in text


def test_a_deleted_or_emptied_readme_drops_every_cited_id(tmp_path, monkeypatch):
    cfg, index = build(tmp_path, monkeypatch, [MAIN],
                       [(12, "erin", 0, {LATTICES: None}), (13, "frank", 0, {LATTICES: ""})])
    found = sorted((r.pr, r.citation.label()) for r in overlaps.removals(cfg.state_dir(), index))
    assert found == [(12, "Lattices #layer-1-basics"), (12, "Lattices §1.3"),
                     (13, "Lattices #layer-1-basics"), (13, "Lattices §1.3")]


def test_shared_folders_say_whether_the_heads_merge(world):
    cfg, index = world
    [lattices] = overlaps.shared(cfg.state_dir(), index)
    assert (lattices.roadmap, lattices.prs) == ("Lattices", (7, 9))
    merge = lattices.conflicts[(7, 9)]
    if merge is None:
        pytest.skip("this git cannot merge-tree --write-tree")
    assert not merge.clean and merge.paths == (LATTICES,)


def test_a_conflict_that_names_no_file_is_still_a_conflict(tmp_path, monkeypatch):
    split = {**MAIN, "notes/one": "1\n", "notes/two": "2\n"}
    cfg, _index = build(tmp_path, monkeypatch, [split],
                        [(21, "gina", 0, {("notes/one", "one/one"): "1\n", ("notes/two", "two/two"): "2\n",
                                          LATTICES_LEAN: "theorem a : True := trivial\n"}),
                         (22, "hal", 0, {"notes/three": "3\n", "TauCetiRoadmap/Groups/Suggested.lean": "\n"})])
    merge = overlaps.merge_conflicts(cfg.state_dir(), 21, 22)
    if merge is None:
        pytest.skip("this git cannot merge-tree --write-tree")
    assert not merge.clean and merge.paths == ()
    assert merge.messages and merge.messages[0].startswith("CONFLICT (directory rename split)")
    assert overlaps._conflict_text((21, 22), merge).startswith(
        "#21 and #22 conflict: CONFLICT (directory rename split)")


def test_report_for_a_citing_roadmap_and_for_the_revision(world):
    cfg, index = world
    state = cfg.state_dir()
    text, facts = overlaps.report(state, index, "ThetaSeries", THETA["TauCetiRoadmap/ThetaSeries/README.md"], pr=8)
    assert facts == 1
    assert "- Lattices §1.3 (line 3): #7 (@alice) changes Lattices and no longer defines it at its head" in text
    assert "drops and other roadmaps cite" not in text                  # #8 changes no roadmap on main

    text, facts = overlaps.report(state, index, "Lattices", RENUMBER[LATTICES], pr=7)
    assert "- #9 (@carol) PR 9" in text and "#7 and #9 conflict in" in text
    assert "- Lattices #layer-1-basics: cited by Groups (main line 3)" in text
    assert "- Lattices §1.3: cited by Groups (main line 3); ThetaSeries (#8 line 3)" in text
    assert facts == 3                                                   # one other pull request, two dropped ids


def test_a_citation_of_a_revisions_numbering_makes_it_a_dependency(world):
    cfg, index = world
    state = cfg.state_dir()
    readme = "# Roadmap: mine\n\nUses Lattices §1.4 and Lattices §1.2.\n"
    assert overlaps.revision_dependencies(state, index, readme, "Mine") == [(7, "Lattices", "alice")]
    text, facts = overlaps.report(state, index, "Mine", readme)
    assert facts == 1 and "## Ids this README cites that only an open revision defines" in text
    assert "- Lattices §1.4 (line 3): #7 (@alice) defines it in Lattices at its head (main does not)" in text
    # The merge-order paragraph and the port lane's Dependencies sentence carry it.
    prs = [{"pr": 905, "name": "Mine"}]
    para = mergeorder.plan(prs, {"Mine": readme}, ["Mine"], set(), {}, {"905": {}}, set(),
                           revisions={905: [(7, "Lattices", "alice")]})[905]
    assert ("It cites roadmaps on main by the numbering of open pull requests that revise them, which must merge "
            "first: #7 (Lattices, alice).") in para
    deps = depgraph.dependency_paragraph([], [(7, "Lattices", "alice")])
    assert "which must merge first: #7 (Lattices, alice)." in deps
    assert "Open revisions" not in depgraph.dependency_paragraph([])

    class Ctx:
        def __init__(self):
            self.cfg, self.state, self.gh = cfg, state, None             # no GitHub: the host's index

    assert mergeorder.revision_facts(Ctx(), prs, {"Mine": readme}) == {905: [(7, "Lattices", "alice")]}


def test_report_all_and_the_command(world, capsys):
    cfg, index = world
    text = overlaps.report_all(cfg.state_dir(), index)
    assert "- Lattices (`TauCetiRoadmap/Lattices`): #7, #9" in text
    assert "- #7 (@alice):" in text
    assert "  - Lattices §1.3: cited by Groups (main line 3); ThetaSeries (#8 line 3)" in text
    assert cli.main(["--conf", CONF, "roadmaps", "overlaps"]) == 0
    assert capsys.readouterr().out == text
    assert cli.main(["--conf", CONF, "roadmaps", "overlaps", "Lattices", "--pr", "7"]) == 0
    assert "## Ids of Lattices that this pull request drops and other roadmaps cite" in capsys.readouterr().out
    assert cli.main(["--conf", CONF, "roadmaps", "overlaps", "ThetaSeries"]) == 0      # read at #8, its only copy
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


def lane_workspace(cfg, tmp_path, job_id, readme, **inputs):
    from tauceti_roadmap_worker.board import new_job
    from tauceti_roadmap_worker.lanes import base, lane_for
    job = new_job(job_id, inputs=inputs)
    lane = lane_for(job)
    board = tmp_path / "board"
    pkg = board / "packages" / job.name
    pkg.mkdir(parents=True)
    (pkg / "README.md").write_text(readme)
    ws = base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "j" / "scratch", board=board, content=tmp_path,
                        workdir=board, report=tmp_path / "r.md", branch="b", job_id=job.id)
    ws.scratch.mkdir(parents=True)

    class Ctx:
        dry_run, conf, state, gh = True, "/conf.toml", cfg.state_dir(), None

        def __init__(self):
            self.cfg = cfg

        def cli(self, *args):
            return "tauceti-roadmap " + " ".join(args)
    return lane, job, ws, Ctx()


def test_a_lane_writes_the_overlaps_of_its_roadmap(world, tmp_path):
    cfg, _index = world
    lane, job, ws, ctx = lane_workspace(cfg, tmp_path, "REV-PKG-Mine~2", "Builds on Lattices §1.3.\n")
    lane.prepare_tools(job, ctx, ws)
    assert "Lattices §1.3 (line 1): #7 (@alice)" in (ws.scratch / "overlaps.md").read_text()
    text = lane.tools_text(ctx, ws)
    assert "- **Overlaps**: `tauceti-roadmap roadmaps overlaps <Name>`" in text and "the worker found 1 (`" in text


def test_a_board_pull_request_number_is_not_taken_for_a_content_one(world, tmp_path):
    # Board pull request #7 carries the package; content pull request #7 is the unrelated revision.
    cfg, _index = world
    lane, job, ws, ctx = lane_workspace(cfg, tmp_path, "REV-PKG-Mine~2", "Builds on Lattices §1.3.\n", board_pr=7)
    ws.extra["pr"] = 7
    lane.prepare_tools(job, ctx, ws)
    assert "Lattices §1.3 (line 1): #7 (@alice) changes Lattices" in (ws.scratch / "overlaps.md").read_text()
