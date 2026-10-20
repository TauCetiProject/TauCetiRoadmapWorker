"""Other authors' pull requests in the "Merge order" paragraph are read from the content repository
(port of ext_deps.py), so "It depends on no other author's open pull request." is said only after
checking; the port lane writes its paragraph with the renderer `merge-order` uses."""

import json
from types import SimpleNamespace

from tauceti_roadmap_worker import mergeorder
from tauceti_roadmap_worker.depgraph import MARK_OPEN
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes.port import PortLane

from .fakes import example_config

REPO = "TauCetiProject/TauCetiRoadmap"


class Gh:
    """Open pull requests with their files, and the content repository's main folders."""

    def __init__(self, pulls: list[dict], files: dict[int, list[str]], main: list[str], fail_files: bool = False):
        self.pulls, self.files, self.main, self.fail_files = pulls, files, main, fail_files
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

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        return {}


def pull(number, login, head="h1", draft=False):
    return {"number": number, "user": {"login": login}, "head": {"sha": head}, "draft": draft}


def ctx_for(tmp_path, gh):
    return SimpleNamespace(cfg=example_config(), gh=gh, state=tmp_path)


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
    others = mergeorder.other_roadmap_prs(ctx_for(tmp_path, gh), ours={900})
    assert others == {"LFunctions": (248, "roed-math"), "Modular": (251, "other")}
    assert not any("/pulls/900/" in c for c in gh.calls)                     # ours are never read
    # The files are cached by head: a second read fetches only a pull request whose head moved.
    gh.calls.clear()
    gh.pulls = [pull(248, "roed-math", head="h2"), *PULLS[2:]]
    mergeorder.other_roadmap_prs(ctx_for(tmp_path, gh), ours={900})
    assert [c for c in gh.calls if "/files" in c] == [f"repos/{REPO}/pulls/248/files?per_page=100"]
    assert json.loads((tmp_path / "other-prs.json").read_text())["248"]["head"] == "h2"


def test_an_unreadable_scan_is_unknown_not_none(tmp_path):
    ctx = ctx_for(tmp_path, Gh(PULLS, FILES, main=[], fail_files=True))
    assert mergeorder.other_roadmap_prs(ctx, ours={900}) is None
    prs = [{"pr": 900, "name": "Mine"}]
    assert mergeorder.externals(ctx, prs, {"Mine": "*Needs:* LFunctions §2.\n"}) == {}
    (tmp_path / "external-deps.json").write_text(json.dumps({"900": {"name": "Mine", "ext": {"Modular": [251, "other"]}}}))
    assert mergeorder.externals(ctx, prs, {"Mine": ""}) == {"900": {"Modular": [251, "other"]}}   # ext_deps.py's shape


def test_externals_are_the_cited_ones(tmp_path):
    ctx = ctx_for(tmp_path, Gh(PULLS, FILES, main=["OnMain"]))
    prs = [{"pr": 900, "name": "Mine"}, {"pr": 901, "name": "Unread"}]
    readme = "*Needs:* LFunctions §2 and OnMain §1. Consumed by Modular.\n"
    assert mergeorder.externals(ctx, prs, {"Mine": readme}) == {"900": {"LFunctions": [248, "roed-math"]}}


def test_plan_skips_what_it_cannot_state():
    prs = [{"pr": 901, "name": "Alpha"}, {"pr": 902, "name": "Bravo"}, {"pr": 903, "name": "Zulu"}]
    paras = mergeorder.plan(prs, {"Alpha": "", "Zulu": ""}, ["Alpha", "Bravo"], set(), {}, {"901": {}}, set())
    assert set(paras) == {901}                    # Bravo's README unread, Zulu not in the order
    assert "It depends on no other author's open pull request." in paras[901]


def test_the_port_lane_writes_its_paragraph_with_the_shared_renderer(tmp_path):
    gh = Gh(PULLS, FILES, main=["OnMain"])
    ctx = ctx_for(tmp_path, gh)
    body = "Synopsis " * 20 + "\n\n<!-- tauceti-roadmap-pr {} -->\n"
    readme = "*Needs:* Alpha §1 and LFunctions §2.\n"
    (tmp_path / "rev" / "901" / "TauCetiRoadmap" / "Alpha").mkdir(parents=True)
    (tmp_path / "rev" / "901" / "TauCetiRoadmap" / "Alpha" / "README.md").write_text("")
    prs = [{"pr": 901, "name": "Alpha"}]
    note = PortLane().write_merge_order(ctx, {"pr": 905, "name": "Mine"}, readme, body, prs, ["Alpha", "Mine"])
    assert note == ""
    [(method, path, payload)] = gh.writes
    assert (method, path) == ("PATCH", f"repos/{REPO}/pulls/905") and payload["body"].startswith(body.rstrip())
    expected = mergeorder.plan(prs + [{"pr": 905, "name": "Mine"}], {"Alpha": "", "Mine": readme}, ["Alpha", "Mine"],
                               set(), {}, {"905": {"LFunctions": [248, "roed-math"]}}, set())[905]
    assert payload["body"].endswith(expected + "\n") and MARK_OPEN in expected
    assert "#901 (Alpha) → **this pull request** (#905). It also depends on other authors' pull requests that add " \
           "roadmaps it cites, which must merge first: #248 (LFunctions, roed-math)." in expected
    assert "not in the merge order" in PortLane().write_merge_order(ctx, {"pr": 906, "name": "Nope"}, readme, body,
                                                                    prs, ["Alpha"])
