"""The open pull requests in the "Merge order" paragraph are read from the content repository: ours
(the port lane's, and those the project's operators opened for a roadmap in the merge order) are
the chain's links; other authors' that add a roadmap the README cites get their clause (port of
ext_deps.py), so "It depends on no other author's open pull request." is said only after checking.
The port lane writes its paragraph with the renderer `merge-order` uses."""

import dataclasses
import json
import subprocess
from types import SimpleNamespace

from tauceti_roadmap_worker import depgraph, mergeorder
from tauceti_roadmap_worker.claims import Claims, author_key, job_key
from tauceti_roadmap_worker.depgraph import MARK_OPEN
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes.port import PortLane

from .fakes import example_config
from .test_checkout import needs_git

REPO = "TauCetiProject/TauCetiRoadmap"


class Gh:
    """Open pull requests with their files and READMEs, and the content repository's main folders."""

    def __init__(self, pulls: list[dict], files: dict[int, list[str]], main: list[str], fail_files: bool = False,
                 readmes: dict[str, str] | None = None):
        self.pulls, self.files, self.main, self.fail_files = pulls, files, main, fail_files
        self.readmes = readmes or {}
        self.calls: list[str] = []
        self.writes: list[tuple[str, str, dict]] = []

    def api(self, path: str, paginate: bool = False):
        self.calls.append(path)
        if path.startswith(f"repos/{REPO}/contents/TauCetiRoadmap"):
            return [{"name": n, "type": "dir"} for n in self.main] + [{"name": "README.md", "type": "file"}]
        if path.startswith(f"repos/{REPO}/contents/Completed"):
            raise GHError("gh: Not Found (HTTP 404)")
        if path.startswith(f"repos/{REPO}/pulls?"):
            return self.pulls
        if "/files" in path:
            if self.fail_files:
                raise GHError("gh: rate limited")
            number = int(path.split("/pulls/")[1].split("/")[0])
            return [{"filename": f} for f in self.files.get(number, [])]
        raise GHError(f"GET {path}: not faked")

    def raw_file(self, repo, path, ref=""):
        self.calls.append(f"raw {path}@{ref}")
        return self.readmes[path.split("/")[1]]

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        return {}


def pull(number, login, head="h1", draft=False):
    return {"number": number, "user": {"login": login}, "head": {"sha": head}, "draft": draft}


def ctx_for(tmp_path, gh, operators=()):
    cfg = example_config()
    cfg.repositories.claims = ""                  # the claims namespace is never read in these tests
    cfg.board.operators = list(operators)
    return SimpleNamespace(cfg=cfg, gh=gh, state=tmp_path, operator="worker")


PULLS = [pull(900, "worker"), pull(248, "roed-math"), pull(250, "someone", draft=True), pull(251, "other"),
         pull(252, "progress"), pull(253, "dropped")]
FILES = {248: ["TauCetiRoadmap/LFunctions/README.md", "TauCetiRoadmap/LFunctions/Suggested.lean"],
         250: ["TauCetiRoadmap/LFunctions/README.md"],                      # a draft adding the same folder
         251: ["TauCetiRoadmap/Modular/README.md", "TauCetiRoadmap/Modular/sub/README.md"],
         252: ["TauCetiRoadmap/OnMain/README.md"],                          # a progress pull request on a merged roadmap
         253: ["TauCetiRoadmap/Dropped/README.md"]}


def test_other_authors_pull_requests_that_add_a_roadmap(tmp_path):
    (tmp_path / "dropped-external.txt").write_text("#253\n")
    gh = Gh(PULLS, FILES, main=["OnMain"])
    found = mergeorder.survey(ctx_for(tmp_path, gh), ours={900}, order=[])
    assert found.others == {"LFunctions": (248, "roed-math"), "Modular": (251, "other")} and found.members == []
    assert not any("/pulls/900/" in c for c in gh.calls)                     # ours are never read
    # The files are cached by head: a second read fetches only a pull request whose head moved.
    gh.calls.clear()
    gh.pulls = [pull(248, "roed-math", head="h2"), *PULLS[2:]]
    mergeorder.survey(ctx_for(tmp_path, gh), ours={900}, order=[])
    assert [c for c in gh.calls if "/files" in c] == [f"repos/{REPO}/pulls/248/files?per_page=100"]
    assert json.loads((tmp_path / "other-prs.json").read_text())["248"]["head"] == "h2"


def test_an_unreadable_scan_writes_no_paragraph(tmp_path):
    ctx = ctx_for(tmp_path, Gh(PULLS, FILES, main=[], fail_files=True))
    assert mergeorder.survey(ctx, ours={900}, order=[]) is None
    assert mergeorder.paragraphs(ctx, [{"pr": 900, "name": "Mine"}], {"Mine": ""}, ["Mine"]) is None
    assert mergeorder.facts_for(ctx, "Mine", "", [], ["Mine"]) is None
    assert PortLane().write_merge_order(ctx, 900, "Mine", "Body " * 20, None, ["Mine"]).startswith(
        " (the open pull requests could not be read")
    assert ctx.gh.writes == []


def test_externals_are_the_cited_ones(tmp_path):
    others = {"LFunctions": (248, "roed-math"), "Modular": (251, "other")}
    prs = [{"pr": 900, "name": "Mine"}, {"pr": 901, "name": "Unread"}]
    readme = "*Needs:* LFunctions §2 and OnMain §1. Consumed by Modular.\n"
    assert mergeorder.externals(tmp_path, prs, {"Mine": readme}, others) == {"900": {"LFunctions": [248, "roed-math"]}}
    (tmp_path / "external-deps.json").write_text(json.dumps({"901": {"name": "Unread", "ext": {"Modular": [251, "other"]}}}))
    assert mergeorder.externals(tmp_path, prs, {}, others) == {"901": {"Modular": [251, "other"]}}   # ext_deps.py's shape


ORDER = ["SchemeAndStackFoundations", "ReductiveGroupsPartII", "SmoothRepresentationsOfLocalGroups", "Mine"]
EXPLORER = [pull(779, "CBirkbeck"), pull(785, "CBirkbeck"), pull(934, "WilliamCoram"), pull(990, "CBirkbeck")]
EXPLORER_FILES = {779: ["TauCetiRoadmap/SchemeAndStackFoundations/README.md"],
                  785: ["TauCetiRoadmap/ReductiveGroupsPartII/README.md",
                        "TauCetiRoadmap/SmoothRepresentationsOfLocalGroups/README.md"],
                  934: ["TauCetiRoadmap/QuaternionicModularForms/README.md"],
                  990: ["TauCetiRoadmap/NotInOrder/README.md"]}
EXPLORER_READMES = {"SchemeAndStackFoundations": "", "ReductiveGroupsPartII": "*Needs:* SchemeAndStackFoundations §2.\n",
                    "SmoothRepresentationsOfLocalGroups": "*Needs:* ReductiveGroupsPartII §1.\n"}


def test_pull_requests_an_operator_opened_for_a_roadmap_in_the_order_are_ours(tmp_path):
    gh = Gh(EXPLORER, EXPLORER_FILES, main=[], readmes=EXPLORER_READMES)
    ctx = ctx_for(tmp_path, gh, operators=["CBirkbeck"])
    found = mergeorder.survey(ctx, ours={1336}, order=ORDER)
    assert [(m["pr"], m["name"], m["names"]) for m in found.members] == [
        (779, "SchemeAndStackFoundations", ["SchemeAndStackFoundations"]),
        (785, "ReductiveGroupsPartII", ["ReductiveGroupsPartII", "SmoothRepresentationsOfLocalGroups"])]
    # Another login's pull request, and an operator's for a roadmap the order does not name, are other authors'.
    assert found.others == {"QuaternionicModularForms": (934, "WilliamCoram"), "NotInOrder": (990, "CBirkbeck")}
    readme = "*Needs:* SmoothRepresentationsOfLocalGroups §3 and QuaternionicModularForms §1.\n"
    para = mergeorder.paragraphs(ctx, [{"pr": 1336, "name": "Mine"}], {"Mine": readme}, ORDER)[1336]
    # The chain runs through #785's README to #779; #785 appears once, under its first roadmap in the order.
    assert ("**Merge order.** #779 (SchemeAndStackFoundations) → #785 (ReductiveGroupsPartII) → **this pull request** "
            "(#1336). It also depends on other authors' pull requests that add roadmaps it cites, which must merge "
            "first: #934 (QuaternionicModularForms, WilliamCoram).") in para
    # The default operator is the running login only: the same pull requests are then other authors'.
    alone = mergeorder.survey(ctx_for(tmp_path, gh), ours={1336}, order=ORDER)
    assert alone.members == [] and alone.others["ReductiveGroupsPartII"] == (785, "CBirkbeck")
    assert sum(c.startswith("raw ") for c in gh.calls) == 3                 # READMEs read once, cached by head


def test_plan_skips_what_it_cannot_state():
    prs = [{"pr": 901, "name": "Alpha"}, {"pr": 902, "name": "Bravo"}, {"pr": 903, "name": "Zulu"}]
    paras = mergeorder.plan(prs, {"Alpha": "", "Zulu": ""}, ["Alpha", "Bravo"], set(), {}, {"901": {}}, set())
    assert set(paras) == {901}                    # Bravo's README unread, Zulu not in the order
    assert "It depends on no other author's open pull request." in paras[901]


def test_the_port_lane_writes_its_paragraph_with_the_shared_renderer(tmp_path):
    gh = Gh(PULLS, FILES, main=["OnMain"])
    ctx = ctx_for(tmp_path, gh)
    gh.calls.clear()
    body = "Synopsis " * 20 + "\n\n<!-- tauceti-roadmap-pr {} -->\n"
    readme = "*Needs:* Alpha §1 and LFunctions §2.\n"
    (tmp_path / "rev" / "901" / "TauCetiRoadmap" / "Alpha").mkdir(parents=True)
    (tmp_path / "rev" / "901" / "TauCetiRoadmap" / "Alpha" / "README.md").write_text("")
    prs = [{"pr": 901, "name": "Alpha"}]
    found = mergeorder.facts_for(ctx, "Mine", readme, prs, ["Alpha", "Mine"])    # before the number exists
    assert found.chain == [(901, "Alpha")]
    assert "In open pull requests of ours: #901 (Alpha)." in depgraph.dependency_paragraph(found.chain)
    note = PortLane().write_merge_order(ctx, 905, "Mine", body, found, ["Alpha", "Mine"])
    assert note == ""
    [(method, path, payload)] = gh.writes
    assert (method, path) == ("PATCH", f"repos/{REPO}/pulls/905") and payload["body"].startswith(body.rstrip())
    expected = mergeorder.plan(prs + [{"pr": 905, "name": "Mine"}], {"Alpha": "", "Mine": readme}, ["Alpha", "Mine"],
                               set(), {}, {"905": {"LFunctions": [248, "roed-math"]}}, set())[905]
    assert payload["body"].endswith(expected + "\n") and MARK_OPEN in expected
    assert "#901 (Alpha) → **this pull request** (#905). It also depends on other authors' pull requests that add " \
           "roadmaps it cites, which must merge first: #248 (LFunctions, roed-math)." in expected
    assert "not in the merge order" in PortLane().write_merge_order(ctx, 906, "Nope", body, found, ["Alpha"])


@dataclasses.dataclass
class LocalClaims(Claims):
    """A claims namespace whose origin is a local bare repository (no network)."""
    remote: str = ""

    def ensure_repo(self):
        if not self.gitdir.is_dir():
            subprocess.run(["git", "init", "-q", "--bare", str(self.gitdir)], check=True)
        self._git("remote", "remove", "origin")
        self._git("remote", "add", "origin", self.remote)


@needs_git
def test_the_claims_namespace_names_the_operators(tmp_path):
    remote = tmp_path / "claims.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    for login, key in (("alice", job_key("P", "RT-Foo~1a")), ("CBirkbeck", author_key("P", "PKG-Foo")),
                       ("carol", job_key("Other", "PKG-Bar"))):
        project = key.split("/")[1]
        writer = LocalClaims(repo="o/c", gitdir=tmp_path / f"{login}.git", owner=f"{login}/w1", operator=login,
                             project=project, remote=str(remote))
        assert writer.acquire(key) == 0
    reader = LocalClaims(repo="o/c", gitdir=tmp_path / "reader.git", owner="r", operator="", project="P",
                         remote=str(remote))
    assert reader.operators() == {"alice", "CBirkbeck"}                   # this project's records only
