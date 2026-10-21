"""A `local:` answer reaches the job and is never asked for again (regression: board #126,
RT-AbelianSchemesAndArithmeticModuliPartII~1b, resumed on the operator's own host, read a manifest
without the answers, which the intake records on the board's main while the package lives on its pull
request, then fetched the scans itself and asked again, "image-only scan ... no OCR tool available",
although OCR had read them)."""

import dataclasses

from tauceti_roadmap_worker import fetcher, gitops, references, sources
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import lane_for

from .fakes import FakeTransport, example_config, make_fetcher
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

TEXT = b"Theorem 1. Every isogeny class of abelian varieties over F_q is determined by its Weil number. " * 10


def library(tmp_path):
    lib = tmp_path / "tauceti-sources"
    lib.mkdir()
    (lib / "waterhouse-milne-1971-abelian-varieties-over-finite-fields.txt").write_bytes(TEXT)
    return lib


def test_a_bare_file_name_resolves_to_the_operators_file(tmp_path):
    lib = library(tmp_path)
    fet = make_fetcher(tmp_path, FakeTransport(), local_dirs=[str(lib)])
    out = fet.fetch(fetcher.parse_citation("waterhouse-milne-1971-abelian-varieties-over-finite-fields.txt"))
    assert out.status == "fetched" and out.entry["licence_class"] == "operator-local"
    assert "Weil number" in out.text.read_text()                              # the .txt file is the text itself


def test_a_local_answer_is_read_by_name_then_by_hash(tmp_path):
    lib = library(tmp_path)
    fet = make_fetcher(tmp_path, FakeTransport(), local_dirs=[str(lib)])
    entry = {"id": "WM", "authors": "W. Waterhouse, J. Milne", "title": "Abelian varieties over finite fields",
             "year": 1971, "status": "certified", "licence_class": "operator-local", "sha256": "2" * 64,
             "local_name": "waterhouse-milne-1971-abelian-varieties-over-finite-fields.txt"}
    [got] = fet.materialize({"source": [entry]})
    assert got.text and "differs from the recorded text" in got.label             # by name, a different hash noted
    renamed = {**entry, "local_name": "wm.txt", "sha256": sources.sha256_bytes(TEXT)}
    [by_hash] = make_fetcher(tmp_path / "b", FakeTransport(), local_dirs=[str(lib)]).materialize({"source": [renamed]})
    assert by_hash.text and "differs" not in by_hash.label


def test_a_local_answer_is_settled():
    item = references.MissingReference("W. Waterhouse", "Abelian varieties over finite fields", "1971", "F1",
                                       ["author page: image-only"], key="WM")
    entry = {"id": "WM", "status": "certified", "licence_class": "operator-local", "local_name": "wm.txt"}
    kept, dropped = references.drop_settled([item], [{"source": [entry]}], True)
    assert kept == [] and dropped == [("WM", "local")]


def test_answers_on_the_boards_main_reach_a_job_on_a_pull_request(ctx, monkeypatch, tmp_path):  # noqa: F811
    lib = library(tmp_path)
    answered = sources.new_manifest("Foo")
    answered["source"] = [{"id": "WM", "authors": "W. Waterhouse, J. Milne", "title": "Abelian varieties", "year": 1971,
                           "status": "local", "licence_class": "operator-local", "locator_scheme": "theorem",
                           "local_name": "waterhouse-milne-1971-abelian-varieties-over-finite-fields.txt",
                           "resolution": "https://github.com/o/board/issues/126#issuecomment-1"}]
    main_text = sources.dump_manifest(answered)
    real_git = gitops.git

    def git(cwd, *args, check=True, timeout=900):
        if args[:1] == ("show",) and args[1].startswith("refs/remotes/origin/main:"):
            return main_text
        return real_git(cwd, *args, check=check, timeout=timeout)
    monkeypatch.setattr(gitops, "git", git)
    cfg = dataclasses.replace(ctx.cfg, sources=dataclasses.replace(ctx.cfg.sources, local_dirs=[str(lib)]))
    run_ctx = dataclasses.replace(ctx, cfg=cfg, dry_run=False, transport=FakeTransport())
    job = new_job("RT-Foo~1b", inputs={"board_pr": 126, "operator": "alice"})
    lane = lane_for(new_job("REV-PKG-Foo"))
    ws = lane.prepare(new_job("REV-PKG-Foo"), run_ctx)                        # the pull request's head has no manifest
    lane.prepare_sources(job, run_ctx, ws)
    assert "`WM` (W. Waterhouse, J. Milne, Abelian varieties, 1971): `" in ws.extra["source_texts"]
    assert not (ws.scratch / "needs-references.toml").exists()


def test_the_brief_says_ocr_is_available_and_how_to_read_local_texts(monkeypatch):
    cfg = example_config()
    monkeypatch.setattr(sources, "ocr_status", lambda: "")
    assert lane_base.ocr_line(cfg).startswith("available: the sources service recognises an image-only scan")
    monkeypatch.setattr(sources, "ocr_status", lambda: "OCR is not installed (numpy is missing). Install ...")
    assert lane_base.ocr_line(cfg) == "not available here (OCR is not installed (numpy is missing))"
    cfg.sources.ocr = False
    assert lane_base.ocr_line(cfg).startswith("off")
    from tauceti_roadmap_worker import prompts
    text = prompts.template("redteam")
    assert "OCR of image-only scans on this worker: __OCR__" in text and "`sources fetch <that file name>`" in text


def test_a_recognised_scan_says_so(tmp_path):
    ocred = sources.Extraction(["Satz 2.3 " * 40], "OCR (RapidOCR, 300 dpi)",
                               "image-only scan (12 of 12 pages had no text layer); the text below was recognised by OCR")
    txt = sources.write_text(tmp_path, "c" * 64, ocred)
    assert "recognised by OCR" in txt.read_text() and not sources.unreadable(txt)
