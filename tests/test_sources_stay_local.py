"""Sources live only on the operator's machine (docs/DESIGN.md §5): a `local:` answer gates who may
resume a job, nothing is downloaded from an issue, and every submission gate refuses source
documents, extracted text, manifests that embed text and long pasted passages."""

import dataclasses
import subprocess
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import board, cli, gates, intake, references, sources, worker
from tauceti_roadmap_worker.board import Board, allowed_paths, file_problems, new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import Context, lane_for
from tauceti_roadmap_worker.paths import HERE

from .fakes import FakeClaims, FakeIssuesGH, example_config, head_tree, job_issue, make_pdf
from .test_gate_scanners import CLEAN_LEAN, CLEAN_README
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

PDF = make_pdf(["Satz 2.3 " * 40])
SHA = sources.sha256_bytes(PDF)
NEED = {"key": "Ket67", "name": "Kettle1967.pdf", "sha256": ""}


# --- who may resume a job answered with `local:` ---------------------------------------------------

def held(tmp_path, name="Kettle1967.pdf"):
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    (lib / name).write_bytes(PDF)
    return [str(lib)]


def test_only_an_operator_who_holds_the_local_text_may_take_the_job(ctx, tmp_path):  # noqa: F811
    job = new_job("DESIGN-Foo", inputs={"local_sources": [NEED]})
    lane = lane_for(job)
    assert lane.eligible(job, ctx) == "needs local source Kettle1967.pdf"
    ctx.cfg.sources.local_dirs = held(tmp_path)
    assert lane.eligible(job, ctx) == ""
    by_hash = new_job("DESIGN-Foo", inputs={"local_sources": [{**NEED, "name": "other name.pdf", "sha256": SHA}]})
    assert lane.eligible(by_hash, ctx) == ""                     # a hash finds the text under any name


def test_a_worker_without_the_text_leaves_the_job_unclaimed_and_uncounted(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "DESIGN-Foo", "available", inputs={"local_sources": [NEED]})])
    wctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=FakeClaims(), worker_id="w1", operator="alice",
                   agent="codex", model="m")
    lines = []
    result = worker.work_once(wctx, None, log=lines.append)
    assert result.status == "idle" and lines == ["skip DESIGN-Foo: needs local source Kettle1967.pdf"]
    assert wctx.claims.acquired == [] and gh.states(1) == ["available"] and worker.failures(wctx) == {}
    assert [w for w in gh.writes] == []


def test_the_spec_update_keeps_the_rest_of_the_issue(tmp_path):
    cfg = example_config()
    issue = job_issue(1, "TARGET-Foo", "needs-references")
    issue["body"] = "The filed form, quoted.\n\n" + issue["body"]
    gh = FakeIssuesGH([issue])
    b = Board(cfg, gh)
    job = b.all_jobs()[0]
    job.inputs["local_sources"] = [NEED]
    b.update_spec(job)
    assert gh.issues[1]["body"].startswith("The filed form, quoted.\n\n")
    assert b.all_jobs()[0].inputs["local_sources"] == [NEED]


def test_a_lane_that_reads_a_local_text_records_its_hash(ctx, tmp_path):  # noqa: F811
    run_ctx = dataclasses.replace(ctx, dry_run=False)
    run_ctx.cfg.sources.local_dirs = held(tmp_path)
    for job_id, recorded in (("DESIGN-Foo", True), ("REV-DESIGN-Foo", False)):
        job = new_job(job_id)
        lane = lane_for(job)
        ws = lane.prepare(job, run_ctx)
        manifest = ws.package(run_ctx.cfg, "Foo") / "sources.toml"
        data = sources.new_manifest("Foo")
        data["source"] = [{"id": "Ket67", "authors": "R. Kettle", "title": "T", "year": 1967, "status": "local",
                           "licence_class": "operator-local", "local_name": "Kettle1967.pdf"}]
        sources.save_manifest(manifest, data)
        lane.prepare_sources(job, run_ctx, ws)
        assert "Ket67" in ws.extra["source_texts"] and str(run_ctx.state / "sources") in ws.extra["source_texts"]
        [entry] = sources.load_manifest(manifest)["source"]
        assert (entry["status"], entry.get("sha256")) == (("certified", SHA) if recorded else ("local", None)), job_id
        assert not (ws.scratch / "needs-references.toml").exists()


# --- the gates -------------------------------------------------------------------------------------

def epub() -> bytes:
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("OEBPS/a.xhtml", "<p>text</p>")
    return out.getvalue()


@pytest.mark.parametrize("path,data", [
    ("packages/Foo/Kettle1967.pdf", b""),
    ("packages/Foo/scan.djvu", b""),
    ("packages/Foo/notes.md", PDF),                       # a PDF under another name
    ("packages/Foo/book.bin", b"AT&TFORM\x00\x00"),
    ("TauCetiRoadmap/Foo/a.ps", b"%!PS-Adobe-3.0"),
    ("packages/Foo/book", epub()),
    ("packages/Foo/Ket67.txt", b"x" * (33 * 1024)),
    ("packages/Foo/reports/X.md", ("   1  === page 1 ===\n" + "   2  line\n" * 4000).encode()),
])
def test_source_material_is_refused(path, data):
    [problem] = gates.source_material_problems(path, data)
    assert problem.startswith(path) and "never enters a repository" in problem


def test_ordinary_files_pass_the_source_material_check():
    assert gates.source_material_problems("packages/Foo/Ket67.txt", b"short notes\n") == []
    assert gates.source_material_problems("packages/Foo/README.md", b"# Foo\n" + b"word " * 20000) == []


def test_manifest_entries_that_embed_text_are_refused():
    data = sources.new_manifest("Foo")
    data["source"] = [{"id": "A", "title": "T", "excerpt": "Satz 2.3. Jede ..."},
                      {"id": "B", "title": "T", "locator": "x" * 1200}, {"id": "C", "title": "T", "locator": "Thm 1"}]
    problems = gates.manifest_text_problems("packages/Foo/sources.toml", sources.dump_manifest(data))
    assert [p.split(": ", 2)[1] for p in problems] == ["source 1 (A)", "source 2 (B)"]


def fence(lang: str, n: int, line: str = "a passage of the source") -> str:
    return f"intro\n```{lang}\n" + f"{line}\n" * n + "```\nafter\n"


def test_long_fenced_passages_are_refused_unless_lean_or_a_table():
    assert [h.line for h in gates.scan_long_fences(fence("text", 41))] == [2]
    assert gates.scan_long_fences(fence("", 41)) and gates.scan_long_fences(fence("text", 40)) == []
    assert gates.scan_long_fences(fence("lean", 200, "theorem foo : True := trivial")) == []
    assert gates.scan_long_fences(fence("", 60, "| a | b |")) == []
    assert gates.scan_long_fences("~~~~\n" + "x\n" * 50 + "~~~~\n")[0].line == 1


def test_board_submissions_refuse_source_material():
    job = new_job("PKG-Foo")
    allowed = allowed_paths(job, "packages")
    assert "never enters a repository" in file_problems("packages/Foo/paper.pdf", "", allowed)[0]
    report = "packages/Foo/reports/PKG-Foo.md"
    assert "fenced block" in file_problems(report, fence("text", 45), allowed)[0]
    assert file_problems(report, fence("lean", 45), allowed) == []
    manifest = sources.dump_manifest({**sources.new_manifest("Foo"), "source": [{"id": "A", "quote": "..."}]})
    assert "embeds text" in file_problems("packages/Foo/sources.toml", manifest, allowed)[0]


class IntakeGH:
    """The packages repository as the intake reads it: one submission pull request."""

    def __init__(self, files: dict[str, str]):
        self.files, self.writes = files, []

    def api(self, path, paginate=False):
        if path.endswith("/pulls/12"):
            return {"number": 12, "state": "open", "draft": False, "user": {"login": "alice"},
                    "author_association": "COLLABORATOR",
                    "body": board.encode_submission("PKG-Foo", "w1", "alice", "codex"),
                    "head": {"sha": "abc", "repo": {"full_name": "alice/jobs"}}}
        if "/pulls/12/files" in path:
            return [{"filename": f, "status": "added"} for f in self.files]
        if "/git/trees/" in path:
            return head_tree(self.files)
        if "/check-runs" in path:
            return {"check_runs": []}
        raise AssertionError(path)

    def raw_file(self, repo, path, ref=""):
        if path not in self.files:
            raise GHError(f"{path}: 404")
        return self.files[path]

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))


def test_the_intake_refuses_a_submission_carrying_a_passage():
    job = new_job("PKG-Foo")
    job.number, job.state = 3, "submitted"
    gh = IntakeGH({"packages/Foo/reports/PKG-Foo.md": fence("text", 45), "packages/Foo/Ket67.pdf": "%PDF-1.4"})
    bot = SimpleNamespace(cfg=example_config(), gh=gh,
                          board=SimpleNamespace(all_jobs=lambda: [job], find=lambda jid, jobs: job))
    line = intake.intake(bot, 12, yes=True)
    assert line.startswith("#12: not merged: ") and "fenced block" in line and "Ket67.pdf: a source document" in line
    assert not any(m == "PUT" for m, *_ in gh.writes)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_the_push_gate_refuses_source_material_in_the_roadmap_folder(tmp_path):
    folder = tmp_path / "wt" / "TauCetiRoadmap" / "Toy"
    folder.mkdir(parents=True)
    git(tmp_path / "wt", "init", "-q")
    (folder / "README.md").write_text(CLEAN_README)
    (folder / "Suggested.lean").write_text(CLEAN_LEAN)
    (folder / "metadata.toml").write_text('topic = "math.GR"\n')
    (folder / "refs").mkdir()
    (folder / "refs" / "Kettle1967.pdf").write_bytes(PDF)
    git(tmp_path / "wt", "add", "-A")
    git(tmp_path / "wt", "commit", "-q", "-m", "toy")
    result = gates.run_gate(tmp_path / "wt", "Toy", gates.GateOptions())
    assert result.counts["source-material"] == 1 and not result.ok
    assert result.details["source-material"][0].startswith("TauCetiRoadmap/Toy/refs/Kettle1967.pdf: a source document")


# --- the cache stays in the state directory ---------------------------------------------------------

def test_the_cache_lives_in_the_state_directory_and_never_in_a_checkout(ctx, tmp_path):  # noqa: F811
    assert ctx.sources_fetcher().cache == ctx.state / "sources"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert sources.enclosing_checkout(repo / "state" / "sources") == repo
    assert sources.enclosing_checkout(HERE.parent / "no-such-dir-anywhere" / "x") in (None, HERE.parent)


def test_ocr_output_is_never_written_into_a_checkout(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    args = ["--conf", str(HERE / "tauceti-roadmap.toml.example"), "sources", "ocr", str(tmp_path / "scan.pdf"),
            "--out", str(repo / "packages" / "Foo" / "scan.txt")]
    assert cli.main(args) == 2 and "never enters a repository" in capsys.readouterr().err


def test_answer_forms_in_the_request_template():
    text = references.request_comment("DESIGN-Foo", "Foo", references.parse_missing(
        {"missing": [{"title": "T", "needed_for": "x", "authors": "A", "year": 1999}]}))
    assert "`local: KEY" in text and "`source: KEY" in text and "`waive: KEY`" in text
    assert "attach:" not in text and "never downloaded" in text


# --- machine paths are rewritten in hand-offs, refused elsewhere ----------------------------------

def test_hand_offs_lose_machine_paths_and_readmes_still_fail_on_them(ctx):  # noqa: F811
    from pathlib import Path
    job = new_job("TARGET-Foo")
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    state_path = f"{ctx.state}/workers/w1/board/packages/Foo/target.md"
    text = (f"Read `{state_path}`, notes in {Path.home()}/notes/q-series.md and the run log /tmp/rmw-1/run.out;\n"
            "a relative path packages/Foo/tmp/x stays.\n")
    pkg = ws.package(ctx.cfg, "Foo")
    for path in (ws.report, pkg / "target.md", pkg / "sources.toml", pkg / "README.md"):
        path.write_text(text if path.suffix != ".toml" else f'note = "{text.splitlines()[0]}"\n')
    changed = lane.scrub_outputs(job, ctx, ws)
    assert sorted(p.name for p in changed) == ["TARGET-Foo.md", "sources.toml", "target.md"]
    assert ws.report.read_text() == ("Read `<state>/workers/w1/board/packages/Foo/target.md`, notes in "
                                     "~/notes/q-series.md and the run log <scratch>/rmw-1/run.out;\n"
                                     "a relative path packages/Foo/tmp/x stays.\n")
    assert file_problems("packages/Foo/reports/TARGET-Foo.md", ws.report.read_text(),
                         allowed_paths(job, "packages")) == []
    readme = (pkg / "README.md").read_text()
    assert readme == text and "local path" in file_problems("packages/Foo/README.md", readme,
                                                            {"packages/Foo/README.md"})[0]


@pytest.mark.parametrize("text", [
    "notes in /Users/alice/notes.md", "see /home/bob/run.log", "the root user's /root/notes/q-series.md",
    "a container's `/workspace/TauCetiRoadmapWorker/.pytest-tmp/state`", "a codespace's /workspaces/repo/x",
    "the run log (/tmp/rmw-1/run.out)", "/tmp/x.lean at the start of a line", "kept in /var/tmp/w1/scratch",
    "macOS temp /var/folders/xy/abc/T/tmp1/a.pdf", "or /private/var/folders/xy/T/a", r"C:\Users\carol\x.md"])
def test_machine_paths_of_every_supported_layout_are_private(text):
    assert board.PRIVATE_PATH.search(text) and gates.scan_private_paths(text)
    assert "local path" in file_problems("packages/Foo/README.md", text, {"packages/Foo/README.md"})[0]


@pytest.mark.parametrize("text", [
    "a relative path packages/Foo/tmp/x stays", "the arXiv listing https://arxiv.org/tmp/2101.00001",
    "a mirror at https://example.org/root/papers/x.pdf and https://host/workspace/y", "a /tmpfs mount, /rooted trees",
    "the square root/sum of squares, x/root/y", "the scrubbed ~/notes/q.md, <scratch>/rmw-1/run.out, <state>/workers",
    "a /workspaces project", "the quotient R/I and the map Z/nZ -> Z/mZ"])
def test_repository_paths_urls_and_prose_are_not_machine_paths(text):
    assert not board.PRIVATE_PATH.search(text) and gates.scan_private_paths(text) == []


def test_the_scrubber_rewrites_the_temporary_roots_the_privacy_check_refuses(tmp_path):
    from tauceti_roadmap_worker.lanes.base import scrub_paths
    text = "logs in /tmp/rmw-1/run.out and /var/folders/xy/T/tmp1/a.pdf; packages/Foo/tmp/x stays\n"
    clean = scrub_paths(text, tmp_path / "state", tmp_path / "home")
    assert clean == "logs in <scratch>/rmw-1/run.out and <scratch>/xy/T/tmp1/a.pdf; packages/Foo/tmp/x stays\n"
    assert not board.PRIVATE_PATH.search(clean)


def test_a_request_comment_carries_no_machine_path(ctx):  # noqa: F811
    from pathlib import Path
    job = new_job("TARGET-Foo")
    job.number = 7
    item = references.MissingReference("A", "T", "1999", "Layer 1", tried=[f"{Path.home()}/library (not there)",
                                                                           f"{ctx.state}/sources/x.txt"])
    posted = []
    ctx.board.comment = lambda number, body: posted.append(body)
    lane_for(job).park(job, ctx, [item])
    assert str(Path.home()) not in posted[0] and "~/library (not there)" in posted[0]
    assert "<state>/sources/x.txt" in posted[0]
