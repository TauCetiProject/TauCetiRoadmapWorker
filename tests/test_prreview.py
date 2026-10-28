"""The AI review of every roadmap pull request (PRREVIEW): the job kind, the `[prreview]` table, the
`board sync` step that files one review per new head, retires stale ones and keeps the `ai-approved`
label honest, and the lane that reviews a head read-only, posts one comment and labels an accepted
head. The final approval stays a human's."""

import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import board, cli, config, gates, intake, prompts, prreviews, replies
from tauceti_roadmap_worker.board import Outcome, new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import Context, LaneError, lane_for
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import prreview as lane_prreview
from tauceti_roadmap_worker.lanes.base import Workspace
from tauceti_roadmap_worker.worker import rank

from .fakes import FakeBoard, example_config
from .test_checkout import needs_git
from .test_lane_dry_run import ctx as dry_ctx  # noqa: F401 (fixture)
from .test_submission_recovery import CONTENT, ROADMAP_BRANCH, Hub, hub, no_agent, process, sh  # noqa: F401

H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40
REPO = example_config().repositories.content
ADVERSARIAL = ("| Statement | Instances tried | Values | Result | Change |\n| --- | --- | --- | --- | --- |\n"
               "| 1.1 | p = 2 | 4 = 4 | holds | none |\n")


# --- the kind ----------------------------------------------------------------------------------------

def test_the_job_id_round_trips_with_an_explicit_round_and_ranks_after_the_fixes():
    jid = board.parse_job_id("PRREVIEW-Foo~1")
    assert jid.kind == "prreview" and jid.round == 1 and str(jid) == "PRREVIEW-Foo~1"
    assert board.make_id("prreview", "Foo") == "PRREVIEW-Foo~1" and board.next_round("PRREVIEW-Foo~1") == "PRREVIEW-Foo~2"
    with pytest.raises(board.JobIdError):
        board.parse_job_id("PRREVIEW-DESIGN-Foo~1")
    assert board.title_for(new_job("PRREVIEW-Foo~1")) == "[PR review] Foo (round 1)"
    assert [k for k, _ in sorted(board.KIND_PRIORITY.items(), key=lambda kv: kv[1])][:3] == ["gate", "fix", "prreview"]
    assert board.stage_of("PRREVIEW-Foo~3") == "PRREVIEW" and "prreview" in board.KINDS
    jobs = [new_job(i) for i in ("RT-Foo~1a", "PRREVIEW-Bar~1", "FIX-RT-Foo~1", "GATE-Baz~1", "REV-PKG-Qux")]
    for n, j in enumerate(jobs, 1):
        j.number = n
    assert [j.id for j in rank(jobs, human_first=False)] == ["GATE-Baz~1", "FIX-RT-Foo~1", "PRREVIEW-Bar~1",
                                                              "RT-Foo~1a", "REV-PKG-Qux"]


def test_a_review_delivers_its_report_and_verdict_and_releases_nothing():
    job = new_job("PRREVIEW-Foo~2", inputs={"pr": 12, "head": H1, "owner": "carol", "name": "Foo"})
    wanted = {"packages/Foo/reports/PRREVIEW-Foo~2.md", "packages/Foo/reports/PRREVIEW-Foo~2.json"}
    assert board.required_paths(job, "packages") == wanted
    assert board.allowed_paths(job, "packages", "merge-order/ORDER.txt") == wanted     # no manifest, no order
    for verdict in ("accepted", "needs_changes", "rejected"):
        assert board.successors(job, Outcome(verdict=verdict)) == []
        assert board.blocked_reason(job, Outcome(verdict=verdict)) == ""
    assert "Pull request: #12 on the content repository." in board.issue_body(job, "o/board", "packages")
    assert lane_for(job).__class__.__name__ == "PRReviewLane" and cli.parser().parse_args(
        ["work", "--worker-id", "w", "--only", "prreview"]).only == "prreview"


# --- the configuration -------------------------------------------------------------------------------

def test_the_prreview_table_defaults_off_and_parses():
    cfg = config.Config()
    assert (cfg.prreview.enabled, cfg.prreview.label, cfg.prreview.per_run, cfg.prreview.authors,
            cfg.prreview.skip_authors, cfg.prreview.max_folders) == (False, "ai-approved", 3, [], [], 10)
    text = ('[project]\nname = "P"\n[prreview]\nenabled = true\nlabel = "ai-ok"\nper_run = 5\nauthors = ["carol"]\n'
            'skip_authors = ["dave"]\nmax_folders = 4\n')
    parsed = config.parse(text, "x.toml").prreview
    assert (parsed.enabled, parsed.label, parsed.per_run, parsed.authors, parsed.skip_authors,
            parsed.max_folders) == (True, "ai-ok", 5, ["carol"], ["dave"], 4)
    for bad, why in (("per_run = -1", "prreview.per_run"), ('authors = ["not a login"]', "prreview.authors"),
                     ('enabled = true\nlabel = ""', "prreview.label"), ('per_run = "3"', "expected int"),
                     ("max_folders = 0", "prreview.max_folders")):
        with pytest.raises(config.ConfigError, match=re.escape(why)):
            config.parse(f'[project]\nname = "P"\n[prreview]\n{bad}\n', "x.toml")


def test_a_file_that_still_sets_the_retired_single_folder_switch_loads_with_a_warning():
    cfg = config.parse('[project]\nname = "P"\n[prreview]\nenabled = true\nrequire_single_folder = true\n', "x.toml")
    assert cfg.prreview.enabled and not hasattr(cfg.prreview, "require_single_folder")
    assert cfg.warnings == [("prreview.require_single_folder is no longer read (every roadmap folder of a pull request "
                             "gets its own review; see prreview.max_folders): remove it")]


def test_the_lane_runs_on_the_review_models_unless_its_own_table_pins_others():
    cfg = example_config()
    assert cfg.lane_models("prreview") == cfg.lane_models("review") == {"codex": "gpt-6-astra", "claude": "fable"}
    cfg.agents.lanes["prreview"] = config.LaneAgent(codex_model="astra-x", claude_model="fable-x")
    assert cfg.lane_models("prreview") == {"codex": "astra-x", "claude": "fable-x"}
    assert config.Config().lane_models("prreview") == {}           # nothing hardcoded without a table


# --- the gate on a nested roadmap's folder -----------------------------------------------------------

def gate_tree(tmp_path, folder, metadata_at=None):
    root = tmp_path / "wt"
    path = root / "TauCetiRoadmap" / folder
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Toy\n")
    (path / "Suggested.lean").write_text("import Mathlib\n")
    if metadata_at is not None:
        (root / "TauCetiRoadmap" / metadata_at / "metadata.toml").write_text('topic = "math.GR"\n')
    return root


@pytest.mark.parametrize("folder,metadata_at,module,missing", [
    ("", "Toy", "TauCetiRoadmap.Toy.Suggested", 0),                        # every other lane: unchanged
    ("", None, "TauCetiRoadmap.Toy.Suggested", 1),
    ("Family/Toy", "Family", "TauCetiRoadmap.Family.Toy.Suggested", 0),    # a family's metadata covers its roadmaps
    ("Family/Toy", None, "TauCetiRoadmap.Family.Toy.Suggested", 1),
])
def test_the_gate_reads_a_nested_folder_when_told_and_builds_its_dotted_module(tmp_path, monkeypatch, folder, metadata_at,
                                                                               module, missing):
    root = gate_tree(tmp_path, folder or "Toy", metadata_at)
    built = []
    monkeypatch.setattr(gates, "build_problems", lambda worktree, command, timeout=0: built.append(command) or [])
    result = gates.run_gate(root, "Toy", gates.GateOptions(folder=folder), build_cmd="lake build {module}")
    assert built == [f"lake build {module}"] and result.counts["metadata"] == missing
    assert "missing-files" not in result.counts
    assert gates.GateOptions().folder == "" and gates.roadmap_module("TauCetiRoadmap", "Toy") == "TauCetiRoadmap.Toy.Suggested"


def test_the_gate_command_takes_a_nested_folder(tmp_path, monkeypatch, capsys):
    root = gate_tree(tmp_path, "Family/Toy", "Family")
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    conf = str(Path(__file__).resolve().parent.parent / "tauceti-roadmap.toml.example")
    cli.main(["--conf", conf, "gate", str(root), "Toy", "--folder", "Family/Toy"])
    out = capsys.readouterr().out
    assert out.startswith("gate Toy: ") and "missing-files" not in out and "metadata=0" in out


def test_a_roadmap_folder_carrying_sources_notes_or_scratch_fails_the_gate(tmp_path):
    root = gate_tree(tmp_path, "Toy", "Toy")
    folder = root / "TauCetiRoadmap" / "Toy"
    (folder / "SOURCES.md").write_text("# Sources, prioritization, and existing formalization\n")
    (folder / "PROGRESS.md").write_text("")
    (folder / "notes").mkdir()
    (folder / "notes" / "x.md").write_text("")
    (folder / "references").mkdir()
    (folder / "references" / "README.md").write_text("")        # lower-case: no nested roadmap
    result = gates.run_gate(root, "Toy", gates.GateOptions())
    assert not result.ok and result.details["stray-files"] == [
        "TauCetiRoadmap/Toy/PROGRESS.md: not a roadmap file", "TauCetiRoadmap/Toy/SOURCES.md: not a roadmap file",
        "TauCetiRoadmap/Toy/notes/: not a roadmap file", "TauCetiRoadmap/Toy/references/: not a roadmap file"]
    assert "sources are the README's references" in gates.GATE_RULES["stray-files"]
    report = lane_prreview.gate_document(result, H1)
    assert "## stray-files (4)" in report and "Rule: a roadmap folder holds only README.md" in report
    # A project may allow more files by name.
    allowed = gates.GateOptions(roadmap_files=["README.md", "Suggested.lean", "metadata.toml", "PROGRESS.md"])
    assert gates.run_gate(root, "Toy", allowed).counts["stray-files"] == 3


def test_a_clean_roadmap_folder_and_a_family_index_beside_a_nested_one_have_no_stray_file(tmp_path):
    root = gate_tree(tmp_path, "Family/Toy", "Family")
    family = root / "TauCetiRoadmap" / "Family"
    for name in ("README.md", "PROGRESS.md", "STATUS.md"):
        (family / name).write_text("")                  # the family index's own files are not the roadmap's
    (family / "Toy" / "CompactTargets.lean").write_text("import Mathlib\n")
    assert gates.run_gate(root, "Toy", gates.GateOptions(folder="Family/Toy")).counts["stray-files"] == 0
    # A roadmap folder holding nested roadmaps keeps them; a top-level clean folder passes too.
    (family / "Suggested.lean").write_text("import Mathlib\n")
    assert gates.stray_files(root, "TauCetiRoadmap/Family", list(gates.ROADMAP_FILES)) == [
        "TauCetiRoadmap/Family/PROGRESS.md: not a roadmap file", "TauCetiRoadmap/Family/STATUS.md: not a roadmap file"]
    clean = gate_tree(tmp_path / "clean", "Toy", "Toy")
    assert gates.run_gate(clean, "Toy", gates.GateOptions()).counts["stray-files"] == 0


def test_the_roadmap_files_are_configurable():
    assert config.Config().gates.roadmap_files == ["README.md", "Suggested.lean", "metadata.toml"]
    cfg = config.parse('[project]\nname = "P"\n[gates]\nroadmap_files = ["README.md", "Suggested.lean", "NOTES.md"]\n',
                       "x.toml")
    assert gates.GateOptions.from_config(cfg).roadmap_files == ["README.md", "Suggested.lean", "NOTES.md"]
    for bad in ('["README.md", "a/b.md"]', '["Suggested.lean"]'):
        with pytest.raises(config.ConfigError, match=re.escape("gates.roadmap_files must list plain file names")):
            config.parse(f'[project]\nname = "P"\n[gates]\nroadmap_files = {bad}\n', "x.toml")


# --- board sync --------------------------------------------------------------------------------------

def node(number, head, author="carol", paths=("TauCetiRoadmap/Foo/README.md",), labels=(), draft=False, title=""):
    """An open pull request as the GraphQL listing gives it; a path is `path` (modified) or
    `(path, changeType)`."""
    files = [(p, "MODIFIED") if isinstance(p, str) else p for p in paths]
    return {"number": number, "title": title or f"Roadmap #{number}", "isDraft": draft, "headRefOid": head,
            "author": {"login": author}, "labels": {"nodes": [{"name": x} for x in labels]},
            "files": {"totalCount": len(files), "nodes": [{"path": p, "changeType": c} for p, c in files]}}


class ContentGH:
    """The content repository's open pull requests (one GraphQL listing), single pull requests by
    REST, main's tree (`tree`: the folders under TauCetiRoadmap/ holding a README.md), the board's main
    (verdict records), and every write; label writes fail with `refuse`."""

    def __init__(self, nodes=(), pulls=None, files=None, refuse=False, tree=()):
        self.nodes, self.pulls, self.files, self.refuse = list(nodes), dict(pulls or {}), dict(files or {}), refuse
        self.tree = [f"TauCetiRoadmap/{d}/README.md" for d in tree]
        self.writes, self.listings, self.dry_run, self.fence = [], 0, False, None

    def graphql(self, query, **variables):
        self.listings += 1
        return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": self.nodes}}}

    def api(self, path, paginate=False):
        m = re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path)
        if m and int(m.group(1)) in self.pulls:
            return self.pulls[int(m.group(1))]
        if re.fullmatch(r"repos/[^/]+/[^/]+/git/trees/main\?recursive=1", path):
            return {"truncated": False, "tree": [{"path": t, "type": "blob"} for t in self.tree]}
        raise GHError(f"GET {path}: 404")

    def raw_file(self, repo, path, ref=""):
        if path in self.files:
            return self.files[path]
        raise GHError(f"{path}: 404")

    def write(self, method, path, payload=None):
        if self.refuse and "/labels" in path:
            raise GHError(f"gh api -X {method} {path}: HTTP 403: Resource not accessible by integration")
        self.writes.append((method, path, payload or {}))
        return {}


def review_job(number, round_, pr, head, state, name="Foo", owner="carol", folder=""):
    inputs = {"pr": pr, "head": head, "owner": owner, "name": name, **({"folder": folder} if folder else {})}
    job = new_job(board.make_id("prreview", name, round_), inputs=inputs)
    job.number, job.state, job.labels = number, state, ["roadmap-job", "kind:prreview", f"state:{state}"]
    return job


def verdict_file(job, status, head):
    return {board.prreview_verdict_path("packages", job): json.dumps(
        {"review": {"status": status, "reviewer": f"independent-review-{job.id}", "head": head, "pr": job.inputs["pr"],
                    "folder": board.prreview_folder(job)}})}


def sync(gh, jobs=(), **over):
    cfg = example_config()
    cfg.prreview.enabled = True
    for key, value in over.items():
        setattr(cfg.prreview, key, value)
    fake = FakeBoard(jobs=list(jobs))
    out = intake.sync_pr_reviews(SimpleNamespace(cfg=cfg, gh=gh, board=fake, operator="board-bot", dry_run=False))
    return out, fake


def test_one_review_is_filed_per_new_head_for_any_author_drafts_included():
    out, fake = sync(ContentGH([node(12, H1, author="outsider", draft=True)]))
    [(job, state, _)] = fake.created
    assert job.id == "PRREVIEW-Foo~1" and state == "available" and job.requires_operator == ""
    # A review is of one roadmap folder, which the job names (relative to the active directory).
    assert job.inputs == {"pr": 12, "head": H1, "owner": "outsider", "name": "Foo", "folder": "Foo"}
    assert out == ["#12: PRREVIEW-Foo~1 created"]
    # The head it names is reviewed (whatever became of that review): no second job until the head moves.
    done = review_job(40, 1, 12, H1, "done")
    out, fake = sync(ContentGH([node(12, H1)]), [done])
    assert fake.created == [] and out == []
    out, fake = sync(ContentGH([node(12, H2)]), [done])
    assert [j.id for j, _s, _p in fake.created] == ["PRREVIEW-Foo~2"] and fake.created[0][0].inputs["head"] == H2


@pytest.mark.parametrize("open_job", [review_job(41, 1, 12, H1, "claimed"), review_job(41, 1, 12, H1, "submitted"),
                                      new_job("GATE-Foo~2", inputs={"pr": 12, "operator": "", "owner": "carol"})])
def test_no_review_is_filed_while_a_review_or_a_gate_round_of_the_pull_request_is_open(open_job):
    if open_job.kind == "gate":
        open_job.number, open_job.state = 41, "claimed"
    _out, fake = sync(ContentGH([node(12, H2)]), [open_job])
    assert fake.created == [] and fake.states == []          # a claimed or submitted review is left to finish


def test_at_most_per_run_jobs_are_filed_oldest_pull_request_first():
    nodes = [node(n, str(n) * 40 if n < 10 else H1, paths=(f"TauCetiRoadmap/R{n}/README.md",)) for n in (31, 7, 19, 5)]
    out, fake = sync(ContentGH(nodes), per_run=2)
    assert [j.inputs["pr"] for j, _s, _p in fake.created] == [5, 7]
    # `per_run` counts jobs (one per roadmap folder), not pull requests.
    assert out[-1] == "2 review job(s) wait ([prreview] per_run = 2)"


def test_the_author_lists_decide_whose_pull_requests_are_reviewed():
    nodes = [node(1, H1, author="Carol", paths=("TauCetiRoadmap/A/README.md",)),
             node(2, H2, author="dave", paths=("TauCetiRoadmap/B/README.md",))]
    _out, fake = sync(ContentGH(nodes), authors=["carol", "dave"], skip_authors=["DAVE"])
    assert [j.name for j, _s, _p in fake.created] == ["A"]
    _out, fake = sync(ContentGH(nodes), authors=["erin"])
    assert fake.created == []


def test_a_pull_request_with_several_roadmap_folders_gets_one_review_per_folder():
    # Formerly skipped (`require_single_folder`): every roadmap folder now has a review of its own,
    # and each job names the pull request's other folders as its own.
    paths = ("TauCetiRoadmap/Foo/README.md", "TauCetiRoadmap/Bar/Suggested.lean")
    out, fake = sync(ContentGH([node(12, H1, paths=paths)], tree=["Bar"]))
    assert [(j.id, j.inputs["folder"], j.inputs["roadmaps"]) for j, _s, _p in fake.created] == [
        ("PRREVIEW-Bar~1", "Bar", ["Bar", "Foo"]), ("PRREVIEW-Foo~1", "Foo", ["Bar", "Foo"])]
    assert out == ["#12: PRREVIEW-Bar~1 created", "#12: PRREVIEW-Foo~1 created"]
    out, fake = sync(ContentGH([node(13, H1, paths=("Completed/Foo/README.md",))]))
    assert fake.created == [] and out == []


REP = "TauCetiRoadmap/RepresentationTheory"
# Main's README folders in the shape of the content repository: a family index with nested roadmaps,
# a roadmap with a `references/` note, and top-level roadmaps.
TREE = ["RepresentationTheory", "RepresentationTheory/SemisimpleAlgebras", "RepresentationTheory/CompactGroups",
        "GeometricTopology", "GeometricTopology/references", "BelyiMaps", "ConformalMapping", "Foo"]


def created(fake):
    return [(j.id, j.inputs["folder"]) for j, _s, _p in fake.created]


def test_a_nested_roadmap_is_one_review_of_its_folder():
    out, fake = sync(ContentGH([node(989, H1, paths=(f"{REP}/SemisimpleAlgebras/README.md",))], tree=TREE))
    [(job, _s, _p)] = fake.created
    assert (job.id, job.inputs["folder"], job.inputs["name"]) == (
        "PRREVIEW-SemisimpleAlgebras~1", "RepresentationTheory/SemisimpleAlgebras", "SemisimpleAlgebras")
    assert "roadmaps" not in job.inputs and out == ["#989: PRREVIEW-SemisimpleAlgebras~1 created"]
    # Extra Lean files belong to their roadmap, whose README is on main when the pull request leaves it
    # alone; the family's index changing beside it is no roadmap of its own.
    paths = ((f"{REP}/CompactGroups/AbelianFourierTargets.lean", "ADDED"), f"{REP}/CompactGroups/Suggested.lean",
             f"{REP}/README.md", f"{REP}/PROGRESS.md")
    _out, fake = sync(ContentGH([node(1047, H1, paths=paths)], tree=TREE))
    assert created(fake) == [("PRREVIEW-CompactGroups~1", "RepresentationTheory/CompactGroups")]


def test_a_bundle_of_new_roadmaps_is_reviewed_folder_by_folder_across_runs():
    paths = [(f"TauCetiRoadmap/{n}/{f}", "ADDED") for n in ("Alpha", "Beta", "Gamma")
             for f in ("README.md", "Suggested.lean")]
    nodes = [node(1397, H1, paths=paths), node(1400, H2, paths=(("TauCetiRoadmap/Delta/README.md", "ADDED"),))]
    out, fake = sync(ContentGH(nodes), per_run=2)
    assert created(fake) == [("PRREVIEW-Alpha~1", "Alpha"), ("PRREVIEW-Beta~1", "Beta")]
    assert fake.created[0][0].inputs["roadmaps"] == ["Alpha", "Beta", "Gamma"]
    assert out[-1] == "2 review job(s) wait ([prreview] per_run = 2)"
    # The next run finishes the bundle before it starts the next pull request.
    first = [review_job(600 + k, 1, 1397, H1, "available", name=n, folder=n) for k, n in enumerate(("Alpha", "Beta"))]
    out, fake = sync(ContentGH(nodes), first, per_run=1)
    assert created(fake) == [("PRREVIEW-Gamma~1", "Gamma")]
    assert out[-1] == "1 review job(s) wait ([prreview] per_run = 1)"
    # A folder reviewed at this head is not filed again; one whose review is open waits for it.
    taken = review_job(610, 1, 1397, H2, "claimed", name="Gamma", folder="Gamma")
    _out, fake = sync(ContentGH(nodes), first + [taken], per_run=5)
    assert created(fake) == [("PRREVIEW-Delta~1", "Delta")]


def test_a_mass_edit_of_existing_roadmaps_is_a_chore_and_a_bundle_of_new_ones_is_not():
    names = [f"R{k:02d}" for k in range(43)]
    bump = node(1187, H1, title="Bump TauCeti", paths=[f"TauCetiRoadmap/{n}/Suggested.lean" for n in names]
                + ["lake-manifest.json", "Completed/Old/Suggested.lean"])
    out, fake = sync(ContentGH([bump], tree=names))
    assert fake.created == [] and out == ["#1187: not reviewed (chore: 43 roadmap folders)"]
    eight = [f"Point{k}" for k in range(8)]
    adds = node(196, H2, paths=[(f"TauCetiRoadmap/Family/{n}/{f}", "ADDED") for n in eight
                                for f in ("README.md", "Suggested.lean")]
                + [("TauCetiRoadmap/Family/README.md", "ADDED")])
    _out, fake = sync(ContentGH([adds]), per_run=20)
    assert [folder for _id, folder in created(fake)] == [f"Family/{n}" for n in eight]      # the index is no roadmap
    twelve = [f"New{k:02d}" for k in range(12)]
    paths = [(f"TauCetiRoadmap/{n}/README.md", "ADDED") for n in twelve]
    _out, fake = sync(ContentGH([node(271, H3, paths=paths)]), per_run=20)
    assert len(fake.created) == 12                  # more than max_folders, every README added
    paths[0] = ("TauCetiRoadmap/New00/README.md", "MODIFIED")
    out, fake = sync(ContentGH([node(271, H3, paths=paths)]), per_run=20)
    assert fake.created == [] and out == ["#271: not reviewed (chore: 12 roadmap folders)"]
    out, fake = sync(ContentGH([node(271, H3, paths=paths[:3])]), per_run=20, max_folders=2)
    assert out == ["#271: not reviewed (chore: 3 roadmap folders)"]


def test_completed_progress_reports_and_archives_are_not_reviewed():
    archive = node(752, H1, title="Archive ConformalMapping", paths=(
        ("Completed/ConformalMapping/README.md", "RENAMED"), ("Completed/ConformalMapping/Suggested.lean", "ADDED"),
        "Completed/README.md", ("TauCetiRoadmap/ConformalMapping/Suggested.lean", "DELETED")))
    progress = node(753, H2, paths=("TauCetiRoadmap/Foo/PROGRESS.md", "TauCetiRoadmap/Foo/STATUS.md",
                                    "TauCetiRoadmap/Foo/metadata.toml", f"{REP}/STATUS.md"))
    notes = node(754, H3, paths=("TauCetiRoadmap/GeometricTopology/references/README.md",))
    out, fake = sync(ContentGH([archive, progress, notes], tree=TREE))
    assert fake.created == [] and out == []
    # A roadmap's README edited beside an archive is reviewed, unless the pull request is a chore by its title
    # (a conventional-commit chore, or an "Archive X …" pull request).
    archive["files"]["nodes"].append({"path": "TauCetiRoadmap/BelyiMaps/README.md", "changeType": "MODIFIED"})
    archive["title"] = "Move ConformalMapping to the completed directory"
    _out, fake = sync(ContentGH([archive], tree=TREE))
    assert created(fake) == [("PRREVIEW-BelyiMaps~1", "BelyiMaps")]
    archive["title"] = "Archive ConformalMapping after its consumers merge"
    out, fake = sync(ContentGH([archive], tree=TREE))
    assert fake.created == [] and out == ["#752: not reviewed (chore by its title)"]
    archive["title"] = "chore(ConformalMapping): archive the finished roadmap"
    out, fake = sync(ContentGH([archive], tree=TREE))
    assert fake.created == [] and out == ["#752: not reviewed (chore by its title)"]
    # A README the pull request removes leaves no roadmap behind it.
    gone = node(755, H1, paths=(("TauCetiRoadmap/Foo/README.md", "DELETED"), "TauCetiRoadmap/Foo/Suggested.lean"))
    out, fake = sync(ContentGH([gone], tree=TREE))
    assert fake.created == [] and out == []


def test_the_folders_of_a_pull_request_follow_the_nearest_readme():
    entry = {"files": [{"path": f"{REP}/CompactGroups/Sub/Deep.lean", "change": "added"},
                       {"path": "TauCetiRoadmap/New/Inner/README.md", "change": "added"},
                       {"path": "TauCetiRoadmap/New/Inner/Suggested.lean", "change": "added"},
                       {"path": "TauCetiRoadmap/Moved/Suggested.lean", "change": "modified"},
                       {"path": "TauCetiRoadmap/Renamed/README.md", "change": "renamed",
                        "previous": "TauCetiRoadmap/Moved/README.md"},
                       {"path": "TauCetiRoadmap.lean", "change": "modified"}]}
    assert intake.review_folders(entry, "TauCetiRoadmap", "Completed", set(TREE) | {"Moved"}) == {
        "New/Inner": "added", "RepresentationTheory/CompactGroups": "", "Renamed": "renamed"}


def two_folder_pr(head, labels=()):
    return node(1164, head, labels=labels, paths=("TauCetiRoadmap/Foo/README.md", "TauCetiRoadmap/Bar/README.md"))


def test_the_label_is_added_once_every_folder_accepts_and_goes_when_one_blocks_or_the_head_moves():
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    labels = f"repos/{REPO}/issues/1164/labels"
    # One folder accepted, the other not reviewed yet (its job is available): no label, no verdict read.
    pending = review_job(42, 1, 1164, H1, "available", name="Bar", folder="Bar")
    gh = ContentGH([two_folder_pr(H1)], files=verdict_file(foo, "accepted", H1))
    out, _fake = sync(gh, [foo, pending])
    assert gh.writes == [] and out == []
    # The last folder's verdict lands: the label is added.
    gh = ContentGH([two_folder_pr(H1)], files={**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)})
    out, _fake = sync(gh, [foo, bar])
    assert gh.writes == [("POST", labels, {"labels": ["ai-approved"]})]
    assert out == [f"#1164: ai-approved added (the AI review of {H1[:7]} accepts all 2 roadmap folders)"]
    # Labelled and both accepted: kept. A later review of one folder that blocks takes it off.
    gh = ContentGH([two_folder_pr(H1, ["ai-approved"])],
                   files={**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)})
    assert sync(gh, [foo, bar])[0] == [] and gh.writes == []
    blocks = review_job(43, 2, 1164, H1, "done", name="Bar", folder="Bar")
    gh = ContentGH([two_folder_pr(H1, ["ai-approved"])], files={
        **verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1), **verdict_file(blocks, "needs_changes", H1)})
    out, _fake = sync(gh, [foo, bar, blocks])
    assert gh.writes == [("DELETE", f"{labels}/ai-approved", {})]
    assert out == [f"#1164: ai-approved removed (the AI review of {H1[:7]} does not accept it (Bar))"]
    # The head moves: the label goes, and both folders get a review of the new head.
    gh = ContentGH([two_folder_pr(H2, ["ai-approved"])])
    out, fake = sync(gh, [foo, bar])
    assert out[0] == "#1164: ai-approved removed (head moved)" and created(fake) == [
        ("PRREVIEW-Bar~2", "Bar"), ("PRREVIEW-Foo~2", "Foo")]
    # A token that may not write the content repository says so.
    gh = ContentGH([two_folder_pr(H1)], files={**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)},
                   refuse=True)
    assert sync(gh, [foo, bar])[0][0].startswith(f"#1164: ai-approved on {REPO} not added (gh api -X POST")


def test_a_chore_loses_a_label_put_on_by_hand():
    chore = node(1187, H1, labels=["ai-approved"], title="chore: bump TauCeti", paths=("TauCetiRoadmap/Foo/Suggested.lean",))
    gh = ContentGH([chore], tree=TREE)
    out, fake = sync(gh)
    assert out == ["#1187: ai-approved removed (not reviewed: chore by its title)", "#1187: not reviewed (chore by its title)"]
    assert fake.created == []


def test_a_review_nobody_took_is_superseded_when_the_head_moves_or_the_pull_request_closes():
    stale, gone = review_job(41, 1, 12, H1, "available"), review_job(42, 1, 13, H1, "available", name="Bar")
    gh = ContentGH([node(12, H2)], pulls={13: {"number": 13, "state": "closed", "head": {"sha": H1}}})
    out, fake = sync(gh, [stale, gone])
    assert out == ["#12: PRREVIEW-Foo~1 superseded", "#13: PRREVIEW-Bar~1 superseded", "#12: PRREVIEW-Foo~2 created"]
    assert fake.states == [("PRREVIEW-Foo~1", "done"), ("PRREVIEW-Bar~1", "done")]
    assert fake.posted[0] == (41, f"Superseded: #12 is at {H2[:7]}. This review is no longer needed; closed by the board.")
    assert fake.posted[1][1].startswith("Closed: #13 is closed.")
    assert fake.labels == [("PRREVIEW-Foo~1", board.SUPERSEDED_LABEL), ("PRREVIEW-Bar~1", board.SUPERSEDED_LABEL)]
    # A claimed review of the old head is left to finish, and holds back the next one.
    taken = review_job(41, 1, 12, H1, "claimed")
    out, fake = sync(ContentGH([node(12, H2)]), [taken])
    assert out == [] and fake.states == [] and fake.created == []


def test_the_label_goes_when_the_head_moves_and_stays_on_an_accepted_current_head():
    accepted_old = review_job(41, 1, 12, H1, "done")
    labels = f"repos/{REPO}/issues/12/labels/ai-approved"
    gh = ContentGH([node(12, H2, labels=["ai-approved", "awaiting-review"])], files=verdict_file(accepted_old, "accepted", H1))
    out, _fake = sync(gh, [accepted_old], per_run=0)
    assert gh.writes == [("DELETE", labels, {})] and out[0] == "#12: ai-approved removed (head moved)"
    accepted_now = review_job(42, 2, 12, H2, "done")
    gh = ContentGH([node(12, H2, labels=["ai-approved"])], files=verdict_file(accepted_now, "accepted", H2))
    out, _fake = sync(gh, [accepted_old, accepted_now])
    assert gh.writes == [] and out == []
    # A later review of the same head that blocks takes it off; one still being finished keeps it.
    blocking = review_job(43, 3, 12, H2, "done")
    gh = ContentGH([node(12, H2, labels=["ai-approved"])],
                   files={**verdict_file(accepted_now, "accepted", H2), **verdict_file(blocking, "needs_changes", H2)})
    out, _fake = sync(gh, [accepted_now, blocking])
    assert out == [f"#12: ai-approved removed (the AI review of {H2[:7]} does not accept it)"]
    finishing = review_job(44, 4, 12, H3, "submitted")
    gh = ContentGH([node(12, H3, labels=["ai-approved"])])
    assert sync(gh, [finishing])[0] == [] and gh.writes == []
    # A label put on by hand, with no AI review at all, goes too; a token that may not write says so.
    gh = ContentGH([node(14, H1, labels=["ai-approved"], paths=("TauCetiRoadmap/Baz/README.md",))], refuse=True)
    out, _fake = sync(gh, per_run=0)
    assert out[0].startswith(f"#14: ai-approved on {REPO} not removed (gh api -X DELETE")


def test_a_verdict_github_did_not_answer_for_keeps_the_label():
    accepted = review_job(42, 1, 12, H2, "done")

    class Blip(ContentGH):
        def raw_file(self, repo, path, ref=""):
            raise GHError(f"gh api repos/{repo}/contents/{path}: HTTP 502: Bad Gateway")
    gh = Blip([node(12, H2, labels=["ai-approved"])])
    with pytest.raises(GHError, match="HTTP 502"):
        sync(gh, [accepted])
    assert gh.writes == []


def test_a_disabled_board_reads_nothing():
    gh = ContentGH([node(12, H1)])
    out, fake = sync(gh, enabled=False)
    assert out == [] and gh.listings == 0 and fake.created == []


def test_board_sync_runs_the_review_step_right_after_the_pull_requests(monkeypatch, capsys):
    cfg, ran = example_config(), []
    for name in ("sync_targets", "sync_pull_requests", "sync_pr_reviews", "repark_capped", "sync_origins",
                 "note_gap_registry_blocks"):
        monkeypatch.setattr(intake, name, lambda ctx, name=name: ran.append(name) or [])
    for name in ("sync_states", "sync_expired", "sync_leases"):
        monkeypatch.setattr(intake, name, lambda ctx, shared, name=name: ran.append(name) or [])
    monkeypatch.setattr(cli.filers, "refresh", lambda ctx: [])
    monkeypatch.setattr(cli.boardpr, "review_queue", lambda ctx, yes=False: [])
    monkeypatch.setattr(cli.errata, "sync_errata", lambda ctx, shared: [])
    ctx = SimpleNamespace(cfg=cfg, gh=None, board=FakeBoard(), operator="maintainer", dry_run=True)
    assert cli.board_run(cli.parser().parse_args(["board", "sync"]), cfg, ctx) == 0
    assert ran[ran.index("sync_pull_requests") + 1] == "sync_pr_reviews"


# --- the comment it posts ------------------------------------------------------------------------------

REPORT = ("# Review of Foo (pull request #905 at {h7})\n\n**Verdict:** {v}\n\n{findings}## Verified sound\n\n"
          + ADVERSARIAL + "\n## Not verifiable\n\n- none\n\n## Source issues\n\n- none\n")
FINDING = "## R01 (P1) — the hypothesis on p is missing\n\nLocation: README line 4. Witness: p = 2.\n\n"


def body_for(status: str, head: str = H1) -> str:
    report = REPORT.format(h7=head[:7], v=status, findings=FINDING if status != "accepted" else "")
    return prreviews.render_pr_review(prreviews.pr_review_head("codex", "gpt-x", head), status, report,
                                      {"job": "PRREVIEW-Foo~1", "head": head}, head)


def test_a_review_that_asks_for_changes_is_a_review_to_answer_and_an_acceptance_is_not():
    markers = config.Config().replies.review_markers
    accepted, blocking = body_for("accepted"), body_for("needs_changes")
    assert "\nVerdict: APPROVE\n" in accepted and replies.APPROVE_VERDICT.search(accepted)
    assert not replies.APPROVE_VERDICT.search(blocking) and f"\nReviewed head: {H1}\n" in blocking
    for login, owner in (("reviewer", "carol"), ("carol", "carol")):     # another login, or the author's own
        ask = replies.Event("2026-10-27T10:00:00Z", login, blocking)
        ok = replies.Event("2026-10-27T10:00:00Z", login, accepted)
        assert replies.is_review(ask, {"carol"}, markers, owner=owner)
        assert not replies.is_review(ok, {"carol"}, markers, owner=owner)
        assert not replies.is_reply(ask, {"carol"}, markers, [], owner=owner)
    assert replies.unanswered([replies.Event("2026-10-27T10:00:00Z", "reviewer", blocking)], {"carol"}, markers, [])
    assert prreviews.prreview_marker(blocking) == {"job": "PRREVIEW-Foo~1", "head": H1}
    assert prreviews.worker_marker(blocking) == {}                 # not a board review comment


# --- the lane ------------------------------------------------------------------------------------------

def lane_ctx(tmp_path, operator="alice", separation="operator", gh=None, dry_run=False):
    cfg = example_config()
    cfg.review.separation = separation
    return Context(cfg=cfg, gh=gh, board=FakeBoard(), claims=None, worker_id="alice-1", operator=operator,
                   agent="codex", model="gpt-x", dry_run=dry_run)


def test_the_author_does_not_review_their_own_pull_request(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 12, "head": H1, "owner": "Carol", "name": "Foo"})
    lane = lane_for(job)
    assert lane.eligible(job, lane_ctx(tmp_path, operator="carol")) == "the author does not review their own pull request"
    assert lane.eligible(job, lane_ctx(tmp_path, operator="carol", separation="family")) != ""
    assert lane.eligible(job, lane_ctx(tmp_path, operator="carol", separation="agent")) == ""
    assert lane.eligible(job, lane_ctx(tmp_path, operator="alice")) == ""


@pytest.mark.parametrize("pull,why", [({"state": "open", "head": {"sha": H2}}, f"#12 moved to {H2[:7]}; the intake files"),
                                      ({"state": "closed", "head": {"sha": H1}}, "#12 is closed"),
                                      ({"state": "closed", "merged": True, "head": {"sha": H1}}, "#12 is merged")])
def test_a_pull_request_that_moved_or_closed_is_not_reviewed(tmp_path, monkeypatch, pull, why):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 12, "head": H1, "owner": "carol", "name": "Foo"})
    ctx = lane_ctx(tmp_path, gh=ContentGH(pulls={12: {"number": 12, **pull}}))
    with pytest.raises(LaneError, match=re.escape(why)):
        lane_for(job).prepare(job, ctx)


def test_the_brief_gets_a_value_for_every_placeholder(dry_ctx):  # noqa: F811
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": "abc1234" + "0" * 33, "owner": "carol", "name": "Foo"})
    job.number = 7
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)
    values = {**lane.common_values(job, dry_ctx, ws), **lane.values(job, dry_ctx, ws)}
    assert prompts.placeholders("prreview") <= set(values)
    text = prompts.render("prreview", values)
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", text)
    assert "# Review of Foo (pull request #905 at abc1234)" in text and "independent-review-PRREVIEW-Foo~1" in text


class LabelGH(ContentGH):
    """Content pull request #905: its comments and labels; a comment write may be lost or crash."""

    def __init__(self, head=H1, labels=(), fail=()):
        super().__init__(pulls={905: {"number": 905, "state": "open", "head": {"sha": head}}})
        self.comments, self.labels, self.fail = [], list(labels), list(fail)

    def api(self, path, paginate=False):
        if path.startswith(f"repos/{REPO}/issues/905/comments"):
            return [dict(c) for c in self.comments]
        if path == f"repos/{REPO}/issues/905/labels":
            return [{"name": x} for x in self.labels]
        return super().api(path, paginate)

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        if path.endswith("/issues/905/comments"):
            self.comments.append({"id": len(self.comments) + 1, "body": payload["body"], "user": {"login": "alice"},
                                  "html_url": f"https://github.com/{REPO}/pull/905#c{len(self.comments) + 1}"})
            fault = self.fail.pop(0) if self.fail else None
            if fault == "lose":
                raise GHError(f"gh api -X POST {path}: HTTP 504: Gateway Timeout")
            if fault == "crash":
                raise KeyboardInterrupt
            return self.comments[-1]
        if path.endswith("/issues/905/labels") and method == "POST":
            self.labels += [x for x in payload["labels"] if x not in self.labels]
        elif "/issues/905/labels/" in path and method == "DELETE":
            self.labels.remove(path.rsplit("/", 1)[1])
        return {}


def finished_ws(tmp_path, ctx, job, status, gate_ok=True):
    """A workspace as the agent left it: the report and verdict in the board checkout, the gate run."""
    board_dir = tmp_path / "board"
    ws = Workspace(job_dir=tmp_path / "job", scratch=tmp_path / "job" / "scratch", board=board_dir,
                   content=tmp_path / "content", workdir=tmp_path / "wt", report=board_dir / "packages" / job.name /
                   "reports" / f"{job.id}.md", branch=f"alice-1/PRREVIEW-{job.name}_r1", job_id=job.id)
    for d in (ws.scratch, ws.workdir, ws.report.parent):
        d.mkdir(parents=True, exist_ok=True)
    report = REPORT.format(h7=H1[:7], v=status, findings="" if status == "accepted" else FINDING)
    ws.report.write_text(report.replace("# Review of Foo", f"# Review of {job.name}"))
    lane = lane_for(job)
    lane.verdict_file(job, ctx, ws).write_text(json.dumps({"review": {
        "status": status, "reviewer": f"independent-review-{job.id}", "head": H1, "pr": 905, "date": "2026-10-27",
        "folder": board.prreview_folder(job),
        "findings": 0 if status == "accepted" else 1, "blocking": 0 if status == "accepted" else 1, "notes": "x"}}))
    gate = gates.GateResult(job.name, counts={"build": 0, "depcheck": 0 if gate_ok else 1, "readme-ids-missing": 0})
    ws.extra.update(pr=905, head=H1, owner="carol", gate=gate, readme=ws.workdir / "README.md")
    return lane, ws


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(lane_base.Lane, "sleep", staticmethod(lambda s: None))
    return tmp_path


def test_an_acceptance_is_one_comment_with_the_approve_line_and_the_label(review_env):
    gh = LabelGH(labels=["awaiting-review"])
    ctx = lane_ctx(review_env, gh=gh)
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    summary = lane.finish(job, ctx, ws)
    [comment] = gh.comments
    lines = comment["body"].split("\n")
    assert lines[0] == f"\U0001f916 AI review (codex, gpt-x) of {H1}" and lines[2] == "Verdict: APPROVE"
    assert f"Reviewed head: {H1}" in lines[:6] and "the targeted build succeeded" in comment["body"]
    assert prreviews.prreview_marker(comment["body"]) == {"job": job.id, "head": H1, "verdict": "accepted",
                                                          "agent": "codex", "model": "gpt-x", "worker": "alice-1"}
    assert gh.labels == ["awaiting-review", "ai-approved"]               # the awaiting-* labels are not touched
    assert summary.endswith("labelled ai-approved") and "accepted" in summary
    assert lane_base.read_pending(ctx, job)["done"] == ["content-comment"]


def test_changes_asked_for_take_the_label_off(review_env):
    gh = LabelGH(labels=["ai-approved", "awaiting-author"])
    ctx = lane_ctx(review_env, gh=gh)
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "needs_changes")
    assert lane.check(job, ctx, ws) == []
    lane.finish(job, ctx, ws)
    [comment] = gh.comments
    assert comment["body"].split("\n")[2] == "Verdict: CHANGES NEEDED" and "1. **F01** (P1)" in comment["body"]
    assert gh.labels == ["awaiting-author"]


def test_an_accepted_head_that_moved_meanwhile_is_not_labelled(review_env):
    gh = LabelGH(head=H2)
    ctx = lane_ctx(review_env, gh=gh)
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    assert lane.finish(job, ctx, ws).endswith(f"not labelled ai-approved: #905 moved to {H2[:7]}")
    assert len(gh.comments) == 1 and gh.labels == []


def test_a_dry_run_writes_nothing(review_env):
    gh = LabelGH()
    ctx = lane_ctx(review_env, gh=gh, dry_run=True)
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    assert lane.finish(job, ctx, ws) == "[dry-run] would comment on #905 (accepted)"
    assert gh.writes == [] and lane_base.read_pending(ctx, job) is None


def test_a_lost_comment_answer_is_found_and_not_posted_twice(review_env):
    gh = LabelGH(fail=["lose"])
    ctx = lane_ctx(review_env, gh=gh)
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    lane.finish(job, ctx, ws)
    assert len(gh.comments) == 1 and gh.labels == ["ai-approved"]


@pytest.mark.parametrize("status,report,gate_ok,problem", [
    ("accepted", "no table", True, "adversarial table"),
    ("accepted", "blocking", True, "an acceptance leaves P3 notes only"),
    ("accepted", "", False, "an acceptance needs the worker's gate to pass at this head"),
    ("needs_changes", "no findings", True, "a verdict that asks for changes writes each finding"),
    ("accepted", "wrong head line", True, "report: first line must be '# Review of Foo (pull request #905 at 1111111)'"),
])
def test_the_checks_refuse_what_the_board_would_not_take(review_env, monkeypatch, status, report, gate_ok, problem):
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, status, gate_ok)
    text = ws.report.read_text()
    text = {"no table": text.replace(ADVERSARIAL, ""), "blocking": text.replace("## Verified sound", FINDING + "## Verified sound"),
            "no findings": text.replace(FINDING, ""), "wrong head line": text.replace("# Review of Foo", "# Review of Bar")
            }.get(report, text)
    ws.report.write_text(text)
    assert any(problem in p for p in lane.check(job, ctx, ws)), lane.check(job, ctx, ws)


def test_a_verdict_must_name_the_pull_request_and_head_of_its_job():
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    good = {"review": {"status": "accepted", "reviewer": "independent-review-PRREVIEW-Foo~1", "head": H1, "pr": 905}}
    assert lane_prreview.prreview_verdict_problems(good, job) == []      # a job filed before folders names none
    bad = {"review": {**good["review"], "head": H2, "pr": 906}}
    assert lane_prreview.prreview_verdict_problems(bad, job) == [f"verdict file: head must be {H1} (the head reviewed)",
                                                                 "verdict file: pr must be 905"]
    nested = new_job("PRREVIEW-CompactGroups~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "CompactGroups",
                                                         "folder": "RepresentationTheory/CompactGroups"})
    record = {"review": {**good["review"], "reviewer": "independent-review-PRREVIEW-CompactGroups~1"}}
    assert lane_prreview.prreview_verdict_problems(record, nested) == [
        "verdict file: folder must be RepresentationTheory/CompactGroups (the roadmap folder reviewed)"]
    record["review"]["folder"] = "RepresentationTheory/CompactGroups"
    assert lane_prreview.prreview_verdict_problems(record, nested) == []


def test_the_intake_takes_a_verdict_naming_its_job_head_and_creates_nothing_after_it():
    job = review_job(61, 1, 905, H1, "submitted")
    path = board.prreview_verdict_path("packages", job)
    verdict = {"review": {"status": "accepted", "reviewer": "independent-review-PRREVIEW-Foo~1", "head": H1, "pr": 905}}
    texts = {"packages/Foo/reports/PRREVIEW-Foo~1.md": "# Review of Foo (pull request #905 at 1111111)\n",
             path: json.dumps(verdict)}
    ctx = SimpleNamespace(cfg=example_config(), gh=ContentGH(), board=FakeBoard(jobs=[job]))
    pr = {"head": {"repo": {"full_name": "alice/TauCetiRoadmapJobs"}, "sha": "f" * 40}, "user": {"login": "alice"}}
    assert intake.deliverable_problems(ctx, job, pr, texts, [job]) == []
    texts[path] = json.dumps({"review": {**verdict["review"], "head": H2}})
    assert intake.deliverable_problems(ctx, job, pr, texts, [job]) == [
        f"verdict file: head must be {H1} (the head reviewed)"]
    assert intake.deliverable_problems(ctx, job, pr, {k: v for k, v in texts.items() if k != path}, [job]) == [
        f"{path} is missing"]
    assert intake.advance(ctx, job, intake.outcome_for(ctx, job, pr, [job]), [job]) == "created nothing"
    assert ctx.board.created == []


# --- a nested roadmap, one folder of several ----------------------------------------------------------

NESTED = {"pr": 905, "head": H1, "owner": "carol", "name": "CompactGroups", "folder": "RepresentationTheory/CompactGroups",
          "roadmaps": ["Foo", "RepresentationTheory/CompactGroups", "RepresentationTheory/LieGroups"]}


def test_the_brief_of_a_nested_roadmap_names_its_folder_module_and_sibling_folders(dry_ctx):  # noqa: F811
    job = new_job("PRREVIEW-CompactGroups~1", inputs={**NESTED, "head": "abc1234" + "0" * 33})
    job.number = 7
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)
    folder = ws.workdir / "TauCetiRoadmap" / "RepresentationTheory" / "CompactGroups"
    assert ws.extra["folder"] == folder and ws.extra["readme"] == folder / "README.md"
    (folder / "Sub").mkdir(parents=True)
    for rel in ("Suggested.lean", "AbelianFourierTargets.lean", "Sub/Helper.lean"):
        (folder / rel).write_text("import Mathlib\n")
    ws.extra["leans"] = lane_prreview.folder_leans(folder)
    values = {**lane.common_values(job, dry_ctx, ws), **lane.values(job, dry_ctx, ws)}
    assert values["FOLDER"] == "TauCetiRoadmap/RepresentationTheory/CompactGroups"
    assert values["BUILD"] == "lake build TauCetiRoadmap.RepresentationTheory.CompactGroups.Suggested"
    assert "gate " in values["GATE"] and "--folder RepresentationTheory/CompactGroups" in values["GATE"]
    files = values["FILES"]
    assert [line[3:-1] for line in files.split("\n") if line.startswith("- `")] == [
        str(folder / f) for f in ("README.md", "Suggested.lean", "AbelianFourierTargets.lean", "Sub/Helper.lean")]
    assert "Lean form rules below apply to each `.lean` file listed" in files
    assert "`TauCetiRoadmap/Foo/`, `TauCetiRoadmap/RepresentationTheory/LieGroups/`" in files
    assert "`TauCetiRoadmap/RepresentationTheory/` (the family's index)" in files
    assert values["OUTSIDE_CHECK"].endswith(
        "-- . ':!TauCetiRoadmap/RepresentationTheory/CompactGroups' ':!TauCetiRoadmap/Foo' "
        "':!TauCetiRoadmap/RepresentationTheory/LieGroups' ':(exclude,glob)TauCetiRoadmap/RepresentationTheory/*'")
    text = prompts.render("prreview", values)
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", text) and "You review ONE roadmap folder" in text
    assert ("first line `# Review of CompactGroups (pull request #905 at abc1234)`, then this second line: "
            "Folder: `TauCetiRoadmap/RepresentationTheory/CompactGroups/`, then") in text
    assert '"folder": "RepresentationTheory/CompactGroups"' in text and "never approves or merges" in text


def test_a_roadmap_with_its_own_readme_below_keeps_that_folder_out_of_its_lean_files(tmp_path):
    folder = tmp_path / "Foo"
    (folder / "Inner").mkdir(parents=True)
    (folder / "Inner" / "README.md").write_text("# Inner\n")
    (folder / "Inner" / "Suggested.lean").write_text("")
    (folder / "ATargets.lean").write_text("")
    assert lane_prreview.folder_leans(folder) == [folder / "Suggested.lean", folder / "ATargets.lean"]


def test_the_gate_of_a_nested_review_checks_its_folder_and_takes_the_sibling_folders_as_the_pull_requests(
        dry_ctx, monkeypatch):  # noqa: F811
    job = new_job("PRREVIEW-CompactGroups~1", inputs=NESTED)
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)
    seen = {}

    def run_gate(worktree, name, opts, base="", build_cmd=""):
        seen.update(name=name, opts=opts)
        return gates.GateResult(name, counts={"build": 0})
    monkeypatch.setattr(lane_prreview.gates, "run_gate", run_gate)
    ctx = dataclasses.replace(dry_ctx, dry_run=False)
    assert lane.review_gate(job, ctx, ws).ok
    opts = seen["opts"]
    assert seen["name"] == "CompactGroups" and opts.folder == "RepresentationTheory/CompactGroups"
    changed = ["TauCetiRoadmap/RepresentationTheory/CompactGroups/README.md", "TauCetiRoadmap/Foo/README.md",
               "TauCetiRoadmap/RepresentationTheory/LieGroups/X.lean", "TauCetiRoadmap/RepresentationTheory/README.md",
               "TauCetiRoadmap/RepresentationTheory/Other/README.md", "TauCetiRoadmap/Bar/README.md", "lakefile.toml"]
    assert gates.outside_folder(changed, "TauCetiRoadmap/RepresentationTheory/CompactGroups", opts.outside_folder_allow) == [
        "TauCetiRoadmap/RepresentationTheory/Other/README.md", "TauCetiRoadmap/Bar/README.md", "lakefile.toml"]


def test_a_nested_review_writes_its_folder_under_the_head_line_and_in_its_verdict(review_env, monkeypatch):
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = new_job("PRREVIEW-CompactGroups~1", inputs=NESTED)
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    second = "Folder: `TauCetiRoadmap/RepresentationTheory/CompactGroups/`"
    assert f"report: the line after the first must be '{second}'" in lane.check(job, ctx, ws)
    head, rest = ws.report.read_text().split("\n", 1)
    ws.report.write_text(f"{head}\n{second}\n{rest}")
    assert lane.check(job, ctx, ws) == []
    body = lane.comment_body(job, ctx, ws)
    assert "Roadmap folder: `TauCetiRoadmap/RepresentationTheory/CompactGroups/`" in body and second not in body
    assert "also adds or changes 2 other roadmap folders, each reviewed on its own" in body
    # The worker records the folder when the agent left it out of the verdict.
    path = lane.verdict_file(job, ctx, ws)
    data = json.loads(path.read_text())
    del data["review"]["folder"]
    path.write_text(json.dumps(data))
    assert path in lane.scrub_outputs(job, ctx, ws)
    assert json.loads(path.read_text())["review"]["folder"] == "RepresentationTheory/CompactGroups"


def test_an_acceptance_labels_the_pull_request_only_once_its_other_folders_accept(review_env):
    gh = LabelGH()
    ctx = lane_ctx(review_env, gh=gh)
    job = new_job("PRREVIEW-CompactGroups~1", inputs={**NESTED, "roadmaps": ["Foo", "RepresentationTheory/CompactGroups"]})
    foo = review_job(70, 1, 905, H1, "done", folder="Foo")
    ctx.board = FakeBoard(jobs=[foo, review_job(71, 1, 906, H1, "done", name="Foo", folder="Foo")])
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    assert lane.finish(job, ctx, ws).endswith("labelled later: 1 folder not yet accepted")
    assert gh.labels == [] and len(gh.comments) == 1
    gh.files.update(verdict_file(foo, "accepted", H1))
    assert lane.update_label(job, ctx, 905, H1, "accepted") == "labelled ai-approved" and gh.labels == ["ai-approved"]
    # One folder asking for changes takes the label off, whatever the others say.
    assert lane.update_label(job, ctx, 905, H1, "needs_changes") == "ai-approved removed" and gh.labels == []


# --- a run that stops after its comment is finished without a second one -----------------------------------

class ReviewHub(Hub):
    """The submission hub, with the content pull request's labels."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.labels = []

    def api(self, path, paginate=False):
        if path == f"repos/{CONTENT}/issues/905/labels":
            return [{"name": x} for x in self.labels]
        return super().api(path, paginate)

    def write(self, method, path, payload=None):
        if path == f"repos/{CONTENT}/issues/905/labels":
            self.labels += payload["labels"]
            return {}
        return super().write(method, path, payload)


@needs_git
def test_a_review_whose_run_stopped_after_its_comment_is_submitted_without_a_second_comment(
        hub, monkeypatch, no_agent, tmp_path):  # noqa: F811
    rh = ReviewHub.__new__(ReviewHub)
    rh.__dict__.update(hub.__dict__, labels=[])
    work = tmp_path / "author"
    sh(tmp_path, "clone", "-q", "-b", ROADMAP_BRANCH, str(rh.content), str(work))
    (work / ".gitignore").write_text(".lake/\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-q", "-m", "ignore the Lake folder")
    sh(work, "push", "-q", "origin", f"HEAD:refs/heads/{ROADMAP_BRANCH}")
    head = rh.head(905)
    passing = gates.GateResult("Foo", counts={"build": 0, "depcheck": 0, "readme-ids-missing": 0})
    monkeypatch.setattr(lane_prreview.gates, "run_gate", lambda *a, **k: passing)
    monkeypatch.setattr(lane_prreview, "open_roadmap_prs", lambda ctx: [])
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": head, "owner": "carol", "name": "Foo"})
    job.number = 61

    ctx = process(rh)
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    assert ws.workdir.name == "Foo" and lane_base.pr_head(ctx, CONTENT, 905) == head
    ws.report.write_text(REPORT.format(h7=head[:7], v="accepted", findings=""))
    lane.verdict_file(job, ctx, ws).write_text(json.dumps({"review": {
        "status": "accepted", "reviewer": f"independent-review-{job.id}", "head": head, "pr": 905,
        "date": "2026-10-27", "findings": 0, "blocking": 0, "notes": "x"}}))
    assert lane.validate(job, ctx, ws) == []
    rh.fail("/issues/905/comments", "crash")
    with pytest.raises(KeyboardInterrupt):
        lane.deliver(job, ctx, ws)
    assert len(rh.comments[905]) == 1 and lane_base.read_pending(ctx, job)["outstanding"][0] == "content-comment"

    ctx = process(rh)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    [comment] = rh.comments[905]
    assert prreviews.prreview_marker(comment["body"])["head"] == head and "\nVerdict: APPROVE\n" in comment["body"]
    assert rh.labels == ["ai-approved"]
    [opened] = rh.opened
    assert "PRREVIEW-Foo~1" in opened["body"] and ctx.board.states == [(job.id, "submitted")]
    assert lane_base.read_pending(ctx, job) is None


def test_an_archive_pull_request_is_a_chore_by_its_title():
    from tauceti_roadmap_worker.intake import CHORE_TITLE
    assert CHORE_TITLE.match("Archive GlobalNumberFields after ray-class naturality lands")
    assert CHORE_TITLE.match("chore: bump TauCeti to ab00ba392")
    assert not CHORE_TITLE.match("Archived-torsion Galois representations: a roadmap")
    assert not CHORE_TITLE.match("Roadmap: archive geometry")

