"""Overlaps between open roadmap pull requests, against local repositories standing in for the content
repository: a revision of a roadmap on main that renumbers a section and renames a heading other roadmaps
cite, a second revision of the same roadmap that conflicts with it, a new roadmap citing both ids, and the
cases that must not count as a removal (a head that fell behind main) or must (a deleted README)."""

import re
import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import cli, config, depgraph, deplabels, gitops, mergeorder, openroadmaps, overlaps
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
                              {"__typename": "PullRequest", "state": got["state"].upper(), "body": got.get("body", "")})
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
        if path.startswith("search/issues"):
            return {"items": [{"number": n} for n in self.merged_labelled]}
        raise GHError(f"GET {path}: HTTP 404: Not Found")

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))


def build(tmp_path, monkeypatch, mains, prs, labels=None, bodies=None):
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
    block = ("Text.\n\n<!-- depends-on -->\n**Depends on** (suppliers still open; they merge first):\n"
             "- [ ] #1126 AnalyticNumberTheoryPartI\n- [x] #1408 ComputationalNumberTheory\n"
             "- [ ] PlanarTopology (no pull request yet)\n<!-- /depends-on -->\nDepends on #44.")
    assert overlaps.declared_items(block, own=9) == [(None, "PlanarTopology"), (44, None),
                                                     (1126, "AnalyticNumberTheoryPartI")]
    # Not dependencies: independence, negation, the other direction, pull requests that come after.
    for text in ("This PR is independent of #17.", "Does not depend on #20.", "#22 depends on this PR.",
                 "#1411 consumes its §4a.1, so the order is this PR (#1410) → #1411; #940 and #856 cite its "
                 "§3a.1 and merge after it."):
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
    assert record == {"suppliers": [{"roadmap": "CStar", "prs": [30]}, {"roadmap": "Ghost", "prs": []}],
                      "revisions": [{"roadmap": "Lattices", "pr": 7}], "declared": [44]}
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
    assert record == {"suppliers": [{"roadmap": "PlanarTopology", "prs": [60]}], "revisions": [], "declared": [61]}
    # Lattices is on main and Helper rides in the same pull request: neither is a dependency.


DEPS = "awaiting-dependencies"


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
