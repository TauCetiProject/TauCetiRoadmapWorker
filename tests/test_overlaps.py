"""Overlaps between open roadmap pull requests, against local repositories standing in for the content
repository: a revision of a roadmap on main that renumbers a section and renames a heading other roadmaps
cite, a second revision of the same roadmap that conflicts with it, a new roadmap citing both ids, and the
cases that must not count as a removal (a head that fell behind main) or must (a deleted README)."""

import re
import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import (
    cli, config, depgraph, deplabels, gitops, mergeorder, openroadmaps, overlaps, prreviews,
)
from tauceti_roadmap_worker.gh import GHError
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


class ContentGH:
    """The content repository as `refresh` and `deplabels` read it: the open pull requests' listing (with
    labels), each pull request's state and description (`states`: number -> {state, body}, or "issue"),
    the merged pull requests a label search finds, and every write. A number in `blip` gets a 502."""

    def __init__(self, listed, states=None, merged_labelled=()):
        self.listed, self.states, self.merged_labelled = listed, dict(states or {}), list(merged_labelled)
        self.blip, self.writes, self.dry_run = set(), [], False

    def graphql(self, query, **variables):
        if "pullRequests(states: OPEN" in query:
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": self.listed}}}
        numbers = [int(n) for n in re.findall(r"p(\d+): issueOrPullRequest", query)]
        if set(numbers) & self.blip:
            raise GHError("graphql: HTTP 502")
        nodes = {}
        for n in numbers:
            got = self.states.get(n)
            nodes[f"p{n}"] = (None if got is None else {"__typename": "Issue"} if got == "issue" else
                              {"__typename": "PullRequest", "state": got["state"].upper(), "body": got.get("body", ""),
                               "files": {"totalCount": len(got.get("added", [])) + len(got.get("removed", [])),
                                         "nodes": [{"path": a, "changeType": "ADDED"} for a in got.get("added", [])]
                                         + [{"path": r, "changeType": "DELETED"} for r in got.get("removed", [])]}})
        return {"repository": nodes}

    def api(self, path, paginate=False):
        m = re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path)
        if m:
            n = int(m.group(1))
            if n in self.blip:
                raise GHError(f"GET {path}: HTTP 502: Bad Gateway")
            got = self.states.get(n)
            if got is None or got == "issue":
                raise GHError(f"GET {path}: HTTP 404: Not Found")
            return {"state": "open" if got["state"] == "open" else "closed", "merged": got["state"] == "merged",
                    "body": got.get("body", "")}
        m = re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/files\?per_page=100", path)
        if m:
            got = self.states[int(m.group(1))]
            return ([{"filename": a, "status": "added"} for a in got.get("added", [])]
                    + [{"filename": r, "status": "removed"} for r in got.get("removed", [])])
        if path.startswith("search/issues"):
            return {"items": [{"number": n} for n in self.merged_labelled]}
        raise GHError(f"GET {path}: HTTP 404: Not Found")

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))


def exists(repo, ref, path) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
                          capture_output=True, check=False).returncode == 0


def build(tmp_path, monkeypatch, mains, prs, labels=None, bodies=None):
    """An origin whose main takes the commits `mains` in turn, and open pull requests (number, login,
    the main commit they branch from, the files they write: None deletes one, an (old, new) key moves
    one with `git mv` and writes the text given, and GitHub's listing names only its new path, as
    RENAMED), indexed as `roadmaps refresh` indexes them."""
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
        head = commit(origin, {k[1] if isinstance(k, tuple) else k: v for k, v in files.items()}, f"pr {number}")
        git(origin, "update-ref", f"refs/pull/{number}/head", head)
        paths = [(k[1], "RENAMED") if isinstance(k, tuple) else
                 (k, "DELETED" if v is None else "MODIFIED" if exists(origin, points[base], k) else "ADDED")
                 for k, v in files.items()]
        listed.append({"number": number, "title": f"PR {number}", "isDraft": False, "headRefOid": head,
                       "author": {"login": login},
                       "labels": {"nodes": [{"name": x} for x in (labels or {}).get(number, [])]},
                       "files": {"nodes": [{"path": p, "changeType": c} for p, c in paths]}})
    git(origin, "checkout", "-q", "main")
    monkeypatch.setattr(gitops, "url", lambda repo: str(origin))
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")

    build.gh = ContentGH(listed, {n: {"state": "open", "body": (bodies or {}).get(n, "")} for n, *_ in prs})
    return cfg, openroadmaps.refresh(build.gh, cfg)


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


def test_a_renumbering_that_maps_its_old_ids_drops_none_of_them(tmp_path, monkeypatch):
    table = RENUMBER[LATTICES] + "\n| Former label | Now |\n|---|---|\n| §1.3 | §1.4 Theta functions |\n"
    sentence = RENUMBER[LATTICES] + "\nThe earlier §1.3 is now §1.4; nothing else moved.\n"
    status = RENUMBER[LATTICES] + "\n| Section | Status |\n|---|---|\n| §1.3 | done |\n"
    for text, kept in ((table, False), (sentence, False), (status, True)):
        root = tmp_path / str(len(text))
        root.mkdir()
        cfg, index = build(root, monkeypatch, [MAIN], [(7, "alice", 0, {LATTICES: text})])
        got = {r.citation.label() for r in overlaps.removals(cfg.state_dir(), index)}
        # The heading anchor never follows a mapping: a link to it lands at the top of the README.
        assert got == ({"Lattices §1.3", "Lattices #layer-1-basics"} if kept else {"Lattices #layer-1-basics"}), text


def test_the_old_to_new_mappings_roadmaps_write():
    def cite(roadmap, id_, kind):
        return overlaps.Citation("main", "Other", 1, roadmap, id_, kind, "")
    # #989's sentence and table for SemisimpleAlgebras, and #617's "Former layer numbers" table.
    semisimple = ("## Layer 1: simple modules\n\nThe earlier separate Layer 1.5 is now §1.3 of this layer; Layers "
                  "0–6 keep their numbers, so a\ncitation of Layers 1.5–2 reads as §1.3 and Layer 2.\n\n"
                  "| Earlier label | Present location |\n| --- | --- |\n| Layer 1.5: every simple module is a "
                  "minimal left ideal | §1.3 Simple modules as minimal left ideals |\n")
    local = ("**Former layer numbers.** This roadmap formerly numbered the material above as Layers 0–4.\n\n"
             "| Former label | Content | Now |\n|---|---|---|\n| Layer 3 | ramification | *Ramified extensions* |\n"
             "| Layer 4 | the tame quotient | *Absolute Galois group* |\n")
    assert overlaps.mapped(semisimple, cite("SemisimpleAlgebras", "1.5", "layer"))
    # #1701's: the old section now resolves to library declarations.
    derived = ("Stage 6. Citations of §1.T.1 (*Towers, `lim` and `lim¹`*, with its targets *lim and lim¹ of a\n"
               "tower*) resolve to the Tau Ceti tower declarations listed in Exact supplier contracts.\n")
    assert overlaps.mapped(derived, cite("DerivedModuleAlgebra", "1.T.1", "section"))
    assert overlaps.mapped(local, cite("LocalFieldsRamification", "4", "layer"))
    assert not overlaps.mapped(local, cite("LocalFieldsRamification", "5", "layer"))
    assert not overlaps.mapped(semisimple, cite("SemisimpleAlgebras", "1.6", "layer"))
    assert not overlaps.mapped(semisimple, cite("SemisimpleAlgebras", "layer-15-every-simple-module", "anchor"))


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


def test_an_id_a_pull_requests_own_revision_defines_is_its_own(tmp_path, monkeypatch):
    mine = {"TauCetiRoadmap/Mine/README.md": "# Roadmap: mine\n\nUses Lattices §1.4.\n"}
    cfg, index = build(tmp_path, monkeypatch, [MAIN], [(7, "alice", 0, RENUMBER), (40, "zed", 0, {**RENUMBER, **mine}),
                                                       (41, "ann", 0, mine)])
    state, readme = cfg.state_dir(), mine["TauCetiRoadmap/Mine/README.md"]
    # #40 renumbers Lattices as #7 does: its citation of §1.4 is its own. #41 needs #7 (or #40).
    assert overlaps.revision_dependencies(state, index, readme, "Mine", pr=40) == []
    assert overlaps.revision_dependencies(state, index, readme, "Mine", pr=41) == [(7, "Lattices", "alice"),
                                                                                   (40, "Lattices", "zed")]


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


def test_a_description_names_its_dependencies():
    deps = overlaps.declared_dependencies
    assert deps("Depends on:\n- #887 \n- #1050 \n\n\nAI assistance", own=1506) == [887, 1050]
    assert deps("**Stack.** #785 (A) → #1688 (B) → **this pull request**. Reopened; #793 merged into #785.",
                own=1689) == [785, 1688]
    merge_order = ("**Merge order.** #1 (A) → **this pull request** (#5). It also depends on other authors' pull "
                   "requests that add roadmaps it cites, which must merge first: #934 (Q, w). It also cites #77 (R), "
                   "which in turn consume this one.")
    assert deps(merge_order, own=5) == [1, 934]
    assert deps("It depends on no other author's open pull request. This pull request now depends on the "
                "revisions #1410 (recorded in its description).", own=617) == [1410]
    assert deps("Merge order: after AnalyticNumberTheoryPartI (#1126) lands, since the statements refer to its §0.34.",
                own=1913) == [1126]
    # #1854's description: a chain into this pull request, and a pull request it says it is independent of.
    assert deps("**Merge order.** #1101 → #1807 → this pull request (the cut consumes what #1807 establishes). "
                "#1808 is independent of this pull request.", own=1854) == [1101, 1807]
    assert deps("Open revisions of roadmaps on main whose numbering this roadmap cites, which merge first: "
                "#1097, #1771.", own=1137) == [1097, 1771]
    # #1705's and #1974's descriptions: a subject that merges first, and one that waits for this pull request.
    assert deps("Prerequisites: #1125 and #1124 must merge first.", own=1705) == [1124, 1125]
    assert deps("**Merge order.** This PR merges after #1683 and #1327, which restructure the parent README; it "
                "will be rebased onto them once they land. It merges before #956, whose PositiveMass §3.1 cites "
                "Surfaces Layer 3 (S3).", own=1974) == [1327, 1683]
    assert deps("#7 merges before this pull request.", own=8) == [7]
    block = ("Text.\n\n<!-- depends-on -->\n**Depends on** (suppliers still open; they merge first):\n"
             "- [ ] #1126 AnalyticNumberTheoryPartI\n- [x] #1408 ComputationalNumberTheory\n"
             "- [ ] PlanarTopology (no pull request yet)\n<!-- /depends-on -->\nDepends on #44.")
    assert overlaps.declared_items(block, own=9) == [(None, "PlanarTopology"), (44, None),
                                                     (1126, "AnalyticNumberTheoryPartI")]
    # Not dependencies: independence, negation, the other direction, pull requests that come after.
    for text in ("This PR is independent of #17.", "Does not depend on #20.", "#22 depends on this PR.",
                 "#1411 consumes its §4a.1, so the order is this PR (#1410) → #1411; #940 and #856 cite its "
                 "§3a.1 and merge after it.", "It can merge before or after #937;",
                 "This pull request must merge first; #22 follows.", "It merges first, then #23."):
        assert deps(text, own=1410) == [], text


def test_the_dependency_record_of_a_head(tmp_path, monkeypatch):
    cstar = {"TauCetiRoadmap/CStar/README.md": "# Roadmap: C*-algebras\n\n### 1.1 Basics\n"}
    mine = {"TauCetiRoadmap/Mine/README.md": "# Roadmap: mine\n",
            "TauCetiRoadmap/Helper/README.md": "# Roadmap: helper\n"}
    cfg, index = build(tmp_path, monkeypatch, [MAIN],
                       [(7, "alice", 0, RENUMBER), (30, "dora", 0, cstar), (31, "eve", 0, mine)])
    readme = ("# Roadmap: mine\n\n### 1.1 Foo\n\n*Needs:* CStar §1.1; Lattices §1.2 and Helper §2.\n\n"
              "Uses Lattices §1.4.\n")
    record = overlaps.dependency_record(cfg.state_dir(), index, readme, "import TauCetiRoadmap.Ghost.Suggested\n",
                                        "Mine", pr=31, body="Depends on #44.")
    assert record == {"suppliers": [{"roadmap": "CStar", "prs": [30], "cited": True},
                                    {"roadmap": "Ghost", "prs": [], "cited": True}],
                      "revisions": [{"roadmap": "Lattices", "pr": 7, "ids": ["Lattices §1.4"]}],
                      "declared": [{"pr": 44, "roadmap": None}]}
    # A path in a code span and a Lean namespace name no supplier.
    path_only = "### 1.1 A\n\n*Needs:* `Foo/LevelOne/CStar/` supplies it.\n"
    record = overlaps.dependency_record(cfg.state_dir(), index, path_only, "namespace TauCetiRoadmap.Ghost\n", "Mine",
                                        pr=31)
    assert record["suppliers"] == []
    # A depends-on block: a roadmap not on main is a supplier (met once on main); one on main is waited
    # for through its pull request; a roadmap the pull request carries itself is no dependency.
    block = ("<!-- depends-on -->\n**Depends on**:\n- [ ] #60 PlanarTopology\n- [ ] #61 Lattices\n"
             "- [ ] #31 Helper\n<!-- /depends-on -->")
    record = overlaps.dependency_record(cfg.state_dir(), index, "# Roadmap: mine\n", "", "Mine", pr=31, body=block)
    assert record == {"suppliers": [{"roadmap": "PlanarTopology", "prs": [60], "cited": False}], "revisions": [],
                      "declared": [{"pr": 61, "roadmap": "Lattices"}]}
    # Lattices is on main and Helper rides in the same pull request: neither is a dependency.


DEPS = "awaiting-dependencies"


def upward(number, on, supplier, prs):
    return (f"#{number}: UPWARD, not waited for: {on} (on main) -> {supplier} ({prs}); a roadmap on main does not "
            "wait for one that is not, so the edge goes")


def label_writes(gh):
    return sorted((method, int(re.search(r"issues/(\d+)/", path).group(1))) for method, path, _payload in gh.writes)


def test_the_label_follows_what_each_open_pull_request_waits_for_now(tmp_path, monkeypatch):
    cstar = {"TauCetiRoadmap/CStar/README.md": "# Roadmap: C*-algebras\n\n### 1.1 Basics\n"}
    def new(name, text):
        return {f"TauCetiRoadmap/{name}/README.md": f"# Roadmap: {name}\n\n{text}\n"}
    prs = [(7, "alice", 0, RENUMBER), (30, "dora", 0, cstar),
           (31, "eve", 0, new("Mine", "*Needs:* CStar §1.1.")),                  # waits for CStar (#30)
           (32, "fay", 0, new("Free", "Nothing needed.")),                      # waits for nothing
           (33, "gus", 0, new("Open", "Plain.")), (34, "hal", 0, new("Done", "Plain.")),
           (35, "ivy", 0, new("Indep", "Plain.")),
           (38, "jon", 0, new("Renum", "Uses Lattices §1.4.")),                 # #7's numbering
           ]
    bodies = {33: "Depends on #44.", 34: "Depends on #45.", 35: "#36 is independent of this pull request."}
    labels = {32: [DEPS], 33: [DEPS], 34: [DEPS], 35: [DEPS]}
    cfg, _index = build(tmp_path, monkeypatch, [MAIN], prs, labels=labels, bodies=bodies)
    gh = build.gh
    gh.states.update({44: {"state": "open"}, 45: {"state": "merged"}, 36: {"state": "open"}})
    gh.merged_labelled = [12]
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    assert label_writes(gh) == [("DELETE", 12), ("DELETE", 32), ("DELETE", 34), ("DELETE", 35),
                                ("POST", 31), ("POST", 38)]
    assert f"#31: {DEPS} added (waits for CStar (#30))" in out
    assert f"#38: {DEPS} added (waits for #7 (revision of Lattices))" in out
    assert f"#12: {DEPS} removed (merged)" in out and f"#35: {DEPS} removed (it waits for no open pull request)" in out
    # #33 still waits for the open #44: kept, nothing written. Its description changes: read as it is now.
    gh.writes.clear()
    gh.states[33]["body"] = "Plain."
    gh.merged_labelled = []
    deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    assert ("DELETE", 33) in label_writes(gh)


def test_what_github_does_not_answer_keeps_the_label(tmp_path, monkeypatch):
    def new(name):
        return {f"TauCetiRoadmap/{name}/README.md": f"# Roadmap: {name}\n"}
    cfg, _index = build(tmp_path, monkeypatch, [MAIN], [(50, "kim", 0, new("A")), (51, "lou", 0, new("B")),
                                                         (52, "max", 0, new("C"))],
                        labels={50: [DEPS], 51: [DEPS], 52: [DEPS]},
                        bodies={50: "Depends on #60.", 52: "Depends on #61."})
    gh = build.gh
    gh.blip = {51, 60}                    # #51's own description, and the state of #60, go unanswered
    gh.states[61] = "issue"               # a 404: no pull request, so nothing waits for it
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    assert label_writes(gh) == [("DELETE", 52)]
    assert f"#51: {DEPS} left as it is (its description was not read)" in out
    assert f"#50: {DEPS} left as it is (a pull request its description names was not read)" in out


def test_the_command_needs_a_label(world, monkeypatch, capsys):
    conf = str(HERE / "tauceti-roadmap.toml.example")
    with pytest.raises(SystemExit, match="give --label"):
        cli.main(["--conf", conf, "roadmaps", "dependency-labels"])
    seen = {}
    monkeypatch.setattr(cli, "_bot_context", lambda args, cfg, dry: SimpleNamespace(dry=dry))
    monkeypatch.setattr(deplabels, "run", lambda ctx, label: seen.update(label=label, dry=ctx.dry) or ["ok"])
    assert cli.main(["--conf", conf, "roadmaps", "dependency-labels", "--label", DEPS]) == 0
    assert seen == {"label": DEPS, "dry": True} and capsys.readouterr().out == "ok\n"


def block(*items):
    return ("<!-- depends-on -->\n**Depends on** (suppliers still open; they merge first):\n"
            + "".join(f"- [ ] {item}\n" for item in items) + "<!-- /depends-on -->")


def roadmap(name, text="", extra=None):
    files = {f"TauCetiRoadmap/{name}/README.md": f"# Roadmap: {name}\n\n{text}\n"}
    files.update({f"TauCetiRoadmap/{name}/{k}": v for k, v in (extra or {}).items()})
    return files


def test_a_dependency_named_only_in_the_block_is_met_once_merged(tmp_path, monkeypatch):
    cfg, _index = build(tmp_path, monkeypatch, [MAIN], [(90, "ann", 0, roadmap("Waiter"))],
                        labels={90: [DEPS]}, bodies={90: block("#44 Lattices")})
    gh = build.gh
    gh.states[44] = {"state": "open"}            # a revision of Lattices, open but not a roadmap pull request here
    deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    assert label_writes(gh) == []
    gh.states[44] = {"state": "merged"}
    deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    assert label_writes(gh) == [("DELETE", 90)]


def test_a_supplier_brought_by_another_pull_request_meets_the_one_named(tmp_path, monkeypatch):
    cstar = roadmap("CStar", "### 1.1 Basics")
    later = {**MAIN, **cstar}                    # CStar reached main through another pull request
    cfg, _index = build(tmp_path, monkeypatch, [MAIN, later],
                        [(70, "bea", 0, roadmap("CStar", "### 1.1 Other basics")),       # opened to add CStar
                         (71, "cal", 1, roadmap("Uses")), (72, "dan", 1, roadmap("Also")),
                         (73, "eli", 1, roadmap("Rev"))],
                        labels={71: [DEPS], 72: [DEPS], 73: [DEPS]},
                        bodies={71: block("#30 CStar"), 72: block("#70 CStar"), 73: block("#61 Lattices")})
    gh = build.gh
    gh.states[30] = {"state": "closed", "added": ["TauCetiRoadmap/CStar/README.md"]}    # replaced by another
    gh.states[61] = {"state": "closed", "added": []}                                       # a revision, abandoned
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    # #71's #30 added CStar and #72's #70 was opened to add it: met. #73's #61 revised Lattices and was
    # closed without merging: still waits, so its label stays.
    assert label_writes(gh) == [("DELETE", 71), ("DELETE", 72)] and not any("#73" in line for line in out)


def test_every_lean_file_of_a_roadmap_counts_and_a_pull_request_that_changes_only_one(tmp_path, monkeypatch):
    ghost = "import TauCetiRoadmap.Ghost.Suggested\n"
    nested = {**roadmap("Fam", "Family."), "TauCetiRoadmap/Fam/Sub/README.md": "# Roadmap: Sub\n",
              "TauCetiRoadmap/Fam/Sub/Suggested.lean": ghost}
    cfg, index = build(tmp_path, monkeypatch, [MAIN, {**MAIN, **nested}],
                       [(80, "fay", 0, roadmap("Mine", "", {"Suggested.lean": "import TauCetiRoadmap.Mine.Extras\n",
                                                            "Extras.lean": ghost})),
                        (81, "gil", 0, {"TauCetiRoadmap/Lattices/Extra.lean": ghost}),
                        (82, "hob", 1, {"TauCetiRoadmap/Fam/README.md": "# Roadmap: Fam\n\nFamily, edited.\n"})])
    gh = build.gh
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    assert f"#80: {DEPS} added (waits for Ghost (in no open pull request))" in out
    assert upward(81, "Lattices", "Ghost", "in no open pull request") in out   # Lattices is on main
    pr82 = next(p for p in openroadmaps.load(cfg.state_dir())["prs"] if p["pr"] == 82)
    assert deplabels.roadmap_folders(cfg.state_dir(), index, pr82) == {
        "TauCetiRoadmap/Fam": ["TauCetiRoadmap/Fam/README.md"]}
    assert label_writes(gh) == [("POST", 80)] and not any(line.startswith("#82") for line in out)
    # (Sub's import is Sub's, not the family's.)


def test_only_an_http_404_says_a_number_is_no_pull_request():
    class Errors:
        def __init__(self, text):
            self.text = text

        def api(self, path, paginate=False):
            raise GHError(f"gh api {path}: {self.text}")
    repo = "TauCetiProject/TauCetiRoadmap"
    assert deplabels.rest_state(Errors("HTTP 502: Bad Gateway"), repo, 404) is None
    assert deplabels.rest_state(Errors("dial tcp: lookup api.github.com: no such host"), repo, 44) is None
    assert deplabels.rest_state(Errors("gh: Not Found (HTTP 404)"), repo, 404) == {"state": "absent"}


def test_a_pull_request_waits_only_for_what_it_brings_not_for_what_main_already_cites(tmp_path, monkeypatch):
    old = roadmap("Old", "*Needs:* Ghost §1.1.", {"PROGRESS.md": "Started.\n", "STATUS.md": "open\n"})
    cfg, _index = build(tmp_path, monkeypatch, [{**MAIN, **old}],
                        [(110, "eve", 0, roadmap("Ghost", "### 1.1 Spirits")),           # Old on main cites it
                         (111, "flo", 0, roadmap("Phantom", "### 2.1 Shades")),
                         (100, "ada", 0, {"TauCetiRoadmap/Old/PROGRESS.md": "Week 2.\n",  # a progress report
                                          "TauCetiRoadmap/Old/STATUS.md": "active\n"}),
                         (101, "bo", 0, roadmap("Old", "*Needs:* Ghost §1.1. Reworded.")),
                         (102, "cy", 0, roadmap("Old", "*Needs:* Ghost §1.1 and Phantom §2.1.")),
                         (103, "di", 0, roadmap("Old", "*Needs:* Ghost §1.1.",
                                                {"Suggested.lean": "import TauCetiRoadmap.Ghost\n"})),
                         (104, "ed", 0, roadmap("Old", "*Needs:* Ghost §1.1. Reworded again.")),
                         (105, "fi", 0, roadmap("Old", "Reworded; Ghost no longer named."))],
                        bodies={104: block("#110 Ghost"), 105: block("#110 Ghost")})
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=build.gh), DEPS)
    # Old is on main and already names Ghost. A progress report touches no roadmap file; an edit that keeps
    # Ghost brings nothing. What an edit newly names (Phantom, or Ghost through an import or beside its own
    # declaration) is an UPWARD edge of a roadmap on main: logged for the author, not waited for. Only a
    # dependency the description alone declares (#105) puts the label on.
    assert label_writes(build.gh) == [("POST", 105)]
    assert f"#105: {DEPS} added (waits for Ghost (#110))" in out
    assert upward(102, "Old", "Phantom", "#111") in out and not any("#102: UPWARD" in x and "Ghost" in x for x in out)
    assert upward(103, "Old", "Ghost", "#110") in out and upward(104, "Old", "Ghost", "#110") in out
    assert not any(line.startswith(("#100", "#101")) for line in out)


def test_a_pin_bump_waits_for_nothing_its_folders_already_cited(tmp_path, monkeypatch):
    lean = "theorem t : True := trivial\n"
    twins = {"Completed/Twin/README.md": "# Roadmap: Twin\n\nDone.\n", "Completed/Twin/Suggested.lean": lean,
             **roadmap("Twin", "*Needs:* Ghost §1.1.", {"Suggested.lean": lean}),
             **roadmap("Drift", "*Needs:* Ghost §1.1.", {"Suggested.lean": lean})}
    later = {**twins, "TauCetiRoadmap/Drift/README.md": "# Roadmap: Drift\n\nGhost no longer needed.\n"}
    bump = "-- bumped\n" + lean
    cfg, _index = build(tmp_path, monkeypatch, [{**MAIN, **twins}, {**MAIN, **later}],
                        [(110, "eve", 1, roadmap("Ghost", "### 1.1 Spirits")),
                         # Two folders named Twin, each compared with its own README on main.
                         (120, "gus", 1, {"Completed/Twin/Suggested.lean": bump,
                                          "TauCetiRoadmap/Twin/Suggested.lean": bump}),
                         # Branched before main's Drift dropped Ghost: its stale README is not its doing.
                         (121, "hal", 0, {"TauCetiRoadmap/Drift/Suggested.lean": bump})])
    deplabels.run(SimpleNamespace(cfg=cfg, gh=build.gh), DEPS)
    assert label_writes(build.gh) == []


def test_a_revision_of_a_roadmap_main_has_since_moved_is_still_a_revision(tmp_path, monkeypatch):
    moved = {LATTICES: None, LATTICES_LEAN: None, "Completed/Lattices/README.md": MAIN[LATTICES],
             "Completed/Lattices/Suggested.lean": MAIN[LATTICES_LEAN]}
    cfg, _index = build(tmp_path, monkeypatch, [MAIN, moved],
                        [(7, "alice", 0, RENUMBER),                       # branched before main moved Lattices
                         (130, "bea", 1, roadmap("Waiter")), (131, "cal", 1, roadmap("Other"))],
                        labels={130: [DEPS], 131: [DEPS]},
                        bodies={130: block("#7 Lattices"), 131: block("#150 Lattices")})
    gh = build.gh
    # #150 moved Lattices itself (an add and a remove, no rename) and was closed: it added no roadmap.
    gh.states[150] = {"state": "closed", "added": ["Completed/Lattices/README.md"], "removed": [LATTICES]}
    deplabels.run(SimpleNamespace(cfg=cfg, gh=gh), DEPS)
    assert label_writes(gh) == []
    assert not deplabels.added_at_base(cfg.state_dir(), openroadmaps.load(cfg.state_dir()), 7, "Lattices")


def test_an_id_main_already_cites_hides_no_other_id_of_the_same_revision(tmp_path, monkeypatch):
    ghost = roadmap("Ghost", "### 1.1 Spirits")
    old = roadmap("Old", "Uses Lattices §1.4.")                       # main already cites #140's §1.4
    upstream = {LATTICES: RENUMBER[LATTICES] + "\n### 1.5 Theta lifts\n",
                "TauCetiRoadmap/Ghost/README.md": "# Roadmap: Ghost\n\n### 1.1 Spirits\n\n### 1.2 Wraiths\n"}
    cfg, _index = build(tmp_path, monkeypatch, [{**MAIN, **ghost, **old}],
                        [(140, "dot", 0, upstream),
                         (141, "eli", 0, roadmap("Old", "Uses Lattices §1.4 and Ghost §1.2.")),
                         (142, "fin", 0, roadmap("Old", "Uses Lattices §1.4 and Lattices §1.5.")),
                         (143, "gil", 0, roadmap("Old", "Uses Lattices §1.4, reworded."))])
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=build.gh), DEPS)
    assert label_writes(build.gh) == [("POST", 141), ("POST", 142)]
    assert f"#141: {DEPS} added (waits for #140 (revision of Ghost))" in out
    assert f"#142: {DEPS} added (waits for #140 (revision of Lattices))" in out


def test_a_folder_is_compared_with_its_own_readme_not_another_of_the_same_name(tmp_path, monkeypatch):
    twins = {"Completed/Twin/README.md": "# Roadmap: Twin\n\n*Needs:* Ghost §1.1.\n",
             **roadmap("Twin", "*Needs:* Phantom §2.1."),
             **roadmap("Drift", "*Needs:* Ghost §1.1.", {"Suggested.lean": "theorem t : True := trivial\n"})}
    cfg, _index = build(tmp_path, monkeypatch, [{**MAIN, **twins}],
                        [(110, "eve", 0, roadmap("Ghost", "### 1.1 Spirits")),
                         (111, "flo", 0, roadmap("Phantom", "### 2.1 Shades")),
                         (121, "gus", 0, roadmap("Twin", "*Needs:* Phantom §2.1 and Ghost §1.1.")),
                         (122, "hal", 0, {"Completed/Twin/README.md": "# Roadmap: Twin\n\n*Needs:* Ghost §1.1, "
                                                                     "Phantom §2.1.\n"}),
                         # Moves Drift to Completed/ with the citation it had: brings nothing.
                         (123, "ida", 0, {"TauCetiRoadmap/Drift/README.md": None,
                                          "TauCetiRoadmap/Drift/Suggested.lean": None,
                                          "Completed/Drift/README.md": twins["TauCetiRoadmap/Drift/README.md"],
                                          "Completed/Drift/Suggested.lean": "theorem t : True := trivial\n"}),
                         # A new Completed/Drift2 is no move: what it cites is its own.
                         (124, "jo", 0, {"Completed/Drift2/README.md":
                                         "# Roadmap: Drift2\n\n*Needs:* Ghost §1.1.\n"})])
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=build.gh), DEPS)
    # Both Twins are on main, so what they newly name is UPWARD; each is compared with its own README, so
    # #121's Ghost is new although Completed/Twin names Ghost, and #122's Phantom although TauCetiRoadmap/Twin
    # names Phantom. The new Completed/Drift2 waits; the move of Drift brings nothing.
    assert label_writes(build.gh) == [("POST", 124)]
    assert upward(121, "Twin", "Ghost", "#110") in out and upward(122, "Twin", "Phantom", "#111") in out
    assert not any("#121: UPWARD" in x and "Phantom" in x for x in out)
    assert not any("#122: UPWARD" in x and "Ghost" in x for x in out)
    assert not any(line.startswith("#123") for line in out)


def test_a_citation_of_a_revision_of_a_roadmap_main_has_since_moved_waits(tmp_path, monkeypatch):
    moved = {LATTICES: None, LATTICES_LEAN: None, "Completed/Lattices/README.md": MAIN[LATTICES],
             "Completed/Lattices/Suggested.lean": MAIN[LATTICES_LEAN]}
    cfg, index = build(tmp_path, monkeypatch, [MAIN, moved],
                       [(7, "alice", 0, RENUMBER),                        # branched before main moved Lattices
                        (160, "ken", 1, roadmap("Uses", "Uses Lattices §1.4."))])
    # #7 revises Lattices where its head has it; main keeps it in Completed/ now.
    assert [(r.pr["pr"], r.name) for r in overlaps.revisions(cfg.state_dir(), index)] == [(7, "Lattices")]
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=build.gh), DEPS)
    assert f"#160: {DEPS} added (waits for #7 (revision of Lattices))" in out


def test_one_pull_request_changing_two_folders_of_the_same_name_reads_both(tmp_path, monkeypatch):
    twins = {"Completed/Twin/README.md": "# Roadmap: Twin\n\nDone.\n", **roadmap("Twin", "Active.")}
    cfg, index = build(tmp_path, monkeypatch, [{**MAIN, **twins}],
                       [(110, "eve", 0, roadmap("Ghost", "### 1.1 Spirits")),
                        (170, "lea", 0, {"Completed/Twin/README.md": "# Roadmap: Twin\n\nDone, reworded.\n",
                                         **roadmap("Twin", "Active. *Needs:* Ghost §1.1.")})])
    pr = next(p for p in index["prs"] if p["pr"] == 170)
    assert sorted(deplabels.roadmap_folders(cfg.state_dir(), index, pr)) == ["Completed/Twin", "TauCetiRoadmap/Twin"]
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=build.gh), DEPS)
    assert upward(170, "Twin", "Ghost", "#110") in out


def test_a_rename_is_compared_with_its_true_source_not_a_folder_of_the_same_name(tmp_path, monkeypatch):
    body = "".join(f"### 1.{i} Topic {i}\n\nText of topic {i}.\n\n" for i in range(1, 12))
    other = f"# Roadmap: Other\n\n{body}"
    on_main = {**roadmap("Twin", "*Needs:* Ghost §1.1."), "TauCetiRoadmap/Other/README.md": other}
    cfg, index = build(tmp_path, monkeypatch, [{**MAIN, **on_main}],
                       [(110, "eve", 0, roadmap("Ghost", "### 1.1 Spirits")),
                        # A real rename Other -> Completed/Twin that adds a Ghost citation; the listing names
                        # no source, and TauCetiRoadmap/Twin, which cites Ghost already, is untouched.
                        (180, "max", 0, {("TauCetiRoadmap/Other/README.md", "Completed/Twin/README.md"):
                                         other + "*Needs:* Ghost §1.1.\n"}),
                        # A plain move of Other, unchanged: what it cites comes with it.
                        (181, "ned", 0, {("TauCetiRoadmap/Other/README.md", "Completed/Other/README.md"): other})])
    state, pr = cfg.state_dir(), next(p for p in index["prs"] if p["pr"] == 180)
    base = overlaps._merge_base(state, "refs/heads/main", openroadmaps.pr_ref(180))
    assert overlaps.rename_source(state, base, openroadmaps.pr_ref(180), "Completed/Twin", pr["files"],
                                  index["main_folders"], tuple(index["dirs"])) == "TauCetiRoadmap/Other"
    out = deplabels.run(SimpleNamespace(cfg=cfg, gh=build.gh), DEPS)
    # Compared with Other's README, the Ghost citation is new: an UPWARD edge of a roadmap on main.
    assert upward(180, "Twin", "Ghost", "#110") in out and not any("#181" in line for line in out)


THETA_README = "TauCetiRoadmap/ThetaSeries/README.md"
SPHERES = "TauCetiRoadmap/Spheres/README.md"
MERGED_SPHERES = ("# Roadmap: spheres\n\n## Layer 1: groups\n\n### 1.1 The group\n\n### 1.2 Finiteness\n\n"
                  "## Layer 2: surgery\n\n### 2.1 Framed surgery\n")
RIVAL_SPHERES = "# Roadmap: spheres\n\n## Layer 1: groups\n\n### 1.1 The group of spheres\n"


def test_a_pull_request_that_adds_a_roadmap_main_gained_since_is_a_second_copy(tmp_path, monkeypatch):
    later = {SPHERES: MERGED_SPHERES, GROUPS: MAIN[GROUPS] + "Also Spheres §2.1 and Spheres §1.1.\n"}
    stacked = MERGED_SPHERES + "\n### 2.2 The exact sequence\n"
    cfg, index = build(tmp_path, monkeypatch, [MAIN, later],
                       [(31, "ivy", 0, {SPHERES: RIVAL_SPHERES}),       # branched before main had Spheres
                        (32, "jack", 1, {SPHERES: stacked}),            # a revision: its merge base has it
                        (33, "kate", 0, {SPHERES: MERGED_SPHERES}),     # the merged text itself: no rival
                        (34, "liam", 0, {THETA_README: THETA[THETA_README]})])
    state = cfg.state_dir()
    [copy] = overlaps.copies(state, index)
    assert (copy.pr["pr"], copy.name, copy.folder) == (31, "Spheres", "TauCetiRoadmap/Spheres")
    assert [r.pr["pr"] for r in overlaps.revisions(state, index)] == [32]     # the copy is no revision
    assert overlaps.copies(state, index, pr=32) == [] and overlaps.copies(state, index, only="Lattices") == []
    # opened against the branch that merged, the same head lists its README as changed: no copy
    behind = {"prs": [dict(p, files=[dict(f, change="modified") for f in p["files"]]) for p in index["prs"]]}
    assert overlaps.copies(state, {**index, **behind}) == []

    text, facts = overlaps.report(state, index, "Spheres", RIVAL_SPHERES, pr=31)
    assert text.index("## A second copy of a roadmap on main") < text.index("## Other open pull requests")
    assert "#31 (@ivy) adds `TauCetiRoadmap/Spheres`, and main has the roadmap Spheres already" in text
    assert "The README has 11 lines on main and 5 at this head, which has 33% of main" in text
    assert "- Ids main defines that this head lacks and other roadmaps cite (1):" in text
    assert "  - Spheres §2.1: cited by Groups (main line 4)" in text and "Spheres §1.1:" not in text
    assert facts >= 2                                                       # the copy, and #32 and #33 beside it
    assert "A second copy" not in overlaps.report(state, index, "Spheres", stacked, pr=32)[0]
    assert "A second copy" not in overlaps.report(state, index, "ThetaSeries", THETA[THETA_README], pr=34)[0]
    assert "- #31 (@ivy) adds `TauCetiRoadmap/Spheres`: main has Spheres since" in overlaps.report_all(state, index)


def test_main_neighbours_and_subject_neighbours():
    filler = [("main", f"M{i}", f"# Roadmap {i}\n\n## Layer {i}: words{i} common{i}\n\nGauss{i} text") for i in range(12)]
    texts = filler + [("#5", "Theta", "theta modular weight lattice poisson summation"),
                      ("main", "Forms", "theta modular weight forms"),
                      ("main", "Mine", "theta modular lattice poisson")]
    near = overlaps.neighbours(texts, "theta modular lattice poisson", own="Mine", on_main=True)
    assert [(where, name) for _s, where, name in near] == [("main", "Forms")]        # only main, never itself

    plan = ("# Roadmap: effective modularity\n\n## Layer 1: Faltings heights\n\nThe theorems of Faltings and "
            "Masser-Wüstholz bound isogenies.\n\n## Layer 2: effective Shafarevich\n\nAfter Faltings.\n")
    twin = ("# Roadmap: comparisons\n\n## Supplier contracts\n\nKolyvagin systems from elsewhere.\n\n"
            "## Layer 1: Faltings heights\n\nFaltings and Masser-Wüstholz again.\n\n## Layer 3: effective Shafarevich\n")
    other = "# Roadmap: descent\n\n## Layer 1: Selmer groups\n\nKolyvagin systems bound Selmer groups.\n"
    texts = filler + [("#7", "Comparisons", twin), ("main", "Comparisons", twin), ("#8", "Descent", other),
                      ("main", "Descent2", other), ("#9", "Effective", plan)]
    near = overlaps.subject_neighbours(texts, plan, own="Effective", own_pr=9)
    assert [(where, name) for _s, where, name in near] == [("main", "Comparisons")]    # once, at main
    assert near[0][0] > 1.0
    assert overlaps.subject_neighbours(texts, plan, own="Effective", own_pr=7)[0][1] == "main"
    assert overlaps.subject_neighbours(texts, other, own="Descent")[0][2] == "Descent2"
    assert "Kolyvagin" not in overlaps._subject(twin)[0]                                # the supplier section is aside


def test_an_acceptance_owes_the_overlap_line_only_when_its_brief_asked_for_it(world, tmp_path):
    cfg, _index = world
    lane, _job, ws, _ctx = lane_workspace(cfg, tmp_path, "REV-PKG-ThetaSeries~2", "# Roadmap: theta series\n")
    report = "# Review of ThetaSeries\n\nNothing shared.\n"
    assert lane.overlap_line_problem(ws, report) is None                    # no overlaps file this round
    ws.extra["overlaps"] = (ws.scratch / "overlaps.md", 0)
    (ws.job_dir / "brief.md").write_text("an older brief")
    assert lane.overlap_line_problem(ws, report) is None                    # briefed before the line existed
    (ws.job_dir / "brief-repair.md").write_text("Close the audit with one line, `Overlap check:`, naming ...")
    assert "no `Overlap check:` line" in lane.overlap_line_problem(ws, report)
    for line in ("Overlap check: read Lattices, Groups; nothing shared", "**Overlap check:** read Lattices",
                 "- Overlap check: Lattices owns the theta function (Lattices §1.3), cited here"):
        assert lane.overlap_line_problem(ws, report + "\n" + line + "\n") is None
    assert lane.overlap_line_problem(ws, report + "\nThe overlap check: was not done.\n")


def test_a_briefed_report_owes_the_source_line_and_the_section_for_the_human_reviewers(world, tmp_path):
    cfg, _index = world
    lane, _job, ws, _ctx = lane_workspace(cfg, tmp_path, "REV-PKG-ThetaSeries~2", "# Roadmap: theta series\n")
    report = "# Review of ThetaSeries\n\n**Verdict:** accepted\n\n## Verified sound\n\n- 3 locators\n"
    (ws.job_dir / "brief.md").write_text("an older brief")
    assert lane.provenance_problems(ws, report, line=True) == []            # briefed before the check existed
    (ws.job_dir / "brief.md").write_text("Close with the line `Source independence:`; an acceptance ...")
    owed = lane.provenance_problems(ws, report, line=True)
    assert len(owed) == 2 and "no `Source independence:` line" in owed[0] and "For the human reviewers" in owed[1]
    assert len(lane.provenance_problems(ws, report, line=False)) == 1       # a review that asks for changes: the section
    done = (report + "\n**Source independence:** 9 passages compared with 4 sources, none shared; largest share "
            "`Serre`, 12 of 40 locators; contents compared with `Serre`: independent\n\n## For the human reviewers\n\n")
    assert lane.provenance_problems(ws, done, line=True) == ["report: no `## For the human reviewers` section with its "
                                                             "bullets: one for each question a human decides (the "
                                                             "scope, a licence), or `- none`"]
    assert lane.provenance_problems(ws, done + "- none\n", line=True) == []
    assert lane.provenance_problems(ws, done + "- **Scope.** Every locator cites `Notes`, unpublished.\n", line=True) == []


def test_the_flags_for_the_human_reviewers_are_read_and_announced_under_the_verdict():
    assert prreviews.human_flags("# Review\n\n## Verified sound\n\n- none\n") is None
    assert prreviews.human_flags("## For the human reviewers\n\nNothing to add.\n") is None
    assert prreviews.human_flags("## For the human reviewers\n\n- none\n") == []
    assert prreviews.human_flags("## For the human reviewers\n- None.\n\n## Section passes\n\n- §1: 0 finding(s)\n") == []
    report = ("# Review of X\n\n**Verdict:** accepted\n\n## Verified sound\n\n- 3 locators\n\n## For the human reviewers\n\n"
              "- **Scope.** Every locator cites `Notes`, unpublished notes;\n  no refereed source found.\n"
              "- **Licence.** `Suggested.lean` line 40 agrees with a public formalisation.\n\n## Section passes\n\n- §1\n")
    flags = prreviews.human_flags(report)
    assert len(flags) == 2 and flags[0].endswith("no refereed source found.")
    body = prreviews.render("Review (claude) of abc1234", "accepted", report, {"job": "REV-PKG-X~1"})
    assert "**For the human reviewers:** this review raises 2 questions that a human decides" in body
    assert body.index("**For the human reviewers:**") < body.index("## Verified sound") < body.index("## For the human reviewers")
    assert prreviews.parse(body).verdict == "accepted"
    one = prreviews.render("Review (claude) of abc1234", "accepted", report.replace("- **Licence.**", "  and"), {})
    assert "raises 1 question that a human decides" in one and "It is in the section" in one
    quiet = prreviews.render("Review (claude) of abc1234", "accepted", "## For the human reviewers\n\n- none\n", {})
    assert "**For the human reviewers:**" not in quiet
