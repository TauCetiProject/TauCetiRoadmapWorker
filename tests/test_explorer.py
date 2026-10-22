"""`board import-explorer` against a small explorer checkout, with git and GitHub faked."""

import base64
import json
import tomllib
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import depgraph, explorer, gitops, intake, sources
from tauceti_roadmap_worker.board import encode_spec, new_job
from tauceti_roadmap_worker.gh import GHError

from .fakes import FakeBoard, example_config


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def package(bp, name, status=None, reviewer=""):
    for f, text in (("README.md", f"# {name}\n"), ("Suggested.lean", "import Mathlib\n"), ("metadata.toml", 'topic = "x"\n')):
        write(bp / "packages" / name / f, text)
    if status:
        write(bp / "packages" / name / "review.json", json.dumps({"review": {"status": status, "reviewer": reviewer}}))


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "explorer"
    bp = root / "research" / "blueprint"
    jobs = [
        {"id": "PKG-Alpha", "kind": "package", "roadmapIds": ["Alpha"], "state": "done", "finishedAt": "2026-10-01"},
        {"id": "REV-PKG-Alpha", "kind": "review", "roadmapIds": ["Alpha"], "state": "done", "finishedAt": "2026-10-02"},
        {"id": "FIX-PKG-Beta~adv", "kind": "fix", "roadmapIds": ["Beta"], "state": "done",
         "note": "deliverables arrived from an external worker on issue #77"},
        {"id": "DESIGN-GAM", "kind": "design", "roadmapIds": ["Gamma"], "state": "done", "finishedAt": "2026-10-01"},
        {"id": "REV-DESIGN-GAM", "kind": "review", "roadmapIds": ["Gamma"], "state": "done", "finishedAt": "2026-10-03"},
        {"id": "DESIGN-Delta", "kind": "design", "roadmapIds": ["Delta"], "state": "done", "finishedAt": "2026-10-05"},
        {"id": "REV-DESIGN-Delta", "kind": "review", "roadmapIds": ["Delta"], "state": "pending"},
        {"id": "DESIGN-Eps", "kind": "design", "roadmapIds": ["Eps"], "state": "pending", "name": "Eps: the topic",
         "scope": "the general statement"},
        {"id": "PKG-Zeta~2", "kind": "package", "roadmapIds": ["Zeta"], "state": "external",
         "note": "claimed on GitHub issue #99"},
        {"id": "PKG-Theta", "kind": "package", "roadmapIds": ["Theta"], "state": "done"},
        {"id": "PKG-Iota", "kind": "package", "roadmapIds": ["Iota"], "state": "done"},
    ]
    write(bp / "queue.json", json.dumps({"purpose": "test", "jobs": jobs}))
    write(bp / "issues.json", json.dumps({"REV-PKG-Alpha": 41, "REV-DESIGN-GAM": 42, "DESIGN-Delta": 43, "DESIGN-Eps": 44}))
    package(bp, "Alpha", "accepted", "independent-review-REV-PKG-Alpha")
    package(bp, "Beta", "accepted", "fix-FIX-PKG-Beta~adv")
    package(bp, "Zeta", "accepted", "independent-review-REV-PKG-Zeta")
    package(bp, "Eta", "needs_changes", "independent-review-REV-PKG-Eta")
    package(bp, "Theta", "accepted", "independent-review-REV-PKG-Theta")
    package(bp, "Iota")
    for name in ("Gamma", "Delta"):
        write(bp / "readmes" / f"{name}.md", f"# {name}: design\n")
        write(bp / "packets" / f"{name}.json", "{}")
    write(bp / "reviews" / "REV-DESIGN-GAM.md", "# Independent review: Gamma\n\n**Accepted as a plan.** Reviewer: x.\n")
    return root


def plan(checkout, **kw):
    board_jobs = kw.pop("jobs", [new_job("TARGET-Theta")])
    return {s.name: s for s in explorer.plan_import(explorer.Explorer.read(checkout), "packages", board_jobs,
                                                     kw.pop("pending", {"Iota": 12}), **kw)}


def test_each_explorer_stage_becomes_the_matching_board_job(checkout):
    steps = plan(checkout)
    assert {n: s.job.id for n, s in steps.items() if not s.skip} == {
        "Alpha": "RT-Alpha~1a", "Beta": "REV-PKG-Beta", "Gamma": "PKG-Gamma", "Delta": "REV-DESIGN-Delta", "Eps": "TARGET-Eps"}
    assert [(j.id, j.requires_agent) for j in steps["Alpha"].extra] == [("RT-Alpha~1b", "claude")]   # red team, not port
    assert sorted(steps["Alpha"].files) == ["packages/Alpha/README.md", "packages/Alpha/Suggested.lean",
                                            "packages/Alpha/metadata.toml", "packages/Alpha/review.json"]
    assert "packages/Beta/review.json" not in steps["Beta"].files          # a fixer's acceptance is not carried
    assert sorted(steps["Gamma"].files) == ["packages/Gamma/design/README.md", "packages/Gamma/design/plan.json"]
    assert steps["Eps"].files == {} and steps["Eps"].job.inputs["form"]["statement"].startswith("Eps: the topic")
    assert steps["Alpha"].job.inputs["explorer"] == {"repo": explorer.EXPLORER, "job": "REV-PKG-Alpha", "issue": 41}
    assert (steps["Beta"].issue, steps["Gamma"].issue, steps["Delta"].issue) == (77, 42, 43)
    assert steps["Zeta"].skip == "in flight in the explorer: PKG-Zeta~2 is external" and steps["Zeta"].issue == 99
    assert steps["Eta"].skip.startswith("the explorer's package review says needs_changes")
    assert steps["Theta"].skip == "already on the board: TARGET-Theta"
    assert steps["Iota"].skip == "waiting in import pull request #12"
    table = explorer.summary(list(steps.values()))
    assert table.splitlines()[0].split()[:3] == ["Roadmap", "Stage", "Explorer"]
    assert "RT-Alpha~1a" in table and "in flight in the explorer" in table and table.endswith("target: 1")


def test_a_ported_roadmap_is_skipped_and_the_lookup_reads_the_content_repository_once(checkout, tmp_path):
    steps = plan(checkout, ported={"Alpha": "open pull request #905 on o/r", "Gamma": "on o/r main (x/Gamma)"})
    assert steps["Alpha"].skip == "ported: open pull request #905 on o/r" and steps["Gamma"].skip.startswith("ported: on")

    calls = []

    class ContentGH:
        def api(self, path, paginate=False):
            calls.append(path)
            if "/contents/TauCetiRoadmap?" in path:
                return [{"type": "dir", "name": "Alpha"}, {"type": "file", "name": "README.md"}]
            if "/contents/Completed?" in path:
                return [{"type": "dir", "name": "Old"}]
            if path.endswith("pulls?state=open&per_page=100"):
                return [{"number": 905}, {"number": 906}]
            if "/pulls/905/files" in path:
                return [{"filename": "TauCetiRoadmap/Beta/README.md", "status": "added"},
                        {"filename": "TauCetiRoadmap/Beta/Suggested.lean", "status": "added"}]
            if "/pulls/906/files" in path:
                return [{"filename": "TauCetiRoadmap/Zeta/notes.md", "status": "added"}]
            raise AssertionError(path)
    found = explorer.ported_roadmaps(SimpleNamespace(cfg=example_config(), gh=ContentGH()))
    repo = example_config().repositories.content
    assert found == {"Alpha": f"on {repo} main (TauCetiRoadmap/Alpha)", "Old": f"on {repo} main (Completed/Old)",
                     "Beta": f"open pull request #905 on {repo}"}
    assert len(calls) == 5


def test_kinds_and_names_filter(checkout):
    assert sorted(plan(checkout, kinds=("design",))) == ["Delta", "Gamma"]
    named = plan(checkout, names=("Alpha", "Nope"))
    assert named["Alpha"].job.id == "RT-Alpha~1a" and named["Nope"].skip == "the explorer has nothing for it"
    rerun = plan(checkout, jobs=[new_job("PORT-Alpha")], names=("Alpha",))
    assert rerun["Alpha"].skip == "already on the board: PORT-Alpha"


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gitops, "sync_checkout", lambda repo, dest, ref="main", scrub=(): dest.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(gitops, "git", lambda cwd, *a, **k: calls.append(a) or "")
    monkeypatch.setattr(gitops, "commit_paths", lambda wt, paths, msg, login: calls.append(("commit", *paths)))
    monkeypatch.setattr(gitops, "ensure_fork", lambda gh, repo, login: f"{login}/jobs")
    monkeypatch.setattr(gitops, "push", lambda wt, fork, branch: calls.append(("push", fork, branch)))
    opened = []
    monkeypatch.setattr(gitops, "open_pull_request",
                        lambda gh, repo, head, base, title, body, draft: opened.append((head, title, body)) or {"number": 61})
    return calls, opened


ORDER = "merge-order/ORDER.txt"


class GH:
    """The packages and roadmap repositories: open pull requests, folder listings (empty), and the
    board's files read by the contents API (the merge order; a missing file is a 404), writes kept."""

    def __init__(self, pulls=(), files=None):
        self.pulls, self.files, self.writes = list(pulls), dict(files or {}), []

    def api(self, path, paginate=False):
        if "/contents/" in path and "?" not in path:
            name = path.split("/contents/", 1)[1]
            if name not in self.files:
                raise GHError(f"GET {path}: 404")
            return {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": "blob-1"}
        return [] if "/contents/" in path or "/files" in path or "TauCetiRoadmap/pulls" in path else self.pulls

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        if method == "PUT" and "/contents/" in path:
            self.files[path.split("/contents/", 1)[1]] = base64.b64decode(payload["content"]).decode()


def bot(tmp_path, board=None, gh=None):
    return SimpleNamespace(cfg=example_config(), gh=gh or GH(), board=board or FakeBoard(), operator="maintainer",
                           state=tmp_path / "state", dry_run=False)


def test_the_import_opens_one_pull_request_per_roadmap_and_creates_targets(checkout, tmp_path, fake_git):
    _, opened = fake_git
    ctx = bot(tmp_path)
    dry = explorer.import_explorer(ctx, checkout, names=("Alpha", "Eps"))
    assert [s.outcome for s in dry] == ["would open an import pull request (dry run)", "would create the job (dry run)"]
    assert opened == [] and ctx.board.created == []
    steps = explorer.import_explorer(ctx, checkout, names=("Alpha", "Eps"), yes=True)
    assert [s.outcome for s in steps] == ["pull request #61; Alpha inserted into the merge order", "created #501"]
    assert depgraph.read_order(ctx.gh.files[ORDER]) == ["Alpha"]          # a target enters it only with its design
    [(head, title, body)] = opened
    assert head == "maintainer:import/explorer-alpha" and "RT-Alpha~1a" in title
    found = explorer.decode_import(body)
    assert [j.id for j in found["jobs"]] == ["RT-Alpha~1a", "RT-Alpha~1b"] and len(found["files"]) == 4
    assert (tmp_path / "state" / "import" / "board" / "packages" / "Alpha" / "review.json").is_file()
    [(job, state, preamble)] = ctx.board.created
    assert job.id == "TARGET-Eps" and state == "available" and "issues/44" in preamble
    # The open pull request now holds Alpha back from a second import.
    again = explorer.import_explorer(bot(tmp_path, gh=GH([{"number": 61, "body": body}])), checkout, names=("Alpha",))
    assert again[0].outcome == "skipped: waiting in import pull request #61"


def test_explorer_files_pass_the_submission_gates_before_they_leave(checkout, tmp_path, fake_git):
    write(checkout / "research" / "blueprint" / "packages" / "Alpha" / "README.md", "see /home/someone/notes.md\n")
    [step] = explorer.import_explorer(bot(tmp_path), checkout, names=("Alpha",), yes=True)
    assert step.outcome.startswith("refused by the submission gates: local path in packages/Alpha/README.md")
    assert fake_git[1] == []


class IntakeGH:
    def __init__(self, body, files, association="MEMBER"):
        self.body, self.files, self.association, self.writes = body, files, association, []

    def api(self, path, paginate=False):
        if path.endswith("/pulls/61"):
            return {"number": 61, "state": "open", "draft": False, "user": {"login": "maintainer"}, "body": self.body,
                    "author_association": self.association, "head": {"sha": "abc", "repo": {"full_name": "m/jobs"}}}
        if "/pulls/61/files" in path:
            return [{"filename": f, "status": "added"} for f in self.files]
        if "/check-runs" in path:
            return {"check_runs": []}
        raise AssertionError(path)

    def raw_file(self, repo, path, ref=""):
        return self.files[path]

    def write(self, method, path, payload=None):
        self.writes.append((method, path))


def import_body():
    step = explorer.Step("Alpha", "package", new_job("PORT-Alpha"), {"packages/Alpha/README.md": None})
    return "Imports Alpha.\n\n" + explorer.encode_import([step])


def test_the_intake_merges_an_import_and_only_then_creates_its_jobs(tmp_path):
    board = FakeBoard()
    gh = IntakeGH(import_body(), {"packages/Alpha/README.md": "# Alpha\n"})
    ctx = bot(tmp_path, board=board, gh=gh)
    assert "would merge the import and create PORT-Alpha" in intake.intake(ctx, 61, yes=False)
    assert intake.intake(ctx, 61, yes=True) == "#61: merged the import; created PORT-Alpha (#501)"
    assert ("PUT", "repos/TauCetiProject/TauCetiRoadmapJobs/pulls/61/merge") in gh.writes
    assert board.created[0][0].inputs == {}


def test_the_intake_refuses_an_untrusted_or_overreaching_import(tmp_path):
    untrusted = IntakeGH(import_body(), {"packages/Alpha/README.md": "# Alpha\n"}, association="CONTRIBUTOR")
    assert "not a maintainer of the board" in intake.intake(bot(tmp_path, gh=untrusted), 61, yes=True)
    extra = IntakeGH(import_body(), {"packages/Alpha/README.md": "# Alpha\n", "packages/Other/README.md": "x"})
    line = intake.intake(bot(tmp_path, gh=extra), 61, yes=True)
    assert line.startswith("#61: import not merged: packages/Other/README.md is not a deliverable")
    assert not any(m == "PUT" for m, _ in extra.writes)
    forged = IntakeGH("<!-- tauceti-roadmap-import {\"jobs\": [{\"id\": \"nope\"}], \"files\": []} -->", {})
    assert "not a submission" in intake.intake(bot(tmp_path, gh=forged), 61, yes=True)
    assert encode_spec(new_job("PORT-Alpha")) not in import_body()          # the marker is its own


# --- on the way in: the merge order, the README's anchor tags, the sources manifest -------------------

PACKET_SOURCES = [
    {"id": "kedlaya-ant-2025", "title": "Notes on analytic number theory", "authors": "Kiran S. Kedlaya",
     "edition": "Author PDF, last modified 21 December 2025", "url": "https://arxiv.org/abs/2101.00001v2",
     "sha256": "7a934fce8272cedd36ad609f79bbe79af0056320bb1e0bc690c87af990f305be", "accessed": "2026-10-14",
     "readSections": ["Theorem 7.1 and Lemma 7.2, printed pp. 43-45", "Theorem 8.3"]},
    {"id": "stacks", "title": "The Stacks project", "authors": "The Stacks project authors", "edition": "online",
     "url": "https://stacks.math.columbia.edu/tag/0001", "readSections": ["Tag 0001"]},
    {"id": "old-preprint", "title": "A preprint", "authors": "A. Author", "edition": "preprint",
     "url": "https://arxiv.org/pdf/math/0304085v2.pdf", "readSections": "§§2-3"},
    {"id": "mirror", "title": "Mirrored", "authors": "B. Author", "edition": "1999 edition",
     "url": "https://libgen.is/book/1", "doi": "10.1234/x.5"},
    {"id": "KEDLAYA-ANT-2025", "title": "A second key for the same id", "authors": "K", "edition": "x"},
    {"id": "no-title", "authors": "C. Author", "edition": "2001"},
    {"id": "undated", "title": "Lecture notes", "authors": "D. Author", "edition": "author page",
     "url": "https://example.org/notes.pdf"},
]


def test_a_packet_becomes_a_sources_manifest_the_gate_accepts(tmp_path):
    packet = tmp_path / "packet.json"
    packet.write_text(json.dumps({"sources": PACKET_SOURCES}), encoding="utf-8")
    cfg = example_config()
    text = explorer.packet_manifest(packet, "Alpha", tuple(cfg.sources.deny))
    data = tomllib.loads(text)
    assert data["roadmap"] == "Alpha" and [e["id"] for e in data["source"]] == [
        "kedlaya-ant-2025", "stacks", "old-preprint", "mirror", "undated"]
    assert sources.validate_manifest(data, cfg.sources.allow, cfg.sources.deny, cfg.sources.uncertified_ok) == []
    k, stacks, old, mirror, undated = data["source"]
    assert k == {"id": "kedlaya-ant-2025", "authors": "Kiran S. Kedlaya", "title": "Notes on analytic number theory",
                 "year": 2025, "arxiv": "2101.00001v2", "locator_scheme": "theorem",
                 "url": "https://arxiv.org/abs/2101.00001v2", "sha256": PACKET_SOURCES[0]["sha256"],
                 "licence_class": "none", "status": "uncertified", "accessed": "2026-10-14",
                 "version_note": "Author PDF, last modified 21 December 2025"}
    assert stacks["locator_scheme"] == "tag" and stacks["year"] == "n.d."
    assert (old["arxiv"], old["year"], old["locator_scheme"]) == ("math/0304085v2", 2003, "section")
    assert "url" not in mirror and mirror["doi"] == "10.1234/x.5" and mirror["year"] == 1999   # a denied host goes
    assert undated["year"] == "n.d." and undated["locator_scheme"] == "section"
    # Nothing usable: no manifest.
    packet.write_text(json.dumps({"sources": [{"id": "x"}]}), encoding="utf-8")
    assert explorer.packet_manifest(packet, "Alpha") == "" and explorer.packet_manifest(tmp_path / "none", "A") == ""


def test_the_import_strips_anchor_tags_carries_the_manifest_and_places_roadmaps_in_order(checkout, tmp_path, fake_git):
    """Alpha (a package) cites Gamma (an accepted design) and Base; Gamma cites Other. Gamma goes in
    first, so that Alpha, its consumer, comes after it. Delta (a design under review) and Eps (a
    target) stay out until the board accepts their designs."""
    calls, opened = fake_git
    bp = checkout / "research" / "blueprint"
    write(bp / "packages" / "Alpha" / "README.md",
          '# Alpha\n\nSee [§1.1](#legacy-1).\n\n<a id="legacy-1"></a>\n\n### 1.1 The θ-function\n\n'
          "*Needs:* Gamma §2 and Base §1.\n")
    write(bp / "readmes" / "Gamma.md", "# Gamma: design\n\n*Needs:* Other §3.\n")
    write(bp / "packets" / "Alpha.json", json.dumps({"sources": PACKET_SOURCES[:1]}))
    gh = GH(files={ORDER: "# Merge order\nBase\nOther\n"})
    ctx = bot(tmp_path, gh=gh)
    dry = explorer.import_explorer(ctx, checkout, names=("Alpha", "Gamma", "Delta", "Eps"))
    assert gh.writes == [] and all("merge order" not in s.outcome for s in dry)
    steps = {s.name: s for s in explorer.import_explorer(ctx, checkout, names=("Alpha", "Gamma", "Delta", "Eps"),
                                                         yes=True)}
    assert steps["Alpha"].outcome == ("pull request #61; 1 anchor tag removed from README.md, 1 internal link pointed "
                                      "at heading slugs; Alpha inserted into the merge order")
    assert steps["Gamma"].outcome == "pull request #61; Gamma inserted into the merge order"
    assert steps["Delta"].outcome == "pull request #61" and steps["Eps"].outcome == "created #501"
    assert depgraph.read_order(gh.files[ORDER]) == ["Base", "Other", "Gamma", "Alpha"]
    [(method, path, payload)] = gh.writes                                   # one commit for the whole run
    assert method == "PUT" and path.endswith(f"/contents/{ORDER}") and payload["sha"] == "blob-1"
    assert payload["message"] == "Merge order: insert Gamma, Alpha after their last suppliers"
    # Alpha's pull request carries the README without its tag and the manifest made from the packet.
    alpha = next(body for head, title, body in opened if "RT-Alpha~1a" in title)
    assert "packages/Alpha/sources.toml" in explorer.decode_import(alpha)["files"]
    assert "`sources.toml` made from the explorer's packet (1 source)" in alpha
    assert ("commit", "packages/Alpha/README.md", "packages/Alpha/Suggested.lean", "packages/Alpha/metadata.toml",
            "packages/Alpha/review.json", "packages/Alpha/sources.toml") in calls
    # The checkout was reused for the later roadmaps; the README went out as normalised (it was read back).
    assert steps["Alpha"].readme.startswith("# Alpha\n\nSee [§1.1](#11-the-θ-function).\n\n### 1.1 The θ-function")
    # Again, with the order already holding them: nothing is written.
    gh.writes.clear()
    explorer.place_in_order(ctx, [steps["Alpha"], steps["Gamma"]])
    assert gh.writes == [] and steps["Alpha"].outcome.endswith("; Alpha already in the merge order")


# --- the intake's backfill: open imports the merge order lacks ------------------------------------------

class BackfillGH(GH):
    """The board's main (the merge order) and the heads of open import pull requests (raw reads)."""

    def __init__(self, files, heads):
        super().__init__(files=files)
        self.heads, self.raw = heads, []

    def raw_file(self, repo, path, ref=""):
        self.raw.append((repo, path, ref))
        if (ref, path) not in self.heads:
            raise GHError(f"GET {path}: 404")
        return self.heads[(ref, path)]


def import_pr(number, name, job_id, sha, marker=True):
    step = explorer.Step(name, "package", new_job(job_id), {f"packages/{name}/README.md": None})
    return {"number": number, "title": f"Import {name} from the explorer ({job_id})",
            "body": "Imported." + ("\n\n" + explorer.encode_import([step]) if marker else ""),
            "head": {"sha": sha, "repo": {"full_name": "maintainer/jobs"}}}


def test_the_intake_places_open_imports_the_merge_order_lacks(tmp_path):
    pulls = [import_pr(70, "Alpha", "RT-Alpha~1a", "a1"), import_pr(71, "Beta", "REV-PKG-Beta", "b1"),
             import_pr(72, "Delta", "REV-DESIGN-Delta", "d1"),
             import_pr(73, "Gamma", "REV-PKG-Gamma", "g1", marker=False),
             import_pr(74, "Zeta", "PKG-Zeta", "z1"),
             {"number": 75, "title": "Fix the board's workflow", "body": "", "head": {"sha": "f1"}}]
    heads = {("a1", "packages/Alpha/README.md"): "# Alpha\n\n*Needs:* Zeta §2 and Base §1.\n",
             ("d1", "packages/Delta/design/README.md"): "# Delta\n",
             ("z1", "packages/Zeta/design/README.md"): "# Zeta: design\n\n*Needs:* Other §1.\n"}
    gh = BackfillGH({ORDER: "Base\nBeta\nOther\n"}, heads)
    ctx = bot(tmp_path, gh=gh)
    lines = []
    intake.backfill_import_order(ctx, pulls, yes=False, log=lines.append)
    assert gh.writes == []
    assert "#74: Zeta inserted into the merge order (dry run: not written) (an open explorer import)" in lines
    lines.clear()
    intake.backfill_import_order(ctx, pulls, yes=True, log=lines.append)
    assert lines == ["#73: Gamma is not in the merge order, and the import's head has no README to place it by",
                     "#74: Zeta inserted into the merge order (an open explorer import)",       # Alpha's supplier first
                     "#70: Alpha inserted into the merge order (an open explorer import)"]
    assert depgraph.read_order(gh.files[ORDER]) == ["Base", "Beta", "Other", "Zeta", "Alpha"]
    assert len(gh.writes) == 1
    # Beta is listed and Delta's design is still under review: neither head was read.
    assert {p for _r, p, _ref in gh.raw} == {"packages/Alpha/README.md", "packages/Gamma/README.md",
                                            "packages/Gamma/design/README.md", "packages/Zeta/README.md",
                                            "packages/Zeta/design/README.md"}
    # Idempotent: a second run reads the merge order and writes nothing.
    lines.clear()
    gh.raw.clear()
    intake.backfill_import_order(ctx, pulls, yes=True, log=lines.append)
    assert lines == ["#73: Gamma is not in the merge order, and the import's head has no README to place it by"]
    assert len(gh.writes) == 1 and {p for _r, p, _ref in gh.raw} == {"packages/Gamma/README.md",
                                                                     "packages/Gamma/design/README.md"}


def test_the_intake_run_backfills_from_its_own_listing(tmp_path):
    pulls = [import_pr(70, "Alpha", "RT-Alpha~1a", "a1", marker=False)]

    class ListingGH(BackfillGH):
        def api(self, path, paginate=False):
            if "/pulls?state=open" in path:
                return list(pulls)
            return super().api(path, paginate)
    gh = ListingGH({ORDER: "Base\n"}, {("a1", "packages/Alpha/README.md"): "*Needs:* Base §1.\n"})
    lines = []
    assert intake.intake_open(bot(tmp_path, gh=gh), yes=True, log=lines.append) == []
    assert lines == ["#70: Alpha inserted into the merge order (an open explorer import)"]
    assert depgraph.read_order(gh.files[ORDER]) == ["Base", "Alpha"]
    gh.files[ORDER] = "Base\n"
    lines.clear()
    intake.intake_open(bot(tmp_path, gh=gh), yes=True, only=70, log=lines.append)    # one pull request: no backfill
    assert lines == [] and gh.files[ORDER] == "Base\n"
