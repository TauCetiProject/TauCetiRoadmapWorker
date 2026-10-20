"""`board import-explorer` against a small explorer checkout, with git and GitHub faked."""

import json
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import explorer, gitops, intake
from tauceti_roadmap_worker.board import encode_spec, new_job

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
        "Alpha": "PORT-Alpha", "Beta": "REV-PKG-Beta", "Gamma": "PKG-Gamma", "Delta": "REV-DESIGN-Delta", "Eps": "TARGET-Eps"}
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
    assert "PORT-Alpha" in table and "in flight in the explorer" in table and table.endswith("target: 1")


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
    assert named["Alpha"].job.id == "PORT-Alpha" and named["Nope"].skip == "the explorer has nothing for it"
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


class GH:
    def __init__(self, pulls=()):
        self.pulls = list(pulls)

    def api(self, path, paginate=False):
        return [] if "/contents/" in path or "/files" in path or "TauCetiRoadmap/pulls" in path else self.pulls


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
    assert [s.outcome for s in steps] == ["pull request #61", "created #501"]
    [(head, title, body)] = opened
    assert head == "maintainer:import/explorer-alpha" and "PORT-Alpha" in title
    found = explorer.decode_import(body)
    assert [j.id for j in found["jobs"]] == ["PORT-Alpha"] and len(found["files"]) == 4
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
