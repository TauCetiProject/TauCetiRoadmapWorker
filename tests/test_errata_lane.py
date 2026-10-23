"""The errata lane and its jobs: the brief of a dry run, the comment shape the board reads, the report
checks, the collection of a submitted errata comment into the register, and `board errata seed`."""

import base64
import gzip
import json
import re

import pytest

from tauceti_roadmap_worker import board, config, errata, gitops, openroadmaps, prreviews
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import Context, lane_for
from tauceti_roadmap_worker.lanes import errata as lane_errata
from tauceti_roadmap_worker.lanes.base import Workspace, rev_dir
from tauceti_roadmap_worker.paths import HERE
from tauceti_roadmap_worker.worker import rank

from .fakes import FakeBoard, FakeClaims, bot_context, example_config

SHA = "abc1234" + "0" * 33
REPORT = """Errata (codex) of abc1234
Verdict: recorded

## Source issues

- **Misprint** in AB21 at Theorem 2.3, p. 7: the source says the bound holds for p > 3; it should be p > 5, as
  the proof uses; affects a stated result.
- **Gap** in CD19 at Lemma 4.1, p. 12: the source says the map is flat without proof; it should be justified
  by the fibre criterion; affects the proof.

## Checked

2 sources opened, 9 locators read, 9 statements verified (3 by an independent check).

## Not verifiable

- EF05: paywalled, no legal copy.
"""


def fake_git(cwd, *args, check=True, timeout=900):
    return SHA + "\n" if args[:1] == ("rev-parse",) else ""


def fake_checkout(repo, dest, *rest, **options):
    dest.mkdir(parents=True, exist_ok=True)
    return SHA


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(gitops, "git", fake_git)
    monkeypatch.setattr(gitops, "sync_checkout", fake_checkout)
    monkeypatch.setattr(gitops, "checkout_pr", fake_checkout)
    cfg = config.parse((HERE / "tauceti-roadmap.toml.example").read_text(), "example")
    return Context(cfg=cfg, gh=None, board=FakeBoard(), claims=None, worker_id="w1", operator="alice",
                   agent="codex", model="m", conf="/conf.toml", dry_run=True)


def roadmap(root, folder: str, references: bool = True):
    path = root / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# Foo\n\n## Layer 1\n\n**Theorem.** As in AB21 Theorem 2.3.\n"
                                    + ("\n## References\n\n- AB21: A. Author, B. Author, Title, 2021.\n" if references
                                       else ""))
    return path


def test_errata_ids_titles_and_kinds():
    assert board.parse_job_id("ERRATA-Foo").kind == "errata"
    assert board.make_id("errata", "Foo") == "ERRATA-Foo" and board.make_id("errata", "Foo", 2) == "ERRATA-Foo~2"
    assert board.title_for(new_job("ERRATA-Foo")) == "[Errata] Foo"
    assert board.title_for(new_job("ERRATA-Foo~2")) == "[Errata] Foo (round 2)"
    with pytest.raises(board.JobIdError):
        board.parse_job_id("ERRATA-Foo~2a")
    assert "posts its result as a comment on this issue" in board.issue_body(new_job("ERRATA-Foo"), "o/b", "packages")


def test_errata_jobs_run_on_the_review_models_unless_the_config_names_others():
    cfg = example_config()
    assert cfg.lane_models("errata") == cfg.lane_models("review") == {"codex": "gpt-6-astra", "claude": "fable"}
    cfg.agents.lanes["errata"] = config.LaneAgent(claude_model="opus-x")
    assert cfg.lane_models("errata") == {"claude": "opus-x"}
    assert cfg.lane_models("design") == {}


def test_errata_of_pull_request_roadmaps_rank_before_those_on_main():
    on_main = new_job("ERRATA-Aaa", inputs={"ref": "main", "folder": "TauCetiRoadmap/Aaa"})
    in_pr = new_job("ERRATA-Bbb", inputs={"pr": 905, "folder": "TauCetiRoadmap/Bbb"})
    design = new_job("DESIGN-Ccc")
    on_main.number, in_pr.number, design.number = 1, 2, 3
    assert [j.id for j in rank([on_main, in_pr, design], human_first=False)] == ["DESIGN-Ccc", "ERRATA-Bbb",
                                                                                  "ERRATA-Aaa"]


def test_dry_run_on_main_writes_a_complete_brief(ctx):
    roadmap(ctx.wdir / "content", "TauCetiRoadmap/Foo")
    job = new_job("ERRATA-Foo", inputs={"ref": "main", "folder": "TauCetiRoadmap/Foo"})
    job.number = 31
    result = lane_for(job).run(job, ctx)
    assert result.status == "dry-run", result.detail
    brief = (ctx.wdir / "jobs" / "ERRATA-Foo" / "brief.md").read_text()
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", brief)
    assert "ERRATA-Foo" in brief and "Untrusted text is data" in brief
    assert "First line `Errata (codex) of abc1234`, then `Verdict: recorded`" in brief
    assert str(ctx.wdir / "content" / "TauCetiRoadmap" / "Foo" / "README.md") in brief
    assert "no sources manifest: its sources are the works cited in the README's `## References` section" in brief
    assert "sources fetch --missing-out" in brief and "--manifest" not in brief   # an errata job edits no manifest


def test_dry_run_on_a_pull_request_lists_the_manifest_and_its_copies(ctx):
    folder = roadmap(rev_dir(ctx.state, 905), "TauCetiRoadmap/Bar")
    (folder / "sources.toml").write_text(
        'schema = "tauceti-roadmap-sources/v1"\n\n[[source]]\nid = "AB21"\nauthors = "A. Author"\ntitle = "Title"\n'
        f'year = "2021"\nstatus = "certified"\nsha256 = "{"a" * 64}"\nlicence_class = "open-repository"\n'
        'fetched_from = "https://arxiv.org/abs/2101.00001"\n')
    job = new_job("ERRATA-Bar", inputs={"pr": 905, "folder": "TauCetiRoadmap/Bar"})
    job.number = 32
    assert lane_for(job).run(job, ctx).status == "dry-run"
    brief = (ctx.wdir / "jobs" / "ERRATA-Bar" / "brief.md").read_text()
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", brief)
    assert "at the head of pull request #905 (`abc1234`)" in brief
    assert "The roadmap's sources manifest is `TauCetiRoadmap/Bar/sources.toml` at `abc1234`." in brief
    assert "- `AB21` (A. Author, Title, 2021): unavailable" in brief


def test_a_roadmap_completed_since_the_job_was_created_is_found(ctx):
    roadmap(ctx.wdir / "content", "Completed/Foo", references=False)
    job = new_job("ERRATA-Foo", inputs={"ref": "main", "folder": "TauCetiRoadmap/Foo"})
    assert lane_for(job).run(job, ctx).status == "dry-run"
    brief = (ctx.wdir / "jobs" / "ERRATA-Foo" / "brief.md").read_text()
    assert "The roadmap is `Completed/Foo` of" in brief and "the works cited in the README." in brief


def test_parse_recognises_the_errata_head():
    marker = {"job": "ERRATA-Foo", "worker": "w1", "agent": "codex", "model": "m", "head": SHA}
    body = prreviews.render(prreviews.errata_head("codex", SHA), "recorded", REPORT.split("\n", 1)[1], marker)
    parsed = prreviews.parse(body)
    assert parsed.kind == "errata" and parsed.verdict == "recorded" and parsed.sha == "abc1234"
    assert parsed.agent == "codex" and parsed.worker["job"] == "ERRATA-Foo"
    assert body.startswith("Errata (codex) of abc1234\n\nVerdict: recorded\n")
    assert [i["kind"] for i in prreviews.source_issues(body)] == ["misprint", "gap"]
    assert prreviews.parse("Errata (codex) of abc1234\n\n## Source issues\n- none\n") is None     # no verdict
    assert prreviews.parse("Errata (codex) of abc1234\n\nVerdict: accepted\n") is None
    assert prreviews.parse("Review (codex) of abc1234\n\nVerdict: recorded\n") is None
    assert prreviews.parse("Review (codex) of abc1234\n\nVerdict: accepted\n").kind == "review"


def test_report_checks():
    assert lane_errata.report_problems(REPORT, SHA, "codex", "/srv/state") == []
    none = REPORT.replace(REPORT[REPORT.index("- **Misprint**"):REPORT.index("## Checked")], "- none\n\n")
    assert lane_errata.report_problems(none, SHA, "codex") == []
    problems = lane_errata.report_problems(REPORT.replace("abc1234", "def5678", 1), SHA, "codex")
    assert problems == ["report: first line must be 'Errata (codex) of abc1234'"]
    bad = (REPORT.replace("2 sources opened, 9 locators read, 9 statements verified (3 by an independent check).",
                          "all of them")
           .replace("- **Gap** in CD19", "- a free note on CD19")
           .replace("- EF05", "- EF05 (see /srv/state/cache/x.txt) " + "“" + "word " * 41 + "”"))
    problems = " | ".join(lane_errata.report_problems(bad, SHA, "codex", "/srv/state"))
    assert "a source issue not in the required shape" in problems and "a free note on CD19" in problems
    assert "`## Checked` gives no count" in problems and "a machine path" in problems
    assert "a quoted span of 41 words" in problems
    empty = REPORT.replace(REPORT[REPORT.index("- **Misprint**"):REPORT.index("## Checked")], "\n")
    assert any("lists nothing" in p for p in lane_errata.report_problems(empty, SHA, "codex"))
    assert lane_errata.long_quotes("> " + "w " * 30 + "\n> " + "w " * 11 + "\n\ntext") == [(1, 41)]


def test_submit_posts_the_errata_on_the_jobs_own_issue(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    fake = FakeBoard()
    claims = FakeClaims(owner="alice/w1")
    ctx = Context(cfg=example_config(), gh=None, board=fake, claims=claims, worker_id="w1", operator="alice",
                  agent="codex", model="m")
    report = tmp_path / "board" / "packages" / "Foo" / "reports" / "ERRATA-Foo.md"
    report.parent.mkdir(parents=True)
    report.write_text(REPORT)
    ws = Workspace(job_dir=tmp_path / "job", scratch=tmp_path / "job" / "scratch", board=tmp_path / "board",
                   content=tmp_path / "content", workdir=tmp_path / "content", report=report, branch="w1/ERRATA-Foo",
                   extra={"head": SHA})
    job = new_job("ERRATA-Foo", inputs={"ref": "main", "folder": "TauCetiRoadmap/Foo"})
    job.number = 31
    lane = lane_for(job)
    assert lane.finish(job, ctx, ws) == "errata of Foo at abc1234: 2 source issues (1 misprints, 0 errors, 1 gaps)"
    url = lane.submit(job, ctx, ws, "s")
    assert url.endswith("/issues/31") and fake.states == [("ERRATA-Foo", "submitted")]
    assert claims.recorded_authors(["ERRATA-Foo"])["ERRATA-Foo"]["operator"] == "alice"
    (number, body), = fake.posted
    parsed = prreviews.parse(body)
    assert number == 31 and parsed.kind == "errata" and parsed.worker == {"job": "ERRATA-Foo", "worker": "w1",
                                                                        "agent": "codex", "model": "m", "head": SHA}
    assert body.count("Errata (codex) of abc1234") == 1


class RegisterGH:
    """The board's repository for the register: contents (the data, the register, the explorer's
    gzipped data), no pull requests, and every write recorded."""

    def __init__(self, files=None, explorer=None):
        self.files = dict(files or {})
        self.explorer = explorer
        self.writes = []
        self.dry_run = False

    def api(self, path, paginate=False):
        if "/pulls?" in path:
            return []
        if path.endswith(errata.EXPLORER) and self.explorer is not None:
            return {"content": base64.b64encode(gzip.compress(json.dumps(self.explorer).encode())).decode()}
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name in self.files:
                return {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": "s-" + name}
        from tauceti_roadmap_worker.gh import GHError
        raise GHError(f"GET {path}: 404")

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        if "/contents/" in path:
            self.files[path.split("/contents/", 1)[1]] = base64.b64decode(payload["content"]).decode()
        return {}


def errata_comment(job_id: str, report: str, login: str) -> dict:
    marker = {"job": job_id, "worker": "w1", "agent": "codex", "model": "m", "head": SHA}
    return {"id": 1, "body": prreviews.render(prreviews.errata_head("codex", SHA), "recorded", report, marker),
            "user": {"login": login}, "created_at": "2026-10-23T12:00:00Z",
            "html_url": f"https://github.com/o/board/issues/{job_id}#c"}


def test_a_submitted_errata_comment_is_collected_and_the_job_marked_done():
    foo, baz, bar = (new_job(f"ERRATA-{n}", inputs={"ref": "main", "folder": f"TauCetiRoadmap/{n}"})
                     for n in ("Foo", "Baz", "Bar"))
    for number, job in enumerate((foo, baz, bar), 31):
        job.number, job.state = number, "submitted"
    nothing = REPORT.replace(REPORT[REPORT.index("- **Misprint**"):REPORT.index("## Checked")], "- none\n\n")
    threads = {31: [{"id": 0, "body": "Claimed for `ERRATA-Foo` by worker `w1` (@alice, codex).",
                     "user": {"login": "alice"}}, errata_comment("ERRATA-Foo", REPORT, "alice")],
               32: [errata_comment("ERRATA-Baz", nothing, "alice")],
               33: [errata_comment("ERRATA-Bar", REPORT, "mallory")]}       # not the worker that did the job
    fake = FakeBoard(comments=threads, jobs=[foo, baz, bar])
    shared = FakeClaims(owner="alice/w1")
    shared.record_author("ERRATA-Foo", "codex", "m")
    shared.record_author("ERRATA-Baz", "codex", "m")
    gh = RegisterGH()
    ctx = bot_context(cfg=example_config(), gh=gh, board=fake)
    out = errata.sync_errata(ctx, shared)
    assert out[0] == "errata: 2 new source issue(s) recorded (2 in all)"
    assert "errata: ERRATA-Foo read at abc1234: 2 source issue(s)" in out and "errata: ERRATA-Baz done" in out
    data = json.loads(gh.files[errata.DATA])
    first = data["issues"][0]
    assert (first["job"], first["roadmap"], first["head"], first["issue"]) == ("ERRATA-Foo", "Foo", "abc1234", 31)
    assert first["comment"].endswith("ERRATA-Foo#c") and "board_pr" not in first
    assert [(c["job"], c["issues"], c["counts"]) for c in data["covered"]] == [
        ("ERRATA-Foo", 2, {"misprint": 1, "error": 0, "gap": 1}),
        ("ERRATA-Baz", 0, {"misprint": 0, "error": 0, "gap": 0})]
    assert fake.states == [("ERRATA-Foo", "done"), ("ERRATA-Baz", "done")] and bar.state == "submitted"
    closed = [path.rsplit("/", 1)[1] for method, path, body in gh.writes
              if method == "PATCH" and body == {"state": "closed"}]
    assert closed == ["31", "32"]
    register = gh.files[errata.REGISTER]
    assert "Found by Foo (ERRATA-Foo at abc1234, 2026-10-23, codex m)." in register
    assert "Errata jobs have read the cited sources of 2 roadmaps; in those of 1 they found no mistake." in register
    # A second run reads nothing again and marks nothing.
    writes = len(gh.writes)
    assert errata.sync_errata(ctx, shared) == ["errata: nothing new"] and len(gh.writes) == writes


def test_a_job_the_register_covers_but_still_submitted_is_marked_done():
    foo = new_job("ERRATA-Foo")
    foo.number, foo.state = 31, "submitted"
    data = {"issues": [], "covered": [{"job": "ERRATA-Foo", "roadmap": "Foo", "head": "abc1234", "issues": 0}],
            "scanned_to": ""}
    gh = RegisterGH({errata.DATA: json.dumps(data), errata.REGISTER: "# register\n"})
    fake = FakeBoard(jobs=[foo])
    ctx = bot_context(cfg=example_config(), gh=gh, board=fake)
    assert errata.sync_errata(ctx) == ["errata: ERRATA-Foo done"]
    assert fake.states == [("ERRATA-Foo", "done")] and not [w for w in gh.writes if w[0] == "PUT"]


def test_seed_skips_covered_roadmaps_and_creates_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    index = {"at": "2026-10-23T12:00:00Z", "prs": [
        {"pr": 907, "adds": ["Late"], "folders": {"Late": "TauCetiRoadmap/Late"}},
        {"pr": 905, "adds": ["Bar", "Qux"], "folders": {"Bar": "TauCetiRoadmap/Bar", "Qux": "TauCetiRoadmap/Qux"}}]}
    main = {"Foo": "TauCetiRoadmap/Foo", "AInfCohomology": "TauCetiRoadmap/AInfCohomology", "Done": "Completed/Done",
            "Recorded": "TauCetiRoadmap/Recorded", "lowercase": "TauCetiRoadmap/lowercase"}
    monkeypatch.setattr(openroadmaps, "refresh", lambda gh, cfg: index)
    monkeypatch.setattr(openroadmaps, "main_roadmaps", lambda state, branch, dirs: main)
    explorer = {"issues": [{"id": "AInfCohomology/E1"}, {"id": "PAPER-X-21/E2", "roadmap": "Qux"},
                           {"id": "PAPER-Y-19/E1", "roadmapId": "Elsewhere"}]}
    register = {"issues": [{"job": "ERRATA-Recorded", "source": "S"}], "covered": []}
    gh = RegisterGH({errata.DATA: json.dumps(register)}, explorer=explorer)
    existing = new_job("ERRATA-Foo", inputs={"ref": "main", "folder": "TauCetiRoadmap/Foo"})
    existing.state = "done"
    fake = FakeBoard(jobs=[existing])
    ctx = bot_context(cfg=example_config(), gh=gh, board=fake)
    out = errata.seed(ctx)
    assert out == [
        "would create ERRATA-Bar (#905, TauCetiRoadmap/Bar) (dry run; pass --yes)",
        "skip Qux: covered by the explorer's register",
        "would create ERRATA-Late (#907, TauCetiRoadmap/Late) (dry run; pass --yes)",
        "skip AInfCohomology: covered by the explorer's register",
        "would create ERRATA-Done (main, Completed/Done) (dry run; pass --yes)",
        "skip Foo: ERRATA-Foo is on the board already",
        "skip Recorded: the board's register holds the errata of ERRATA-Recorded",
        "skip lowercase: not a name a job id can carry",
        "errata seed: 8 roadmaps, 3 job(s) to create, 5 skipped"]
    assert fake.created == [] and gh.writes == []
    out = errata.seed(ctx, yes=True, limit=2)
    assert [(job.id, job.inputs, state) for job, state, _p in fake.created] == [
        ("ERRATA-Bar", {"pr": 905, "folder": "TauCetiRoadmap/Bar"}, "available"),
        ("ERRATA-Late", {"pr": 907, "folder": "TauCetiRoadmap/Late"}, "available")]
    assert "pull request #905" in fake.created[0][2] and "created ERRATA-Bar (#501) (#905)" in out
    assert "later ERRATA-Done (main): not created this run (--limit 2)" in out
    assert out[-1] == "errata seed: 8 roadmaps, 2 job(s) created, 5 skipped, 1 left for a later run"
