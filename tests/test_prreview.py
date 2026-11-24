"""The AI review of every roadmap pull request (PRREVIEW): the job kind, the `[prreview]` table, the
`board sync` step that files one review per new head, retires stale ones and keeps the `ai-found-no-errors`
label honest, and the lane that reviews a head read-only, posts one comment and labels an accepted
head. The final approval stays a human's."""

import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import (
    board,
    cli,
    config,
    gates,
    intake,
    prbatch,
    prompts,
    prreviews,
    replies,
    summary,
    worker,
)
from tauceti_roadmap_worker.board import Outcome, new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import Context, LaneError, lane_for
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import pr as lane_pr
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


class SeenLane:
    """A lane every job is ineligible for: the worker's log then lists the jobs in the order it tries them."""

    def eligible(self, job, ctx):
        return "seen"


def test_a_worker_takes_the_re_reviews_of_a_repaired_head_first_within_the_review_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))

    def job(jid, number, origin="explorer:EXPLORE-1", **inputs):
        made = new_job(jid, inputs={"origin": origin, **inputs})
        made.number, made.state = number, "available"
        return made
    jobs = [job("ERRATA-Eee", 1), job("PRREVIEW-Aaa~1", 2, rereview=False), job("RT-Foo~1a", 3, pr=905),
            job("PRREVIEW-Bbb~2", 4, rereview=True, previous_head=H1, previous_job="PRREVIEW-Bbb~1"),
            job("PRREVIEW-Ccc~1", 5), job("GATE-Foo~2", 6, pr=905),
            job("PRREVIEW-Ddd~3", 7, rereview=True, previous_head=H2, previous_job="PRREVIEW-Ddd~2"),
            job("PRREVIEW-Hhh~1", 8, origin="target:9", rereview=False), job("FIX-RT-Foo~1", 9, pr=905),
            job("PRREVIEW-Iii~2", 10, origin="target:9", rereview=True, previous_head=H1, previous_job="PRREVIEW-Iii~1")]
    fake = FakeBoard(jobs=jobs)
    ctx = Context(cfg=example_config(), gh=None, board=fake, claims=None, worker_id="w1", operator="alice",
                  agent="codex", model="m", dry_run=True)
    monkeypatch.setattr(worker, "lane_for", lambda j: SeenLane())
    lines = []
    assert worker.work_once(ctx, None, log=lines.append).status == "idle"
    # The human-filed tier first, the stages in their order, and within PRREVIEW the re-reviews first.
    assert [line.split()[1].rstrip(":") for line in lines] == [
        "PRREVIEW-Iii~2", "PRREVIEW-Hhh~1", "GATE-Foo~2", "FIX-RT-Foo~1", "PRREVIEW-Bbb~2", "PRREVIEW-Ddd~3",
        "PRREVIEW-Aaa~1", "PRREVIEW-Ccc~1", "RT-Foo~1a", "ERRATA-Eee"]
    # Without the re-reviews the order of the other kinds is what it was.
    plain = [j for j in jobs if not j.inputs.get("rereview")]
    assert [j.id for j in rank(plain)] == ["PRREVIEW-Hhh~1", "GATE-Foo~2", "FIX-RT-Foo~1", "PRREVIEW-Aaa~1",
                                           "PRREVIEW-Ccc~1", "RT-Foo~1a", "ERRATA-Eee"]
    flat = [j.id for j in rank(jobs, human_first=False)]
    assert flat[2:6] == ["PRREVIEW-Bbb~2", "PRREVIEW-Ddd~3", "PRREVIEW-Iii~2", "PRREVIEW-Aaa~1"]


def test_a_review_delivers_its_report_and_verdict_and_releases_nothing():
    job = new_job("PRREVIEW-Foo~2", inputs={"pr": 12, "head": H1, "owner": "carol", "name": "Foo"})
    wanted = {"packages/Foo/reports/PRREVIEW-Foo~2.md", "packages/Foo/reports/PRREVIEW-Foo~2.json"}
    assert board.required_paths(job, "packages") == wanted
    # no order; the manifest and the plan (its gaps) only for the in-job repair of a pool author's roadmap
    assert board.allowed_paths(job, "packages", "merge-order/ORDER.txt") == wanted | {
        "packages/Foo/sources.toml", "packages/Foo/design/plan.json"}
    assert board.gaps_only(job)
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
            cfg.prreview.skip_authors, cfg.prreview.max_folders, cfg.prreview.small_change_lines,
            cfg.prreview.diff_cap_lines) == (False, "ai-found-no-errors", 3, [], [], 10, 150, 2000)
    text = ('[project]\nname = "P"\n[prreview]\nenabled = true\nlabel = "ai-ok"\nper_run = 5\nauthors = ["carol"]\n'
            'skip_authors = ["dave"]\nmax_folders = 4\nsmall_change_lines = 0\ndiff_cap_lines = 300\n')
    parsed = config.parse(text, "x.toml").prreview
    assert (parsed.enabled, parsed.label, parsed.per_run, parsed.authors, parsed.skip_authors,
            parsed.max_folders, parsed.small_change_lines, parsed.diff_cap_lines) == (
        True, "ai-ok", 5, ["carol"], ["dave"], 4, 0, 300)
    for bad, why in (("per_run = -1", "prreview.per_run"), ('authors = ["not a login"]', "prreview.authors"),
                     ('enabled = true\nlabel = ""', "prreview.label"), ('per_run = "3"', "expected int"),
                     ("max_folders = 0", "prreview.max_folders"), ("small_change_lines = -1", "prreview.small_change_lines"),
                     ("diff_cap_lines = 0", "prreview.diff_cap_lines")):
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
    ("Family/Toy", None, "TauCetiRoadmap.Family.Toy.Suggested", 0),       # the family arrives with its own topic
    ("Family/Toy", "Family/Toy", "TauCetiRoadmap.Family.Toy.Suggested", 1),   # a nested roadmap carries none
])
def test_the_gate_reads_a_nested_folder_when_told_and_builds_its_dotted_module(tmp_path, monkeypatch, folder, metadata_at,
                                                                               module, missing):
    root = gate_tree(tmp_path, folder or "Toy", metadata_at)
    built = []
    monkeypatch.setattr(gates, "build_problems", lambda worktree, command, timeout=0, folder="": built.append(command) or [])
    result = gates.run_gate(root, "Toy", gates.GateOptions(folder=folder), build_cmd="lake build {module}")
    assert built == [f"lake build {module}"] and result.counts["metadata"] == missing
    assert "missing-files" not in result.counts
    assert gates.GateOptions().folder == "" and gates.roadmap_module("TauCetiRoadmap", "Toy") == "TauCetiRoadmap.Toy.Suggested"


def test_the_topic_file_belongs_to_the_directory_under_the_active_directory(tmp_path):
    """The content repository's topic check reads `metadata.toml` only from a roadmap directory directly
    under the active directory and rejects one anywhere below. A repair once answered `metadata.toml
    missing` on a nested roadmap, whose parent was in another pull request, by writing the file into the
    nested folder; the required check failed as soon as the parent reached main."""
    root = gate_tree(tmp_path, "Family/Toy", "Family/Toy")
    nested = gates.metadata_problems(root, "TauCetiRoadmap", "TauCetiRoadmap/Family/Toy")
    assert len(nested) == 1 and nested[0].startswith("TauCetiRoadmap/Family/Toy/metadata.toml: a nested roadmap")
    (root / "TauCetiRoadmap" / "Family" / "README.md").write_text("# Family\n")      # the parent is in the tree
    (root / "TauCetiRoadmap" / "Family" / "Toy" / "metadata.toml").unlink()
    assert gates.metadata_problems(root, "TauCetiRoadmap", "TauCetiRoadmap/Family/Toy") == [
        "TauCetiRoadmap/Family/metadata.toml missing"]
    (root / "TauCetiRoadmap" / "Family" / "metadata.toml").write_text('topic = "math.GR"\n')
    assert gates.metadata_problems(root, "TauCetiRoadmap", "TauCetiRoadmap/Family/Toy") == []
    assert "never add one there" in gates.GATE_RULES["metadata"]


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
    # The reviewer reads what the check protects, in words, and whether it is about substance or form;
    # never the check's label, which a review must not cite.
    assert "## A roadmap folder holds only README.md" in report and "About substance. 4 hits:" in report
    assert "stray-files" not in report and "A false positive is dropped" in report
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


def pull_node(listed, reviews, comments, inline):
    """The batch query's node (`prbatch.query`) for a listed pull request (`node`), with its reviews,
    issue comments and inline comments given as REST gives them."""
    def page(nodes):
        return {"pageInfo": {"hasNextPage": False}, "nodes": nodes}

    def author(item):
        return {"__typename": "User", "login": (item.get("user") or {}).get("login", "")}
    return {
        "number": listed["number"], "isDraft": listed["isDraft"], "state": "OPEN", "headRefOid": listed["headRefOid"],
        "headRefName": f"roadmap/{listed['number']}", "baseRefName": "main", "headRepository": None,
        "author": listed["author"], "labels": page(listed["labels"]["nodes"]),
        "reviews": page([{"databaseId": r["id"], "state": r["state"], "submittedAt": r["submitted_at"],
                          "body": r.get("body") or "", "author": author(r),
                          "commit": {"oid": r["commit_id"]} if r.get("commit_id") else None,
                          "comments": page([{"databaseId": c["id"], "createdAt": c["created_at"], "path": c.get("path"),
                                             "line": c.get("line"), "originalLine": c.get("original_line"),
                                             "body": c.get("body") or "", "author": author(c),
                                             "replyTo": {"databaseId": c["in_reply_to_id"]}
                                             if c.get("in_reply_to_id") else None}
                                            for c in inline if c.get("pull_request_review_id") == r["id"]])}
                         for r in reviews]),
        "comments": page([{"databaseId": c.get("id"), "createdAt": c.get("created_at") or "", "body": c.get("body") or "",
                           "author": author(c)} for c in comments])}


class ContentGH:
    """The content repository's open pull requests (one GraphQL listing), single pull requests by
    REST, main's tree (`tree`: the folders under TauCetiRoadmap/ holding a README.md), the board's main
    (verdict records), and every write; label writes fail with `refuse`. `trees`: (commit, path) -> the
    git object id of that folder there (a folder not listed differs at every commit); "unread": the
    query fails. `entries`: (commit, path) -> the folder's entries {name: id} (by default a README and
    a Suggested.lean whose ids are the folder's own: two folders that differ differ in both); `blobs`:
    (commit, path) -> a file's text at that commit (`prbatch.blob_texts`), "unread": the query fails.
    `reviews` and `inline`: pull request -> its reviews and inline comments as REST gives them; a listed
    pull request's reviews and comments are read by the batch query (`prbatch.PullReader`) too."""

    def __init__(self, nodes=(), pulls=None, files=None, refuse=False, tree=(), comments=None, unreadable=False,
                 compares=None, trees=None, entries=None, blobs=None, reviews=None, inline=None):
        self.nodes, self.pulls, self.files, self.refuse = list(nodes), dict(pulls or {}), dict(files or {}), refuse
        self.comments, self.unreadable = dict(comments or {}), unreadable   # pull request -> its issue comments
        self.reviews, self.inline, self.pull_queries = dict(reviews or {}), dict(inline or {}), 0
        self.compares = dict(compares or {})                                  # (old, new) -> the comparison's files
        self.tree = [f"TauCetiRoadmap/{d}/README.md" for d in tree]
        self.writes, self.listings, self.dry_run, self.fence = [], 0, False, None
        self.trees, self.tree_queries, self.root_queries = trees, 0, 0
        self.entries, self.layout_queries = dict(entries or {}), 0
        self.blobs = blobs if blobs == "unread" else dict(blobs or {})

    def tree_id(self, sha, path):
        return (self.trees or {}).get((sha, path), f"{sha[:8]}:{path}") if self.trees != "unread" else None

    def graphql(self, query, **variables):
        if "fragment PullFields" in query:         # `prbatch.PullReader`: the listed pull requests asked for
            self.pull_queries += 1
            if self.unreadable:
                raise GHError("graphql: 502")
            asked = {int(n) for n in re.findall(r"p(\d+): pullRequest", query)}
            return {"repository": {f"p{n['number']}": pull_node(n, self.reviews.get(n["number"], []),
                                                                self.comments.get(n["number"], []),
                                                                self.inline.get(n["number"], []))
                                   for n in self.nodes if n["number"] in asked}}
        if "{ title body }" in query:              # `prbatch.pull_texts`: the titles and descriptions asked for
            asked = [int(n) for n in re.findall(r"p(\d+): pullRequest", query)]
            return {"repository": {f"p{n}": {"title": self.pulls[n].get("title", ""),
                                             "body": self.pulls[n].get("body", "")} for n in asked if n in self.pulls}}
        roots = re.findall(r'(e\d+): object\(expression: "([0-9a-f]+):"\) \{ \.\.\. on Tree', query)
        if roots:                                  # `prbatch.tree_entries` of root trees (`intake.outside_unchanged`)
            self.root_queries += 1
            if self.trees == "unread":
                raise GHError("graphql: 502")
            return {"repository": {alias: {"entries": [{"name": n, "oid": o} for n, o in self.entries.get(
                (sha, ""), {"TauCetiRoadmap": f"roadmaps-{sha[:8]}", "lakefile.toml": "lakefile"}).items()]}
                for alias, sha in roots}}
        listed = re.findall(r'(e\d+): object\(expression: "([0-9a-f]+):([^"]+)"\) \{ \.\.\. on Tree', query)
        if listed:                                 # `prbatch.tree_entries`
            self.layout_queries += 1
            if self.trees == "unread":
                raise GHError("graphql: 502")
            return {"repository": {alias: {"entries": [{"name": n, "oid": o} for n, o in self.entries.get(
                (sha, path), {"README.md": f"readme-{self.tree_id(sha, path)}",
                              "Suggested.lean": f"lean-{self.tree_id(sha, path)}"}).items()]}
                for alias, sha, path in listed}}
        texts = re.findall(r'(b\d+): object\(expression: "([0-9a-f]+):([^"]+)"\) \{ \.\.\. on Blob', query)
        if texts:                                  # `prbatch.blob_texts`
            self.layout_queries += 1
            if self.blobs == "unread":
                raise GHError("graphql: 502")
            return {"repository": {alias: ({"text": self.blobs[(sha, path)], "isTruncated": False}
                                           if (sha, path) in self.blobs else None) for alias, sha, path in texts}}
        asked = re.findall(r'(t\d+): object\(expression: "([0-9a-f]+):([^"]+)"\) \{ oid \}', query)
        if asked:
            self.tree_queries += 1
            if self.trees == "unread":
                raise GHError("graphql: 502")
            return {"repository": {alias: {"oid": (self.trees or {}).get((sha, path), f"{sha[:8]}:{path}")}
                                   for alias, sha, path in asked}}
        self.listings += 1
        return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": self.nodes}}}

    def api(self, path, paginate=False):
        m = re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path)
        if m and int(m.group(1)) in self.pulls:
            return self.pulls[int(m.group(1))]
        m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)/comments\?per_page=100", path)
        if m and not self.unreadable:
            return list(self.comments.get(int(m.group(1)), []))
        m = re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/(reviews|comments)\?per_page=100", path)
        if m and not self.unreadable:
            return list((self.reviews if m.group(2) == "reviews" else self.inline).get(int(m.group(1)), []))
        m = re.fullmatch(r"repos/[^/]+/[^/]+/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)", path)
        if m and (m.group(1), m.group(2)) in self.compares:
            return {"files": self.compares[(m.group(1), m.group(2))]}
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
    cfg.prreview.text_per_run = 0             # the check rounds of accepted heads' text: their own tests
    for key, value in over.items():
        if key == "pool":
            cfg.roadmap.pool = list(value)
        else:
            setattr(cfg.prreview, key, value)
    fake = FakeBoard(jobs=list(jobs))
    out = intake.sync_pr_reviews(SimpleNamespace(cfg=cfg, gh=gh, board=fake, operator="board-bot", dry_run=False))
    return out, fake


def test_one_review_is_filed_per_new_head_for_any_author_drafts_included():
    out, fake = sync(ContentGH([node(12, H1, author="outsider", draft=True)]))
    [(job, state, _)] = fake.created
    assert job.id == "PRREVIEW-Foo~1" and state == "available" and job.requires_operator == ""
    # A review is of one roadmap folder, which the job names (relative to the active directory).
    assert job.inputs == {"pr": 12, "head": H1, "owner": "outsider", "name": "Foo", "folder": "Foo", "rereview": False}
    assert out == ["#12: PRREVIEW-Foo~1 created"]
    # The head it names is reviewed (whatever became of that review): no second job until the head moves.
    done = review_job(40, 1, 12, H1, "done")
    out, fake = sync(ContentGH([node(12, H1)]), [done])
    assert fake.created == [] and out == []
    out, fake = sync(ContentGH([node(12, H2)]), [done])
    assert [j.id for j, _s, _p in fake.created] == ["PRREVIEW-Foo~2"] and fake.created[0][0].inputs["head"] == H2


def test_a_new_round_is_a_re_review_of_the_head_the_latest_done_review_read():
    first = review_job(40, 1, 12, H1, "done")
    skipped = review_job(41, 2, 12, H2, "done")              # superseded: it reviewed nothing
    skipped.labels.append(board.SUPERSEDED_LABEL)
    elsewhere = review_job(42, 3, 11, H2, "done")            # the same leaf in another pull request
    out, fake = sync(ContentGH([node(12, H3)]), [first, skipped, elsewhere])
    [(job, _s, _p)] = fake.created
    assert job.id == "PRREVIEW-Foo~4" and out == ["#12: PRREVIEW-Foo~4 created"]
    assert {k: job.inputs[k] for k in ("previous_head", "previous_job", "rereview")} == {
        "previous_head": H1, "previous_job": "PRREVIEW-Foo~1", "rereview": True}
    # A done review wins over a later submitted one; a submitted one serves when none is done.
    submitted = review_job(43, 5, 12, H3, "submitted")
    assert intake.previous_review([first, skipped, submitted]) is first
    assert intake.previous_review([skipped, submitted]) is submitted
    assert intake.rereview_inputs([skipped]) == {"rereview": False}
    later = review_job(44, 6, 12, H2, "done")
    assert intake.rereview_inputs([first, later, submitted]) == {"previous_head": H2, "previous_job": "PRREVIEW-Foo~6",
                                                                 "rereview": True}


def pass_job(number, round_, pr, head, state="done"):
    job = review_job(number, round_, pr, head, state)
    job.inputs["pre"] = True
    job.requires_agent, job.requires_model = "claude", "claude-opus-5-5"
    return job


PASSES = {"pre_passes": 2, "pre_model": "claude-opus-5-5", "pool": ["carol"]}


def test_the_first_rounds_of_a_pool_pull_request_are_passes_before_the_final_review():
    """`[prreview] pre_passes`: the first rounds of a folder of a pull request the pool repairs run on
    the pass model and never label; when a pass leaves the head where it was, or after `pre_passes` of
    them, the final review is filed, a full review on the review models, and it alone decides."""
    labels, H4 = f"repos/{REPO}/issues/12/labels", "4" * 40
    # A new head of a pool author's pull request: pass 1, pinned to the pass model.
    out, fake = sync(ContentGH([node(12, H1)]), **PASSES)
    [(job, _state, _p)] = fake.created
    assert (job.inputs.get("pre"), job.requires_agent, job.requires_model) == (True, "claude", "claude-opus-5-5")
    assert out == ["#12: PRREVIEW-Foo~1 created (pass 1 of at most 2 before the final review, on claude-opus-5-5)"]
    # Another author's pull request gets no pass: nobody repairs it between the two reviewers.
    _out, fake = sync(ContentGH([node(12, H1, author="outsider")]), **PASSES)
    assert "pre" not in fake.created[0][0].inputs and fake.created[0][0].requires_model == ""
    # Without the setting nothing changes.
    _out, fake = sync(ContentGH([node(12, H1)]), pool=["carol"])
    assert "pre" not in fake.created[0][0].inputs and fake.created[0][0].requires_agent == ""

    # Pass 1 asked for changes and its repair moved the head: pass 2, a re-review of the repaired head.
    one = pass_job(40, 1, 12, H1)
    out, fake = sync(ContentGH([node(12, H2)], files=verdict_file(one, "needs_changes", H1)), [one], **PASSES)
    [(job, _state, _p)] = fake.created
    assert job.inputs.get("pre") is True and job.inputs["rereview"] is True and job.requires_model == "claude-opus-5-5"
    assert out == ["#12: PRREVIEW-Foo~2 created (pass 2 of at most 2 before the final review, on claude-opus-5-5)"]
    # Pass 2 asked for changes too: the passes are spent, the next round is the final review, in full.
    two = pass_job(41, 2, 12, H2)
    files = {**verdict_file(one, "needs_changes", H1), **verdict_file(two, "needs_changes", H2)}
    out, fake = sync(ContentGH([node(12, H3)], files=files), [one, two], **PASSES)
    [(job, _state, _p)] = fake.created
    assert "pre" not in job.inputs and job.requires_model == "" and job.inputs["rereview"] is False
    assert out == ["#12: PRREVIEW-Foo~3 created (the final review, after 2 passes)"]

    # A pass that asks for nothing does not label: it brings the final review of that head.
    gh = ContentGH([node(12, H1)], files=verdict_file(one, "accepted", H1))
    out, fake = sync(gh, [one], **PASSES)
    assert gh.writes == [] and [j.id for j, _s, _p in fake.created] == ["PRREVIEW-Foo~2"]
    assert "pre" not in fake.created[0][0].inputs and fake.created[0][0].inputs["head"] == H1
    assert out == ["#12: PRREVIEW-Foo~2 created (the final review, after 1 pass)"]
    # A label left on the pull request does not survive a pass either.
    gh = ContentGH([node(12, H1, labels=["ai-found-no-errors"])], files=verdict_file(one, "accepted", H1))
    out, _fake = sync(gh, [one], **PASSES)
    assert ("DELETE", f"{labels}/ai-found-no-errors", {}) in gh.writes
    # A pass whose points were all P3 and whose repair is in settles nothing: the next round is filed.
    minor = {board.prreview_verdict_path("packages", one): json.dumps({"review": {
        "status": "needs_changes", "head": H1, "pr": 12, "folder": "Foo",
        "carried": {"from": H1, "head": H2, "lines": 3, "folders": ["Foo"], "outside": False}}})}
    gh = ContentGH([node(12, H2)], files=minor)
    out, fake = sync(gh, [one], carry_minor=True, **PASSES)
    assert gh.writes == [] and [j.id for j, _s, _p in fake.created] == ["PRREVIEW-Foo~2"]
    assert fake.created[0][0].inputs.get("pre") is True

    # The final review decides, and the rounds after it are final too.
    final = review_job(42, 3, 12, H3, "done")
    gh = ContentGH([node(12, H3)], files={**files, **verdict_file(final, "accepted", H3)})
    out, fake = sync(gh, [one, two, final], **PASSES)
    assert gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})] and fake.created == []
    gh = ContentGH([node(12, H4)], files={**files, **verdict_file(final, "needs_changes", H3)})
    _out, fake = sync(gh, [one, two, final], **PASSES)
    [(job, _state, _p)] = fake.created
    assert "pre" not in job.inputs and job.requires_model == "" and job.inputs["rereview"] is True
    assert job.inputs["previous_job"] == "PRREVIEW-Foo~3"


def test_a_pass_that_asked_for_nothing_brings_the_final_review_when_the_head_moved_in_another_folder():
    """A repair of one roadmap folder moves the head for all of them. A folder whose pass asked for
    nothing, and which holds the files that pass read, gets its final review at the new head."""
    def passed(status="accepted"):
        foo = review_job(41, 1, 1164, H1, "done", folder="Foo")
        foo.inputs["pre"] = True
        bar = review_job(42, 1, 1164, H1, "claimed", name="Bar", folder="Bar")     # under way: no round of Bar
        return foo, bar, verdict_file(foo, status, H1)
    moved_elsewhere = {(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): "foo"}
    foo, bar, files = passed()
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=files, trees=moved_elsewhere), [foo, bar], **PASSES)
    [(job, _state, _p)] = fake.created
    assert (job.id, job.inputs["head"], job.inputs["rereview"]) == ("PRREVIEW-Foo~2", H2, False)
    assert "pre" not in job.inputs and job.requires_model == ""
    assert out == ["#1164: PRREVIEW-Foo~2 created (the final review, after 1 pass)"]
    # The folder's own files changed since the pass (its repair, or an edit): another pass, as before.
    foo, bar, files = passed()
    _out, fake = sync(ContentGH([two_folder_pr(H2)], files=files), [foo, bar], **PASSES)
    assert fake.created[0][0].inputs.get("pre") is True
    # A pass that asked for changes and whose folder did not move (no repair was made) gets another pass.
    foo, bar, files = passed("needs_changes")
    _out, fake = sync(ContentGH([two_folder_pr(H2)], files=files, trees=moved_elsewhere), [foo, bar], **PASSES)
    assert fake.created[0][0].inputs.get("pre") is True
    # Trees GitHub did not give, or the folder rule switched off: another pass.
    foo, bar, files = passed()
    _out, fake = sync(ContentGH([two_folder_pr(H2)], files=files, trees=moved_elsewhere), [foo, bar],
                      carry_unchanged_folders=False, **PASSES)
    assert fake.created[0][0].inputs.get("pre") is True
    foo, bar, files = passed()
    assert intake.passed_folders(SimpleNamespace(cfg=example_config(), gh=ContentGH(files=files, trees="unread")),
                                 REPO, H2, ["Foo"], [foo], None, 5) == set()


def test_a_pass_verdict_never_accepts_or_settles():
    review = {"status": "accepted", "head": H1, "carried": {"head": H2}, "findings": 0}
    assert intake.pass_verdict(review) == {"status": "passed", "head": H1, "findings": 0, "pre": True}
    assert review["status"] == "accepted" and "carried" in review          # the record read is left as it is
    assert intake.pass_verdict({"status": "needs_changes"})["status"] == "needs_changes"
    p = example_config().prreview
    assert intake.review_stage(p, [], True) == "final"                       # off by default
    p.pre_passes, p.pre_model = 3, "claude-opus-5-5"
    one, two = pass_job(40, 1, 12, H1), pass_job(41, 2, 12, H2)
    skipped = pass_job(42, 3, 12, H2)
    skipped.labels.append(board.SUPERSEDED_LABEL)                            # a superseded pass read nothing
    assert intake.review_stage(p, [one, two, skipped], True) == "pre"
    assert intake.review_stage(p, [one, two, pass_job(43, 4, 12, H3)], True) == "final"
    assert intake.review_stage(p, [one, review_job(44, 2, 12, H2, "done")], True) == "final"
    assert intake.review_stage(p, [], False) == "final"
    for bad, why in (("pre_passes = -1", "prreview.pre_passes"), ("pre_passes = 2", "prreview.pre_model"),
                     ('pre_passes = 1\npre_model = "m"\npre_agent = "nobody"', "prreview.pre_agent")):
        with pytest.raises(config.ConfigError, match=re.escape(why)):
            config.parse(f'[project]\nname = "P"\n[prreview]\n{bad}\n', "x.toml")


def test_a_review_an_operator_filed_at_an_accepted_head_is_not_superseded():
    # As for a check of the text: the files are settled by an earlier head, and an ordinary untaken review
    # there is closed, but a review an operator asked for (a second model's reading) stays on the board.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    asked = review_job(44, 2, 1164, H2, "available", folder="Foo")
    asked.inputs["operator_check"] = True
    out, fake = sync(ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, trees=same_trees(H1, H2)),
                     [foo, bar, asked])
    assert fake.states == [] and not any("superseded" in line for line in out)


def test_a_head_reviewed_twice_gets_a_check_round_only_for_a_rewritten_description():
    # 2026-11-10: the second review of an unmoved head asked for a corrected description; the answer corrected
    # it without a push, and the revision stayed unapproved because a head reviewed twice got no further round.
    first, second = review_job(40, 1, 12, H1, "done"), review_job(41, 2, 12, H1, "done")
    second.inputs["dispute_rereview"] = True

    def answer(job, **record):
        return {"id": 9, "user": {"login": "alice"},
                "body": "Fixed in `1111111` (reply to the review, no change needed):\n\n- **R01** (applied) — done.\n"
                        + replies.answered_marker(job, {"comment-8": "abc"}, worker="alice-1", head=H1, **record)}

    def run(comment, jobs, status="needs_changes"):
        gh = ContentGH([node(12, H1)], comments={12: [comment]}, files=verdict_file(second, status, H1))
        return sync(gh, jobs)

    out, fake = run(answer("PRREVIEW-Foo~2", text=True), [first, second])
    [(job, _s, _p)] = fake.created
    assert job.id == "PRREVIEW-Foo~3" and out == ["#12: PRREVIEW-Foo~3 created"]
    assert job.inputs == {"pr": 12, "head": H1, "owner": "carol", "name": "Foo", "folder": "Foo", "rereview": True,
                          "previous_head": H1, "previous_job": "PRREVIEW-Foo~2", "text_rereview": True,
                          "not_worker": "alice-1"}
    # One check round: whatever it finds, the head is then settled.
    third = review_job(42, 3, 12, H1, "done")
    third.inputs["text_rereview"] = True
    assert run(answer("PRREVIEW-Foo~3", text=True), [first, second, third])[1].created == []
    rest = ("#12: no round is due at 1111111 (Foo was reviewed there {} times and the last review does not accept "
            "it): it waits for a push or an operator's round")
    out, fake = run(answer("PRREVIEW-Foo~3", text=True), [first, second, third])
    assert fake.created == [] and out == [rest.format(3)]
    # A second dispute, an answer to the first review only, and an accepted head file nothing; the head that
    # is left unapproved is reported, since nothing else will ever say so.
    out, fake = run(answer("PRREVIEW-Foo~2"), [first, second])
    assert fake.created == [] and out == [rest.format(2)]
    assert run(answer("PRREVIEW-Foo~1", text=True), [first, second])[1].created == []
    assert run(answer("PRREVIEW-Foo~2", text=True), [first, second], status="accepted")[1].created == []


def verdict_read(job, status, head, text):
    """A verdict record that says which text of the pull request its review read (`text`)."""
    files = verdict_file(job, status, head)
    [(path, raw)] = files.items()
    data = json.loads(raw)
    data["review"]["text"] = text
    return {path: json.dumps(data)}


def text_pr(number, title="Roadmap: Foo", body="It builds Foo.\n"):
    return {number: {"number": number, "state": "open", "head": {"sha": H1}, "title": title, "body": body}}


def test_an_accepted_head_whose_text_its_review_did_not_read_gets_one_check_round():
    done = review_job(40, 1, 12, H1, "done")
    pulls = text_pr(12, body="It builds Foo.\n\n<!-- merge-order -->\n**Merge order.** #3 → this pull request.\n"
                             "<!-- /merge-order -->")
    digest = summary.text_digest("Roadmap: Foo", pulls[12]["body"])
    listing = [node(12, H1, labels=["ai-found-no-errors", "awaiting-review"])]
    # The verdict predates the record of the text: one incremental round of the head checks it.
    out, fake = sync(ContentGH(listing, pulls=pulls, files=verdict_file(done, "accepted", H1)), [done], text_per_run=10)
    [(job, _s, _p)] = fake.created
    assert job.inputs == {"pr": 12, "head": H1, "owner": "carol", "name": "Foo", "folder": "Foo", "rereview": True,
                          "previous_head": H1, "previous_job": "PRREVIEW-Foo~1", "text_check": True,
                          "text_digest": digest}
    assert out == [("#12: PRREVIEW-Foo~2 created (a check of its title and description: its review did not record "
                    "it; dependency depth 0)")]
    # The review read this text: nothing. A merge order rewritten by a tool is not the text it read.
    retold = text_pr(12, body=pulls[12]["body"].replace("#3 → this", "#4 → this"))
    out, fake = sync(ContentGH(listing, pulls=retold, files=verdict_read(done, "accepted", H1, digest)), [done],
                     text_per_run=10)
    assert fake.created == [] and out == []
    # The description was edited since the review: a round, for the new text.
    edited = text_pr(12, body="It builds Foo and Bar.\n")
    out, fake = sync(ContentGH(listing, pulls=edited, files=verdict_read(done, "accepted", H1, digest)), [done],
                     text_per_run=10)
    [(job, _s, _p)] = fake.created
    assert job.inputs["text_digest"] == summary.text_digest("Roadmap: Foo", "It builds Foo and Bar.\n")
    assert out[0].endswith("(a check of its title and description: changed since its review; dependency depth 0)")
    # A head and text whose check round finished on a worker that records no digest (an older build) get
    # no second one, and nothing is filed with the setting off.
    check = review_job(41, 2, 12, H1, "done")
    check.inputs.update(text_check=True, text_digest=digest)
    gh = ContentGH(listing, pulls=pulls,
                   files={**verdict_file(done, "accepted", H1), **verdict_file(check, "accepted", H1)})
    assert sync(gh, [done, check], text_per_run=10)[1].created == []
    assert sync(ContentGH(listing, pulls=pulls, files=verdict_file(done, "accepted", H1)), [done])[1].created == []


def test_text_checks_go_first_to_the_pull_requests_that_wait_for_nothing():
    deps = "awaiting-dependencies"
    shapes = {20: ([deps, "awaiting-review"], False, "<!-- depends-on -->\n- [ ] #21 R21\n<!-- /depends-on -->"),
              21: ([deps, "awaiting-review"], False, "Plain."),
              22: (["awaiting-review"], False, "Plain."),
              23: (["awaiting-author"], False, "Plain."),
              24: (["awaiting-review"], True, "Plain.")}
    listing, jobs, files, pulls = [], [], {}, {}
    for i, (n, (labels, draft, body)) in enumerate(sorted(shapes.items())):
        listing.append(node(n, H1, paths=(f"TauCetiRoadmap/R{n}/README.md",), labels=["ai-found-no-errors", *labels],
                            draft=draft))
        job = review_job(60 + i, 1, n, H1, "done", name=f"R{n}", folder=f"R{n}")
        jobs.append(job)
        files.update(verdict_file(job, "accepted", H1))
        pulls.update(text_pr(n, body=body))

    def order(**over):
        out, fake = sync(ContentGH(listing, pulls=pulls, files=files), jobs, **over)
        return [j.inputs["pr"] for j, _s, _p in fake.created], out

    # Waiting for nothing first (awaiting-review, then drafts, then the rest), then by the depth of what they
    # wait for: #21 waits for something the description does not name, #20 for #21.
    got, out = order(text_per_run=5, per_run=10, dependency_label=deps)
    assert got == [22, 24, 23, 21, 20]
    assert [line.rsplit("depth ", 1)[1] for line in out] == ["0)", "0)", "0)", "1)", "2)"]
    got, out = order(text_per_run=2, per_run=10, dependency_label=deps)
    assert got == [22, 24]
    assert out[-1] == ("3 roadmap folder(s) of accepted pull requests wait for a check of their text "
                       "([prreview] text_per_run = 2)")
    # Without the label's name, a pull request waits when its description names an open one.
    assert order(text_per_run=5, per_run=10)[0] == [21, 22, 24, 23, 20]
    # And per_run caps them too.
    assert order(text_per_run=5, per_run=3, dependency_label=deps)[0] == [22, 24, 23]
    # The other rounds come first, within per_run.
    fresh = node(30, H2, paths=("TauCetiRoadmap/R30/README.md",))
    out, fake = sync(ContentGH([*listing, fresh], pulls=pulls, files=files), jobs, text_per_run=5, per_run=1)
    assert [j.inputs["pr"] for j, _s, _p in fake.created] == [30]


def test_a_correction_a_text_check_asks_for_answered_without_a_push_gets_the_check_round():
    done, check = review_job(40, 1, 12, H1, "done"), review_job(41, 2, 12, H1, "done")
    check.inputs.update(text_check=True, text_digest="0" * 16)
    answer = {"id": 9, "user": {"login": "alice"},
              "body": "Fixed in `1111111` (reply to the review):\n\n- **R01** (applied) — the description is "
                      "rewritten.\n" + replies.answered_marker("PRREVIEW-Foo~2", {"comment-8": "abc"}, worker="alice-1",
                                                               head=H1, text=True)}
    gh = ContentGH([node(12, H1)], pulls=text_pr(12), comments={12: [answer]},
                   files=verdict_file(check, "needs_changes", H1))
    _out, fake = sync(gh, [done, check], text_per_run=10)
    [(job, _s, _p)] = fake.created
    assert job.inputs == {"pr": 12, "head": H1, "owner": "carol", "name": "Foo", "folder": "Foo", "rereview": True,
                          "previous_head": H1, "previous_job": "PRREVIEW-Foo~2", "text_rereview": True,
                          "not_worker": "alice-1"}


def test_a_carried_acceptance_has_its_text_checked_against_the_verdict_it_carries():
    foo = review_job(41, 1, 1164, H1, "done", folder="Foo")
    bar = review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    pulls = {1164: {"number": 1164, "state": "open", "head": {"sha": H2}, "title": "Two roadmaps", "body": "Both.\n"}}
    digest = summary.text_digest("Two roadmaps", "Both.\n")
    carried = {1164: [{"id": 1, "body": "Applied the P3 repairs.\n\n" + replies.carried_marker(H1, H2, "maintainer"),
                       "user": {"login": "carol"}}]}
    small = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 5, "deletions": 2}]

    def run(files):
        gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], pulls=pulls, files=files, comments=carried,
                       compares={(H1, H2): small})
        return sync(gh, [foo, bar], carry_accept=True, text_per_run=10)

    out, fake = run({**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)})
    # Each folder's review read the text against its own folder: each is checked, at the current head.
    assert sorted(j.inputs["folder"] for j, _s, _p in fake.created) == ["Bar", "Foo"]
    assert all(j.inputs["head"] == H2 and j.inputs["previous_head"] == H1 and j.inputs["text_check"]
               for j, _s, _p in fake.created)
    assert out[0].startswith("#1164: ai-found-no-errors kept") and "a check of its title and description" in out[1]
    read = {**verdict_read(foo, "accepted", H1, digest), **verdict_read(bar, "accepted", H1, digest)}
    assert run(read)[1].created == []


def test_every_folder_of_a_pull_request_has_its_own_check_of_the_text():
    # A review reads the text against its own folder's change: a false claim about Foo is Foo's to find, so
    # Bar's acceptance of the text does not cover Foo.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    pulls = text_pr(1164, title="Two roadmaps", body="Both.\n")
    digest = summary.text_digest("Two roadmaps", "Both.\n")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    _out, fake = sync(ContentGH([two_folder_pr(H1, ["ai-found-no-errors"])], pulls=pulls, files=accepted), [foo, bar],
                      text_per_run=10)
    assert sorted(j.id for j, _s, _p in fake.created) == ["PRREVIEW-Bar~2", "PRREVIEW-Foo~2"]
    # Bar's check read the text and accepted; Foo's verdict still records none: Foo alone is checked.
    check = review_job(43, 2, 1164, H1, "done", name="Bar", folder="Bar")
    check.inputs.update(text_check=True, text_digest=digest)
    files = {**accepted, **verdict_read(check, "accepted", H1, digest)}
    _out, fake = sync(ContentGH([two_folder_pr(H1, ["ai-found-no-errors"])], pulls=pulls, files=files), [foo, bar, check],
                      text_per_run=10)
    assert [j.id for j, _s, _p in fake.created] == ["PRREVIEW-Foo~2"]


def test_a_later_text_check_gets_its_correction_round_whatever_earlier_rounds_of_the_head_had():
    # An earlier dispute and its accepted text round settled the head; the description was edited since, its
    # check asked for a correction, and the correction was made without a push: it gets its check round.
    first, second = review_job(40, 1, 12, H1, "done"), review_job(41, 2, 12, H1, "done")
    second.inputs["dispute_rereview"] = True
    third = review_job(42, 3, 12, H1, "done")
    third.inputs["text_rereview"] = True
    check = review_job(43, 4, 12, H1, "done")
    check.inputs.update(text_check=True, text_digest="0" * 16)
    answer = {"id": 9, "user": {"login": "alice"},
              "body": "Fixed in `1111111` (reply to the review):\n\n- **R01** (applied) — rewritten.\n"
                      + replies.answered_marker("PRREVIEW-Foo~4", {"comment-8": "abc"}, worker="alice-1", head=H1,
                                                text=True)}
    files = {**verdict_file(third, "accepted", H1), **verdict_file(check, "needs_changes", H1)}
    gh = ContentGH([node(12, H1)], pulls=text_pr(12), comments={12: [answer]}, files=files)
    _out, fake = sync(gh, [first, second, third, check], text_per_run=10)
    [(job, _s, _p)] = fake.created
    assert job.inputs["text_rereview"] is True and job.inputs["previous_job"] == "PRREVIEW-Foo~4"
    # That round is the sequence's last: after it nothing more is filed.
    after = review_job(44, 5, 12, H1, "done")
    after.inputs["text_rereview"] = True
    gh = ContentGH([node(12, H1)], pulls=text_pr(12), comments={12: [answer]}, files=files)
    assert sync(gh, [first, second, third, check, after], text_per_run=10)[1].created == []


def test_a_check_covers_the_text_its_worker_read_not_the_text_it_was_filed_for():
    done = review_job(40, 1, 12, H1, "done")
    text_a, text_b = "It builds Foo.\n", "It builds Foo and Bar.\n"
    digest_a, digest_b = summary.text_digest("Roadmap: Foo", text_a), summary.text_digest("Roadmap: Foo", text_b)
    check = review_job(41, 2, 12, H1, "done")
    check.inputs.update(text_check=True, text_digest=digest_a)
    listing = [node(12, H1, labels=["ai-found-no-errors", "awaiting-review"])]
    # Filed for A, the text was B when the worker fetched it, and it is A again now: A is checked.
    files = {**verdict_file(done, "accepted", H1), **verdict_read(check, "accepted", H1, digest_b)}
    _out, fake = sync(ContentGH(listing, pulls=text_pr(12, body=text_a), files=files), [done, check], text_per_run=10)
    [(job, _s, _p)] = fake.created
    assert job.inputs["text_digest"] == digest_a and job.id == "PRREVIEW-Foo~3"
    # A worker of an older build records no digest: the round it finished counts for the text it was filed for.
    files = {**verdict_file(done, "accepted", H1), **verdict_file(check, "accepted", H1)}
    assert sync(ContentGH(listing, pulls=text_pr(12, body=text_a), files=files), [done, check],
                text_per_run=10)[1].created == []


def test_a_check_of_the_text_waiting_at_a_head_the_folders_settle_is_not_superseded():
    # The files a check round would read are settled by an earlier head (the head only moved, or the folder
    # holds the files its last review accepted), but the text it checks is not: it stays on the board, while an
    # ordinary untaken review there is superseded as before.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}

    def waiting(**inputs):
        job = review_job(44, 2, 1164, H2, "available", folder="Foo")
        job.inputs.update(inputs)
        return job

    check = waiting(text_check=True, text_digest="0" * 16)
    out, fake = sync(ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, trees=same_trees(H1, H2)),
                     [foo, bar, check])
    assert fake.states == [] and not any("superseded" in line for line in out)
    _out, fake = sync(ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, trees=same_trees(H1, H2)),
                      [foo, bar, waiting()])
    assert fake.states == [("PRREVIEW-Foo~2", "done")]
    first = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "needs_changes", H1)}
    repaired = {(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): "foo",
                (H1, "TauCetiRoadmap/Bar"): "bar", (H2, "TauCetiRoadmap/Bar"): "bar-repaired"}
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=first, trees=repaired), [foo, bar, check])
    assert fake.states == [] and not any("superseded" in line for line in out)
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=first, trees=repaired), [foo, bar, waiting()])
    assert out[0] == "#1164: PRREVIEW-Foo~2 superseded"


def test_an_older_builds_check_never_overrides_a_later_verdicts_digest():
    # A check filed for A finished on an older build (no digest) and asked for changes; the correction round,
    # on a newer build, accepted B; the author put A back. A is checked again.
    text_a, text_b = "It builds Foo.\n", "It builds Foo and Bar.\n"
    digest_a, digest_b = summary.text_digest("Roadmap: Foo", text_a), summary.text_digest("Roadmap: Foo", text_b)
    done, legacy = review_job(40, 1, 12, H1, "done"), review_job(41, 2, 12, H1, "done")
    rewrite = review_job(42, 3, 12, H1, "done")
    legacy.inputs.update(text_check=True, text_digest=digest_a)
    rewrite.inputs["text_rereview"] = True
    files = {**verdict_file(done, "accepted", H1), **verdict_file(legacy, "needs_changes", H1),
             **verdict_read(rewrite, "accepted", H1, digest_b)}
    listing = [node(12, H1, labels=["ai-found-no-errors", "awaiting-review"])]
    _out, fake = sync(ContentGH(listing, pulls=text_pr(12, body=text_a), files=files), [done, legacy, rewrite],
                      text_per_run=10)
    [(job, _s, _p)] = fake.created
    assert job.inputs["text_check"] is True and job.inputs["text_digest"] == digest_a and job.id == "PRREVIEW-Foo~4"


def test_a_text_corrected_without_an_answered_record_gets_its_round():
    # 2026-11-17: the check of an accepted head's text asked for a correction, and the author, outside the pool,
    # corrected the description: no push, and no answered record (only a worker's reply carries one). The text
    # is not the one the check read: it gets its round.
    texts = ["It builds Foo.\n", "It builds Foo from Bar.\n", "It builds Foo from Bar and Baz.\n",
             "It builds Foo from Bar, Baz and Qux.\n", "It builds Foo from all of them.\n"]
    digests = [summary.text_digest("Roadmap: Foo", t) for t in texts]
    done, check = review_job(40, 1, 12, H1, "done"), review_job(41, 2, 12, H1, "done")
    check.inputs.update(text_check=True, text_digest=digests[0])
    files = {**verdict_file(done, "accepted", H1), **verdict_read(check, "needs_changes", H1, digests[0])}
    thanks = {12: [{"id": 9, "user": {"login": "carol"}, "body": "Thanks: F01 is corrected in the description."}]}
    rest = ("#12: no round is due at 1111111 (Foo was reviewed there {} times and the last review does not accept "
            "it): it waits for a push or an operator's round")

    def run(body, jobs, records):
        return sync(ContentGH([node(12, H1)], pulls=text_pr(12, body=body), files=records, comments=thanks), jobs)

    out, fake = run(texts[1], [done, check], files)
    [(job, _s, _p)] = fake.created
    assert job.id == "PRREVIEW-Foo~3" and out == ["#12: PRREVIEW-Foo~3 created"]
    assert job.inputs == {"pr": 12, "head": H1, "owner": "carol", "name": "Foo", "folder": "Foo", "rereview": True,
                          "previous_head": H1, "previous_job": "PRREVIEW-Foo~2", "text_rereview": True,
                          "text_digest": digests[1]}
    # The text the check read is still there: nothing reads it again, and the head is reported at rest.
    out, fake = run(texts[0], [done, check], files)
    assert fake.created == [] and out == [rest.format(1)]
    # Each later text gets its round as well, three rounds at most from the check on; the round that read a
    # text is not filed for it again.
    rounds = []
    for i in (1, 2, 3):
        job = review_job(41 + i, 2 + i, 12, H1, "done")
        job.inputs.update(text_rereview=True, text_digest=digests[i])
        rounds.append(job)
        files = {**files, **verdict_read(job, "needs_changes", H1, digests[i])}
        out, fake = run(texts[i], [done, check, *rounds], files)
        assert fake.created == [] and out == [rest.format(1 + i)]
        out, fake = run(texts[i + 1], [done, check, *rounds], files)
        if i < 3:
            [(job, _s, _p)] = fake.created
            assert job.id == f"PRREVIEW-Foo~{3 + i}" and job.inputs["text_digest"] == digests[i + 1]
        else:
            assert fake.created == [] and out == [rest.format(4)]
    # A round that accepts the text ends it.
    accepted = {**files, **verdict_read(rounds[-1], "accepted", H1, digests[3])}
    out, fake = run(texts[4], [done, check, *rounds], accepted)
    assert fake.created == [] and out == ["#12: ai-found-no-errors added (the AI review of 1111111 accepts it)"]


def test_a_first_review_answered_by_a_corrected_text_gets_a_text_round():
    # The head's only review asked for changes and read text A; the description is now B and no reply carries an
    # answered record. The round reads the text (it is no dispute); the same text, a verdict of an older build
    # (no digest) and a text that cannot be read file nothing and say nothing: the head waits for its author.
    text_a, text_b = "It builds Foo.\n", "It builds Foo from Bar.\n"
    digest_a, digest_b = summary.text_digest("Roadmap: Foo", text_a), summary.text_digest("Roadmap: Foo", text_b)
    first = review_job(40, 1, 12, H1, "done")
    read = verdict_read(first, "needs_changes", H1, digest_a)
    out, fake = sync(ContentGH([node(12, H1)], pulls=text_pr(12, body=text_b), files=read), [first])
    [(job, _s, _p)] = fake.created
    assert job.id == "PRREVIEW-Foo~2" and out == ["#12: PRREVIEW-Foo~2 created"]
    assert job.inputs == {"pr": 12, "head": H1, "owner": "carol", "name": "Foo", "folder": "Foo", "rereview": True,
                          "previous_head": H1, "previous_job": "PRREVIEW-Foo~1", "text_rereview": True,
                          "text_digest": digest_b}
    for gh in (ContentGH([node(12, H1)], pulls=text_pr(12, body=text_a), files=read),
               ContentGH([node(12, H1)], pulls=text_pr(12, body=text_b), files=verdict_file(first, "needs_changes", H1)),
               ContentGH([node(12, H1)], files=read)):
        out, fake = sync(gh, [first])
        assert fake.created == [] and out == []


def test_each_folders_text_correction_gets_its_round_whatever_the_other_folder_answered():
    # Both folders' checks asked for a text-only correction at the same head; Foo's answer came first, Bar's
    # last. Each folder's answer is its own: both get their check round.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    foo_check, bar_check = (review_job(43, 2, 1164, H1, "done", folder="Foo"),
                            review_job(44, 2, 1164, H1, "done", name="Bar", folder="Bar"))
    for check in (foo_check, bar_check):
        check.inputs.update(text_check=True, text_digest="0" * 16)

    def answer(n, job_id):
        return {"id": n, "user": {"login": "alice"},
                "body": "Fixed in `1111111` (reply to the review):\n\n- **R01** (applied) — rewritten.\n"
                        + replies.answered_marker(job_id, {"comment-8": "abc"}, worker="alice-1", head=H1, text=True)}

    files = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1),
             **verdict_file(foo_check, "needs_changes", H1), **verdict_file(bar_check, "needs_changes", H1)}
    gh = ContentGH([two_folder_pr(H1)], pulls=text_pr(1164), files=files,
                   comments={1164: [answer(8, "PRREVIEW-Foo~2"), answer(9, "PRREVIEW-Bar~2")]})
    _out, fake = sync(gh, [foo, bar, foo_check, bar_check], text_per_run=10)
    assert sorted((j.id, j.inputs.get("text_rereview")) for j, _s, _p in fake.created) == [
        ("PRREVIEW-Bar~3", True), ("PRREVIEW-Foo~3", True)]


def test_a_check_of_the_text_that_asks_for_changes_ends_a_carried_acceptance():
    # The acceptance of H1 carried to H2; the check of H2's text asked for changes, and its worker could not
    # take the label off. The sync does.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    check = review_job(43, 2, 1164, H2, "done", folder="Foo")
    check.inputs.update(text_check=True, text_digest="0" * 16)
    carried = {1164: [{"id": 1, "body": replies.carried_marker(H1, H2, "maintainer"), "user": {"login": "carol"}}]}
    small = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 1, "deletions": 1}]
    files = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1),
             **verdict_file(check, "needs_changes", H2)}
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], pulls=text_pr(1164), files=files, comments=carried,
                   compares={(H1, H2): small})
    out, _fake = sync(gh, [foo, bar, check], carry_accept=True, text_per_run=10)
    assert ("DELETE", f"repos/{REPO}/issues/1164/labels/ai-found-no-errors", {}) in gh.writes
    assert not any("kept" in line for line in out)


def test_a_rebase_of_a_repaired_head_does_not_check_text_its_reviews_read():
    # Both folders were reviewed at H1 with this text; Bar's P3-only repair left H2; H3 is H2 rebased, with the
    # same folders and the same text: nothing to check.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    pulls = text_pr(1164, title="Two roadmaps", body="Both.\n")
    digest = summary.text_digest("Two roadmaps", "Both.\n")
    minor = json.loads(verdict_file(bar, "needs_changes", H1)[board.prreview_verdict_path("packages", bar)])
    minor["review"].update(text=digest, carried={"from": H1, "head": H2, "lines": 4, "folders": ["Bar"],
                                                 "findings": ["D01"]})
    files = {**verdict_read(foo, "accepted", H1, digest),
             board.prreview_verdict_path("packages", bar): json.dumps(minor)}
    for head, trees in ((H2, None), (H3, same_trees(H2, H3))):
        gh = ContentGH([two_folder_pr(head, ["ai-found-no-errors"])], pulls=pulls, files=files, trees=trees)
        out, fake = sync(gh, [foo, bar], carry_minor=True, text_per_run=10)
        assert created(fake) == [] and not any("check of its title" in line for line in out), head


def test_the_text_of_a_head_that_only_moved_is_held_against_the_review_that_accepted_its_files():
    # H1's reviews accepted the files with text A; Foo's review of H2 read other files and text B and asked for
    # changes; H3 holds H1's files again and keeps text B. H1's acceptance stands, and nobody read B against
    # those files: Foo's text is checked.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    later = review_job(43, 2, 1164, H2, "done", folder="Foo")
    digest_a = summary.text_digest("Two roadmaps", "Both.\n")
    digest_b = summary.text_digest("Two roadmaps", "Both, with Foo's new section.\n")
    files = {**verdict_read(foo, "accepted", H1, digest_a), **verdict_read(bar, "accepted", H1, digest_a),
             **verdict_read(later, "needs_changes", H2, digest_b)}
    other = {**same_trees(H1, H3), (H2, "TauCetiRoadmap/Foo"): "repaired", (H2, "TauCetiRoadmap/Bar"): "tree-of-Bar"}
    pulls = text_pr(1164, title="Two roadmaps", body="Both, with Foo's new section.\n")
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], pulls=pulls, files=files, trees=other)
    out, fake = sync(gh, [foo, bar, later], text_per_run=10)
    assert out[0].startswith("#1164: ai-found-no-errors kept")
    checks = sorted((j.inputs["folder"], j.inputs["text_digest"]) for j, _s, _p in fake.created)
    assert checks == [("Bar", digest_b), ("Foo", digest_b)]


def test_the_worker_that_repaired_a_folders_text_does_not_take_its_check_round(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    foo_check, bar_check = (review_job(43, 2, 1164, H1, "done", folder="Foo"),
                            review_job(44, 2, 1164, H1, "done", name="Bar", folder="Bar"))
    for check in (foo_check, bar_check):
        check.inputs.update(text_check=True, text_digest="0" * 16)

    def answer(n, job_id, worker):
        return {"id": n, "user": {"login": "carol"},
                "body": "Fixed in `1111111` (reply to the review):\n\n- **R01** (applied) — rewritten.\n"
                        + replies.answered_marker(job_id, {"comment-8": "abc"}, worker=worker, head=H1, text=True)}

    files = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1),
             **verdict_file(foo_check, "needs_changes", H1), **verdict_file(bar_check, "needs_changes", H1)}
    gh = ContentGH([two_folder_pr(H1)], pulls=text_pr(1164), files=files,
                   comments={1164: [answer(8, "PRREVIEW-Foo~2", "alice-1"), answer(9, "PRREVIEW-Bar~2", "bob-1")]})
    _out, fake = sync(gh, [foo, bar, foo_check, bar_check], text_per_run=10)
    made = {j.inputs["folder"]: j for j, _s, _p in fake.created}
    assert made["Foo"].inputs["not_worker"] == "alice-1" and made["Bar"].inputs["not_worker"] == "bob-1"
    lane = lane_for(made["Foo"])
    assert lane.eligible(made["Foo"], lane_ctx(tmp_path)) == "the worker that repaired this head does not review it"
    # `carry_minor`: each folder's review asked for a P3 correction of the text, read before its repair, and
    # the repairs were text-only (same-head carried records); the folders are settled and their text checked.
    # Each check excludes the worker that repaired its own folder's text.
    foo_review, bar_review = (review_job(45, 3, 1164, H1, "done", folder="Foo"),
                              review_job(46, 3, 1164, H1, "done", name="Bar", folder="Bar"))
    carried = {"from": H1, "head": H1, "lines": 0, "folders": [], "findings": ["P01"]}
    files = {}
    for job, read in ((foo_review, "a" * 16), (bar_review, "b" * 16)):
        verdict = json.loads(verdict_read(job, "needs_changes", H1, read)[board.prreview_verdict_path("packages", job)])
        verdict["review"]["carried"] = carried
        files[board.prreview_verdict_path("packages", job)] = json.dumps(verdict)
    gh = ContentGH([two_folder_pr(H1)], pulls=text_pr(1164), files=files,
                   comments={1164: [answer(8, "PRREVIEW-Foo~3", "alice-1"), answer(9, "PRREVIEW-Bar~3", "bob-1")]})
    _out, fake = sync(gh, [foo_review, bar_review], carry_minor=True, text_per_run=10)
    made = {j.inputs["folder"]: j for j, _s, _p in fake.created}
    assert made["Foo"].inputs["text_check"] and made["Bar"].inputs["text_check"]
    assert made["Foo"].inputs["not_worker"] == "alice-1" and made["Bar"].inputs["not_worker"] == "bob-1"
    assert lane.eligible(made["Foo"], lane_ctx(tmp_path)) == "the worker that repaired this head does not review it"
    # The branch was then rebased to H2, with the same folder files and the repaired text; the replies name H1.
    # The settlements carry to H2 (`carry_unchanged_folders`), and each check there still excludes its repairer.
    gh = ContentGH([two_folder_pr(H2)], pulls=text_pr(1164), files=files, trees=same_trees(H1, H2),
                   comments={1164: [answer(8, "PRREVIEW-Foo~3", "alice-1"), answer(9, "PRREVIEW-Bar~3", "bob-1")]})
    _out, fake = sync(gh, [foo_review, bar_review], carry_minor=True, text_per_run=10)
    made = {j.inputs["folder"]: j for j, _s, _p in fake.created}
    assert made["Foo"].inputs["head"] == H2 and made["Foo"].inputs["text_check"] and made["Bar"].inputs["text_check"]
    assert made["Foo"].inputs["not_worker"] == "alice-1" and made["Bar"].inputs["not_worker"] == "bob-1"
    assert lane.eligible(made["Foo"], lane_ctx(tmp_path)) == "the worker that repaired this head does not review it"


def test_an_old_rejection_brought_back_by_a_reset_does_not_cover_its_text():
    # Foo's review of H1 read text A and asked for changes; its review of H2 (the same folder files) read B and
    # accepted. The branch was reset to H1 and A restored: H2's acceptance carries to the files, and A, which
    # the accepting review did not read, is checked.
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar",
                                                                               folder="Bar")
    again = review_job(43, 2, 1164, H2, "done", folder="Foo")
    text_a, text_b = "Both; Foo proves it all.\n", "Both.\n"
    digest_a, digest_b = summary.text_digest("Two roadmaps", text_a), summary.text_digest("Two roadmaps", text_b)
    files = {**verdict_read(foo, "needs_changes", H1, digest_a), **verdict_read(bar, "accepted", H1, digest_a),
             **verdict_read(again, "accepted", H2, digest_b)}
    gh = ContentGH([two_folder_pr(H1, ["ai-found-no-errors"])], pulls=text_pr(1164, title="Two roadmaps", body=text_a),
                   files=files, trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar, again], text_per_run=10)
    assert out[0].startswith("#1164: ai-found-no-errors kept")
    assert [(j.inputs["folder"], j.inputs["text_digest"]) for j, _s, _p in fake.created] == [("Foo", digest_a)]
    # A check of A at H1 that accepted after H2's review is the later review, and covers A.
    check = review_job(44, 3, 1164, H1, "done", folder="Foo")
    check.inputs.update(text_check=True, text_digest=digest_a)
    gh = ContentGH([two_folder_pr(H1, ["ai-found-no-errors"])], pulls=text_pr(1164, title="Two roadmaps", body=text_a),
                   files={**files, **verdict_read(check, "accepted", H1, digest_a)}, trees=same_trees(H1, H2))
    assert sync(gh, [foo, bar, again, check], text_per_run=10)[1].created == []


def test_the_dependency_depth_cuts_a_cycle_and_ignores_closed_pull_requests():
    listing = [{"pr": n, "labels": ["deps"]} for n in (1, 2, 3)] + [{"pr": 4, "labels": []}]
    bodies = {1: {"body": "Depends on #2."}, 2: {"body": "Depends on #1 and #3."}, 3: {"body": "Depends on #99."},
              4: {"body": "Depends on #1."}}
    depths = intake.dependency_depths(listing, bodies, "deps")
    assert depths[4] == 0 and depths[3] == 1 and depths[2] == 2 and depths[1] == 3


def test_a_new_round_is_numbered_past_the_highest_round_when_a_number_is_missing():
    # Round 2 is gone from the board: a count of the folder's rounds would name round 3, which exists and is
    # done, and the board would hand that finished round back instead of filing one.
    first, third = review_job(40, 1, 12, H1, "done"), review_job(42, 3, 11, H2, "done")
    assert intake.next_review_round([first, third], "Foo") == 4 and intake.next_review_round([], "Foo") == 1
    out, fake = sync(ContentGH([node(12, H3)]), [first, third])
    assert [j.id for j, _s, _p in fake.created] == ["PRREVIEW-Foo~4"] and out == ["#12: PRREVIEW-Foo~4 created"]


def test_the_re_review_of_a_head_a_worker_repaired_is_left_to_another_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    reply = {"id": 9, "user": {"login": "alice"},
             "body": "Fixed in `2222222` (reply to the review):\n\n- **H01** (applied) — done.\n"
                     + replies.answered_marker("PRREVIEW-Foo~1", {"comment-8": "abc"}, worker="alice-1", head=H2)}
    done = review_job(40, 1, 12, H1, "done")
    _out, fake = sync(ContentGH([node(12, H2)], comments={12: [reply]}), [done])
    [(job, _s, _p)] = fake.created
    assert job.inputs["rereview"] is True and job.inputs["not_worker"] == "alice-1"
    lane = lane_for(job)
    assert lane.eligible(job, lane_ctx(tmp_path)) == "the worker that repaired this head does not review it"
    assert lane.eligible(job, dataclasses.replace(lane_ctx(tmp_path), worker_id="alice-2")) == ""
    # A head no reply records (the author pushed it) names no worker; a first review reads no comments.
    _out, fake = sync(ContentGH([node(12, H3)], comments={12: [reply]}), [done])
    assert "not_worker" not in fake.created[0][0].inputs
    _out, fake = sync(ContentGH([node(12, H2)], unreadable=True))
    assert fake.created[0][0].inputs == {"pr": 12, "head": H2, "owner": "carol", "name": "Foo", "folder": "Foo",
                                         "rereview": False}
    # Comments that cannot be read leave the re-review to the next run.
    out, fake = sync(ContentGH([node(12, H2)], unreadable=True), [done])
    assert fake.created == [] and out[0].startswith("#12: not filed this run (its comments could not be read")


@pytest.mark.parametrize("open_job", [review_job(41, 1, 12, H1, "claimed"), review_job(41, 1, 12, H1, "submitted"),
                                      new_job("GATE-Foo~2", inputs={"pr": 12, "operator": "", "owner": "carol"})])
def test_no_review_is_filed_while_a_review_or_a_gate_round_of_the_pull_request_is_open(open_job):
    if open_job.kind == "gate":
        open_job.number, open_job.state = 41, "claimed"
    _out, fake = sync(ContentGH([node(12, H2)]), [open_job])
    assert fake.created == [] and fake.states == []          # a claimed or submitted review is left to finish


def test_a_closed_review_round_left_with_the_label_of_a_state_under_way_does_not_stop_the_next_one():
    stale = review_job(41, 1, 12, H1, "available")
    stale.closed = True
    _out, fake = sync(ContentGH([node(12, H2)]), [stale])
    assert [j.inputs["head"] for j, _s, _p in fake.created] == [H2]


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


def test_a_chore_carries_the_label_while_a_pool_member_records_an_accepting_chore_review_of_its_head():
    """`[prreview] chore_review`: a chore (here a pin bump titled `chore:`) gets no folder review; it is
    labelled when a comment by a pool member records `tauceti-roadmap-chore-review {head, status:
    accepted}` for its current head, and loses the label when the head moves or the review blocks."""
    bump = node(1800, H1, title="chore: bump the Tau Ceti pin",
                paths=[f"TauCetiRoadmap/R{k:02d}/Suggested.lean" for k in range(30)] + ["lake-manifest.json"])
    names = [f"R{k:02d}" for k in range(30)]
    labels = f"repos/{REPO}/issues/1800/labels"
    ok = {1800: [{"id": 1, "body": "🤖 AI review of the bump\n\n" + replies.chore_marker(H1, "accepted", "claude/fable"),
                  "user": {"login": "CBirkbeck"}}]}
    # Off: a chore is simply not reviewed.
    gh = ContentGH([bump], comments=ok, tree=names)
    out, fake = sync(gh)
    assert gh.writes == [] and fake.created == [] and out == ["#1800: not reviewed (chore by its title)"]
    # On: the accepting record of the current head labels it; no folder review is filed.
    gh = ContentGH([bump], comments=ok, tree=names)
    out, fake = sync(gh, chore_review=True, pool=["CBirkbeck"])
    assert fake.created == [] and gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})]
    assert f"#1800: ai-found-no-errors added (the AI review of {H1[:7]} accepts it)" in out
    # A record by a login outside the pool counts for nothing.
    stranger = {1800: [{**ok[1800][0], "user": {"login": "mallory"}}]}
    gh = ContentGH([bump], comments=stranger, tree=names)
    out, fake = sync(gh, chore_review=True, pool=["CBirkbeck"])
    assert gh.writes == [] and not any("ai-found-no-errors" in line for line in out)
    # A blocking record: no label; a labelled chore whose head moved loses it.
    blocks = {1800: [{"id": 2, "body": replies.chore_marker(H1, "needs_changes"), "user": {"login": "CBirkbeck"}}]}
    gh = ContentGH([bump], comments=blocks, tree=names)
    out, fake = sync(gh, chore_review=True, pool=["CBirkbeck"])
    assert gh.writes == [] and not any("ai-found-no-errors" in line for line in out)
    moved = node(1800, H2, title="chore: bump the Tau Ceti pin", labels=["ai-found-no-errors"],
                 paths=[f"TauCetiRoadmap/R{k:02d}/Suggested.lean" for k in range(30)])
    gh = ContentGH([moved], comments=ok, tree=names)
    out, fake = sync(gh, chore_review=True, pool=["CBirkbeck"])
    assert gh.writes == [("DELETE", f"{labels}/ai-found-no-errors", {})]
    assert f"#1800: ai-found-no-errors removed (not reviewed: chore by its title; no chore review of {H2[:7]})" in out


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
    assert gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})]
    assert out == [f"#1164: ai-found-no-errors added (the AI review of {H1[:7]} accepts all 2 roadmap folders)"]
    # Labelled and both accepted: kept. A later review of one folder that blocks takes it off.
    gh = ContentGH([two_folder_pr(H1, ["ai-found-no-errors"])],
                   files={**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)})
    assert sync(gh, [foo, bar])[0] == [] and gh.writes == []
    blocks = review_job(43, 2, 1164, H1, "done", name="Bar", folder="Bar")
    gh = ContentGH([two_folder_pr(H1, ["ai-found-no-errors"])], files={
        **verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1), **verdict_file(blocks, "needs_changes", H1)})
    out, _fake = sync(gh, [foo, bar, blocks])
    assert gh.writes == [("DELETE", f"{labels}/ai-found-no-errors", {})]
    assert out == [f"#1164: ai-found-no-errors removed (the AI review of {H1[:7]} does not accept it (Bar))"]
    # The head moves: the label goes, and both folders get a review of the new head.
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])])
    out, fake = sync(gh, [foo, bar])
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and created(fake) == [
        ("PRREVIEW-Bar~2", "Bar"), ("PRREVIEW-Foo~2", "Foo")]
    # With it goes the label that says a human approval is all the pull request needs, when one is kept.
    ready = "awaiting-human-approval"
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors", ready])])
    out, _fake = sync(gh, [foo, bar], approval_label=ready)
    assert gh.writes[:2] == [("DELETE", f"{labels}/ai-found-no-errors", {}), ("DELETE", f"{labels}/{ready}", {})]
    assert out[:2] == ["#1164: ai-found-no-errors removed (head moved)", f"#1164: {ready} removed (ai-found-no-errors is off)"]
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors", ready])])
    sync(gh, [foo, bar])                                  # no approval label configured: not this step's
    assert ("DELETE", f"{labels}/{ready}", {}) not in gh.writes
    # A token that may not write the content repository says so.
    gh = ContentGH([two_folder_pr(H1)], files={**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)},
                   refuse=True)
    assert sync(gh, [foo, bar])[0][0].startswith(f"#1164: ai-found-no-errors on {REPO} not added (gh api -X POST")


def test_small_repairs_with_a_carried_record_keep_the_label_and_file_no_review():
    """`[prreview] carry_accept`: a reply recording that H2 carries the acceptance of H1 keeps the label
    on H2 when the folders changed by at most small_change_lines lines, and no review of H2 is filed;
    a bigger change, a change outside the folders, or a chain from an unaccepted head carries nothing."""
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    carried = {1164: [{"id": 1, "body": "Applied the P3 repairs.\n\n" + replies.carried_marker(H1, H2, "maintainer"),
                       "user": {"login": "carol"}}]}
    small = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 5, "deletions": 2},
             {"filename": "TauCetiRoadmap/Bar/README.md", "additions": 3, "deletions": 0}]
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, comments=carried, compares={(H1, H2): small})
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert gh.writes == [] and created(fake) == []
    assert out == [f"#1164: ai-found-no-errors kept (the acceptance of {H1[:7]} carries to {H2[:7]}: 10 lines of repairs "
                   "inside the roadmap folders, no review filed)"]
    # Without the setting the ordinary rule applies: the label goes and both folders are reviewed.
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, comments=carried, compares={(H1, H2): small})
    out, fake = sync(gh, [foo, bar])
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    # Too many lines: nothing carries.
    big = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 200, "deletions": 0}]
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, comments=carried, compares={(H1, H2): big})
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    # A file outside the roadmap folders changed: nothing carries.
    outside = small + [{"filename": "scripts/x.py", "additions": 1, "deletions": 0}]
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, comments=carried, compares={(H1, H2): outside})
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    # A chain of two carries (H1 -> H2 -> H3) is followed back to the accepted head, with the total diff.
    chain = {1164: carried[1164] + [{"id": 2, "body": replies.carried_marker(H2, H3), "user": {"login": "carol"}}]}
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, comments=chain, compares={(H1, H3): small})
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert gh.writes == [] and created(fake) == [] and out[0].startswith("#1164: ai-found-no-errors kept")
    # The record and marker round-trip; a reply without one has none.
    assert replies.carried_record(replies.carried_marker(H1, H2, "w")) == {"from": H1, "head": H2, "worker": "w"}
    assert replies.carried_record("Fixed in `abc`") is None and intake.carried_from([], H2) == ""


def same_trees(*heads, folders=("Foo", "Bar")):
    """The roadmap folders hold the same files at each of `heads`."""
    return {(h, f"TauCetiRoadmap/{f}"): f"tree-of-{f}" for h in heads for f in folders}


def test_a_head_that_only_moved_keeps_the_acceptance_of_the_head_it_came_from():
    """`[prreview] carry_unchanged`: after a rebase or a merge of the base branch every roadmap folder
    holds the files an accepted head held, so the label stays and no review of the new head is filed."""
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    labels = f"repos/{REPO}/issues/1164/labels"
    kept = (f"#1164: ai-found-no-errors kept (the roadmap folders at {H2[:7]} are those of {H1[:7]}, whose acceptance "
            "stands: the head only moved, no review filed)")
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar])
    assert out == [kept] and gh.writes == [] and created(fake) == [] and gh.tree_queries == 1
    # One folder changed with the move: the label goes and that folder is reviewed (the other is what its
    # review accepted, `carry_unchanged_folders`; both are reviewed with that off, as before).
    changed = {**same_trees(H1, H2), (H2, "TauCetiRoadmap/Bar"): "another-tree"}
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, trees=changed)
    out, fake = sync(gh, [foo, bar])
    assert gh.writes == [("DELETE", f"{labels}/ai-found-no-errors", {})]
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and created(fake) == [("PRREVIEW-Bar~2", "Bar")]
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, trees=changed)
    out, fake = sync(gh, [foo, bar], carry_unchanged_folders=False)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    # The earlier head was not accepted: nothing to keep, and the folder that was refused is reviewed.
    refused = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "needs_changes", H1)}
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=refused, trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar])
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and created(fake) == [("PRREVIEW-Bar~2", "Bar")]
    # Switched off: the head's own reviews alone count.
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=accepted, trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar], carry_unchanged=False, carry_unchanged_folders=False)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2 and gh.tree_queries == 0
    # A second move is compared with the reviewed head again.
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, trees=same_trees(H1, H2, H3))
    out, fake = sync(gh, [foo, bar])
    assert gh.writes == [] and created(fake) == [] and out[0].startswith("#1164: ai-found-no-errors kept")
    # A later review of the same files that asked for changes decides: the earlier acceptance is not
    # carried past it, whether or not the other folder was reviewed at that head too.
    later = review_job(43, 2, 1164, H2, "done", folder="Foo")
    asked = {**accepted, **verdict_file(later, "needs_changes", H2)}
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=asked, trees=same_trees(H1, H2, H3))
    out, fake = sync(gh, [foo, bar, later])
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and created(fake) == [("PRREVIEW-Foo~3", "Foo")]
    # One that read other files does not: the head holds what the accepted head held.
    other = {**same_trees(H1, H3), (H2, "TauCetiRoadmap/Foo"): "repaired", (H2, "TauCetiRoadmap/Bar"): "tree-of-Bar"}
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=asked, trees=other)
    out, fake = sync(gh, [foo, bar, later])
    assert gh.writes == [] and created(fake) == [] and out[0].startswith("#1164: ai-found-no-errors kept")


def test_a_label_lost_to_a_move_is_put_back_and_the_untaken_reviews_of_the_moved_head_are_closed():
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    again = review_job(43, 2, 1164, H2, "available", folder="Foo")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    gh = ContentGH([two_folder_pr(H2)], files=accepted, trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar, again])
    assert gh.writes == [("POST", f"repos/{REPO}/issues/1164/labels", {"labels": ["ai-found-no-errors"]})]
    assert out == ["#1164: PRREVIEW-Foo~2 superseded",
                   f"#1164: ai-found-no-errors added (the roadmap folders at {H2[:7]} are those of {H1[:7]}, whose "
                   "acceptance stands: the head only moved)"]
    assert fake.states == [("PRREVIEW-Foo~2", "done")] and created(fake) == []
    assert fake.posted[0][1].startswith(f"Superseded: the roadmap folders of #1164 at {H2[:7]} are those of {H1[:7]}")
    # A review of the moved head that is under way is left to finish, and decides for its folder; the
    # other folder gets no review of a head that only moved, and does of one that changed.
    taken = review_job(43, 2, 1164, H2, "claimed", folder="Foo")
    gh = ContentGH([two_folder_pr(H2)], files=accepted, trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar, taken])
    assert out == [] and gh.writes == [] and created(fake) == []
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=accepted), [foo, bar, taken])
    assert out == ["#1164: PRREVIEW-Bar~2 created"]
    # A finished review of the moved head that asks for changes stands: nothing is put back.
    blocks = review_job(43, 2, 1164, H2, "done", folder="Foo")
    gh = ContentGH([two_folder_pr(H2)], files={**accepted, **verdict_file(blocks, "needs_changes", H2)},
                   trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar, blocks])
    assert gh.writes == [] and created(fake) == []      # nor is the other folder read again: it is as accepted
    gh = ContentGH([two_folder_pr(H2)], files={**accepted, **verdict_file(blocks, "needs_changes", H2)},
                   trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar, blocks], carry_unchanged_folders=False)
    assert gh.writes == [] and gh.tree_queries == 0 and created(fake) == [("PRREVIEW-Bar~2", "Bar")]
    # One that accepts its folder leaves the other folder to the head it came from.
    accepts = {**accepted, **verdict_file(blocks, "accepted", H2)}
    gh = ContentGH([two_folder_pr(H2)], files=accepts, trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar, blocks])
    assert gh.writes == [("POST", f"repos/{REPO}/issues/1164/labels", {"labels": ["ai-found-no-errors"]})]
    assert created(fake) == []


def test_trees_github_did_not_give_change_nothing():
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    for labelled in (["ai-found-no-errors"], []):
        gh = ContentGH([two_folder_pr(H2, labelled)], files=accepted, trees="unread")
        out, fake = sync(gh, [foo, bar])
        assert gh.writes == [] and created(fake) == []
        assert out == [f"#1164: ai-found-no-errors not checked (the roadmap folders at {H2[:7]} could not be compared "
                       "with those of an earlier head)"]
    assert prbatch.tree_ids(ContentGH(trees={(H1, "a/b"): "x"}), REPO, [(H1, "a/b"), (H2, "a/b")], 5) == {
        (H1, "a/b"): "x", (H2, "a/b"): f"{H2[:8]}:a/b"}
    assert prbatch.tree_ids(ContentGH(trees="unread"), REPO, [(H1, "a/b")], 5) == {}


def test_a_folder_its_last_review_settled_is_not_reviewed_again_when_another_folder_moves_the_head():
    """`[prreview] carry_unchanged_folders`: a repair of one roadmap folder moves the head of a pull
    request with several; a folder whose latest review accepted the files it still holds is not read
    again, and counts as accepted at the new head."""
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    first = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "needs_changes", H1)}
    repaired = {(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): "foo", (H3, "TauCetiRoadmap/Foo"): "foo",
                (H1, "TauCetiRoadmap/Bar"): "bar", (H2, "TauCetiRoadmap/Bar"): "bar-repaired",
                (H3, "TauCetiRoadmap/Bar"): "bar-repaired"}
    labels = f"repos/{REPO}/issues/1164/labels"
    # Bar's repair moved the head: Bar alone is reviewed there.
    gh = ContentGH([two_folder_pr(H2)], files=first, trees=repaired)
    out, fake = sync(gh, [foo, bar])
    assert gh.writes == [] and created(fake) == [("PRREVIEW-Bar~2", "Bar")]
    assert gh.tree_queries == 2          # the pull request as a whole (`carry_unchanged`), then folder by folder
    # A review of Foo filed for the new head before, and not taken, is closed; one that was taken is left.
    untaken = review_job(44, 2, 1164, H2, "available", folder="Foo")
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=first, trees=repaired), [foo, bar, untaken])
    assert out == ["#1164: PRREVIEW-Foo~2 superseded", "#1164: PRREVIEW-Bar~2 created"]
    taken = review_job(44, 2, 1164, H2, "claimed", folder="Foo")
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=first, trees=repaired), [foo, bar, taken])
    assert out == ["#1164: PRREVIEW-Bar~2 created"] and fake.states == []
    # Switched off: both are.
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=first, trees=repaired), [foo, bar], carry_unchanged_folders=False)
    assert len(created(fake)) == 2
    # Bar's review of the new head accepts: the label, with Foo accepted where it held these files.
    again = review_job(43, 2, 1164, H2, "done", name="Bar", folder="Bar")
    both = {**first, **verdict_file(again, "accepted", H2)}
    added = f"#1164: ai-found-no-errors added (every roadmap folder at {H2[:7]} holds the files its last review settled)"
    gh = ContentGH([two_folder_pr(H2)], files=both, trees=repaired)
    out, fake = sync(gh, [foo, bar, again])
    assert out == [added] and gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})] and created(fake) == []
    # Labelled: kept. An untaken review of Foo at the new head is closed here too.
    gh = ContentGH([two_folder_pr(H2, ["ai-found-no-errors"])], files=both, trees=repaired)
    out, fake = sync(gh, [foo, bar, again])
    kept = f"#1164: ai-found-no-errors kept (every roadmap folder at {H2[:7]} holds the files its last review settled: no review filed)"
    assert out == [kept] and gh.writes == [] and created(fake) == []
    untaken = review_job(44, 2, 1164, H2, "available", folder="Foo")
    gh = ContentGH([two_folder_pr(H2)], files=both, trees=repaired)
    out, fake = sync(gh, [foo, bar, again, untaken])
    assert out == ["#1164: PRREVIEW-Foo~2 superseded", added] and fake.states == [("PRREVIEW-Foo~2", "done")]
    assert fake.posted[0][1].startswith(f"Superseded: Foo of #1164 at {H2[:7]} holds the files of {H1[:7]}")
    # One that is under way decides for Foo: no label until it ends.
    gh = ContentGH([two_folder_pr(H2)], files=both, trees=repaired)
    out, fake = sync(gh, [foo, bar, again, taken])
    assert out == [] and gh.writes == [] and created(fake) == []
    # The head moves again without touching either folder: the label stays, nothing is filed.
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=both, trees=repaired)
    out, fake = sync(gh, [foo, bar, again])
    assert gh.writes == [] and created(fake) == [] and out[0].startswith("#1164: ai-found-no-errors kept")
    # A later review of Foo that asked for changes is Foo's last word, though it read the same files.
    later = review_job(45, 2, 1164, H2, "done", folder="Foo")
    asked = {**both, **verdict_file(later, "needs_changes", H2)}
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=asked, trees=repaired)
    out, fake = sync(gh, [foo, bar, again, later])
    assert gh.writes == [("DELETE", f"{labels}/ai-found-no-errors", {})] and created(fake) == [("PRREVIEW-Foo~3", "Foo")]
    # GitHub does not give the trees: nothing is known, so nothing is filed and the label is left.
    gh = ContentGH([two_folder_pr(H2)], files=both, trees="unread")
    out, fake = sync(gh, [foo, bar, again])
    assert gh.writes == [] and created(fake) == []
    unknown = f"#1164: ai-found-no-errors not checked (the roadmap folders at {H2[:7]} could not be compared with those "
    assert out == [unknown + "of an earlier head)"]
    gh = ContentGH([two_folder_pr(H2)], files=both, trees="unread")
    out, fake = sync(gh, [foo, bar, again], carry_unchanged=False)
    assert gh.writes == [] and created(fake) == []
    assert out == [unknown + "their last reviews read)"]
    gh = ContentGH([two_folder_pr(H2)], files=first, trees="unread")
    out, fake = sync(gh, [foo, bar], carry_unchanged=False)
    assert gh.writes == [] and created(fake) == [] and out[0].startswith("#1164: ai-found-no-errors not checked")


def test_a_moved_head_keeps_what_small_repairs_carried_to_the_head_it_came_from():
    """The head a carried record names (`carry_accept`) and the head a P3-only repair left
    (`carry_minor`) are accepted heads too: a rebase of either keeps the label."""
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    carried = {1164: [{"id": 1, "body": replies.carried_marker(H1, H2, "maintainer"), "user": {"login": "carol"}}]}
    small = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 5, "deletions": 2}]
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, comments=carried, compares={(H1, H2): small},
                   trees=same_trees(H2, H3))
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert gh.writes == [] and created(fake) == []
    assert out == [f"#1164: ai-found-no-errors kept (the roadmap folders at {H3[:7]} are those of {H2[:7]}, whose acceptance "
                   "stands: the head only moved, no review filed)"]
    # Without the record's setting the repaired head was never accepted.
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, comments=carried, compares={(H1, H2): small},
                   trees=same_trees(H2, H3))
    assert sync(gh, [foo, bar])[0][0] == "#1164: ai-found-no-errors removed (head moved)"
    # carry_minor: Bar's review of H1 found P3 points only and its repair left H2; H3 is H2 rebased.
    minor = json.loads(verdict_file(bar, "needs_changes", H1)[board.prreview_verdict_path("packages", bar)])
    minor["review"]["carried"] = {"from": H1, "head": H2, "lines": 4, "folders": ["Bar"], "findings": ["D01"]}
    files = {**verdict_file(foo, "accepted", H1), board.prreview_verdict_path("packages", bar): json.dumps(minor)}
    gh = ContentGH([two_folder_pr(H3)], files=files, trees=same_trees(H2, H3))
    out, fake = sync(gh, [foo, bar], carry_minor=True)
    assert gh.writes == [("POST", f"repos/{REPO}/issues/1164/labels", {"labels": ["ai-found-no-errors"]})]
    assert created(fake) == [] and "the head only moved" in out[0]


def test_small_repairs_made_after_the_head_moved_keep_the_label():
    """A carried record may start at a head that only moved: the base branch merged into an accepted
    head, then a small repair. The lines counted are the repair's; what earlier records carried into
    the head it moved from counts too, and the whole chain is held to small_change_lines."""
    H4 = "4" * 40
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    accepted = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H1)}
    small = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 5, "deletions": 2}]
    # H1 is accepted, H2 is H1 with the base branch merged, H3 repairs H2 and says so.
    after = {1164: [{"id": 1, "body": replies.carried_marker(H2, H3, "maintainer"), "user": {"login": "carol"}}]}
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, comments=after, compares={(H2, H3): small},
                   trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert gh.writes == [] and created(fake) == []
    assert out == [f"#1164: ai-found-no-errors kept (the acceptance of {H1[:7]} carries to {H3[:7]}: 7 lines of repairs "
                   "inside the roadmap folders, no review filed)"]
    # H2 changed a folder (it did not only move), or moved heads carry nothing: the record holds nothing.
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, comments=after, compares={(H2, H3): small})
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, comments=after, compares={(H2, H3): small},
                   trees=same_trees(H1, H2))
    out, fake = sync(gh, [foo, bar], carry_accept=True, carry_unchanged=False)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    # A repair (H1 -> H2), a merge of the base branch (H3), a second repair (H3 -> H4): both repairs count.
    chain = {1164: [{"id": 1, "body": replies.carried_marker(H1, H2, "maintainer"), "user": {"login": "carol"}},
                    {"id": 2, "body": replies.carried_marker(H3, H4, "maintainer"), "user": {"login": "carol"}}]}
    second = [{"filename": "TauCetiRoadmap/Bar/README.md", "additions": 60, "deletions": 40}]
    gh = ContentGH([two_folder_pr(H4, ["ai-found-no-errors"])], files=accepted, comments=chain,
                   compares={(H1, H2): small, (H3, H4): second}, trees=same_trees(H2, H3))
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert gh.writes == [] and created(fake) == []
    assert out == [f"#1164: ai-found-no-errors kept (the acceptance of {H1[:7]} carries to {H4[:7]}: 107 lines of repairs "
                   "inside the roadmap folders, no review filed)"]
    large = [{"filename": "TauCetiRoadmap/Bar/README.md", "additions": 100, "deletions": 45}]
    gh = ContentGH([two_folder_pr(H4, ["ai-found-no-errors"])], files=accepted, comments=chain,
                   compares={(H1, H2): small, (H3, H4): large}, trees=same_trees(H2, H3))
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    # Records that lead round in a ring of heads holding the same files settle nothing, and end.
    ring = {1164: [{"id": 1, "body": replies.carried_marker(H2, H3, "maintainer"), "user": {"login": "carol"}},
                   {"id": 2, "body": replies.carried_marker(H3, H2, "maintainer"), "user": {"login": "carol"}}]}
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=accepted, comments=ring, compares={(H2, H3): [], (H3, H2): []},
                   trees=same_trees(H2, H3))
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2


def test_small_repairs_made_after_a_p3_only_repair_keep_the_label():
    """A carried record may start at the head a P3-only repair left (`carry_minor`): that head is settled
    without a further review, and the lines of the review's own repair count towards the limit."""
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    minor = json.loads(verdict_file(bar, "needs_changes", H1)[board.prreview_verdict_path("packages", bar)])
    minor["review"]["carried"] = {"from": H1, "head": H2, "lines": 4, "folders": ["Bar"], "findings": ["D01"]}
    files = {**verdict_file(foo, "accepted", H1), board.prreview_verdict_path("packages", bar): json.dumps(minor)}
    record = {1164: [{"id": 1, "body": replies.carried_marker(H2, H3, "maintainer"), "user": {"login": "carol"}}]}
    small = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 5, "deletions": 2}]
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=files, comments=record, compares={(H2, H3): small})
    out, fake = sync(gh, [foo, bar], carry_accept=True, carry_minor=True)
    assert gh.writes == [] and created(fake) == []
    assert out == [f"#1164: ai-found-no-errors kept (the acceptance of {H2[:7]} carries to {H3[:7]}: 11 lines of repairs "
                   "inside the roadmap folders, no review filed)"]
    # Without carry_minor the head the repair left was never settled; too many lines in all carry nothing.
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=files, comments=record, compares={(H2, H3): small})
    out, fake = sync(gh, [foo, bar], carry_accept=True)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    large = [{"filename": "TauCetiRoadmap/Foo/README.md", "additions": 100, "deletions": 47}]
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=files, comments=record, compares={(H2, H3): large})
    out, fake = sync(gh, [foo, bar], carry_accept=True, carry_minor=True)
    assert out[0] == "#1164: ai-found-no-errors removed (head moved)" and len(created(fake)) == 2
    # With text checks on: both reviews read this text at H1, and no review ran at H2, the head the P3-only
    # repair left. The text counts as read through H1's verdicts; a changed text gets its checks.
    digest = summary.text_digest("Two roadmaps", "Both.\n")
    minor["review"]["text"] = digest
    read = {**verdict_read(foo, "accepted", H1, digest),
            board.prreview_verdict_path("packages", bar): json.dumps(minor)}
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=read, comments=record, compares={(H2, H3): small},
                   pulls=text_pr(1164, title="Two roadmaps", body="Both.\n"))
    out, fake = sync(gh, [foo, bar], carry_accept=True, carry_minor=True, text_per_run=10)
    assert created(fake) == [] and out[0].startswith("#1164: ai-found-no-errors kept")
    gh = ContentGH([two_folder_pr(H3, ["ai-found-no-errors"])], files=read, comments=record, compares={(H2, H3): small},
                   pulls=text_pr(1164, title="Two roadmaps", body="Both, edited.\n"))
    _out, fake = sync(gh, [foo, bar], carry_accept=True, carry_minor=True, text_per_run=10)
    assert sorted(f for _id, f in created(fake)) == ["Bar", "Foo"]


def test_a_chore_loses_a_label_put_on_by_hand():
    chore = node(1187, H1, labels=["ai-found-no-errors"], title="chore: bump TauCeti", paths=("TauCetiRoadmap/Foo/Suggested.lean",))
    gh = ContentGH([chore], tree=TREE)
    out, fake = sync(gh)
    assert out == ["#1187: ai-found-no-errors removed (not reviewed: chore by its title)", "#1187: not reviewed (chore by its title)"]
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


def test_a_review_nobody_took_stays_when_the_head_moved_in_another_folder():
    """`[prreview] carry_unchanged_folders`: a repair of one roadmap folder moves the head of a pull
    request with several; the queued review of a folder that holds the files it held is neither closed
    nor filed again."""
    def queued():
        return [review_job(41, 1, 1164, H1, "available", folder="Foo"),
                review_job(42, 1, 1164, H1, "available", name="Bar", folder="Bar")]
    repaired = {(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): "foo",
                (H1, "TauCetiRoadmap/Bar"): "bar", (H2, "TauCetiRoadmap/Bar"): "bar-repaired"}
    kept = f"#1164: 1 review nobody took kept at {H2[:7]} (the folder holds the files of the head the job names)"
    out, fake = sync(ContentGH([two_folder_pr(H2)], trees=repaired), queued())
    assert out == ["#1164: PRREVIEW-Bar~1 superseded", "#1164: PRREVIEW-Bar~2 created", kept]
    assert fake.states == [("PRREVIEW-Bar~1", "done")] and created(fake) == [("PRREVIEW-Bar~2", "Bar")]
    # Switched off: every round of the old head is closed and filed again, as before.
    out, fake = sync(ContentGH([two_folder_pr(H2)], trees=repaired), queued(), carry_unchanged_folders=False)
    assert fake.states == [("PRREVIEW-Foo~1", "done"), ("PRREVIEW-Bar~1", "done")]
    assert sorted(created(fake)) == [("PRREVIEW-Bar~2", "Bar"), ("PRREVIEW-Foo~2", "Foo")]
    # A tree GitHub did not give: nothing is known, and the rounds are left as they are for this run.
    out, fake = sync(ContentGH([two_folder_pr(H2)], trees="unread"), queued())
    assert fake.states == [] and fake.created == [] and kept not in out
    # A round that reads the pull request's text, or an answer given at its head, belongs to that head.
    for key in ("text_check", "operator_check", "text_rereview", "dispute_rereview"):
        foo, bar = queued()
        foo.inputs[key] = True
        assert not intake.folder_round(foo) and intake.folder_round(bar)
        _out, fake = sync(ContentGH([two_folder_pr(H2)], trees=repaired), [foo, bar])
        assert ("PRREVIEW-Foo~1", "done") in fake.states


def test_a_round_is_not_kept_across_a_change_outside_the_roadmaps():
    """A merge of the base branch changes files outside the active directory; the repair of a round kept
    across it could not be carried onto the branch, so the round is closed and filed at the new head."""
    def queued():
        return [review_job(41, 1, 1164, H1, "available", folder="Foo")]
    same = same_trees(H1, H2)
    merged = {(H1, ""): {"TauCetiRoadmap": "r1", "lakefile.toml": "lakefile", "umbrellas": "u1"},
              (H2, ""): {"TauCetiRoadmap": "r2", "lakefile.toml": "lakefile", "umbrellas": "u2"}}
    out, fake = sync(ContentGH([two_folder_pr(H2)], trees=same, entries=merged), queued())
    assert fake.states == [("PRREVIEW-Foo~1", "done")] and "#1164: PRREVIEW-Foo~1 superseded" in out
    assert ("PRREVIEW-Foo~2", "Foo") in created(fake)
    # Only the active directory's own entry differs: kept.
    inside = {(H1, ""): {"TauCetiRoadmap": "r1", "lakefile.toml": "lakefile"},
              (H2, ""): {"TauCetiRoadmap": "r2", "lakefile.toml": "lakefile"}}
    gh = ContentGH([two_folder_pr(H2)], trees=same, entries=inside)
    out, fake = sync(gh, queued())
    assert fake.states == [] and gh.root_queries == 1
    assert intake.outside_unchanged(SimpleNamespace(cfg=example_config(), gh=gh), REPO, {(H1, H2)}, 5) == {(H1, H2): True}
    assert intake.outside_unchanged(SimpleNamespace(cfg=example_config(), gh=ContentGH(entries=merged)), REPO,
                                    {(H1, H2)}, 5) == {(H1, H2): False}
    assert intake.outside_unchanged(SimpleNamespace(cfg=example_config(), gh=ContentGH(trees="unread")), REPO,
                                    {(H1, H2)}, 5) == {}
    # The lane takes the round under the same condition.
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 12, "head": H1, "owner": "carol", "name": "Foo"})
    foo = {(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): "foo"}
    lane = lane_for(job)
    assert lane.folder_unmoved(job, SimpleNamespace(cfg=example_config(), gh=ContentGH(trees=foo, entries=inside)), H1, H2)
    assert not lane.folder_unmoved(job, SimpleNamespace(cfg=example_config(), gh=ContentGH(trees=foo, entries=merged)), H1, H2)


def test_a_kept_round_is_closed_when_its_folder_needs_no_review():
    """A round kept from an earlier head is closed like one of the head itself once the folder's latest
    review settles the files it still holds."""
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    first = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "needs_changes", H1)}
    repaired = {(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): "foo",
                (H1, "TauCetiRoadmap/Bar"): "bar", (H2, "TauCetiRoadmap/Bar"): "bar-repaired"}
    stray = review_job(44, 2, 1164, H1, "available", folder="Foo")
    out, fake = sync(ContentGH([two_folder_pr(H2)], files=first, trees=repaired), [foo, bar, stray])
    assert out == ["#1164: PRREVIEW-Foo~2 superseded", "#1164: PRREVIEW-Bar~2 created"]
    assert fake.posted[0][1].startswith(f"Superseded: Foo of #1164 at {H2[:7]} holds the files of {H1[:7]}")


def test_the_trees_of_the_kept_rounds_are_read_in_one_query():
    jobs = [review_job(41, 1, 1164, H1, "available", folder="Foo"),
            review_job(42, 1, 1164, H1, "available", name="Bar", folder="Bar")]
    gh = ContentGH([two_folder_pr(H2)], trees=same_trees(H1, H2))
    kept, untold = intake.unmoved_rounds(SimpleNamespace(cfg=example_config(), gh=gh), REPO,
                                         [(j, H2) for j in jobs], 5)
    assert kept == {"PRREVIEW-Foo~1", "PRREVIEW-Bar~1"} and untold == set() and gh.tree_queries == 1
    assert intake.unmoved_rounds(SimpleNamespace(cfg=example_config(), gh=gh), REPO, [], 5) == (set(), set())
    gone = ContentGH([two_folder_pr(H2)], trees={(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): None})
    assert intake.unmoved_rounds(SimpleNamespace(cfg=example_config(), gh=gone), REPO, [(jobs[0], H2)], 5) == (set(), set())


def test_the_label_goes_when_the_head_moves_and_stays_on_an_accepted_current_head():
    accepted_old = review_job(41, 1, 12, H1, "done")
    labels = f"repos/{REPO}/issues/12/labels/ai-found-no-errors"
    gh = ContentGH([node(12, H2, labels=["ai-found-no-errors", "awaiting-review"])], files=verdict_file(accepted_old, "accepted", H1))
    out, _fake = sync(gh, [accepted_old], per_run=0)
    assert gh.writes == [("DELETE", labels, {})] and out[0] == "#12: ai-found-no-errors removed (head moved)"
    accepted_now = review_job(42, 2, 12, H2, "done")
    gh = ContentGH([node(12, H2, labels=["ai-found-no-errors"])], files=verdict_file(accepted_now, "accepted", H2))
    out, _fake = sync(gh, [accepted_old, accepted_now])
    assert gh.writes == [] and out == []
    # A later review of the same head that blocks takes it off; one still being finished keeps it.
    blocking = review_job(43, 3, 12, H2, "done")
    gh = ContentGH([node(12, H2, labels=["ai-found-no-errors"])],
                   files={**verdict_file(accepted_now, "accepted", H2), **verdict_file(blocking, "needs_changes", H2)})
    out, _fake = sync(gh, [accepted_now, blocking])
    assert out == [f"#12: ai-found-no-errors removed (the AI review of {H2[:7]} does not accept it)"]
    finishing = review_job(44, 4, 12, H3, "submitted")
    gh = ContentGH([node(12, H3, labels=["ai-found-no-errors"])])
    assert sync(gh, [finishing])[0] == [] and gh.writes == []
    # A label put on by hand, with no AI review at all, goes too; a token that may not write says so.
    gh = ContentGH([node(14, H1, labels=["ai-found-no-errors"], paths=("TauCetiRoadmap/Baz/README.md",))], refuse=True)
    out, _fake = sync(gh, per_run=0)
    assert out[0].startswith(f"#14: ai-found-no-errors on {REPO} not removed (gh api -X DELETE")


def test_a_verdict_github_did_not_answer_for_keeps_the_label():
    accepted = review_job(42, 1, 12, H2, "done")

    class Blip(ContentGH):
        def raw_file(self, repo, path, ref=""):
            raise GHError(f"gh api repos/{repo}/contents/{path}: HTTP 502: Bad Gateway")
    gh = Blip([node(12, H2, labels=["ai-found-no-errors"])])
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


def test_a_round_is_taken_at_the_head_it_names_when_its_folder_did_not_move(tmp_path, monkeypatch):
    """The head of the pull request moved by a repair of another roadmap folder: the round is still the
    review of the files its folder holds, and the worker takes it (`[prreview] carry_unchanged_folders`)."""
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 12, "head": H1, "owner": "carol", "name": "Foo"})
    pulls = {12: {"number": 12, "state": "open", "head": {"sha": H2}}}
    same = {(H1, "TauCetiRoadmap/Foo"): "foo", (H2, "TauCetiRoadmap/Foo"): "foo"}
    lane = lane_for(job)
    assert lane.folder_unmoved(job, lane_ctx(tmp_path, gh=ContentGH(pulls=pulls, trees=same)), H1, H2)
    # Not when the folder's files changed, GitHub did not give the trees, or the switch is off ...
    assert not lane.folder_unmoved(job, lane_ctx(tmp_path, gh=ContentGH(pulls=pulls)), H1, H2)
    assert not lane.folder_unmoved(job, lane_ctx(tmp_path, gh=ContentGH(pulls=pulls, trees="unread")), H1, H2)
    off = lane_ctx(tmp_path, gh=ContentGH(pulls=pulls, trees=same))
    off.cfg.prreview.carry_unchanged_folders = False
    assert not lane.folder_unmoved(job, off, H1, H2)
    # ... nor for a round that reads the pull request's text at its head.
    check = new_job("PRREVIEW-Foo~2", inputs={**job.inputs, "text_check": True})
    assert not lane.folder_unmoved(check, lane_ctx(tmp_path, gh=ContentGH(pulls=pulls, trees=same)), H1, H2)

    class Reached(Exception):
        pass

    def checkout(self, job, ctx):
        raise Reached

    monkeypatch.setattr(lane_base.Lane, "prepare", checkout)
    with pytest.raises(Reached):            # past the check of the head: the checkout is next
        lane.prepare(job, lane_ctx(tmp_path, gh=ContentGH(pulls=pulls, trees=same)))
    with pytest.raises(LaneError, match=re.escape(f"#12 moved to {H2[:7]}; the intake files")):
        lane.prepare(job, lane_ctx(tmp_path, gh=ContentGH(pulls=pulls)))


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
    assert f"Reviewed head: {H1}" in lines[:6] and "the build succeeded with `sorry` as the only warning" in comment["body"]
    # The posted layout: a summary at the top, the details folded, the evidence left in the report.
    body = comment["body"]
    assert any(x.startswith("**Summary.** Accepted: this roadmap folder is ready for a human's final approval")
               for x in lines[:12])
    assert body.index("<details><summary>What was checked</summary>") < body.index("<!-- tauceti-roadmap-prreview")
    assert "## Verified sound" not in body and "| Statement |" not in body
    assert prreviews.prreview_marker(comment["body"]) == {"job": job.id, "head": H1, "verdict": "accepted",
                                                          "agent": "codex", "model": "gpt-x", "worker": "alice-1",
                                                          "mode": "full"}
    assert gh.labels == ["awaiting-review", "ai-found-no-errors"]               # the awaiting-* labels are not touched
    assert summary.endswith("labelled ai-found-no-errors") and "accepted" in summary
    assert lane_base.read_pending(ctx, job)["done"] == ["content-comment"]


def test_changes_asked_for_take_the_label_off(review_env):
    gh = LabelGH(labels=["ai-found-no-errors", "awaiting-author"])
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
    assert lane.finish(job, ctx, ws).endswith(f"not labelled ai-found-no-errors: #905 moved to {H2[:7]}")
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
    assert len(gh.comments) == 1 and gh.labels == ["ai-found-no-errors"]


@pytest.mark.parametrize("status,report,gate_ok,problem", [
    ("accepted", "no table", True, "adversarial table"),
    ("accepted", "blocking", True, "an acceptance lists no finding"),
    ("needs_changes", "no findings", True, "a verdict that asks for changes writes each finding"),
    ("needs_changes", "minor", True, "a finding is P1 or P2; write each point below that bar as a small correction"),
    ("needs_changes", "rule id", True, "names the label of an automatic check ('`prnum`')"),
    ("accepted", "private", True, "names a file or folder of the worker, or a local path ('gate.md')"),
    ("accepted", "wrong head line", True, "report: first line must be '# Review of Foo (pull request #905 at 1111111)'"),
])
def test_the_checks_refuse_what_the_board_would_not_take(review_env, monkeypatch, status, report, gate_ok, problem):
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, status, gate_ok)
    text = ws.report.read_text()
    text = {"no table": text.replace(ADVERSARIAL, ""), "blocking": text.replace("## Verified sound", FINDING + "## Verified sound"),
            "no findings": text.replace(FINDING, ""), "wrong head line": text.replace("# Review of Foo", "# Review of Bar"),
            "minor": text.replace("## R01 (P1)", "## R01 (P3)"),
            "rule id": text.replace("Witness: p = 2.", "The `prnum` check flagged it."),
            "private": text.replace("## Verified sound", "## C01 — Example 2 names the wrong group\n\nSee gate.md.\n\n"
                                    "## Verified sound"),
            }.get(report, text)
    ws.report.write_text(text)
    assert any(problem in p for p in lane.check(job, ctx, ws)), lane.check(job, ctx, ws)


def test_an_acceptance_with_a_failing_check_set_aside_and_with_small_corrections_is_taken(review_env, monkeypatch):
    """No forced relay: a failed automatic check is evidence the reviewer may set aside, and small corrections
    and format notes never stop an acceptance."""
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "accepted", gate_ok=False)
    ws.report.write_text(ws.report.read_text().replace("## Verified sound", (
        "## C01 — Example 2 is labelled with the wrong group\n\nIt is the dihedral group of order 8.\n\n"
        "## N01 — A link points at a pull request\n\nName the roadmap instead.\n\n## Verified sound")).replace(
        "## Not verifiable\n\n- none\n", "## Not verifiable\n\n- none\n\nAutomatic checks set aside as false "
        "positives: the merge-order check counts a consumer named in the scope as a supplier.\n"))
    assert [p for p in lane.check(job, ctx, ws) if "provenance" not in p and "Overlap" not in p] == []
    body = lane.comment_body(job, ctx, ws)
    assert "## Small corrections (non-blocking)\n\n1. **C01** Example 2 is labelled with the wrong group" in body
    assert "## Format notes (non-blocking)\n\n1. **N01** A link points at a pull request" in body
    assert body.index("Automatic checks set aside as false positives") > body.index("<details>")
    assert prreviews.findings_with_bodies(body) == []           # nothing a tool counts as a finding


def test_an_acceptance_does_not_pass_over_a_failing_build_or_merge_order_in_silence(review_env, monkeypatch):
    """The build failing, or a cycle or an upward supplier, is always a finding when real: an acceptance
    at such a head says why the check is a false positive, or the lane sends the report back."""
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "accepted", gate_ok=False)
    silent = [p for p in lane.check(job, ctx, ws) if "merge-order check" in p]
    assert len(silent) == 1 and "Automatic checks set aside as false positives" in silent[0]
    ws.report.write_text(ws.report.read_text().replace(
        "## Not verifiable\n\n- none\n", "## Not verifiable\n\n- none\n\nAutomatic checks set aside as false "
        "positives: the merge-order check counts a consumer named in the scope as a supplier.\n"))
    assert [p for p in lane.check(job, ctx, ws) if "merge-order check" in p] == []
    passing, ws2 = finished_ws(review_env, ctx, job, "accepted")
    assert [p for p in passing.check(job, ctx, ws2) if "merge-order check" in p] == []


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


def test_the_index_of_a_family_another_folder_is_nested_in_is_the_pull_requests_own(dry_ctx, monkeypatch):  # noqa: F811
    """2026-11-12: a pull request changed a top-level folder and a nested one. The nested folder's review
    asked for a change of its family index, which the gate of the top-level folder would have failed as a
    change outside the folder."""
    job = new_job("PRREVIEW-Foo~1", inputs={**NESTED, "name": "Foo", "folder": "Foo"})
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)
    assert lane_prreview.other_families(job) == ["RepresentationTheory"]
    nested = new_job("PRREVIEW-CompactGroups~1", inputs={**NESTED, "roadmaps": [*NESTED["roadmaps"], "Other/Deep/Leaf"]})
    assert lane_prreview.other_families(nested) == ["Other", "Other/Deep"]       # its own family is not listed twice
    seen = {}

    def run_gate(worktree, name, opts, base="", build_cmd=""):
        seen.update(opts=opts)
        return gates.GateResult(name, counts={"build": 0})
    monkeypatch.setattr(lane_prreview.gates, "run_gate", run_gate)
    assert lane.review_gate(job, dataclasses.replace(dry_ctx, dry_run=False), ws).ok
    changed = ["TauCetiRoadmap/Foo/README.md", "TauCetiRoadmap/RepresentationTheory/README.md",
               "TauCetiRoadmap/RepresentationTheory/CompactGroups/README.md",
               "TauCetiRoadmap/RepresentationTheory/Other/README.md", "TauCetiRoadmap/Bar/README.md",
               "TauCetiRoadmap/README.md"]
    assert gates.outside_folder(changed, "TauCetiRoadmap/Foo", seen["opts"].outside_folder_allow) == [
        "TauCetiRoadmap/RepresentationTheory/Other/README.md", "TauCetiRoadmap/Bar/README.md", "TauCetiRoadmap/README.md"]
    values = {**lane.common_values(job, dry_ctx, ws), **lane.values(job, dry_ctx, ws)}
    assert ("The files directly in `TauCetiRoadmap/RepresentationTheory/` (the index of a family that one of those "
            "other folders is nested in) are the pull request's own too") in values["FILES"]
    assert "(the family's index)" not in values["FILES"]                         # Foo itself is not nested
    assert values["OUTSIDE_CHECK"].endswith(
        "-- . ':!TauCetiRoadmap/Foo' ':!TauCetiRoadmap/RepresentationTheory/CompactGroups' "
        "':!TauCetiRoadmap/RepresentationTheory/LieGroups' ':(exclude,glob)TauCetiRoadmap/RepresentationTheory/*'")


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
    # It records the digest of the pull request's text the review read (`summary.text_digest`), set when the
    # lane prepared the round (this workspace was made without it).
    ws.extra["pr_text_digest"] = summary.text_digest("Roadmap: CompactGroups", "It builds compact groups.")
    assert path in lane.scrub_outputs(job, ctx, ws)
    assert json.loads(path.read_text())["review"]["text"] == ws.extra["pr_text_digest"]


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
    assert lane.update_label(job, ctx, 905, H1, "accepted") == "labelled ai-found-no-errors" and gh.labels == ["ai-found-no-errors"]
    # One folder asking for changes takes the label off, whatever the others say.
    assert lane.update_label(job, ctx, 905, H1, "needs_changes") == "ai-found-no-errors removed" and gh.labels == []


def test_a_pass_before_the_final_review_says_so_and_leaves_the_label_alone(review_env, monkeypatch):
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    gh = LabelGH()
    ctx = lane_ctx(review_env, gh=gh)
    job = new_job("PRREVIEW-CompactGroups~1", inputs={**NESTED, "pre": True})
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    body = lane.comment_body(job, ctx, ws)
    # (under the header lines the tools read, before the summary)
    assert (f"Reviewed head: {H1}\n\nRoadmap folder: `TauCetiRoadmap/RepresentationTheory/CompactGroups/`\n\n"
            "This is a pass before the final review. It does not decide `ai-found-no-errors`: "
            "the final review, on another model, follows it.\n\n**Summary.**") in body
    assert lane.update_label(job, ctx, 905, H1, "accepted") == (
        "a pass before the final review: ai-found-no-errors is decided by that review") and gh.labels == []
    # A pass that asks for changes takes a label off like any review, and its P3-only repair settles nothing.
    gh.labels.append("ai-found-no-errors")
    assert lane.update_label(job, ctx, 905, H1, "needs_changes") == "ai-found-no-errors removed"
    ctx.cfg.prreview.carry_minor = True
    assert lane.carry_record(job, ctx, ws) is None
    # The same review as a final one carries no such note.
    final = new_job("PRREVIEW-CompactGroups~1", inputs=NESTED)
    lane, ws = finished_ws(review_env, ctx, final, "accepted")
    assert "a pass before the final review" not in lane.comment_body(final, ctx, ws)


# --- a run that stops after its comment is finished without a second one -----------------------------------

class ReviewHub(Hub):
    """The submission hub, with the content pull request's labels (and no inline comments)."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.labels = []

    def api(self, path, paginate=False):
        if path == f"repos/{CONTENT}/issues/905/labels":
            return [{"name": x} for x in self.labels]
        if path == f"repos/{CONTENT}/pulls/905/comments?per_page=100":
            return []
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
    assert rh.labels == ["ai-found-no-errors"]
    [opened] = rh.opened
    assert "PRREVIEW-Foo~1" in opened["body"] and ctx.board.states == [(job.id, "submitted")]
    assert lane_base.read_pending(ctx, job) is None


def test_an_archive_pull_request_is_a_chore_by_its_title():
    from tauceti_roadmap_worker.intake import CHORE_TITLE
    assert CHORE_TITLE.match("Archive GlobalNumberFields after ray-class naturality lands")
    assert CHORE_TITLE.match("chore: bump TauCeti to ab00ba392")
    assert not CHORE_TITLE.match("Archived-torsion Galois representations: a roadmap")
    assert not CHORE_TITLE.match("Roadmap: archive geometry")



# --- a re-review of a repaired head ------------------------------------------------------------------

REAL_GIT = lane_prreview.gitops.git          # bound before a fixture fakes git


def repaired_repo(tmp_path, edited=10):
    """A content repository whose second commit rewrites `edited` lines of Foo's README and touches a
    file outside the folder: (repository, the first head, the second head)."""
    repo = tmp_path / "repo"
    readme = repo / "TauCetiRoadmap" / "Foo" / "README.md"
    readme.parent.mkdir(parents=True)
    sh(tmp_path, "init", "-q", str(repo))
    readme.write_text("# Foo\n\n" + "".join(f"line {k}\n" for k in range(40)))
    (repo / "lakefile.toml").write_text('name = "x"\n')
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "first")
    first = sh(repo, "rev-parse", "HEAD")
    readme.write_text("# Foo\n\n" + "".join(f"line {k}{' (repaired)' if k < edited else ''}\n" for k in range(40)))
    (repo / "lakefile.toml").write_text('name = "y"\n')
    sh(repo, "commit", "-q", "-am", "second")
    return repo, first, sh(repo, "rev-parse", "HEAD")


def real_git_in(repo, monkeypatch):
    """The dry-run context's fake git, except that the change set's commands run in `repo`."""
    fake = lane_prreview.gitops.git

    def git(cwd, *args, **kw):
        return REAL_GIT(repo, *args, **kw) if args[0] in ("cat-file", "diff") or args[:2] == ("fetch", "-q") and \
            len(args) == 4 and re.fullmatch(r"[0-9a-f]{40}", args[3]) else fake(cwd, *args, **kw)
    monkeypatch.setattr(lane_prreview.gitops, "git", git)


def rereview_job(first, second, round_=2):
    job = new_job(f"PRREVIEW-Foo~{round_}", inputs={"pr": 905, "head": second, "owner": "carol", "name": "Foo",
                                                    "folder": "Foo", "rereview": True, "previous_head": first,
                                                    "previous_job": f"PRREVIEW-Foo~{round_ - 1}"})
    job.number = 8
    return job


@needs_git
def test_prepare_writes_the_change_since_the_previous_reviewed_head(dry_ctx, tmp_path, monkeypatch):  # noqa: F811
    repo, first, second = repaired_repo(tmp_path)
    real_git_in(repo, monkeypatch)
    job = rereview_job(first, second)
    ws = lane_for(job).prepare(job, dry_ctx)
    assert ws.extra["change_lines"] == 20                     # ten lines rewritten: ten insertions, ten deletions
    text = (ws.scratch / "changes.md").read_text()
    assert f"# Changes since {first[:7]} (data)" in text and f"this review reads `{second}`" in text
    assert "`TauCetiRoadmap/Foo/`: 20 changed lines (10 insertions, 10 deletions)." in text
    assert "TauCetiRoadmap/Foo/README.md | 20" in text and "+line 3 (repaired)" in text and "-line 3\n" in text
    assert text.endswith("## Files changed outside the folder\n\n- `lakefile.toml`\n") and "Cut at" not in text
    # The diff is cut at `diff_cap_lines`, with a note naming the command that shows the rest.
    dry_ctx.cfg.prreview.diff_cap_lines = 5
    ws = lane_for(job).prepare(job, dry_ctx)
    text = (ws.scratch / "changes.md").read_text()
    assert ws.extra["change_lines"] == 20 and "+line 3 (repaired)" not in text
    assert re.search(rf"Cut at 5 of \d+ lines: read the rest with `git -C \S+ diff {first}\.\.{second} -- "
                     r"TauCetiRoadmap/Foo`\.", text)


@needs_git
def test_a_previous_head_the_checkout_cannot_read_counts_as_a_large_change(dry_ctx, tmp_path, monkeypatch):  # noqa: F811
    repo, _first, second = repaired_repo(tmp_path)
    real_git_in(repo, monkeypatch)
    job = rereview_job("f" * 40, second)
    ws = lane_for(job).prepare(job, dry_ctx)
    assert ws.extra["change_lines"] is None
    assert "cannot be read in this checkout" in (ws.scratch / "changes.md").read_text()
    # A first review has no change set.
    first = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": second, "owner": "carol", "name": "Foo",
                                              "rereview": False})
    ws = lane_for(first).prepare(first, dry_ctx)
    assert ws.extra["change_lines"] is None and not (ws.scratch / "changes.md").exists()


def test_the_change_counts_insertions_and_deletions_and_a_fence_holds_any_backticks():
    assert lane_prreview.numstat_lines("3\t1\tA/README.md\n-\t-\tA/x.png\n2\t0\tA/B.lean\n") == (5, 1)
    text = "```lean\ntheorem x : True := trivial\n```"
    assert lane_prreview.fenced(text, "diff").startswith("````diff\n```lean") and lane_prreview.fenced(text).endswith("```\n````")


def base_repo(tmp_path):
    """A content repository whose `origin/main` holds Foo, and whose head drops Foo's Layer 2 and `b`,
    adds Layer 3 and `d`, restates `a`, leaves `c`, and adds a roadmap Bar."""
    repo = tmp_path / "repo"
    foo, bar = repo / "TauCetiRoadmap" / "Foo", repo / "TauCetiRoadmap" / "Bar"
    foo.mkdir(parents=True)
    sh(tmp_path, "init", "-q", str(repo))
    (foo / "README.md").write_text("# Foo\n\n## Layer 1\n\ntext\n\n## Layer 2\n\ntext\n")
    (foo / "Suggested.lean").write_text("theorem a : True := trivial\n\ntheorem b : True := trivial\n\n"
                                        "noncomputable def c : Nat := 0\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "main")
    sh(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (foo / "README.md").write_text("# Foo\n\n## Layer 1\n\ntext\n\n## Layer 3\n\ntext\n")
    (foo / "Suggested.lean").write_text("theorem a (p : Nat) : True := trivial\n\n-- theorem b is gone\n\n"
                                        "noncomputable def c : Nat := 0\n\n@[simp] theorem d : True := trivial\n")
    bar.mkdir()
    (bar / "README.md").write_text("# Bar\n\n## One\n\n## Two\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "head")
    return repo


@needs_git
def test_the_pull_request_text_comes_with_the_folders_change_against_its_base(tmp_path, monkeypatch):
    repo = base_repo(tmp_path)
    text = lane_prreview.base_change(repo, "origin/main", "TauCetiRoadmap/Foo")
    assert text.startswith("## The change to `TauCetiRoadmap/Foo/` against `origin/main` (computed by the worker)\n")
    assert "The roadmap is on `origin/main`: this pull request revises it." in text
    assert "Headings: 1 removed, 1 added. Lean declarations: 1 removed, 1 added, 1 restated." in text
    assert "```text\n- ## Layer 2\n+ ## Layer 3\n```" in text and "Layer 1" not in text
    assert "```text\n- b\n+ d\n~ a\n```" in text and " c\n" not in text
    assert "TauCetiRoadmap/Foo/Suggested.lean |" in text and "diff origin/main...HEAD -- TauCetiRoadmap/Foo` shows" in text
    # A roadmap the base does not hold is added; each list is cut at LISTED, the rest counted.
    monkeypatch.setattr(lane_prreview, "LISTED", 2)
    text = lane_prreview.base_change(repo, "origin/main", "TauCetiRoadmap/Bar")
    assert "The roadmap is not on `origin/main`: this pull request adds it." in text
    assert "```text\n+ # Bar\n+ ## One\n+ [... 1 more]\n```" in text and "```text\n(none)\n```" in text
    # A base this checkout cannot compare with the head is said so.
    assert "`origin/gone` cannot be compared with this head" in lane_prreview.base_change(
        repo, "origin/gone", "TauCetiRoadmap/Foo")


def test_a_revision_is_compared_with_the_layers_of_its_readme_at_the_merge_base():
    # A roadmap on main is a record of what the library contains (prompts/sections/record.md): a revision keeps
    # every layer under its id with its subject. Read by id as the progress tools read the layers.
    before = ("# Roadmap: foo\n\n## Layer 0: Groups\n\n## Layer 1: Rings\n\n## Layer 2: Fields\n\n"
              "## Layer 3: Modules (p odd)\n")
    after = ("# Roadmap: foo\n\n## Layer 0:   groups\n\n## Layer 2: A declaration catalogue\n\n"
             "## Layer 3: Modules\n\n## Layer 4: Algebras\n")
    assert lane_prreview.layer_changes(before, after) == (
        ["Layer 1: Rings"], [("Layer 2: Fields", "Layer 2: A declaration catalogue")], ["Layer 4: Algebras"])
    assert lane_prreview.layer_changes(before, before) == ([], [], [])


@needs_git
def test_the_change_against_the_base_lists_the_layers_a_revision_drops_or_retitles(tmp_path):
    repo = tmp_path / "repo"
    foo, bar = repo / "TauCetiRoadmap" / "Foo", repo / "TauCetiRoadmap" / "Bar"
    foo.mkdir(parents=True)
    sh(tmp_path, "init", "-q", str(repo))
    (foo / "README.md").write_text("# Foo\n\n" + "".join(f"## Layer {k}: Part {k}\n\nThe statement {k}.\n\n"
                                                           for k in range(10)))
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "main")
    sh(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # The revision keeps three layers out of ten and gives Layer 8's id to another subject.
    (foo / "README.md").write_text("# Foo\n\n## Layer 0: Part 0\n\n## Layer 8: A declaration catalogue\n\n"
                                   "## Layer 9: Part 9\n")
    sh(repo, "commit", "-q", "-am", "head")
    text = lane_prreview.base_change(repo, "origin/main", "TauCetiRoadmap/Foo")
    assert "### Layers of the README at the merge base (`" in text
    assert ("The README at the merge base has 10 layers as the progress tools read its headings; this head lacks 7 "
            "of those ids (-) and gives 1 another title (~); it adds 0 (+).") in text
    listing = "".join(f"- Layer {k}: Part {k}\n" for k in range(1, 8)) + "~ Layer 8: Part 8 → Layer 8: A declaration catalogue"
    assert f"```text\n{listing}\n```" in text
    main = sh(repo, "rev-parse", "origin/main")
    assert f"`git -C {repo} show {main}:TauCetiRoadmap/Foo/README.md` prints the README at the merge base" in text
    # A revision that keeps every layer says so; a roadmap the base does not hold has no such section.
    (foo / "README.md").write_text("# Foo\n\n" + "".join(f"## Layer {k}: Part {k}\n\nThe statement {k}, kept.\n\n"
                                                           for k in range(10)) + "## Layer 10: More\n")
    sh(repo, "commit", "-q", "-am", "keep")
    text = lane_prreview.base_change(repo, "origin/main", "TauCetiRoadmap/Foo")
    assert "this head lacks 0 of those ids (-) and gives 0 another title (~); it adds 1 (+)." in text
    assert "```text\n+ Layer 10: More\n```" in text
    bar.mkdir()
    (bar / "README.md").write_text("# Bar\n\n## Layer 0: One\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "bar")
    assert "Layers of the README" not in lane_prreview.base_change(repo, "origin/main", "TauCetiRoadmap/Bar")


@needs_git
def test_every_review_reads_the_pull_requests_text_against_the_head(dry_ctx, tmp_path, monkeypatch):  # noqa: F811
    repo, first, second = repaired_repo(tmp_path)
    real_git_in(repo, monkeypatch)
    job = rereview_job(first, second)
    ctx = rereview_ctx(dry_ctx, second, 20)
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    assert ws.extra["mode"] == "incremental"
    pr_text = (ws.scratch / "pull-request.md").read_text()
    assert pr_text.index("## Description") < pr_text.index("## The change to `TauCetiRoadmap/Foo/` against `origin/main`")
    text = prompts.render("prreview", {**lane.common_values(job, ctx, ws), **lane.values(job, ctx, ws)})
    assert "8. **The pull request's text.** Read the title and the description's prose against the folder's" in text
    # An incremental re-review checks it too: a repair makes the text wrong without touching it.
    assert "3. **The pull request's text** is not untouched text" in text and "6. **The verdict.**" in text


PREVIOUS_REPORT = ("# Review of Foo (pull request #905 at 1111111)\n\n**Verdict:** needs changes\n\n"
                   "## R01 (P1) — the hypothesis on p is missing\n\nLocation: README line 4.\n\n"
                   "## R02 (P3) — a locator names no page\n\nLocation: README line 9.\n\n"
                   "```text\n## R09 (P1) — quoted, not a finding\n```\n\n## Verified sound\n\n" + ADVERSARIAL)


def rereview_ctx(dry_ctx, head, small=150):  # noqa: F811
    gh = ContentGH(pulls={905: {"number": 905, "state": "open", "head": {"sha": head}}},
                   files={"packages/Foo/reports/PRREVIEW-Foo~1.md": PREVIOUS_REPORT})
    ctx = dataclasses.replace(dry_ctx, gh=gh)
    ctx.cfg.prreview.small_change_lines = small
    return ctx


@needs_git
@pytest.mark.parametrize("small,mode", [(20, "incremental"), (19, "full")])
def test_the_mode_follows_the_threshold_and_the_brief_renders_it(dry_ctx, tmp_path, monkeypatch, small, mode):  # noqa: F811
    repo, first, second = repaired_repo(tmp_path)
    real_git_in(repo, monkeypatch)
    job = rereview_job(first, second)
    ctx = rereview_ctx(dry_ctx, second, small)
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    assert ws.extra["change_lines"] == 20 and ws.extra["mode"] == mode
    assert ws.extra["previous_report"].read_text() == PREVIOUS_REPORT
    values = {**lane.common_values(job, ctx, ws), **lane.values(job, ctx, ws)}
    assert prompts.placeholders("prreview") <= set(values)
    text = prompts.render("prreview", values)
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", text)
    assert f"if any: `{ws.extra['previous_report']}`. The change since the head it read\n(data): `{ws.scratch / 'changes.md'}`." in text
    assert "## Verified sound" in text and "## Not verifiable" in text and "## Source issues" in text
    if mode == "incremental":
        assert "**An incremental re-review.**" in text and "**A full review.**" not in text
        assert f"read `{first[:7]}`; since then the folder changed by 20 lines (at most `[prreview] small_change_lines` = 20)" in text
        assert f'"incremental re-review of 20 changed lines since {first[:7]}"' in text and "## Previous findings" in text
    else:
        assert "**An incremental re-review.**" not in text
        assert ("**A full review.** This re-review of a repaired head is not incremental: the folder changed by 20 lines "
                f"since `{first[:7]}`, the head the previous review read (more than `[prreview] small_change_lines` = 19). "
                "First check finding by finding") in text
        assert f"The change set `{ws.scratch / 'changes.md'}` is context" in text


@needs_git
def test_the_check_round_of_a_rewritten_description_is_incremental_and_says_what_to_read(dry_ctx, tmp_path, monkeypatch):  # noqa: F811
    repo, _first, second = repaired_repo(tmp_path)
    real_git_in(repo, monkeypatch)
    job = rereview_job(second, second, round_=3)
    job.inputs["text_rereview"] = True
    gh = ContentGH(pulls={905: {"number": 905, "state": "open", "head": {"sha": second}}},
                   files={"packages/Foo/reports/PRREVIEW-Foo~2.md": PREVIOUS_REPORT})
    ctx = dataclasses.replace(dry_ctx, gh=gh)
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    assert ws.extra["change_lines"] == 0 and ws.extra["mode"] == "incremental"
    text = prompts.render("prreview", {**lane.common_values(job, ctx, ws), **lane.values(job, ctx, ws)})
    assert "**An incremental re-review.**" in text
    assert "**The files are unchanged; the title and description were rewritten.**" in text
    assert f"They are quoted in `{ws.scratch / 'pull-request.md'}`." in text
    # A check round of an accepted head's text says so instead.
    check = rereview_job(second, second, round_=3)
    check.inputs["text_check"] = True
    ws = lane.prepare(check, ctx)
    text = prompts.render("prreview", {**lane.common_values(check, ctx, ws), **lane.values(check, ctx, ws)})
    assert "**This round checks the pull request's title and description.**" in text
    assert "the title and description were rewritten" not in text
    # An ordinary incremental re-review does not carry the paragraph.
    plain = rereview_job(second, second, round_=3)
    ws = lane.prepare(plain, ctx)
    assert "the title and description were rewritten" not in prompts.render(
        "prreview", {**lane.common_values(plain, ctx, ws), **lane.values(plain, ctx, ws)})


@needs_git
def test_a_re_review_without_the_previous_report_on_the_board_is_a_full_review(dry_ctx, tmp_path, monkeypatch):  # noqa: F811
    repo, first, second = repaired_repo(tmp_path)
    real_git_in(repo, monkeypatch)
    job = rereview_job(first, second)
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)                          # no GitHub: no previous report
    assert ws.extra["change_lines"] == 20 and ws.extra["previous_report"] is None and ws.extra["mode"] == "full"
    text = prompts.render("prreview", {**lane.common_values(job, dry_ctx, ws), **lane.values(job, dry_ctx, ws)})
    assert "not incremental: the previous review's report is not on the board. Review the whole roadmap folder" in text


def test_the_brief_of_a_first_review_is_a_full_review_without_a_change_set(dry_ctx):  # noqa: F811
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": "abc1234" + "0" * 33, "owner": "carol", "name": "Foo",
                                            "rereview": False})
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)
    assert ws.extra["mode"] == "full" and ws.extra["changes"] is None
    text = prompts.render("prreview", {**lane.common_values(job, dry_ctx, ws), **lane.values(job, dry_ctx, ws)})
    assert "if any: none. The change since the head it read\n(data): none.\n\n**A full review.** Review the whole" in text


def test_an_incremental_re_review_says_so_in_its_head_line_and_its_marker(review_env):
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = rereview_job(H2, H1)
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    ws.extra["mode"] = "incremental"
    body = lane.comment_body(job, ctx, ws)
    assert body.split("\n")[0] == f"\U0001f916 AI review (codex, gpt-x) of {H1} (incremental re-review since {H2[:7]})"
    assert prreviews.prreview_marker(body)["mode"] == "incremental" and same_review_ok(body, job)
    ws.extra["mode"] = "full"
    body = lane.comment_body(job, ctx, ws)
    assert body.split("\n")[0] == f"\U0001f916 AI review (codex, gpt-x) of {H1}"
    assert prreviews.prreview_marker(body)["mode"] == "full"


def same_review_ok(body, job):
    return lane_prreview.same_review(body, job.id, H1)


def test_an_incremental_re_review_accounts_for_every_finding_of_the_previous_report(review_env, monkeypatch):
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = rereview_job(H2, H1)
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    previous = review_env / "previous.md"
    previous.write_text(PREVIOUS_REPORT)
    ws.extra.update(previous_report=previous, mode="incremental")
    assert prreviews.report_finding_ids(PREVIOUS_REPORT) == ["R01", "R02"]          # R09 sits in a code fence
    assert [p.split(" (write")[0] for p in lane.check(job, ctx, ws)] == [
        "incremental re-review: finding R01 of the previous report is not accounted for",
        "incremental re-review: finding R02 of the previous report is not accounted for"]
    accounted = ws.report.read_text().replace("## Verified sound", (
        "## Previous findings\n\n- R01 — repaired: README line 4 now assumes p odd.\n"
        "- R02 — not repaired: the locator still names no page.\n\n## Verified sound"))
    ws.report.write_text(accounted)
    assert lane.check(job, ctx, ws) == []
    ws.report.write_text(accounted.replace("- R02 — not repaired: the locator still names no page.\n", ""))
    assert lane.check(job, ctx, ws) == [
        ("incremental re-review: finding R02 of the previous report is not accounted for (write "
         "`- R02 — repaired | partly repaired | not repaired: <the passage>` under `## Previous findings`)")]
    # A full review checks the previous findings too, but the worker does not hold it to a line for each.
    ws.extra["mode"] = "full"
    assert lane.check(job, ctx, ws) == []
    assert lane_prreview.unaccounted_findings(PREVIOUS_REPORT, "| R01 | partly | x |\nR02: unrepaired") == []
    assert lane_prreview.unaccounted_findings(PREVIOUS_REPORT, "R01 — the hypothesis\nR012 repaired") == ["R01", "R02"]


# --- a review of the change to a roadmap on the base branch ---------------------------------------------

UNTOUCHED_CORRECTION = "## C02 — Untouched text: the locator of 4.2 names no page\n\nIn 4.2 (line 80). Give it.\n\n"


def test_the_review_of_changes_of_merged_roadmaps_is_off_by_default_and_a_boolean():
    assert config.PRReview().review_changes_of_merged is False
    text = '[project]\nname = "P"\n[prreview]\nreview_changes_of_merged = true\n'
    assert config.parse(text).prreview.review_changes_of_merged is True
    with pytest.raises(config.ConfigError, match="prreview.review_changes_of_merged: expected bool, got int"):
        config.parse(text.replace("true", "1"))


@pytest.mark.parametrize("on,extra,rereview,mode", [
    (True, {"on_base": True, "base_lines": 30}, False, "change"),
    (True, {"on_base": True, "base_lines": 0}, False, "change"),
    (False, {"on_base": True, "base_lines": 30}, False, "full"),                # the setting off: as before
    (True, {"on_base": False, "base_lines": None}, False, "full"),              # a roadmap the pull request adds
    (True, {"on_base": True, "base_lines": None}, False, "full"),               # its change could not be read
    (True, {"on_base": True, "base_lines": 30, "change_lines": 20, "previous_report": "r.md"}, True, "incremental"),
    (True, {"on_base": True, "base_lines": 30, "change_lines": 151, "previous_report": "r.md"}, True, "change"),
    (False, {"on_base": True, "base_lines": 30, "change_lines": 151, "previous_report": "r.md"}, True, "full")])
def test_a_review_of_a_roadmap_on_the_base_branch_reviews_its_change_with_the_setting_on(on, extra, rereview, mode):
    cfg = example_config()
    cfg.prreview.review_changes_of_merged = on
    job = new_job("PRREVIEW-Foo~2", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo", "rereview": rereview})
    assert lane_prreview.review_mode(job, cfg, extra) == mode


def merged_repo(tmp_path):
    """A content repository whose `origin/main` holds Foo, and two heads of a pull request that revises
    it, the first rewriting five lines of its README and the second eight more: (repository, main, the
    first head, the second head)."""
    repo = tmp_path / "repo"
    readme = repo / "TauCetiRoadmap" / "Foo" / "README.md"
    readme.parent.mkdir(parents=True)
    sh(tmp_path, "init", "-q", str(repo))
    heads = []
    for revised in (set(), set(range(5)), set(range(5)) | set(range(30, 38))):
        readme.write_text("# Foo\n\n" + "".join(f"line {k}{' (revised)' if k in revised else ''}\n" for k in range(40)))
        sh(repo, "add", "-A")
        sh(repo, "commit", "-q", "-m", f"revision {len(heads)}")
        heads.append(sh(repo, "rev-parse", "HEAD"))
    sh(repo, "update-ref", "refs/remotes/origin/main", heads[0])
    return repo, *heads


def merged_git_in(repo, monkeypatch):
    """The dry-run context's fake git, except that what reads the content repository's history runs in `repo`."""
    fake = lane_prreview.gitops.git

    def git(cwd, *args, **kw):
        return (REAL_GIT(repo, *args, **kw) if args[0] in ("cat-file", "diff", "ls-tree", "merge-base", "show")
                else fake(cwd, *args, **kw))
    monkeypatch.setattr(lane_prreview.gitops, "git", git)


def flat_brief(lane, job, ctx, ws):
    text = prompts.render("prreview", {**lane.common_values(job, ctx, ws), **lane.values(job, ctx, ws)})
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", text)
    return " ".join(text.split())


@needs_git
def test_a_review_of_the_change_reads_the_change_since_the_merge_base(dry_ctx, tmp_path, monkeypatch):  # noqa: F811
    repo, main, first, second = merged_repo(tmp_path)
    merged_git_in(repo, monkeypatch)
    dry_ctx.cfg.prreview.review_changes_of_merged = True
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": second, "owner": "carol", "name": "Foo", "folder": "Foo",
                                            "rereview": False})
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)
    assert ws.extra["mode"] == "change" and ws.extra["on_base"] is True and ws.extra["merge_base"] == main
    assert ws.extra["base_lines"] == 26 and ws.extra["change_lines"] is None
    text = (ws.scratch / "changes.md").read_text()
    assert text.startswith(f"# Changes since {main[:7]}, the merge base with `main` (data)\n")
    assert f"The merge base of this head with `main` is `{main}`" in text and f"this review reads `{second}`" in text
    assert "`TauCetiRoadmap/Foo/`: 26 changed lines (13 insertions, 13 deletions)." in text
    assert f"review mode: change (26 changed lines of the folder since the merge base {main[:7]} with main)" in ws.extra["notes"]
    brief = flat_brief(lane, job, dry_ctx, ws)
    assert "**A review of a change to a roadmap on `main`.**" in brief and "**A full review.**" not in brief
    assert (f"from the merge base `{main[:7]}` to this head the folder changed by 26 lines, set out in "
            f"`{ws.scratch / 'changes.md'}`") in brief
    assert "(data): none (a review of the change reads the change since the merge base instead: below)." in brief
    assert "First check finding by finding" not in brief
    # A re-review of a small repair stays incremental, on the change since the head the previous review read.
    ctx = rereview_ctx(dry_ctx, second, 16)
    rereview = rereview_job(first, second)
    ws = lane.prepare(rereview, ctx)
    assert ws.extra["mode"] == "incremental" and ws.extra["change_lines"] == 16 and ws.extra["base_lines"] == 26
    assert (ws.scratch / "changes.md").read_text().startswith(f"# Changes since {first[:7]} (data)\n")
    # A larger one is a review of the change since the merge base that first checks the previous findings.
    ctx.cfg.prreview.small_change_lines = 15
    ws = lane.prepare(rereview, ctx)
    assert ws.extra["mode"] == "change" and ws.extra["change_lines"] == 16
    assert (ws.scratch / "changes.md").read_text().startswith(f"# Changes since {main[:7]}, the merge base")
    assert (f"This re-review of a repaired head is not incremental: the folder changed by 16 lines since `{first[:7]}`, "
            "the head the previous review read (more than `[prreview] small_change_lines` = 15). First check finding "
            "by finding") in flat_brief(lane, rereview, ctx, ws)
    # The setting off: a full review as before, with the change since the previous head as context.
    ctx.cfg.prreview.review_changes_of_merged = False
    ws = lane.prepare(rereview, ctx)
    assert ws.extra["mode"] == "full" and ws.extra["on_base"] is None and ws.extra["change_lines"] == 16
    assert not any(n.startswith("review mode") for n in ws.extra.get("notes", []))
    assert (ws.scratch / "changes.md").read_text().startswith(f"# Changes since {first[:7]} (data)\n")


@pytest.mark.parametrize("fails", ["ls-tree", "merge-base"])
def test_a_git_failure_leaves_the_review_of_a_roadmap_on_the_base_full(dry_ctx, monkeypatch, fails):  # noqa: F811
    fake = lane_prreview.gitops.git

    def git(cwd, *args, **kw):
        if args[0] == fails:
            raise lane_prreview.gitops.GitError(f"git {fails} origin/main: fatal: bad revision 'origin/main'")
        return "TauCetiRoadmap/Foo/README.md\n" if args[0] == "ls-tree" else fake(cwd, *args, **kw)
    monkeypatch.setattr(lane_prreview.gitops, "git", git)
    dry_ctx.cfg.prreview.review_changes_of_merged = True
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": "abc1234" + "0" * 33, "owner": "carol", "name": "Foo",
                                            "rereview": False})
    lane = lane_for(job)
    ws = lane.prepare(job, dry_ctx)
    assert ws.extra["mode"] == "full" and ws.extra["base_lines"] is None and ws.extra["changes"] is None
    assert (f"review mode: full (TauCetiRoadmap/Foo/ not compared with origin/main: git {fails} origin/main: fatal: "
            "bad revision 'origin/main')") in ws.extra["notes"]
    assert "**A full review.** Review the whole roadmap folder as below." in flat_brief(lane, job, dry_ctx, ws)


def test_the_brief_of_a_review_of_the_change_makes_a_defect_in_untouched_text_a_remark():
    cfg = example_config()
    job = new_job("PRREVIEW-Foo~2", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo", "rereview": True,
                                            "previous_head": H2})
    extra = {"base_ref": "main", "merge_base": H3, "base_lines": 24, "changes": "changes.md", "change_lines": 400}
    text = " ".join(lane_prreview.mode_text("change", job, cfg, extra).split())
    assert text.startswith("**A review of a change to a roadmap on `main`.** This roadmap folder is on `main`")
    assert ("every passage the diff adds or changes: its statements, routes, citations and Lean declarations. Review "
            "as well every untouched passage the change affects") in text
    assert ("A defect in untouched text that the change does not affect is NOT a finding of this review, however "
            "serious") in text
    assert "whose title begins `Untouched text:` and names its place" in text and "does not block" in text
    assert "The automatic checks and the elaboration still cover the whole folder, and so does their report." in text
    assert "the build failing, or a cycle or an upward supplier, is a finding wherever it lies" in text
    assert f'opens with the sentence "A review of the change against `main`: 24 changed lines since `{H3[:7]}`."' in text
    assert (f"This re-review of a repaired head is not incremental: the folder changed by 400 lines since `{H2[:7]}`"
            in text and "First check finding by finding" not in text)       # no previous report to check against
    assert "First check finding by finding" in lane_prreview.mode_text("change", job, cfg, {**extra, "previous_report": "r.md"})
    assert "**A full review.**" in lane_prreview.mode_text("full", job, cfg, extra)


def test_a_review_of_the_change_records_its_mode_and_is_checked_as_a_full_review(review_env, monkeypatch):
    monkeypatch.setattr(lane_prreview.gitops, "git", lambda cwd, *args, **kw: H1 + "\n" if args[0] == "rev-parse" else "")
    ctx = lane_ctx(review_env, gh=LabelGH())
    job = rereview_job(H2, H1)
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    ws.extra["mode"] = "change"
    body = lane.comment_body(job, ctx, ws)
    assert body.split("\n")[0] == f"\U0001f916 AI review (codex, gpt-x) of {H1}"       # a full review's first line
    assert prreviews.prreview_marker(body)["mode"] == "change" and same_review_ok(body, job)
    assert prreviews.read_posted(body).since == ""
    # Its previous findings are checked as a full review's: no line for each is required.
    previous = review_env / "previous.md"
    previous.write_text(PREVIOUS_REPORT)
    ws.extra["previous_report"] = previous
    assert lane.check(job, ctx, ws) == []


def test_the_repair_after_a_review_of_the_change_leaves_the_untouched_text_alone(review_env):
    for status in ("accepted", "needs_changes"):
        assert lane_prreview.CHANGE_REPAIR in lane_prreview.remarks_brief(status, 150, "change")
        assert lane_prreview.CHANGE_REPAIR not in lane_prreview.remarks_brief(status, 150)
    assert "`Untouched text:`" in lane_prreview.CHANGE_REPAIR and "leave that text as it is" in lane_prreview.CHANGE_REPAIR
    report = REPORT.format(h7=H1[:7], v="accepted", findings=CORRECTION + UNTOUCHED_CORRECTION)
    assert [title for title, _b in lane_prreview.own_corrections(report, "change")] == ["a locator names no page"]
    assert len(lane_prreview.own_corrections(report, "full")) == 2
    # An acceptance whose only small corrections are on untouched text has nothing to apply.
    ctx = lane_ctx(review_env, gh=LabelGH())
    ctx.cfg.prreview.carry_minor = True
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    lane, ws = finished_ws(review_env, ctx, job, "accepted")
    ws.report.write_text(REPORT.format(h7=H1[:7], v="accepted", findings=UNTOUCHED_CORRECTION))
    ws.extra["mode"] = "change"
    assert not lane.applies_corrections(job, ctx, ws)
    ws.extra["mode"] = "full"
    assert lane.applies_corrections(job, ctx, ws)


# --- the repair of a pool author's pull request -----------------------------------------------------

DEPENDS_BLOCK = "<!-- depends-on -->\n**Depends on**\n- [ ] #77 Bar\n<!-- /depends-on -->"
PR_MARKER_LINE = '<!-- tauceti-roadmap-pr {"roadmap": "Foo"} -->'
FOOTER = "🤖 Generated with a tool"
PR_TEXT = {"title": "Foo: first layers", "body": "\n\n".join([
    "Adds Layers 1 and 2. Validation: built at the first head.", DEPENDS_BLOCK,
    "A stale paragraph between the blocks.", PR_MARKER_LINE, FOOTER]) + "\n"}


class RepairHub(ReviewHub):
    """The review hub, with the content pull request's (empty) reviews and inline comments, label
    removal, and the time of each comment."""

    def api(self, path, paginate=False):
        if path.split("?")[0] in (f"repos/{CONTENT}/pulls/905/reviews", f"repos/{CONTENT}/pulls/905/comments"):
            return []
        if path == f"repos/{CONTENT}/pulls/905":
            return {**super().api(path, paginate), **self.__dict__.setdefault("text", dict(PR_TEXT))}
        return super().api(path, paginate)

    def write(self, method, path, payload=None):
        if method == "DELETE" and f"repos/{CONTENT}/issues/905/labels/" in path:
            self.labels.remove(path.rsplit("/", 1)[1])
            return {}
        if method == "PATCH" and path == f"repos/{CONTENT}/pulls/905":
            if self.fault(path) == "refuse":
                raise GHError(f"gh api -X PATCH {path}: HTTP 503: Service Unavailable")
            self.__dict__.setdefault("text", dict(PR_TEXT)).update(payload)
            self.__dict__.setdefault("patches", []).append((dict(payload), len(self.comments[905])))
            return {}
        stamp = f"2026-10-28T10:00:{len(self.comments[905]):02d}Z"
        made = super().write(method, path, payload)
        if path.endswith("/issues/905/comments"):
            made["created_at"] = stamp
        return made


def repair_setup(local, monkeypatch, tmp_path, gate_fails=False):
    """The `hub` fixture's GitHub as a RepairHub, the author's branch ignoring `.lake/`, the gates faked
    (the review's passes; the repair's fails with `gate_fails`), and the review job of its head."""
    rh = RepairHub.__new__(RepairHub)
    rh.__dict__.update(local.__dict__, labels=[])
    work = tmp_path / "author"
    sh(tmp_path, "clone", "-q", "-b", ROADMAP_BRANCH, str(rh.content), str(work))
    (work / ".gitignore").write_text(".lake/\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-q", "-m", "ignore the Lake folder")
    sh(work, "push", "-q", "origin", f"HEAD:refs/heads/{ROADMAP_BRANCH}")
    passing = gates.GateResult("Foo", counts={"build": 0, "depcheck": 0, "readme-ids-missing": 0})
    failing = gates.GateResult("Foo", counts={"build": 1, "depcheck": 0, "readme-ids-missing": 0})
    calls = []

    def run_gate(*a, **k):
        calls.append(a)
        return failing if gate_fails and len(calls) > 1 else passing
    monkeypatch.setattr(lane_prreview.gates, "run_gate", run_gate)
    monkeypatch.setattr(lane_prreview, "open_roadmap_prs", lambda ctx: [])
    job = new_job("PRREVIEW-Foo~2", inputs={"pr": 905, "head": rh.head(905), "owner": "carol", "name": "Foo",
                                            "folder": "Foo", "rereview": False})
    job.number = 62
    return rh, rh.head(905), job


def pool_ctx(rh, pool=("alice", "carol")):
    ctx = process(rh)
    ctx.cfg.roadmap.pool = list(pool)
    return ctx


def reviewed(lane, job, ctx, head, status="needs_changes"):
    """The review as its agent left it: the report and the verdict in the board checkout."""
    ws = lane.prepare(job, ctx)
    ws.report.write_text(REPORT.format(h7=head[:7], v=status, findings=FINDING))
    lane.verdict_file(job, ctx, ws).write_text(json.dumps({"review": {
        "status": status, "reviewer": f"independent-review-{job.id}", "head": head, "pr": 905, "folder": "Foo",
        "date": "2026-10-28", "findings": 1, "blocking": 1, "notes": "x"}}))
    assert lane.validate(job, ctx, ws) == []
    return ws


REPAIR_LINE = "## H01 (AI review PRREVIEW-Foo~2) — applied\nThe README now states the hypothesis on p.\n"


PLAN_BEFORE = json.dumps({"gaps": [{"slug": "a-gap", "supplier": "Bar", "needs": "x"},
                                   {"slug": "kept-gap", "supplier": "Baz", "needs": "y"}]}, indent=1) + "\n"
MANIFEST_BEFORE = 'schema = "tauceti-roadmap-sources/v1"\nroadmap = "Foo"\n\n[[source]]\nid = "AUTH"\ntitle = "The author\'s source"\nyear = 2001\n'
NEW_RECORD = '\n[[source]]\nid = "NEW"\ntitle = "A source the README cites"\nyear = 2020\n'


def seed_board_plan_and_manifest(rh, tmp_path):
    """The board's main with Foo's plan (two gaps) and sources manifest (one entry of the author)."""
    seed = tmp_path / "board-seed"
    sh(tmp_path, "clone", "-q", "-b", "main", str(rh.board), str(seed))
    (seed / "packages" / "Foo" / "design" / "plan.json").write_text(PLAN_BEFORE)
    (seed / "packages" / "Foo" / "sources.toml").write_text(MANIFEST_BEFORE)
    sh(seed, "add", "-A")
    sh(seed, "commit", "-q", "-m", "Foo: plan and manifest")
    sh(seed, "push", "-q", "origin", "HEAD:refs/heads/main")


REPAIR_MODELS = []     # the model each repair run was asked for ('' = the worker's own)


def repair_agent(monkeypatch, briefs, report=REPAIR_LINE, edit=True, stray=False, forget_commit_once=False,
                 board_edits=None, description=(), summary=()):
    """`description`: what the agent writes as the pull request's new text, run by run (the last one from then on);
    `summary`: the same for the summary block."""
    def run_agent(self, ctx, ws, brief, label="", model=""):
        REPAIR_MODELS.append(model)
        briefs.append((label, brief, (ws.scratch / "reviews.md").read_text()))
        if description:
            (ws.scratch / lane_prreview.DESCRIPTION).write_text(description[min(len(briefs), len(description)) - 1])
        if summary:
            (ws.scratch / "summary.md").write_text(summary[min(len(briefs), len(summary)) - 1])
        for rel, text in (board_edits or {}).items():
            (ws.board / rel).write_text(text)
        if edit:
            (ws.workdir / "TauCetiRoadmap" / "Foo" / "README.md").write_text("# Foo\n\nFor every odd prime p.\n")
            if not (forget_commit_once and len(briefs) == 1) and sh(ws.workdir, "status", "--short").strip():
                sh(ws.workdir, "commit", "-q", "-am", "Foo: review repairs (the hypothesis on p)")
        if stray:
            (ws.board / "packages" / "Foo" / "stray.md").write_text("not a deliverable\n")
        (ws.job_dir / lane_prreview.REPAIR_REPORT).write_text(report)
        return SimpleNamespace(exit_code=0, limited=False, seconds=1.0, timed_out=False)
    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", run_agent)


@needs_git
def test_the_repair_runs_on_the_model_configured_for_the_reviewing_model(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.repair_models = {ctx.model: "claude-opus-5-5", "another-model": "x"}
    lane = lane_for(job)
    lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert REPAIR_MODELS[-1] == "claude-opus-5-5"
    ctx.cfg.prreview.repair_models = {"another-model": "x"}          # a reviewing model not listed repairs with itself
    assert ctx.cfg.prreview.repair_models.get(ctx.model, "") == ""


@needs_git
def test_a_review_asking_changes_of_a_pool_author_repairs_and_answers_in_the_same_job(
        hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, stray=True)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    new = rh.head(905)
    assert result.status == "submitted", result.detail
    assert new != head and sh(rh.content, "rev-parse", f"{new}^") == head
    assert f"repaired at {new[:7]} and answered the review (awaiting-review)" in result.detail
    # The repair ran the gate brief on the review comment just posted, as the review to answer.
    [(label, brief, reviews)] = briefs
    review, reply = rh.comments[905]
    assert label == "repair" and brief.startswith("# Answering human review — job PRREVIEW-Foo~2")
    assert "roadmap folder\n`TauCetiRoadmap/Foo/`" in brief and str(ws.job_dir / "repair-report.md") in brief
    assert "## AI review PRREVIEW-Foo~2 at 2026-10-28T10:00:00Z" in reviews and "CHANGES NEEDED" in reviews
    # The reply answers that comment, names the worker and the head, and the labels follow.
    assert reply["body"].startswith(f"Fixed in `{new[:7]}` (reply to the review):\n\n- **H01** (applied)")
    assert replies.answered_record(reply["body"]) == {
        "job": job.id, "answered": {f"comment-{review['id']}": replies.text_version(review["body"])},
        "worker": "alice-1", "head": new}
    events = lane_pr.events_from_api([], rh.comments[905])
    assert replies.unanswered(events, {"alice", "carol"}, config.Config().replies.review_markers,
                              config.Config().replies.housekeeping_prefixes, owner="carol") == []
    assert rh.labels == ["awaiting-review"]
    # The board gets the review alone, whatever the repair agent left in the board checkout.
    [opened] = rh.opened
    assert "Files: `packages/Foo/reports/PRREVIEW-Foo~2.json`, `packages/Foo/reports/PRREVIEW-Foo~2.md`" in opened["body"]
    assert ctx.board.states == [(job.id, "submitted")] and lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_repair_that_fails_the_checks_gets_a_second_pass_with_them(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, forget_commit_once=True)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert [label for label, _b, _r in briefs] == ["repair", "repair-2"]
    assert "## The worker's checks failed" in briefs[1][1] and "- uncommitted changes left in the worktree" in briefs[1][1]
    assert rh.head(905) != head and "repaired at" in result.detail


@needs_git
def test_a_repair_that_fails_the_gate_leaves_the_review_submitted(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path, gate_fails=True)
    repair_agent(monkeypatch, [])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert "; repair not pushed: its checks failed: build=1" in result.detail
    assert rh.head(905) == head and len(rh.comments[905]) == 1 and rh.labels == []
    assert ctx.board.states == [(job.id, "submitted")] and lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_repair_stopped_after_its_push_is_finished_without_a_second_push_or_reply(
        hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    rh.fail("/issues/905/comments", None, "crash")             # the reply is posted, then the process dies
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    with pytest.raises(KeyboardInterrupt):
        lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    new = rh.head(905)
    record = lane_base.read_pending(ctx, job)
    assert record["done"] == ["content-comment", "repair", "content-push", "repair-text"] and record["repair"] == {"sha": new}
    assert record["outstanding"] == ["repair-comment", "push", "pull-request", "submitted"]

    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", lambda *a, **k: pytest.fail("the agent ran again"))
    ctx = pool_ctx(rh)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    assert rh.head(905) == new and len(rh.comments[905]) == 2 and rh.labels == ["awaiting-review"]
    assert ctx.board.states == [(job.id, "submitted")] and lane_base.read_pending(ctx, job) is None


# --- a repair is carried onto a head that moved outside its folder -------------------------------------------
#
# 2026-11-10: the three roadmap folders of one pull request were reviewed in parallel. The first repair's
# push moved the head, and the two other repairs, which change other folders, were not pushed. A repair is
# now put on top of such a head, checked there, and pushed; any other move still stops the push.

def moving(monkeypatch, tmp_path, rh, rel, text="changed meanwhile\n"):
    """The pull request's branch gains a commit that writes `rel` while the repair agent runs."""
    inner = lane_prreview.PRReviewLane.run_agent

    def run_agent(self, ctx, ws, brief, label="", model=""):
        out, other = inner(self, ctx, ws, brief, label), tmp_path / "other"
        if not other.exists():
            sh(tmp_path, "clone", "-q", "-b", ROADMAP_BRANCH, str(rh.content), str(other))
            (other / rel).parent.mkdir(parents=True, exist_ok=True)
            (other / rel).write_text(text)
            sh(other, "add", "-A")
            sh(other, "commit", "-q", "-m", "a change made meanwhile")
            sh(other, "push", "-q", "origin", f"HEAD:refs/heads/{ROADMAP_BRANCH}")
        return out
    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", run_agent)


@needs_git
def test_a_repair_is_carried_onto_a_head_that_moved_in_another_roadmap_folder(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    moving(monkeypatch, tmp_path, rh, "TauCetiRoadmap/Bar/README.md")
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    new = rh.head(905)
    onto = sh(rh.content, "rev-parse", f"{new}^")
    assert result.status == "submitted", result.detail
    # The other folder's commit is kept, the repair is the one commit on top of it, and it changes this folder only.
    assert onto != head and sh(rh.content, "rev-parse", f"{onto}^") == head
    assert sh(rh.content, "diff", "--name-only", onto, new).split() == ["TauCetiRoadmap/Foo/README.md"]
    assert sh(rh.content, "show", f"{new}:TauCetiRoadmap/Bar/README.md") == "changed meanwhile"
    assert f"repaired at {new[:7]} on top of {onto[:7]} and answered the review (awaiting-review)" in result.detail
    # The reply names the carried head, says what happened, and records that head as the one answered at.
    review, reply = rh.comments[905]
    assert reply["body"].startswith(f"Fixed in `{new[:7]}` (reply to the review):")
    assert (f"The review read `{head[:7]}`. The pull request moved to `{onto[:7]}` while this repair was made, by "
            "changes outside `Foo/`") in reply["body"]
    assert replies.answered_record(reply["body"])["head"] == new
    assert rh.labels == ["awaiting-review"] and lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_carried_repair_stopped_after_its_push_is_finished_with_the_same_reply(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    moving(monkeypatch, tmp_path, rh, "TauCetiRoadmap/Bar/README.md")
    rh.fail("/issues/905/comments", None, "crash")             # the reply is posted, then the process dies
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    with pytest.raises(KeyboardInterrupt):
        lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    new = rh.head(905)
    onto = sh(rh.content, "rev-parse", f"{new}^")
    assert onto != head and lane_base.read_pending(ctx, job)["repair"] == {"sha": new, "onto": onto}

    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", lambda *a, **k: pytest.fail("the agent ran again"))
    ctx = pool_ctx(rh)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    assert rh.head(905) == new and len(rh.comments[905]) == 2 and rh.labels == ["awaiting-review"]
    assert f"The pull request moved to `{onto[:7]}` while this repair was made" in rh.comments[905][1]["body"]
    assert f"repaired at {new[:7]} on top of {onto[:7]}" in result.detail


@needs_git
@pytest.mark.parametrize("rel", ["TauCetiRoadmap/Foo/Suggested.lean", "lake-manifest.json"])
def test_a_repair_is_not_carried_over_a_change_to_its_folder_or_outside_the_roadmaps(hub, monkeypatch, tmp_path, rel):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    moving(monkeypatch, tmp_path, rh, rel)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    moved = rh.head(905)
    assert result.status == "submitted", result.detail
    assert f"; repair not pushed: the pull request moved from {head[:7]} to {moved[:7]} while the fix ran" in result.detail
    assert sh(rh.content, "rev-parse", f"{moved}^") == head and len(rh.comments[905]) == 1 and rh.labels == []


def moved_already(tmp_path, rh, rel, text="changed meanwhile\n"):
    """The pull request's branch gains a commit that writes `rel` after the review and before its repair."""
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", "-b", ROADMAP_BRANCH, str(rh.content), str(other))
    (other / rel).parent.mkdir(parents=True, exist_ok=True)
    (other / rel).write_text(text)
    sh(other, "add", "-A")
    sh(other, "commit", "-q", "-m", "a change made meanwhile")
    sh(other, "push", "-q", "origin", f"HEAD:refs/heads/{ROADMAP_BRANCH}")


@needs_git
@pytest.mark.parametrize("rel", ["TauCetiRoadmap/Foo/Suggested.lean", "lake-manifest.json"])
def test_no_repair_is_made_for_a_head_that_already_moved_in_its_folder_or_outside_the_roadmaps(
        hub, monkeypatch, tmp_path, rel):  # noqa: F811
    # 2026-11-12: an author pushed to four pull requests while their reviews ran; each review was followed by a
    # full repair of the old head, which could not be pushed.
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", lambda *a, **k: pytest.fail("the repair agent ran"))
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    moved_already(tmp_path, rh, rel)
    result = lane.deliver(job, ctx, ws)
    moved = rh.head(905)
    assert result.status == "submitted", result.detail
    assert (f"; repair not pushed: the pull request moved from {head[:7]} to {moved[:7]} before the repair started; "
            "no repair was made") in result.detail
    assert sh(rh.content, "rev-parse", f"{moved}^") == head and len(rh.comments[905]) == 1 and rh.labels == []
    assert ctx.board.states == [(job.id, "submitted")] and lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_head_that_already_moved_in_another_roadmap_folder_is_still_repaired_and_carried(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    moved_already(tmp_path, rh, "TauCetiRoadmap/Bar/README.md")
    result = lane.deliver(job, ctx, ws)
    new = rh.head(905)
    onto = sh(rh.content, "rev-parse", f"{new}^")
    assert result.status == "submitted", result.detail
    assert onto != head and sh(rh.content, "rev-parse", f"{onto}^") == head
    assert f"repaired at {new[:7]} on top of {onto[:7]} and answered the review (awaiting-review)" in result.detail


@needs_git
def test_a_carried_repair_that_fails_the_gate_on_the_moved_head_is_not_pushed(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    moving(monkeypatch, tmp_path, rh, "TauCetiRoadmap/Bar/README.md")
    calls = []

    def run_gate(*a, **k):                  # the review's gate and the repair's pass; the carried head's fails
        calls.append(a)
        return gates.GateResult("Foo", counts={"build": int(len(calls) > 2), "depcheck": 0, "readme-ids-missing": 0})
    monkeypatch.setattr(lane_prreview.gates, "run_gate", run_gate)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    moved = rh.head(905)
    assert result.status == "submitted", result.detail
    assert f"; repair not pushed: the pull request moved to {moved[:7]} while the fix ran, and on that head its checks failed: build=1" in result.detail
    assert sh(rh.content, "rev-parse", f"{moved}^") == head and len(rh.comments[905]) == 1 and rh.labels == []


# 2026-11-10: on 22 open pull requests the reviews asked for a merge of the base branch, round after round:
# the branch pinned an older Tau Ceti than main. A repair works in its roadmap folder and could not make
# it. The repair now starts by merging the base branch when the branch's Lake pins are behind.

PINS_MOVED = {"lake-manifest.json": '{"packages": ["a newer pin"]}\n', "TauCetiRoadmap/Bar/README.md": "# Bar\n"}


def main_moves(rh, tmp_path, files=PINS_MOVED):
    """The content repository's main gains a commit that writes `files`; returns it."""
    seed = tmp_path / "main-moves"
    sh(tmp_path, "clone", "-q", "-b", "main", str(rh.content), str(seed))
    for rel, text in files.items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    sh(seed, "add", "-A")
    sh(seed, "commit", "-q", "-m", "main moves")
    sh(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    return sh(seed, "rev-parse", "HEAD")


def gate_bases(monkeypatch):
    """The gates faked as passing, the `base` of each recorded."""
    bases = []

    def run_gate(*a, **k):
        bases.append(sh(a[0], "rev-parse", k["base"]))
        return gates.GateResult("Foo", counts={"build": 0, "depcheck": 0, "readme-ids-missing": 0})
    monkeypatch.setattr(lane_prreview.gates, "run_gate", run_gate)
    return bases


@needs_git
def test_a_repair_first_merges_the_base_branch_into_a_branch_whose_pins_are_behind(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    tip, briefs, bases = main_moves(rh, tmp_path), [], gate_bases(monkeypatch)
    repair_agent(monkeypatch, briefs)
    rh.fail("/issues/905/comments", None, "crash")             # the reply is posted, then the process dies
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    with pytest.raises(KeyboardInterrupt):
        lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    new = rh.head(905)
    merged = sh(rh.content, "rev-parse", f"{new}^")
    # The branch is the reviewed head, the merge of main, and the repair's one commit in its folder.
    assert sh(rh.content, "rev-parse", f"{merged}^1") == head and sh(rh.content, "rev-parse", f"{merged}^2") == tip
    assert sh(rh.content, "log", "-1", "--format=%an|%s", merged) == "alice|Merge main into this branch"
    assert sh(rh.content, "diff", "--name-only", merged, new).split() == ["TauCetiRoadmap/Foo/README.md"]
    assert sh(rh.content, "show", f"{new}:lake-manifest.json") == PINS_MOVED["lake-manifest.json"].strip()
    # The agent was told, the repair's gate took the merge as what the repair starts from, and the record keeps it.
    [(_, brief, _)] = briefs
    assert f"the worker merged `main` into this worktree (commit `{merged[:7]}`)" in brief
    assert bases[-1] == merged
    assert lane_base.read_pending(ctx, job)["repair"] == {"sha": new, "merged": merged}

    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", lambda *a, **k: pytest.fail("the agent ran again"))
    ctx = pool_ctx(rh)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    assert rh.head(905) == new and len(rh.comments[905]) == 2 and rh.labels == ["awaiting-review"]
    assert f"repaired at {new[:7]} with main merged at {merged[:7]} and answered the review" in result.detail
    reply = rh.comments[905][1]["body"]
    assert reply.startswith(f"Fixed in `{new[:7]}` (reply to the review):")
    assert (f"The review read `{head[:7]}`, whose Lake pins were behind `main`. Before the repair, `main` was "
            f"merged into the branch at `{merged[:7]}`") in reply


@needs_git
def test_a_merge_of_the_base_branch_alone_is_pushed_when_the_folder_needs_no_change(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    tip = main_moves(rh, tmp_path)
    repair_agent(monkeypatch, [], edit=False)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    new = rh.head(905)
    assert result.status == "submitted", result.detail
    assert sh(rh.content, "rev-parse", f"{new}^1") == head and sh(rh.content, "rev-parse", f"{new}^2") == tip
    assert f"repaired at {new[:7]} with main merged at {new[:7]} and answered the review" in result.detail


@needs_git
@pytest.mark.parametrize("case", ["main's pins did not move", "the branch moved its own pins", "conflict", "switched off"])
def test_a_repair_merges_no_base_branch_it_need_not_or_cannot(hub, monkeypatch, tmp_path, case):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    if case == "the branch moved its own pins":
        work = tmp_path / "author"
        (work / "lake-manifest.json").write_text('{"packages": ["the author\'s pin"]}\n')
        sh(work, "add", "-A")
        sh(work, "commit", "-q", "-m", "a pin bump of the author's")
        sh(work, "push", "-q", "origin", f"HEAD:refs/heads/{ROADMAP_BRANCH}")
        head = rh.head(905)
        job.inputs["head"] = head
    main_moves(rh, tmp_path, {"TauCetiRoadmap/Bar/README.md": "# Bar\n"} if case == "main's pins did not move"
               else {**PINS_MOVED, ".gitignore": "build/\n"} if case == "conflict" else PINS_MOVED)
    briefs, bases = [], gate_bases(monkeypatch)
    repair_agent(monkeypatch, briefs)
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.repair_merges_base = case != "switched off"
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    new = rh.head(905)
    assert result.status == "submitted", result.detail
    # The repair is the one commit on the reviewed head, as before.
    assert sh(rh.content, "rev-parse", f"{new}^") == head and sh(rh.content, "rev-list", "--count", "--merges", new) == "0"
    assert f"repaired at {new[:7]} and answered the review" in result.detail and bases[-1] == head
    assert "the worker merged" not in briefs[0][1] and "was merged into the branch" not in rh.comments[905][1]["body"]
    notes = "\n".join(ws.extra.get("notes") or [])
    assert ("main does not merge into the branch without conflicts" in notes) == (case == "conflict")


def moving_with_main(monkeypatch, tmp_path, rh, merge=True):
    """While the repair agent runs, another folder's repair of the same head is pushed: with `merge`,
    its own merge of main, then its commit in the folder Bar."""
    inner = lane_prreview.PRReviewLane.run_agent

    def run_agent(self, ctx, ws, brief, label="", model=""):
        out, other = inner(self, ctx, ws, brief, label), tmp_path / "other"
        if not other.exists():
            sh(tmp_path, "clone", "-q", "-b", ROADMAP_BRANCH, str(rh.content), str(other))
            if merge:
                sh(other, "merge", "-q", "--no-ff", "-m", "Merge main into this branch", "origin/main")
            (other / "TauCetiRoadmap" / "Bar").mkdir(parents=True, exist_ok=True)
            (other / "TauCetiRoadmap" / "Bar" / "README.md").write_text("# Bar\n\nrepaired meanwhile\n")
            sh(other, "add", "-A")
            sh(other, "commit", "-q", "-m", "Bar: review repairs")
            sh(other, "push", "-q", "origin", f"HEAD:refs/heads/{ROADMAP_BRANCH}")
        return out
    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", run_agent)


@needs_git
def test_a_repair_that_merged_the_base_branch_is_carried_onto_another_folders_repair_that_merged_it_too(
        hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    tip = main_moves(rh, tmp_path)
    repair_agent(monkeypatch, [])
    moving_with_main(monkeypatch, tmp_path, rh)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    new = rh.head(905)
    onto = sh(rh.content, "rev-parse", f"{new}^")
    assert result.status == "submitted", result.detail
    # The other repair's merge and commit are kept; this repair's commit stands on them, its own merge left behind.
    assert sh(rh.content, "log", "-1", "--format=%s", onto) == "Bar: review repairs"
    assert sh(rh.content, "rev-parse", f"{onto}^^1") == head and sh(rh.content, "rev-parse", f"{onto}^^2") == tip
    assert sh(rh.content, "rev-list", "--count", "--merges", new) == "1"
    assert sh(rh.content, "diff", "--name-only", onto, new).split() == ["TauCetiRoadmap/Foo/README.md"]
    assert f"repaired at {new[:7]} on top of {onto[:7]} and answered the review (awaiting-review)" in result.detail
    reply = rh.comments[905][1]["body"]
    assert f"The pull request moved to `{onto[:7]}` while this repair was made" in reply
    assert "was merged into the branch" not in reply


@needs_git
def test_a_repair_that_merged_the_base_branch_is_not_carried_onto_a_head_without_that_merge(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    main_moves(rh, tmp_path)
    repair_agent(monkeypatch, [])
    moving_with_main(monkeypatch, tmp_path, rh, merge=False)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    moved = rh.head(905)
    assert result.status == "submitted", result.detail
    assert f"; repair not pushed: the pull request moved from {head[:7]} to {moved[:7]} while the fix ran" in result.detail
    assert sh(rh.content, "rev-parse", f"{moved}^") == head and len(rh.comments[905]) == 1 and rh.labels == []


# --- a repair answers the findings about the pull request's title and description ---------------------------
#
# 2026-11-10: a repair may edit the roadmap folder only, so a finding about the description (it describes an
# earlier head, it names a supplier the roadmap no longer has) was "disputed" or left to the author round
# after round: one pull request carried such a finding through thirty-two reviews, and the repairs' reports
# drafted the replacement text nobody posted. The repair now writes it and the worker puts it on the pull
# request, keeping the generated blocks.

NEW_TEXT = "Title: Foo: the hypothesis on p\n\nStates Layer 1 for every odd prime p.\n\nCloses #5.\n"


@needs_git
def test_a_repair_rewrites_the_title_and_description_and_keeps_the_generated_blocks(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, description=[NEW_TEXT])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    new = rh.head(905)
    assert result.status == "submitted", result.detail
    assert f"repaired at {new[:7]}, title and description rewritten and answered the review (awaiting-review)" in result.detail
    [(_label, brief, _reviews)] = briefs
    assert "The pull request's title and description as well" in brief
    assert str(ws.scratch / "pull-request.md") in brief and str(ws.scratch / "description.md") in brief
    # The repair also rewrites what its own change makes wrong, and is told how to see that change.
    assert "and for what your own repair makes wrong in them" in brief
    assert f"`git -C {ws.workdir} diff --stat origin/main...HEAD -- TauCetiRoadmap/Foo` shows it at yours" in brief
    assert "When neither a finding nor your repair makes the title or description wrong" in brief
    assert "The roadmap is on `origin/main`: this pull request revises it." in (ws.scratch / "pull-request.md").read_text()
    # One write, after the push and before the reply: the prose, then the blocks, the marker and the footer as they were.
    [(payload, comments_then)] = rh.patches
    assert comments_then == 1 and len(rh.comments[905]) == 2
    assert payload == {"title": "Foo: the hypothesis on p", "body": "\n\n".join([
        "States Layer 1 for every odd prime p.\n\nCloses #5.", DEPENDS_BLOCK, PR_MARKER_LINE, FOOTER]) + "\n"}
    assert "pull request #905: title and description rewritten" in ws.extra["notes"]
    assert lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_description_alone_is_delivered_when_the_files_need_no_change(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [], edit=False, description=["Title: Foo: first layers\n\nStates Layer 1 for every odd prime p.\n"])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert "answered the review, no change to the files needed, description rewritten (awaiting-review)" in result.detail
    [(payload, _n)] = rh.patches
    assert rh.head(905) == head and list(payload) == ["body"] and payload["body"].startswith("States Layer 1")
    # The head did not move, so the reply's record is what tells the intake that the text did.
    record = replies.answered_record(rh.comments[905][-1]["body"])
    assert record["head"] == head and record["text"] is True


OLD_SUMMARY = ("<!-- roadmap-summary -->\n**What this roadmap builds.** Layers 1 and 2 for every prime p, with the "
               "even prime included.\n\n**Why Tau Ceti wants it.** It feeds the later layers of the library with the "
               "first statements.\n\n**Approach and generality.** The route is the direct one, over an arbitrary "
               "commutative ring.\n<!-- /roadmap-summary -->")
NEW_SUMMARY = ("**What this roadmap builds.** Layers 1 and 2 for every odd prime p, with the even prime left out.\n\n"
               "**Why Tau Ceti wants it.** It feeds the later layers of the library with the first statements.\n\n"
               "**Approach and generality.** The route is the direct one, over an arbitrary commutative ring.\n")


def with_summary(rh):
    rh.text = {**PR_TEXT, "body": OLD_SUMMARY + "\n\n" + PR_TEXT["body"]}


@needs_git
def test_a_repair_rewrites_the_summary_block_and_leaves_the_rest_of_the_description(hub, monkeypatch, tmp_path):  # noqa: F811
    """2026-11-11: the summary block is written once, and a review's finding that it describes an earlier
    head came back round after round, since the repair was told that a generated block is not its to repair."""
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    with_summary(rh)
    briefs = []
    repair_agent(monkeypatch, briefs, edit=False, summary=[NEW_SUMMARY])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted", result.detail
    assert "answered the review, no change to the files needed, summary rewritten (awaiting-review)" in result.detail
    [(_label, brief, _reviews)] = briefs
    assert "The summary block that opens the description as well" in brief and str(ws.scratch / "summary.md") in brief
    assert "**What this roadmap builds.**, **Why Tau Ceti wants it.**, **Approach and generality.**" in brief
    assert "the dependency task list or of the merge order is not yours to repair" in brief
    [(payload, _n)] = rh.patches
    assert list(payload) == ["body"]
    assert payload["body"] == ("<!-- roadmap-summary -->\n" + NEW_SUMMARY.strip() + "\n<!-- /roadmap-summary -->\n\n"
                               + PR_TEXT["body"])
    # The head did not move: the reply's record tells the intake that the text did.
    assert replies.answered_record(rh.comments[905][-1]["body"])["text"] is True


@needs_git
def test_a_repair_writes_the_description_and_the_summary_in_one_write(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    with_summary(rh)
    repair_agent(monkeypatch, [], description=[NEW_TEXT], summary=[NEW_SUMMARY])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert "title, description and summary rewritten and answered the review" in result.detail
    [(payload, _n)] = rh.patches
    assert payload == {"title": "Foo: the hypothesis on p", "body": "\n\n".join([
        "<!-- roadmap-summary -->\n" + NEW_SUMMARY.strip() + "\n<!-- /roadmap-summary -->",
        "States Layer 1 for every odd prime p.\n\nCloses #5.", DEPENDS_BLOCK, PR_MARKER_LINE, FOOTER]) + "\n"}


@needs_git
def test_a_summary_out_of_form_goes_to_the_second_pass_and_none_is_asked_without_a_block(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    with_summary(rh)
    briefs = []
    repair_agent(monkeypatch, briefs, edit=False, summary=["One paragraph about pull request #905.\n", NEW_SUMMARY])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert [label for label, _b, _r in briefs] == ["repair", "repair-2"]
    assert "summary.md: a pull-request or issue number" in briefs[1][1]
    assert "summary.md: it is not the three paragraphs" in briefs[1][1]
    assert "summary rewritten" in result.detail and len(rh.patches) == 1


@needs_git
def test_a_description_without_a_summary_block_gets_no_summary_brief_and_none_written(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, edit=False, summary=[NEW_SUMMARY])     # written unasked: ignored
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert "The summary block that opens the description as well" not in briefs[0][1]
    assert "answered the review, no change needed" in result.detail and not rh.__dict__.get("patches")


@needs_git
def test_the_summary_is_written_when_the_prose_changed_under_the_repair(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    with_summary(rh)
    repair_agent(monkeypatch, [], edit=False, description=[NEW_TEXT], summary=[NEW_SUMMARY])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    theirs = OLD_SUMMARY + "\n\nThe author rewrote this meanwhile.\n\n" + DEPENDS_BLOCK + "\n"
    rh.text["body"] = theirs
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted", result.detail
    assert ("title and description not written: the description was changed while the repair ran; summary rewritten"
            in result.detail)
    [(payload, _n)] = rh.patches
    assert list(payload) == ["body"] and "The author rewrote this meanwhile." in payload["body"]
    assert "odd prime p, with the even prime left out" in payload["body"] and "even prime included" not in payload["body"]


FAMILY_README = "TauCetiRoadmap/Family/README.md"


def family_checkout(tmp_path, change_index):
    """A checkout whose branch adds the nested roadmap Family/Toy to a family already on `origin/main`,
    and changes the family's README with `change_index`."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "clone", "-q", str(origin), str(work))
    family = work / "TauCetiRoadmap" / "Family"
    (family / "Old").mkdir(parents=True)
    (family / "README.md").write_text("# Family\n\n- Old: the first member.\n")
    (family / "PROGRESS.md").write_text("progress\n")
    (family / "Old" / "README.md").write_text("# Old\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-q", "-m", "the family")
    sh(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    (family / "Toy").mkdir()
    (family / "Toy" / "README.md").write_text("# Toy\n")
    (family / "PROGRESS.md").write_text("progress, changed\n")
    if change_index:
        (family / "README.md").write_text("# Family\n\n- Old: the first member.\n- Toy: owns everything.\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-q", "-m", "Toy")
    return work


@needs_git
@pytest.mark.parametrize("change_index, folder, expected", [
    (True, "Family/Toy", [FAMILY_README]),     # the pull request changes the index: the repair may answer for it
    (False, "Family/Toy", []),                 # an index the pull request leaves alone stays out of reach
    (True, "Toy", []),                         # a top-level roadmap has no family
])
def test_a_repair_may_edit_the_family_index_only_when_the_pull_request_changes_it(tmp_path, change_index, folder, expected):
    """2026-11-11: three members of one family came back round after round for what the family index said
    of them, since the repair was held to the roadmap's own folder."""
    work = family_checkout(tmp_path, change_index)
    ctx = SimpleNamespace(cfg=SimpleNamespace(roadmap=SimpleNamespace(active_dir="TauCetiRoadmap"),
                                              project=SimpleNamespace(branch="main")))
    ws = SimpleNamespace(workdir=work, extra={"head": sh(work, "rev-parse", "HEAD").strip(), "base_ref": "main"})
    files = lane_prreview.family_index_changed(ctx, ws, folder)
    assert files == expected                    # the README only: never PROGRESS.md, which the branch also changed
    brief = lane_prreview.family_brief(files, folder)
    assert bool(brief) == bool(expected)
    if expected:
        assert f"`{FAMILY_README}` in the worktree as well" in brief and "of `Family/Toy` only" in brief
        assert "leave every other roadmap's entry" in brief


@needs_git
@pytest.mark.parametrize("enabled", [True, False])
def test_the_repair_brief_names_the_family_index_unless_the_option_is_off(hub, monkeypatch, tmp_path, enabled):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    asked = []
    monkeypatch.setattr(lane_prreview, "family_index_changed",
                        lambda ctx, ws, folder: asked.append(folder) or [FAMILY_README])
    briefs = []
    repair_agent(monkeypatch, briefs)
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.repair_index = enabled
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert asked == (["Foo"] if enabled else [])
    assert (f"`{FAMILY_README}` in the worktree as well" in briefs[0][1]) is enabled
    # The paragraph on the worktree's files comes before the one on the board's.
    if enabled:
        assert briefs[0][1].index(FAMILY_README) < briefs[0][1].index("Two files of the board checkout as well")


@needs_git
def test_a_description_delivered_before_a_crash_is_reported_the_same_by_the_next_attempt(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [], edit=False, description=["Title: Foo: first layers\n\nStates Layer 1 for every odd prime p.\n"])
    rh.fail("/issues/905/comments", None, "crash")             # the reply is posted, then the process dies
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    with pytest.raises(KeyboardInterrupt):
        lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    # what was done to the text, and the digest of the text that write left (a check of the text records it)
    assert lane_base.read_pending(ctx, job)["text"] == {
        "line": "description rewritten", "digest": summary.text_digest("Foo: first layers", rh.patches[0][0]["body"])}
    assert lane_prreview.text_rewritten(lane_prreview.delivered_text(ctx, job))
    assert not lane_prreview.text_rewritten("title and description not written: the description was changed while the repair ran")

    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", lambda *a, **k: pytest.fail("the agent ran again"))
    ctx = pool_ctx(rh)
    result = lane_for(job).run(job, ctx)
    assert result.status == "submitted", result.detail
    assert "answered the review, no change to the files needed, description rewritten (awaiting-review)" in result.detail
    # The record still says the text was rewritten, so the reply already posted is recognised: no second one.
    assert len(rh.comments[905]) == 2 and len(rh.patches) == 1
    assert replies.answered_record(rh.comments[905][-1]["body"])["text"] is True


@needs_git
def test_a_description_the_worker_refuses_goes_back_to_the_repair(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, description=[
        "Title: Foo\n\nFixed, thanks @carol.\n\n<!-- depends-on -->\n- [x] #77\n<!-- /depends-on -->\n", NEW_TEXT])
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted", result.detail
    assert [label for label, _b, _r in briefs] == ["repair", "repair-2"]
    assert "- description.md: it carries an HTML comment" in briefs[1][1]
    assert "- description.md: it carries an @-mention" in briefs[1][1]
    [(payload, _n)] = rh.patches
    assert payload["title"] == "Foo: the hypothesis on p" and DEPENDS_BLOCK in payload["body"] and "[x] #77" not in payload["body"]


@needs_git
def test_a_refused_description_write_leaves_the_repair_and_its_reply(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [], description=[NEW_TEXT])
    rh.fail("/pulls/905", "refuse")
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted", result.detail
    assert "title and description not written: " in result.detail and "HTTP 503" in result.detail
    assert rh.head(905) != head and len(rh.comments[905]) == 2 and "patches" not in rh.__dict__
    assert rh.text == PR_TEXT and lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_description_changed_while_the_repair_ran_is_not_overwritten(hub, monkeypatch, tmp_path):  # noqa: F811
    # 2026-11-10: three folders of one pull request were repaired at once; each repair's description was
    # written from the text all three had read, so the last one delivered would have undone the others'.
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [], description=[NEW_TEXT])
    inner = lane_prreview.PRReviewLane.run_agent
    theirs = PR_TEXT["body"].replace("\n\n", " Another folder's repair says more here.\n\n", 1)

    def run_agent(self, ctx, ws, brief, label="", model=""):
        out = inner(self, ctx, ws, brief, label)
        rh.__dict__.setdefault("text", dict(PR_TEXT))["body"] = theirs
        return out
    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", run_agent)
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted", result.detail
    assert "title and description not written: the description was changed while the repair ran" in result.detail
    assert rh.head(905) != head and len(rh.comments[905]) == 2 and "patches" not in rh.__dict__
    assert rh.text["body"] == theirs and lane_base.read_pending(ctx, job) is None


def test_the_prose_of_a_description_leaves_out_what_the_worker_keeps():
    body = "\n\n".join(["States  Layer 1.", DEPENDS_BLOCK, "Closes #5.  ", PR_MARKER_LINE, FOOTER])
    assert lane_prreview.description_prose(body) == "States Layer 1.\nCloses #5."
    assert lane_prreview.description_prose(lane_prreview.merged_description("States Layer 1.\n\nCloses #5.", body)) == \
        lane_prreview.description_prose(body)


@needs_git
def test_with_repair_text_off_the_brief_does_not_offer_it_and_a_description_is_ignored(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, description=[NEW_TEXT])
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.repair_text = False
    lane = lane_for(job)
    result = lane.deliver(job, ctx, reviewed(lane, job, ctx, head))
    assert result.status == "submitted" and "rewritten" not in result.detail
    assert "title and description as well" not in briefs[0][1] and "patches" not in rh.__dict__


def test_the_description_keeps_blocks_markers_and_the_attribution_line_in_their_order():
    old = "\n\n".join(["Old prose.", DEPENDS_BLOCK, "More old prose.", "<!-- merge-order -->\n**Merge order.** #77 → this.\n<!-- /merge-order -->",
                       PR_MARKER_LINE, FOOTER])
    assert lane_prreview.merged_description("New prose.\n", old) == "\n\n".join([
        "New prose.", DEPENDS_BLOCK, "<!-- merge-order -->\n**Merge order.** #77 → this.\n<!-- /merge-order -->",
        PR_MARKER_LINE, FOOTER]) + "\n"
    assert lane_prreview.merged_description("New prose.", "Old prose, no block.") == "New prose.\n"
    assert lane_prreview.merged_description(f"New prose.\n\n{FOOTER}", f"Old.\n\n{FOOTER}") == f"New prose.\n\n{FOOTER}\n"
    # an unclosed marker is kept as a comment; the line under it is prose and goes
    assert lane_prreview.merged_description("New.", "Old.\n\n<!-- x -->\nits line\n") == "New.\n\n<!-- x -->\n"


@pytest.mark.parametrize("text,problem", [
    ("", None), ("Title: Foo: a\n\nIt adds §2 and cites `@[simp]` and user@example.org.\n", None),
    ("Foo: a\n\nIt adds §2.\n", "its first line is not `Title: <the title>`"),
    ("Title: Foo\n", "it has a title and no description"),
    ("Title: " + "x" * 201 + "\n\nIt adds §2.\n", "its title is over 200 characters"),
    ("Title: Foo\n\n" + "x" * 12001, "its description is over 12000 characters"),
    ("Title: Foo\n\nSee <!-- tauceti-roadmap-pr {} -->\n", "it carries an HTML comment"),
    ("Title: Foo\n\nBuilt in HOME/work.\n", "it names a path of this machine"),
    ("Title: Foo\n\nAs @carol asked.\n", "it carries an @-mention"),
])
def test_a_description_is_read_only_in_the_form_asked(tmp_path, text, problem):
    ctx = lane_ctx(tmp_path)
    path = tmp_path / "description.md"
    path.write_text(text.replace("HOME", str(Path.home())))
    got, problems = lane_prreview.read_description(path, ctx)
    if problem is None:
        assert problems == [] and (got is None) == (not text)
    else:
        assert got is None and any(problem in p for p in problems), problems


@pytest.mark.parametrize("change,repairs", [
    ({}, True),
    ({"status": "rejected"}, False), ({"status": "accepted"}, False), ({"dry_run": True}, False),
    ({"pool": ["alice"]}, False),                       # the author is not in the pool
    ({"pool": ["carol"]}, False),                       # the operator is not
    ({"repair_pool": False}, False), ({"head_repo": ""}, False),
])
def test_only_a_review_asking_changes_of_a_pool_author_by_a_pool_operator_repairs(tmp_path, change, repairs):
    ctx = lane_ctx(tmp_path, dry_run=change.get("dry_run", False))
    ctx.cfg.roadmap.pool = change.get("pool", ["alice", "carol"])
    ctx.cfg.prreview.repair_pool = change.get("repair_pool", True)
    ws = SimpleNamespace(extra={"owner": "carol", "pr_branch": "roadmap/foo",
                                "head_repo": change.get("head_repo", "TauCetiProject/TauCetiRoadmap")},
                         report=tmp_path / "no-report.md")
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
    assert lane_for(job).repairs(job, ctx, ws, change.get("status", "needs_changes")) is repairs
    assert config.Config().prreview.repair_pool is True


def test_a_review_of_a_pool_author_in_a_dry_run_or_a_rejection_never_repairs(review_env, monkeypatch):
    monkeypatch.setattr(lane_prreview.PRReviewLane, "run_agent", lambda *a, **k: pytest.fail("the repair ran"))
    for status, dry_run in (("rejected", False), ("needs_changes", True)):
        gh = LabelGH()
        ctx = lane_ctx(review_env, gh=gh, dry_run=dry_run)
        ctx.cfg.roadmap.pool = ["alice", "carol"]
        job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": H1, "owner": "carol", "name": "Foo"})
        lane, ws = finished_ws(review_env, ctx, job, status)
        ws.extra.update(pr_branch="roadmap/foo", head_repo="TauCetiProject/TauCetiRoadmap")
        summary = lane.finish(job, ctx, ws)
        assert "repair" not in summary and len(gh.comments) == (0 if dry_run else 1)
        lane_base.clear_pending(ctx, job)


def test_a_stacked_pull_request_is_reviewed_against_its_own_base(dry_ctx):  # noqa: F811
    head = "abc1234" + "0" * 33
    gh = ContentGH(pulls={905: {"number": 905, "state": "open", "head": {"sha": head, "ref": "roadmap/child"},
                                "base": {"ref": "roadmap/diamonds-and-v-stacks"}}})
    ctx = dataclasses.replace(dry_ctx, gh=gh)
    job = new_job("PRREVIEW-Foo~1", inputs={"pr": 905, "head": head, "owner": "carol", "name": "Foo"})
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    assert ws.extra["base_ref"] == "roadmap/diamonds-and-v-stacks"
    values = lane.values(job, ctx, ws)
    assert values["GATE"].endswith("--base origin/roadmap/diamonds-and-v-stacks")
    assert values["OUTSIDE_CHECK"].startswith(f"git -C {ws.workdir} diff --stat origin/roadmap/diamonds-and-v-stacks...HEAD")
    ws = lane.prepare(job, dry_ctx)                           # nothing read: the project branch
    assert lane.values(job, dry_ctx, ws)["GATE"].endswith("--base origin/main")


def test_the_managed_pull_requests_name_their_base(tmp_path):
    marker = '<!-- tauceti-roadmap-pr {"roadmap": "PerfectoidTowers"} -->'
    pulls = [{"number": 816, "labels": [{"name": "roadmap-worker"}], "body": marker, "user": {"login": "alice"},
              "head": {"sha": H1, "ref": "roadmap/perfectoid-towers", "repo": {"full_name": "TauCetiProject/TauCetiRoadmap"}},
              "base": {"ref": "roadmap/diamonds-and-v-stacks"}},
             {"number": 817, "labels": [{"name": "roadmap-worker"}], "body": marker.replace("PerfectoidTowers", "Other"),
              "user": {"login": "alice"}, "head": {"sha": H2, "ref": "roadmap/other", "repo": None}}]
    ctx = SimpleNamespace(cfg=example_config(), gh=SimpleNamespace(api=lambda path, paginate=False: pulls),
                          state=tmp_path)
    assert [(p["pr"], p["base"]) for p in lane_base.open_roadmap_prs(ctx)] == [
        (816, "roadmap/diamonds-and-v-stacks"), (817, "main")]


@needs_git
def test_a_repair_delivers_its_plan_gap_retirements_and_manifest_additions_with_the_review(hub, monkeypatch, tmp_path):  # noqa: F811
    """2026-11-03: five rounds in a row disputed board-side findings (a plan gap the README now owns, a
    cited source the manifest lacks) that the repair could not deliver: those two edits now ride on
    the review's submission; a stray file does not."""
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    seed_board_plan_and_manifest(rh, tmp_path)
    briefs = []
    retired = json.dumps({"gaps": [{"slug": "kept-gap", "supplier": "Baz", "needs": "y"}]}, indent=1) + "\n"
    repair_agent(monkeypatch, briefs, stray=True, board_edits={
        "packages/Foo/design/plan.json": retired, "packages/Foo/sources.toml": MANIFEST_BEFORE + NEW_RECORD})
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted", result.detail
    [(label, brief, _reviews)] = briefs
    assert "Two files of the board checkout as well" in brief and "packages/Foo/design/plan.json" in brief
    [opened] = rh.opened
    assert ("Files: `packages/Foo/design/plan.json`, `packages/Foo/reports/PRREVIEW-Foo~2.json`, "
            "`packages/Foo/reports/PRREVIEW-Foo~2.md`, `packages/Foo/sources.toml`") in opened["body"]
    assert "board edits delivered with the review: packages/Foo/design/plan.json, packages/Foo/sources.toml" in ws.extra["notes"]
    assert lane_base.read_pending(ctx, job) is None


@needs_git
def test_a_repair_that_declares_a_gap_or_rewrites_an_author_entry_delivers_neither(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    seed_board_plan_and_manifest(rh, tmp_path)
    briefs = []
    declared = json.dumps({"gaps": [{"slug": "a-gap", "supplier": "Bar", "needs": "x"},
                                    {"slug": "kept-gap", "supplier": "Baz", "needs": "y"},
                                    {"slug": "new-gap", "supplier": "Qux", "needs": "z"}]}, indent=1) + "\n"
    rewritten = MANIFEST_BEFORE.replace("year = 2001", "year = 1999") + NEW_RECORD
    repair_agent(monkeypatch, briefs, board_edits={"packages/Foo/design/plan.json": declared,
                                                   "packages/Foo/sources.toml": rewritten})
    ctx = pool_ctx(rh)
    lane = lane_for(job)
    ws = reviewed(lane, job, ctx, head)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted", result.detail
    [opened] = rh.opened
    assert "Files: `packages/Foo/reports/PRREVIEW-Foo~2.json`, `packages/Foo/reports/PRREVIEW-Foo~2.md`" in opened["body"]
    notes = "\n".join(ws.extra["notes"])
    assert ("the repair's edit of packages/Foo/design/plan.json is not delivered: packages/Foo/design/plan.json: "
            "a review's repair declares only gaps the README names as `gap:<slug>` (new-gap)") in notes
    assert "the repair's edit of packages/Foo/sources.toml is not delivered: sources.toml: a repair keeps the author's entries: AUTH: year changed" in notes


def test_a_title_the_project_fixes_survives_a_repair():
    from tauceti_roadmap_worker.lanes import prreview as lp
    cfg = example_config()
    cfg.prreview.fixed_title_prefixes = ["Template update: "]
    assert lp.fixed_title_prefix(cfg, "Template update: Foo") == "Template update: "
    assert lp.fixed_title_prefix(cfg, "Foo: roadmap") == "" and lp.fixed_title_prefix(cfg, "") == ""
    want = {"title": "Template update and new targets: Foo", "body": "What it changes."}
    assert lp.kept_title(cfg, want, "Template update: Foo") == {"body": "What it changes."}
    assert lp.kept_title(cfg, want, "Foo: roadmap") == want
    doc = lp.pull_request_document(7, {"title": "Template update: Foo", "body": "x"}, "someone", fixed="Template update: ")
    assert "The project fixes this title" in doc and "`Template update: <Name>`" in doc
    assert "The project fixes this title" not in lp.pull_request_document(7, {"title": "Foo: roadmap", "body": "x"}, "someone")
    cfg.prreview.fixed_title_prefixes = [""]
    from tauceti_roadmap_worker import config as config_module
    with pytest.raises(config_module.ConfigError, match="fixed_title_prefixes must list non-empty title prefixes"):
        config_module.validate(cfg)


def test_plan_and_manifest_edit_guards():
    from tauceti_roadmap_worker.lanes import prreview as lp
    before = json.dumps({"gaps": [{"slug": "a"}, {"slug": "b"}], "layers": []})
    assert lp.plan_edit_problems("p/plan.json", before, json.dumps({"gaps": [{"slug": "b"}], "layers": []})) == []
    declared = json.dumps({"gaps": [{"slug": "b"}, {"slug": "c"}], "layers": []})
    assert lp.plan_edit_problems("p/plan.json", before, declared) == [
        "p/plan.json: a review's repair declares only gaps the README names as `gap:<slug>` (c)"]
    # A gap the repaired README names is declared with the review, once the entry says what is missing and of whom.
    readme = "- What Bar must provide (gap:c): the comparison map.\n"
    assert lp.plan_edit_problems("p/plan.json", before, declared, readme) == [
        "p/plan.json: gap c: `needs` (one sentence: what is missing) is required"]
    full = json.dumps({"gaps": [{"slug": "b"}, {"slug": "c", "needs": "the comparison map", "supplier": "Bar",
                                                 "section": "§1.8"}], "layers": []})
    assert lp.plan_edit_problems("p/plan.json", before, full, readme) == []
    assert lp.plan_edit_problems("p/plan.json", before, full, "The README names no gap.") == [
        "p/plan.json: a review's repair declares only gaps the README names as `gap:<slug>` (c)"]
    assert lp.plan_edit_problems("p/plan.json", before, json.dumps({"gaps": [], "layers": [1]})) == [
        "p/plan.json: at the package stage only its top-level `gaps` list may change"]
    assert lp.plan_edit_problems("p/plan.json", before, "{") == ["invalid JSON in p/plan.json"]
    assert lp.manifest_edit_problems(MANIFEST_BEFORE, MANIFEST_BEFORE + NEW_RECORD) == []
    assert lp.manifest_edit_problems(MANIFEST_BEFORE, "[[source") == ["sources.toml does not parse: Expected ']]' at the end of a table declaration (at line 1, column 9)"][:0] or \
        lp.manifest_edit_problems(MANIFEST_BEFORE, "[[source")[0].startswith("sources.toml does not parse")
    assert lp.manifest_edit_problems(MANIFEST_BEFORE, MANIFEST_BEFORE.replace("2001", "1999"))[0].startswith(
        "sources.toml: a repair keeps the author's entries")


# --- operator-filed rounds and parent roadmaps ----------------------------------------------------

def test_the_operator_files_the_rounds_of_a_listed_pull_request():
    out, fake = sync(ContentGH([node(617, H1)]), operator_prs=[617])
    assert fake.created == [] and out == ["#617: rounds filed by the operator (prreview.operator_prs)"]
    _out, fake = sync(ContentGH([node(617, H1)]), operator_prs=[9])
    assert created(fake) == [("PRREVIEW-Foo~1", "Foo")]


def test_a_parent_roadmap_keeps_its_own_readme_under_review():
    entry = {"files": [{"path": "TauCetiRoadmap/Family/README.md", "change": "added"},
                       {"path": "TauCetiRoadmap/Family/Inner/README.md", "change": "added"}]}
    assert intake.review_folders(entry, "TauCetiRoadmap", "Completed", set()) == {"Family/Inner": "added"}
    assert intake.review_folders(entry, "TauCetiRoadmap", "Completed", set(), {"Family"}) == {
        "Family": "added", "Family/Inner": "added"}
    paths = [(f"TauCetiRoadmap/Family/{p}", "ADDED") for p in ("README.md", "Inner/README.md")]
    _out, fake = sync(ContentGH([node(44, H2, paths=paths)]), per_run=20, parent_roadmaps=["Family"])
    assert sorted(folder for _id, folder in created(fake)) == ["Family", "Family/Inner"]


def test_operator_prs_and_parent_roadmaps_parse_and_are_checked():
    parsed = config.parse('[project]\nname = "P"\n[prreview]\noperator_prs = [617, 1854]\n'
                          'parent_roadmaps = ["RiemannianGeometry"]\n', "x.toml").prreview
    assert (parsed.operator_prs, parsed.parent_roadmaps) == ([617, 1854], ["RiemannianGeometry"])
    for bad, why in (("operator_prs = [0]", "prreview.operator_prs"), ('operator_prs = ["617"]', "prreview.operator_prs"),
                     ('parent_roadmaps = ["not a folder"]', "prreview.parent_roadmaps")):
        with pytest.raises(config.ConfigError, match=re.escape(why)):
            config.parse(f'[project]\nname = "P"\n[prreview]\n{bad}\n', "x.toml")


# --- rounds that end without another review: P3-only repairs, findings outside the folder ----------------

def carried_file(job, record, status="needs_changes"):
    """The verdict of `job` (a review of the head it names) with the worker's `carried` record."""
    head = job.inputs["head"]
    return {board.prreview_verdict_path("packages", job): json.dumps({"review": {
        "status": status, "reviewer": f"independent-review-{job.id}", "head": head, "pr": job.inputs["pr"],
        "folder": board.prreview_folder(job), "carried": record}})}


def test_a_p3_only_review_ends_with_its_repair():
    """`[prreview] carry_minor`: the folder is settled at the head its P3-only repair left, the label is
    added there and no review of that head is filed; without the setting the repaired head is reviewed."""
    done = review_job(41, 1, 12, H1, "done")
    record = {"from": H1, "head": H2, "findings": 2, "lines": 12, "folders": ["Foo"]}
    labels = f"repos/{REPO}/issues/12/labels"
    gh = ContentGH([node(12, H2)], files=carried_file(done, record))
    out, fake = sync(gh, [done], carry_minor=True)
    assert gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})] and fake.created == []
    assert out == [f"#12: ai-found-no-errors added (every roadmap folder is settled at {H2[:7]}: the last reviews accepted, "
                   "or found P3 points only and their repairs are in)"]
    # Labelled already: kept, silently.
    gh = ContentGH([node(12, H2, labels=["ai-found-no-errors"])], files=carried_file(done, record))
    assert sync(gh, [done], carry_minor=True)[0] == [] and gh.writes == []
    # Off by default: the repaired head gets its review.
    gh = ContentGH([node(12, H2)], files=carried_file(done, record))
    out, fake = sync(gh, [done])
    assert gh.writes == [] and created(fake) == [("PRREVIEW-Foo~2", "Foo")]
    # An answer that changed nothing settles the head reviewed: no dispute round, and the label.
    same = {"from": H1, "head": H1, "findings": 1, "lines": 0, "folders": []}
    gh = ContentGH([node(12, H1)], files=carried_file(done, same))
    out, fake = sync(gh, [done], carry_minor=True)
    assert gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})] and fake.created == []


@pytest.mark.parametrize("record,status,head", [
    ({"from": H1, "head": H2, "findings": 2, "lines": 12, "folders": ["Foo"]}, "needs_changes", H3),   # moved on since
    ({"from": H1, "head": H2, "findings": 2, "lines": 151, "folders": ["Foo"]}, "needs_changes", H2),  # too big a repair
    ({"from": H3, "head": H2, "findings": 2, "lines": 12, "folders": ["Foo"]}, "needs_changes", H2),   # not of the head reviewed
    ({"from": H1, "head": H2, "findings": 2, "lines": "12", "folders": ["Foo"]}, "needs_changes", H2), # a malformed record
    ({"from": H1, "head": H2, "findings": 2, "lines": 12, "folders": ["Foo"]}, "rejected", H2),        # not a repairable verdict
    ("yes", "needs_changes", H2),
])
def test_a_carried_record_that_does_not_hold_settles_nothing(record, status, head):
    done = review_job(41, 1, 12, H1, "done")
    gh = ContentGH([node(12, head)], files=carried_file(done, record, status))
    out, fake = sync(gh, [done], carry_minor=True)
    assert gh.writes == [] and created(fake) == [("PRREVIEW-Foo~2", "Foo")]


def test_a_p3_only_repair_of_one_folder_leaves_the_other_folders_settled():
    foo, bar = review_job(41, 1, 1164, H1, "done", folder="Foo"), review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    labels = f"repos/{REPO}/issues/1164/labels"
    step = {"from": H1, "head": H2, "findings": 1, "lines": 4, "folders": ["Bar"]}
    # Foo accepted at H1; Bar's P3-only repair moved the head to H2 and changed Bar alone: both are settled at H2.
    gh = ContentGH([two_folder_pr(H2)], files={**verdict_file(foo, "accepted", H1), **carried_file(bar, step)})
    out, fake = sync(gh, [foo, bar], carry_minor=True)
    assert gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})] and fake.created == []
    # The repair's push changed Foo too, or a file outside the roadmap folders (a merge of the base branch):
    # Foo is reviewed at the new head, Bar is not, and the label waits for Foo.
    for wider in ({**step, "folders": ["Bar", "Foo"]}, {**step, "outside": True}, {**step, "folders": None}):
        gh = ContentGH([two_folder_pr(H2)], files={**verdict_file(foo, "accepted", H1), **carried_file(bar, wider)})
        out, fake = sync(gh, [foo, bar], carry_minor=True)
        assert gh.writes == [] and created(fake) == [("PRREVIEW-Foo~2", "Foo")], wider
    # Two P3-only repairs in a row, the second carried onto the first (`onto`): both folders are settled at H3.
    first = {"from": H1, "head": H2, "findings": 1, "lines": 3, "folders": ["Foo"]}
    second = {"from": H1, "onto": H2, "head": H3, "findings": 2, "lines": 6, "folders": ["Bar"]}
    gh = ContentGH([two_folder_pr(H3)], files={**carried_file(foo, first), **carried_file(bar, second)})
    out, fake = sync(gh, [foo, bar], carry_minor=True)
    assert gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})] and fake.created == []
    # A folder whose review found more than P3 points is reviewed again at the head the other's repair left.
    gh = ContentGH([two_folder_pr(H2)], files={**verdict_file(foo, "needs_changes", H1), **carried_file(bar, step)})
    out, fake = sync(gh, [foo, bar], carry_minor=True)
    assert gh.writes == [] and created(fake) == [("PRREVIEW-Foo~2", "Foo")]


CORRECTION = "## C01 — a locator names no page\n\nThe locator of 1.1 gives no page (line 9). Give it.\n\n"
# A P3 finding, as the reviews before 2026-11-18 wrote them: still read wherever a report of then is read.
MINOR = "## R01 (P3) — a locator names no page\n\nLocation: README line 9.\n\n"


def reviewed_with(lane, job, ctx, head, remarks, blocking, status="needs_changes"):
    """The review as its agent left it, with the findings and small corrections given."""
    ws = lane.prepare(job, ctx)
    ws.report.write_text(REPORT.format(h7=head[:7], v=status, findings=remarks))
    lane.verdict_file(job, ctx, ws).write_text(json.dumps({"review": {
        "status": status, "reviewer": f"independent-review-{job.id}", "head": head, "pr": 905, "folder": "Foo",
        "date": "2026-10-28", "findings": remarks.count("## R"), "blocking": blocking, "notes": "x",
        "carried": {"from": head, "head": head, "lines": 0}, "deferred": [{"id": "D09", "title": "the agent's own"}]}}))
    lane.scrub_outputs(job, ctx, ws)              # as the run does before it validates
    assert lane.validate(job, ctx, ws) == []
    return ws


def submitted_verdict(lane, job, ctx, ws):
    """The verdict record as the board submission carries it."""
    rel = board.prreview_verdict_path(ctx.cfg.board.packages_dir, job)
    assert json.loads(sh(ws.board, "show", f"HEAD:{rel}")) == json.loads(lane.verdict_file(job, ctx, ws).read_text())
    return json.loads(lane.verdict_file(job, ctx, ws).read_text())["review"]


@needs_git
def test_an_acceptance_applies_its_small_corrections_in_one_small_commit_and_the_acceptance_carries(
        hub, monkeypatch, tmp_path):  # noqa: F811
    """(2026-11-18) A review of a pool author's head that accepts with small corrections applies them in one
    small commit; its verdict records the commit (`carried`, with `corrections`), and the intake reads the
    folder as settled at the head the corrections left, as for the P3-only repairs before it. Format notes
    alone push nothing."""
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, report="## H01 (AI review PRREVIEW-Foo~2) — applied\nThe page is given.\n")
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.carry_minor = True
    lane = lane_for(job)
    ws = reviewed_with(lane, job, ctx, head, CORRECTION, 0, status="accepted")
    result = lane.deliver(job, ctx, ws)
    new = rh.head(905)
    assert result.status == "submitted" and new != head, result.detail
    assert f"small corrections applied: no further review of {new[:7]} for this folder" in result.detail
    assert "This review accepts the head." in briefs[0][1] and "Apply its small corrections" in briefs[0][1]
    review = submitted_verdict(lane, job, ctx, ws)
    lines = review["carried"]["lines"]
    assert review["status"] == "accepted" and review["carried"] == {
        "from": head, "head": new, "findings": 1, "lines": lines, "folders": ["Foo"], "corrections": True}
    reply = rh.comments[905][-1]["body"]
    assert ("This review accepted the head; its small corrections are applied here and the repair changes "
            f"{lines} line{'' if lines == 1 else 's'} of the folder") in reply
    # The intake reads the record, and settles the folder at the new head.
    assert intake.carried_record(review, 150) == review["carried"]
    assert intake.carried_record({**review, "carried": {**review["carried"], "corrections": False}}, 150) is None


@needs_git
def test_a_review_of_the_change_applies_only_its_own_corrections_and_posts_its_mode(hub, monkeypatch, tmp_path):  # noqa: F811
    """`[prreview] review_changes_of_merged`: Foo is on main, so its review reads the change since the merge
    base; the acceptance applies its small correction on the change and leaves the one on untouched text."""
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, report="## H01 (AI review PRREVIEW-Foo~2) — applied\nThe page is given.\n")
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.carry_minor = True
    ctx.cfg.prreview.review_changes_of_merged = True
    lane = lane_for(job)
    ws = reviewed_with(lane, job, ctx, head, CORRECTION + UNTOUCHED_CORRECTION, 0, status="accepted")
    assert ws.extra["mode"] == "change" and ws.extra["on_base"] is True and ws.extra["base_lines"] == 0
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted" and rh.head(905) != head, result.detail
    assert lane_prreview.CHANGE_REPAIR in briefs[0][1]
    assert submitted_verdict(lane, job, ctx, ws)["carried"]["findings"] == 1
    [review] = [c["body"] for c in rh.comments[905] if prreviews.prreview_marker(c["body"])]
    assert prreviews.prreview_marker(review)["mode"] == "change"
    assert "**C02** Untouched text: the locator of 4.2 names no page" in review


@needs_git
@pytest.mark.parametrize("check", [True, False])
def test_a_check_of_the_text_that_corrects_the_description_records_the_text_it_leaves(
        hub, monkeypatch, tmp_path, check):  # noqa: F811
    """(2026-11-19) A check of an accepted head's text that accepts it and applies a small correction to the
    description left a text no review had read, so the intake filed the next check, which corrected a
    sentence in turn: three checks of one unchanged head in two hours. The check's verdict now records the
    text it leaves, and the intake files nothing for it. A review that is not a check of the text keeps the
    digest of the text it read, so its rewritten description still gets its one check."""
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    if check:
        job.inputs.update(text_check=True, text_digest="0" * 16)
    repair_agent(monkeypatch, [], report="## H01 (AI review PRREVIEW-Foo~2) — applied\nThe description says it.\n",
                 edit=False, description=["Title: Foo: first layers\n\nStates Layer 1 for every odd prime p.\n"])
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.carry_minor = True
    lane = lane_for(job)
    ws = reviewed_with(lane, job, ctx, head, CORRECTION, 0, status="accepted")
    read = json.loads(lane.verdict_file(job, ctx, ws).read_text())["review"].get("text")
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted" and rh.head(905) == head, result.detail
    assert "description rewritten" in result.detail and "small corrections applied" in result.detail
    [(payload, _n)] = rh.patches
    left = summary.text_digest(payload.get("title", "Foo: first layers"), payload["body"])
    review = submitted_verdict(lane, job, ctx, ws)
    assert read and read != left                                     # the review read the text before the rewrite
    assert review["text"] == (left if check else read)
    assert ("the description as corrected here is the text this check read" in result.detail) is check


@needs_git
@pytest.mark.parametrize("remarks,blocking,status,on", [
    (FINDING, 1, "needs_changes", True), (CORRECTION + FINDING, 1, "needs_changes", True),
    (CORRECTION, 0, "accepted", False), ("## N01 — a link names a pull request\n\nName the roadmap.\n\n", 0,
                                         "accepted", True)])
def test_findings_or_the_setting_off_or_format_notes_alone_carry_nothing(
        hub, monkeypatch, tmp_path, remarks, blocking, status, on):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    repair_agent(monkeypatch, [])
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.carry_minor = on
    lane = lane_for(job)
    ws = reviewed_with(lane, job, ctx, head, remarks, blocking, status=status)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted" and "small corrections applied" not in result.detail
    assert "carried" not in submitted_verdict(lane, job, ctx, ws)
    if status == "needs_changes":
        assert "repaired at" in result.detail
    else:                                   # nothing pushed: an acceptance without corrections to apply
        assert rh.head(905) == head and "repaired at" not in result.detail and "labelled ai-found-no-errors" in result.detail


OUTSIDE_LINE = ("## H01 (AI review PRREVIEW-Foo~2) — outside this folder: the Bar roadmap's README\n"
                "F01: Bar's README has to state the hypothesis on p; nothing in this folder can.\n")


@needs_git
def test_a_finding_only_another_folder_can_settle_is_recorded_once(hub, monkeypatch, tmp_path):  # noqa: F811
    rh, head, job = repair_setup(hub, monkeypatch, tmp_path)
    briefs = []
    repair_agent(monkeypatch, briefs, report=OUTSIDE_LINE, edit=False)
    ctx = pool_ctx(rh)
    ctx.cfg.prreview.defer_outside = True
    lane = lane_for(job)
    ws = reviewed_with(lane, job, ctx, head, FINDING, 1)
    result = lane.deliver(job, ctx, ws)
    assert result.status == "submitted" and rh.head(905) == head, result.detail
    assert "1 finding deferred as outside the folder (D01)" in result.detail
    assert "outside this folder: <where the change belongs, at most 15 words>" in briefs[0][1]
    review = submitted_verdict(lane, job, ctx, ws)
    assert review["deferred"] == [{"id": "D01", "severity": "P1", "title": "the hypothesis on p is missing",
                                   "where": "the Bar roadmap's README", "raised": job.id}]
    assert "carried" not in review
    assert "- **H01** (outside this folder: the Bar roadmap's README)" in rh.comments[905][-1]["body"]


def test_deferred_findings_are_numbered_on_matched_to_the_review_and_not_entered_twice():
    review = (MINOR + "## R02 (P2) — the comparison map is owned by Bar\n\nLocation: README line 30.\n\n"
              "## R03 (P1) — the hypothesis on p is missing\n\nLocation: README line 4.\n\n")
    standing = [{"id": "D02", "severity": "P2", "title": "An older one", "where": "Baz", "raised": "PRREVIEW-Foo~1"}]
    repair = ("## H01 (AI review PRREVIEW-Foo~2) — applied\nThe locator names the page.\n\n"
              "## H02 (AI review PRREVIEW-Foo~2) — outside this folder: `Bar/README.md`, Layer 2\nF02: Bar must hand it over.\n\n"
              "## H03 (AI review PRREVIEW-Foo~2) — Outside the folder — the library\nNo number named here.\n")
    assert lane_prreview.new_deferred(review, repair, "PRREVIEW-Foo~2", standing) == [
        {"id": "D03", "severity": "P2", "title": "the comparison map is owned by Bar", "where": "`Bar/README.md`, Layer 2",
         "raised": "PRREVIEW-Foo~2"},
        {"id": "D04", "severity": "P1", "title": "the hypothesis on p is missing", "where": "the library",
         "raised": "PRREVIEW-Foo~2"}]
    # One standing already under the same title is not entered again; a section naming no finding of the review
    # (and not in a finding's place) takes its own first sentence.
    again = [{"id": "D01", "severity": "P2", "title": "The comparison map is owned by Bar.", "where": "Bar", "raised": "x"}]
    loose = "## H01 (carol) — outside this folder: Bar\nF02: as before.\n\n## H02 (carol) — outside this folder: Baz\nBaz owns the trace. More.\n"
    assert lane_prreview.new_deferred(review, loose, "PRREVIEW-Foo~3", again) == [
        {"id": "D02", "severity": "", "title": "Baz owns the trace.", "where": "Baz", "raised": "PRREVIEW-Foo~3"}]
    assert lane_prreview.new_deferred(review, REPAIR_LINE, "PRREVIEW-Foo~2", []) == []
    assert lane_prreview.minor_findings(MINOR) == 1 and lane_prreview.minor_findings(review) == 0
    assert lane_prreview.minor_findings("no findings") == 0


@needs_git
def test_a_later_review_is_told_of_the_deferred_findings_and_keeps_those_it_does_not_resolve(dry_ctx, tmp_path, monkeypatch):  # noqa: F811
    repo, first, second = repaired_repo(tmp_path)
    real_git_in(repo, monkeypatch)
    job = rereview_job(first, second)
    ctx = rereview_ctx(dry_ctx, second)
    ctx.cfg.prreview.defer_outside = True
    held = [{"id": "D01", "severity": "P2", "title": "the comparison map is owned by Bar", "where": "Bar's README",
             "raised": "PRREVIEW-Foo~1"},
            {"id": "D02", "severity": "P1", "title": "the trace formula is missing from the library", "where": "the library",
             "raised": "PRREVIEW-Foo~1"}]
    ctx.gh.files["packages/Foo/reports/PRREVIEW-Foo~1.json"] = json.dumps({"review": {
        "status": "needs_changes", "head": first, "pr": 905, "deferred": held + [{"id": "bad"}]}})
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    assert ws.extra["deferred"] == held
    text = prompts.render("prreview", {**lane.common_values(job, ctx, ws), **lane.values(job, ctx, ws)})
    assert "**Deferred findings.**" in text and "not raise them again" in text
    assert "- D01 (P2, raised by PRREVIEW-Foo~1) — the comparison map is owned by Bar. Outside this folder: Bar's README." in text
    # The review resolves one and says nothing of the other: the other stands in its verdict.
    ws.report.write_text(REPORT.format(h7=second[:7], v="accepted", findings="")
                         + "\n## Deferred findings\n\n- D01 — resolved: Bar's README hands the map over since its last revision\n")
    lane.verdict_file(job, ctx, ws).parent.mkdir(parents=True, exist_ok=True)
    lane.verdict_file(job, ctx, ws).write_text(json.dumps({"review": {
        "status": "accepted", "reviewer": f"independent-review-{job.id}", "head": second, "pr": 905,
        "date": "2026-10-28", "findings": 0, "blocking": 0, "notes": "x", "deferred": []}}))
    lane.scrub_outputs(job, ctx, ws)
    assert json.loads(lane.verdict_file(job, ctx, ws).read_text())["review"]["deferred"] == [held[1]]
    # Another pull request's review of the same name hands nothing on; nor does the setting when off.
    ctx.gh.files["packages/Foo/reports/PRREVIEW-Foo~1.json"] = json.dumps({"review": {"pr": 906, "deferred": held}})
    assert lane.previous_deferred(job, ctx) == []
    ctx.cfg.prreview.defer_outside = False
    assert lane.previous_deferred(job, ctx) == []


def test_the_round_settings_default_off_and_parse():
    p = config.Config().prreview
    assert (p.carry_minor, p.defer_outside, p.late_authors, p.late_except) == (False, False, [], [])
    parsed = config.parse('[project]\nname = "P"\n[prreview]\ncarry_minor = true\ndefer_outside = true\n'
                          'late_authors = ["dave"]\nlate_except = [12]\n', "x.toml").prreview
    assert (parsed.carry_minor, parsed.defer_outside, parsed.late_authors, parsed.late_except) == (True, True, ["dave"], [12])
    for bad, why in (('late_authors = ["not a login"]', "prreview.late_authors"), ("late_except = [0]", "prreview.late_except")):
        with pytest.raises(config.ConfigError, match=re.escape(why)):
            config.parse(f'[project]\nname = "P"\n[prreview]\n{bad}\n', "x.toml")


def test_no_round_is_filed_for_a_head_a_human_has_approved():
    """A round that ends in a repair pushes to the branch, and the content repository dismisses an
    approval on any push: an approved pull request gets no review, re-review or check of its text."""
    approved = {**node(12, H1), "reviewDecision": "APPROVED"}
    out, fake = sync(ContentGH([approved, node(13, H1)]))
    assert [j.inputs["pr"] for j, _s, _p in fake.created] == [13]
    assert f"#12: no round filed: a human approval stands at {H1[:7]}" in out
    # the same head without the approval is reviewed as any other
    out, fake = sync(ContentGH([{**node(12, H1), "reviewDecision": "REVIEW_REQUIRED"}]))
    assert [j.id for j, _s, _p in fake.created] == ["PRREVIEW-Foo~1"]


# --- aggregate pull requests: folders reviewed in their members ------------------------------------

def aggregate_nodes(foo_labels=("ai-found-no-errors",), bar_labels=("ai-found-no-errors",), labels=()):
    """Two members (#12 adds Foo at H1, #13 adds Bar at H2) and the aggregate #99, which adds both at H3."""
    foo, bar = ("TauCetiRoadmap/Foo/README.md", "ADDED"), ("TauCetiRoadmap/Bar/README.md", "ADDED")
    return [node(12, H1, paths=[foo], labels=foo_labels), node(13, H2, paths=[bar], labels=bar_labels),
            node(99, H3, paths=[foo, bar], labels=labels)]


SAME_TREES = {(H1, "TauCetiRoadmap/Foo"): "foo", (H3, "TauCetiRoadmap/Foo"): "foo",
              (H2, "TauCetiRoadmap/Bar"): "bar", (H3, "TauCetiRoadmap/Bar"): "bar"}


def test_an_aggregate_is_not_reviewed_and_is_labelled_when_its_members_are_accepted_with_the_same_files():
    foo, bar = review_job(41, 1, 12, H1, "done"), review_job(42, 1, 13, H2, "done", name="Bar")
    files = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H2)}
    labels = f"repos/{REPO}/issues/99/labels"
    gh = ContentGH(aggregate_nodes(), files=files, trees=SAME_TREES)
    out, fake = sync(gh, [foo, bar], aggregate_prs=[99], pool=["alice"])
    assert fake.created == []                                   # no review round of the aggregate
    # The acceptance is recorded on the aggregate (the folders' files at its head), then the label is added.
    [(verb, path, sent), label] = gh.writes
    assert (verb, path) == ("POST", f"repos/{REPO}/issues/99/comments") and sent["body"].startswith("Housekeeping at `")
    assert intake.aggregate_recorded([replies.Event("t", "Alice", sent["body"])], {"alice"}) == {"Foo": {"foo"}, "Bar": {"bar"}}
    assert label == ("POST", labels, {"labels": ["ai-found-no-errors"]})
    assert out == ["#99: acceptance recorded for 2 roadmap folders at " + H3[:7] + " (aggregate)",
                   "#99: ai-found-no-errors added (aggregate: all 2 roadmap folders hold the files of accepted members)"]
    # The note is housekeeping, whoever reads the timeline: no review to answer (on a pull request of someone else's too).
    cfg = example_config()
    for user in ("alice", "someone-else", "github-actions[bot]"):
        assert not replies.is_review(replies.Event("2026-11-22T00:00:00Z", user, sent["body"]), {"alice"},
                                     cfg.replies.review_markers, housekeeping=cfg.replies.housekeeping_prefixes,
                                     ignore={"github-actions[bot]"})
    # Labelled, recorded, and nothing changed: kept without a write.
    record = {"id": 5, "user": {"login": "alice"}, "body": sent["body"], "created_at": "2026-11-22T00:00:00Z"}
    gh = ContentGH(aggregate_nodes(labels=["ai-found-no-errors"]), files=files, trees=SAME_TREES, comments={99: [record]})
    assert sync(gh, [foo, bar], aggregate_prs=[99], pool=["alice"])[0] == [] and gh.writes == []
    # Without the setting it is a pull request like any other: both its folders get a review.
    gh = ContentGH(aggregate_nodes(), files=files, trees=SAME_TREES)
    _out, fake = sync(gh, [foo, bar], per_run=20)
    assert sorted(job.inputs["pr"] for job, _s, _p in fake.created) == [99, 99]


def test_an_aggregate_loses_the_label_when_a_folder_differs_or_its_member_is_not_accepted():
    foo, bar = review_job(41, 1, 12, H1, "done"), review_job(42, 1, 13, H2, "done", name="Bar")
    files = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "accepted", H2)}
    gone = ("DELETE", f"repos/{REPO}/issues/99/labels/ai-found-no-errors", {})
    # The aggregate's Bar is not the member's Bar.
    trees = {**SAME_TREES, (H3, "TauCetiRoadmap/Bar"): "other"}
    gh = ContentGH(aggregate_nodes(labels=["ai-found-no-errors"]), files=files, trees=trees)
    out, _fake = sync(gh, [foo, bar], aggregate_prs=[99])
    assert gh.writes == [gone] and out == ["#99: ai-found-no-errors removed (aggregate: Bar: differs from #13)"]
    # Same files, but the member's review does not accept them: its own label goes, and the aggregate's.
    blocked = {**verdict_file(foo, "accepted", H1), **verdict_file(bar, "needs_changes", H2)}
    gh = ContentGH(aggregate_nodes(labels=["ai-found-no-errors"]), files=blocked, trees=SAME_TREES)
    out, _fake = sync(gh, [foo, bar], aggregate_prs=[99])
    assert gone in gh.writes and "#99: ai-found-no-errors removed (aggregate: Bar: #13 holds these files and is not accepted)" in out
    # Not labelled and not accepted: nothing is written for the aggregate.
    gh = ContentGH(aggregate_nodes(bar_labels=()), files=blocked, trees=SAME_TREES)
    out, _fake = sync(gh, [foo, bar], aggregate_prs=[99])
    assert not [w for w in gh.writes if "/issues/99/" in w[1]] and not [line for line in out if line.startswith("#99")]


def test_an_aggregate_keeps_its_label_as_it_is_while_a_member_is_undecided():
    foo, bar = review_job(41, 1, 12, H1, "done"), review_job(42, 1, 13, H2, "claimed", name="Bar")
    files = verdict_file(foo, "accepted", H1)
    for labels in ((), ("ai-found-no-errors",)):
        gh = ContentGH(aggregate_nodes(labels=labels), files=files, trees=SAME_TREES)
        out, _fake = sync(gh, [foo, bar], aggregate_prs=[99])
        assert not [w for w in gh.writes if "/issues/99/" in w[1]]
        assert out == [("#99: ai-found-no-errors not checked (aggregate: 1 of 2 roadmap folders could not be "
                        "compared with their members)")]
    # A folder GitHub did not answer for is not told either.
    gh = ContentGH(aggregate_nodes(labels=["ai-found-no-errors"]), files={
        **files, **verdict_file(review_job(42, 1, 13, H2, "done", name="Bar"), "accepted", H2)}, trees="unread")
    out, _fake = sync(gh, [foo, review_job(42, 1, 13, H2, "done", name="Bar")], aggregate_prs=[99])
    assert not [w for w in gh.writes if "/issues/99/" in w[1]] and any("#99: ai-found-no-errors not checked" in x for x in out)


def test_an_aggregate_keeps_its_label_by_its_record_when_its_members_are_closed():
    # 2026-11-22: 41 members were to be closed once their folders sat in one pull request; the aggregate's
    # label was computed from open members only, so closing them would have taken it off.
    foo, bar = ("TauCetiRoadmap/Foo/README.md", "ADDED"), ("TauCetiRoadmap/Bar/README.md", "ADDED")
    labels, comments = f"repos/{REPO}/issues/99/labels", f"repos/{REPO}/issues/99/comments"
    gone = ("DELETE", f"{labels}/ai-found-no-errors", {})
    trees = {(H3, "TauCetiRoadmap/Foo"): "foo", (H3, "TauCetiRoadmap/Bar"): "bar"}
    kept = ("all 2 roadmap folders hold accepted files (2 by the record of an earlier acceptance, their own pull "
            "requests being closed)")

    def run(body, who="alice", labelled=True, trees=trees, nodes=None):
        record = {"id": 5, "user": {"login": who}, "body": body, "created_at": "2026-11-22T00:00:00Z"}
        gh = ContentGH(nodes or [node(99, H3, paths=[foo, bar], labels=["ai-found-no-errors"] if labelled else [])],
                       trees=trees, comments={99: [record]})
        return gh, sync(gh, aggregate_prs=[99], pool=["alice"])[0]

    note = intake.aggregate_note(H3, {"Foo": "foo", "Bar": "bar"})
    gh, out = run(note)                                         # both members closed: the record covers both folders
    assert gh.writes == [] and out == []
    gh, out = run(note, labelled=False)                         # ... and brings the label back
    assert gh.writes == [("POST", labels, {"labels": ["ai-found-no-errors"]})]
    assert out == [f"#99: ai-found-no-errors added (aggregate: {kept})"]
    # A record by a login that is not trusted counts for nothing.
    gh, out = run(note, who="mallory")
    assert gh.writes == [gone]
    assert out == ["#99: ai-found-no-errors removed (aggregate: Bar: no other open pull request carries it (and 1 more))"]
    # A folder changed after its member was closed is not covered (and no record is written of it).
    gh, out = run(note, trees={**trees, (H3, "TauCetiRoadmap/Bar"): "edited"})
    assert gh.writes == [gone] and out == [("#99: ai-found-no-errors removed (aggregate: Bar: changed since its "
                                           "acceptance was recorded, and no other open pull request carries it)")]
    # While a member is open it decides, whatever the record says: here #13 holds the files and is not accepted.
    bar_job = review_job(42, 1, 13, H2, "done", name="Bar")
    record = {"id": 5, "user": {"login": "alice"}, "body": note, "created_at": "2026-11-22T00:00:00Z"}
    gh = ContentGH([node(13, H2, paths=[bar], labels=["ai-found-no-errors"]),
                    node(99, H3, paths=[foo, bar], labels=["ai-found-no-errors"])],
                   files=verdict_file(bar_job, "needs_changes", H2), trees={**trees, (H2, "TauCetiRoadmap/Bar"): "bar"},
                   comments={99: [record]})
    out, _fake = sync(gh, [bar_job], aggregate_prs=[99], pool=["alice"])
    assert gone in gh.writes and "#99: ai-found-no-errors removed (aggregate: Bar: #13 holds these files and is not accepted)" in out
    # One member closed, the other open and accepted with new files the aggregate follows: a new record is written.
    moved = {(H3, "TauCetiRoadmap/Foo"): "foo", (H3, "TauCetiRoadmap/Bar"): "bar2", (H2, "TauCetiRoadmap/Bar"): "bar2"}
    gh = ContentGH([node(13, H2, paths=[bar], labels=["ai-found-no-errors"]),
                    node(99, H3, paths=[foo, bar], labels=["ai-found-no-errors"])],
                   files=verdict_file(bar_job, "accepted", H2), trees=moved, comments={99: [record]})
    out, _fake = sync(gh, [bar_job], aggregate_prs=[99], pool=["alice"])
    [(verb, path, sent)] = gh.writes
    assert (verb, path) == ("POST", comments) and out == ["#99: acceptance recorded for 2 roadmap folders at " + H3[:7] + " (aggregate)"]
    assert intake.aggregate_recorded([replies.Event("t", "alice", sent["body"])], {"alice"}) == {"Foo": {"foo"}, "Bar": {"bar2"}}


def test_aggregate_records_are_read_from_trusted_comments_only():
    one, two = intake.aggregate_note(H1, {"Foo": "a"}), intake.aggregate_note(H2, {"Foo": "b", "Bar": "c"})
    assert one.startswith(f"Housekeeping at `{H1[:7]}`: the roadmap folder this pull request adds holds")
    events = [replies.Event("t1", "Alice", one), replies.Event("t2", "alice", two), replies.Event("t3", "mallory",
              intake.aggregate_note(H3, {"Foo": "z"})), replies.Event("t4", "alice", "<!-- tauceti-roadmap-aggregate {broken -->"),
              replies.Event("t5", "alice", '<!-- tauceti-roadmap-aggregate {"trees": {"Foo": 7, "Baz": ""}} -->')]
    assert intake.aggregate_recorded(events, {"alice"}) == {"Foo": {"a", "b"}, "Bar": {"c"}}
    assert intake.aggregate_recorded(None, {"alice"}) == {} and intake.aggregate_recorded(events, set()) == {}


def test_aggregate_state_reads_the_members():
    path_of = {"Foo": "R/Foo", "Bar": "R/Bar"}
    members = {"Foo": [(12, H1)], "Bar": [(13, H2), (14, H1)]}
    ids = {(H3, "R/Foo"): "foo", (H1, "R/Foo"): "foo", (H3, "R/Bar"): "bar", (H2, "R/Bar"): "old", (H1, "R/Bar"): "bar"}
    state = lambda settled, undecided=(), ids=ids, members=members: intake.aggregate_state(
        H3, ["Foo", "Bar"], path_of, members, ids, set(settled), set(undecided))
    assert state({12, 14}) == (True, "all 2 roadmap folders hold the files of accepted members")
    assert state({12, 13}) == (False, "Bar: #14 holds these files and is not accepted")       # #13 holds other files
    assert state({12}, {14}) == (None, "1 of 2 roadmap folders could not be compared with their members")
    assert state(set()) == (False, "Bar: #14 holds these files and is not accepted (and 1 more)")
    assert state({12, 14}, members={"Foo": [(12, H1)], "Bar": []}) == (False, "Bar: no other open pull request carries it")
    missing = {k: v for k, v in ids.items() if k != (H3, "R/Foo")}
    assert state({12, 14}, ids=missing)[0] is None
    # A folder that is not at the aggregate's head matches no member.
    assert state({12, 14}, ids={**ids, (H3, "R/Foo"): None}) == (False, "Foo: differs from #12")
    # The record covers a folder no other open pull request carries, and only such a folder.
    alone = {"Foo": [(12, H1)], "Bar": []}
    recorded = lambda **trees: intake.aggregate_state(H3, ["Foo", "Bar"], path_of, alone, ids, {12}, set(), trees)
    assert recorded(Bar={"bar"}) == (True, ("all 2 roadmap folders hold accepted files (1 by the record of an earlier "
                                           "acceptance, their own pull requests being closed)"))
    assert recorded(Bar={"old"}) == (False, "Bar: changed since its acceptance was recorded, and no other open pull request carries it")
    assert recorded(Foo={"foo"}) == (False, "Bar: no other open pull request carries it")
    assert intake.aggregate_state(H3, ["Foo", "Bar"], path_of, members, ids, {12, 13}, set(), {"Bar": {"bar"}}) == (
        False, "Bar: #14 holds these files and is not accepted")


def test_aggregate_prs_parse_are_checked_and_leave_the_listing():
    parsed = config.parse('[project]\nname = "P"\n[prreview]\naggregate_prs = [2950]\n', "x.toml").prreview
    assert parsed.aggregate_prs == [2950]
    with pytest.raises(config.ConfigError, match=re.escape("prreview.aggregate_prs")):
        config.parse('[project]\nname = "P"\n[prreview]\naggregate_prs = ["2950"]\n', "x.toml")
    from tauceti_roadmap_worker import openroadmaps
    gh = ContentGH(aggregate_nodes())
    assert [x["pr"] for x in openroadmaps.inventory(gh, REPO, ("TauCetiRoadmap",))] == [12, 13, 99]
    assert [x["pr"] for x in openroadmaps.inventory(gh, REPO, ("TauCetiRoadmap",), skip=[99])] == [12, 13]
