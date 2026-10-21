import shutil
import tomllib

import pytest

from tauceti_roadmap_worker import config, sources
from tauceti_roadmap_worker.config import Sources
from tauceti_roadmap_worker.paths import HERE
from tauceti_roadmap_worker.sources import (cache_paths, cached_file, citation_key, classify_url, deny_list,
                                            dump_manifest, extract, find_source, group_ocr_lines, html_to_text,
                                            image_only, numbered_text, sniff, store_bytes, upsert_source,
                                            validate_manifest)

from .fakes import make_pdf

S = Sources()
SHA = "a" * 64


def manifest(**over):
    entry = {"id": "AB21", "authors": "A. Author, B. Author", "title": "An example paper", "year": 2021,
             "locator_scheme": "theorem", "url": "https://arxiv.org/abs/2101.00001",
             "fetched_from": "https://arxiv.org/pdf/2101.00001v2", "sha256": SHA, "licence_class": "open-repository",
             "status": "certified"}
    entry.update(over)
    return {"schema": "tauceti-roadmap-sources/v1", "roadmap": "ExampleRoadmap", "source": [entry]}


# --- classification -------------------------------------------------------------------------------

def test_classify_url():
    assert classify_url("https://arxiv.org/abs/1", S.allow, S.deny) == "open-repository"
    assert classify_url("https://export.arxiv.org/abs/1", S.allow, S.deny) == "open-repository"
    assert classify_url("https://www.numdam.org/item/X", S.allow, S.deny) == "open-repository"
    assert classify_url("https://hal.archives-ouvertes.fr/hal-1/document", S.allow, S.deny) == "open-repository"
    assert classify_url("https://sci-hub.se/10.1/x", S.allow, S.deny) == "denied"
    assert classify_url("https://libgen.is/book", S.allow, S.deny) == "denied"
    assert classify_url("https://example.edu/~someone/paper.pdf", S.allow, S.deny) == "unknown"
    assert classify_url("not a url", S.allow, S.deny) == "unknown"


def test_deny_list_merges_the_sandbox_lists(tmp_path):
    (tmp_path / "deny.txt").write_text("# the operator's own\n.mirror.example # trailing comment\n")
    rules = deny_list([".libgen.is"], env={"ALLOWPROXY_DIR": str(tmp_path), "HOME": str(tmp_path)})
    assert ".libgen.is" in rules and ".z-lib.gs" in rules and "pastebin.com" in rules and ".mirror.example" in rules
    assert len(rules) == len(set(rules))
    assert classify_url("https://books.mirror.example/x", S.allow, rules) == "denied"


# --- the manifest ---------------------------------------------------------------------------------

def test_valid_manifest():
    assert validate_manifest(manifest(), S.allow, S.deny) == []


def test_manifest_problems():
    assert any("deny list" in p for p in validate_manifest(manifest(fetched_from="https://sci-hub.se/x"), S.allow, S.deny))
    assert any("SHA-256" in p for p in validate_manifest(manifest(sha256=""), S.allow, S.deny))
    assert any("missing year" in p for p in validate_manifest(manifest(year=""), S.allow, S.deny))
    assert any("licence_class" in p for p in validate_manifest(manifest(licence_class="pirate"), S.allow, S.deny))
    assert any("doi must" in p for p in validate_manifest(manifest(doi="https://doi.org/10.1/x"), S.allow, S.deny))
    assert any("ISO 8601" in p for p in validate_manifest(manifest(fetched_at="yesterday"), S.allow, S.deny))
    assert validate_manifest(manifest(fetched_at="2026-10-20T09:15:00Z"), S.allow, S.deny) == []
    unc = manifest(status="uncertified", fetched_from="", sha256="", licence_class="none", doi="10.1016/j.x.2020.1")
    assert validate_manifest(unc, S.allow, S.deny) == []
    assert validate_manifest(unc, S.allow, S.deny, uncertified_ok=False)
    assert any("certified source names the licence class" in p
               for p in validate_manifest(manifest(licence_class="none"), S.allow, S.deny))
    waived = {"schema": sources.SCHEMA, "source": [{"id": "X", "authors": "A", "title": "T", "year": 1970,
                                                    "status": "waived", "licence_class": "none"}]}
    assert validate_manifest(waived, S.allow, S.deny, uncertified_ok=False) == []
    assert validate_manifest({"schema": "x"}, S.allow, S.deny) == ["schema must be 'tauceti-roadmap-sources/v1'",
                                                                   "no [[source]] entries"]


def test_manifest_round_trip_keeps_every_value():
    data = manifest(title='Quotes " and back\\slashes, tabs\tand ünïcode', cited_for=["1.2", "Layer 3"],
                    version_note="line one\nline two", extra_flag=True, ratio=0.5)
    data["note"] = "top-level value"
    text = dump_manifest(data)
    assert tomllib.loads(text) == data
    assert text.index("id = ") < text.index("title = ") < text.index("sha256 = ") < text.index("extra_flag")


def test_upsert_merges_and_never_erases():
    data = manifest(cited_for=["1.2"])
    assert upsert_source(data, {"id": "ab21", "cited_for": ["2.1"], "locator": "Theorem 3", "venue": ""}) == "updated"
    entry = data["source"][0]
    assert entry["cited_for"] == ["1.2", "2.1"] and entry["locator"] == "Theorem 3" and "venue" not in entry
    assert upsert_source(data, {"id": "Sam73", "title": "A course", "doi": ""}) == "added"
    assert data["source"][1] == {"id": "Sam73", "title": "A course"}
    refetched = {"id": "AB21", "status": "certified", "sha256": "e" * 64, "licence_class": "operator-local"}
    assert upsert_source(data, refetched) == "updated"
    entry = data["source"][0]
    assert entry["sha256"] == "e" * 64 and "fetched_from" not in entry and "url" not in entry   # provenance as a group
    assert entry["title"] == "An example paper" and entry["cited_for"] == ["1.2", "2.1"]
    assert upsert_source(data, {"id": "AB21", "status": "waived", "title": "An example paper"}, replace=True) == "updated"
    assert data["source"][0] == {"id": "AB21", "status": "waived", "title": "An example paper"}


def test_find_source():
    data = manifest(doi="10.1234/abc", arxiv="2101.00001v2")
    assert find_source(data, id="ab21") is data["source"][0]
    assert find_source(data, arxiv="2101.00001v5") is data["source"][0]
    assert find_source(data, doi="10.1234/ABC") is data["source"][0]
    assert find_source(data, sha256=SHA) is data["source"][0]
    assert find_source(data, id="nope", doi="10.9/x") is None


@pytest.mark.parametrize("authors,year,key", [
    ("J.-P. Sample", 1973, "Sam73"),
    ("Sample, J.-P.", "1973", "Sam73"),
    ("S. Bauer, W. Lüttich, M. Renard", 1990, "BLR90"),
    ("Bauer, S. and Lüttich, W.", 1993, "BL93"),
    ("A. One; B. Two; C. Three; D. Four", 2021, "OTT+21"),
    ("", "", "Ano"),
])
def test_citation_key(authors, year, key):
    assert citation_key(authors, year) == key


def test_citation_key_avoids_existing():
    assert citation_key("J.-P. Sample", 1973, {"sam73", "Sam73a"}) == "Sam73b"


# --- cache ----------------------------------------------------------------------------------------

def test_cache_is_flat_and_named_by_content(tmp_path):
    f, txt = cache_paths(tmp_path, SHA)
    assert f == tmp_path / f"{SHA}.pdf" and txt == tmp_path / f"{SHA}.txt"
    path = store_bytes(tmp_path, b"%PDF-1.4 hello", "pdf")
    sha = sources.sha256_bytes(b"%PDF-1.4 hello")
    assert path == tmp_path / f"{sha}.pdf" and sources.sha256_file(path) == sha
    assert store_bytes(tmp_path, b"%PDF-1.4 hello", "pdf", expected=sha) == path
    with pytest.raises(sources.HashMismatch):
        store_bytes(tmp_path, b"%PDF-1.4 other", "pdf", expected=sha)
    (tmp_path / f"{sha}.txt").write_text("text")
    assert cached_file(tmp_path, sha) == path and cached_file(tmp_path, "b" * 64) is None
    assert not list(tmp_path.glob(".*.part"))


def test_sniff():
    assert sniff(b"%PDF-1.7\n...") == "pdf"
    assert sniff(b"\xef\xbb\xbf  %PDF-1.4") == "pdf"
    assert sniff(b"AT&TFORM\x00\x00") == "djvu"
    assert sniff(b"%!PS-Adobe-3.0") == "ps"
    assert sniff(b"<!DOCTYPE html><html>login</html>") == "html"
    assert sniff(b"{}", "text/html; charset=utf-8") == "html"
    assert sniff("Satz 2.3. Über".encode()) == "text"
    assert sniff(b"\x00\x01\x02\xff") == "bin"


# --- text extraction ------------------------------------------------------------------------------

def test_image_only_threshold():
    text_page = "word " * 200
    assert image_only(["cover sheet " * 30, "", "", ""])
    assert not image_only([text_page, text_page, ""])
    assert image_only([])


def test_numbered_text_marks_pages_and_numbers_every_line():
    out = numbered_text(["first\nsecond  ", "third"], ["Text of x."])
    lines = out.splitlines()
    assert lines[0] == "     1  # Text of x." and lines[1] == "     2  === page 1 ===" and lines[3] == "     4  second"
    assert lines[-1] == "     6  third" and lines[4].endswith("=== page 2 ===")


def test_html_to_text():
    page = "<html><head><style>p{}</style><script>alert(1)</script></head><body><h1>Tag 0AV8</h1>" \
           "<p>Lemma.   Let <i>R</i> be&nbsp;a ring.</p></body></html>"
    assert html_to_text(page) == "Tag 0AV8\n\nLemma. Let R be a ring.\n"


def test_extract_plain_text_and_unknown_kinds(tmp_path):
    (tmp_path / "a.text").write_text("Theorem 1.\nProof.")
    e = extract(tmp_path / "a.text", "text")
    assert e.pages == ["Theorem 1.\nProof."] and e.method == "plain text"
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    assert extract(tmp_path / "b.bin", "bin").note.startswith("no text extractor")


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="poppler's pdftotext is not installed")
def test_extract_pdf_with_pdftotext_and_image_only_note(tmp_path):
    (tmp_path / "t.pdf").write_bytes(make_pdf(["Theorem 2.3 holds for every ring " * 10, "Proof of Theorem 2.3 " * 15]))
    e = extract(tmp_path / "t.pdf", "pdf")
    assert e.method == "pdftotext -layout" and len(e.pages) == 2 and "Theorem 2.3" in e.pages[0] and not e.note
    (tmp_path / "scan.pdf").write_bytes(make_pdf(["Digitised by a library", "", "", ""]))
    off = extract(tmp_path / "scan.pdf", "pdf")
    assert "image-only scan (4 of 4 pages have no text layer)" in off.note and "enable [sources] ocr" in off.note
    if sources.ocr_status():
        on = extract(tmp_path / "scan.pdf", "pdf", ocr=True)
        assert "OCR is not installed" in on.note and "uv tool install" in on.note


def test_extract_pdf_with_pypdf(tmp_path, monkeypatch):
    pytest.importorskip("pypdf")
    monkeypatch.setattr(sources, "pdf_pages_pdftotext", lambda path: None)
    (tmp_path / "t.pdf").write_bytes(make_pdf(["Lemma 4.1 " * 40]))
    e = extract(tmp_path / "t.pdf", "pdf")
    assert e.method == "pypdf" and "Lemma 4.1" in e.pages[0]


def test_no_pdf_extractor_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "pdf_pages_pdftotext", lambda path: None)
    monkeypatch.setattr(sources, "pdf_pages_pypdf", lambda path: None)
    (tmp_path / "t.pdf").write_bytes(make_pdf(["x"]))
    assert "install poppler-utils" in extract(tmp_path / "t.pdf", "pdf").note


# --- OCR ------------------------------------------------------------------------------------------

def test_group_ocr_lines_reads_rows_left_to_right():
    def box(x, y):
        return [[x, y], [x + 50, y], [x + 50, y + 20], [x, y + 20]]
    items = [(box(300, 102), "holds."), (box(10, 100), "Theorem"), (box(120, 98), "2.3"), (box(10, 160), "Proof.")]
    assert group_ocr_lines(items) == ["Theorem 2.3 holds.", "Proof."]
    assert group_ocr_lines([]) == []


def test_ocr_result_adapters():
    old_api = ([[[[0, 0], [1, 0], [1, 1], [0, 1]], "Satz", 0.9]], 0.1)
    assert sources._ocr_items(old_api) == [([[0, 0], [1, 0], [1, 1], [0, 1]], "Satz")]
    assert sources._ocr_items((None, 0.1)) == []

    class NewApi:
        boxes = [[[0, 0], [1, 0], [1, 1], [0, 1]]]
        txts = ("Lemma",)
    assert sources._ocr_items(NewApi()) == [([[0, 0], [1, 0], [1, 1], [0, 1]], "Lemma")]


def test_ocr_not_installed_message(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_ocr(name, *args, **kwargs):
        if name.split(".")[0] in ("rapidocr_onnxruntime", "rapidocr", "pypdfium2"):
            raise ImportError(f"No module named {name!r}", name=name)
        return real(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_ocr)
    message = sources.ocr_status()
    assert message.startswith("OCR is not installed") and "[ocr]" in message and "opencv-python-headless" in message


def test_config_rejects_unknown_answer_association():
    text = '[project]\nname = "x"\n[sources]\nanswer_associations = ["ANYONE"]\n'
    with pytest.raises(config.ConfigError, match="answer_associations"):
        config.parse(text)


def fake_imports(monkeypatch, failures: dict):
    """importlib.import_module with some modules missing or failing to load."""
    import importlib as real_importlib

    def import_module(name):
        if name in failures:
            raise failures[name]
        return real_importlib.import_module(name) if name not in ("numpy", "pypdfium2") else object()
    monkeypatch.setattr(sources.importlib, "import_module", import_module)


def test_an_ocr_that_does_not_load_says_why(monkeypatch):
    """Regression: rapidocr's opencv-python failed on a headless server ("libGL.so.1: cannot open
    shared object file") and the K3 design parked twice on GDZ scans."""
    libgl = ImportError("libGL.so.1: cannot open shared object file: No such file or directory")
    fake_imports(monkeypatch, {"rapidocr_onnxruntime": libgl,
                               "rapidocr": ModuleNotFoundError("No module named 'rapidocr'", name="rapidocr")})
    message = sources.ocr_status()
    assert message.startswith("OCR is installed but does not load (ImportError: libGL.so.1: cannot open shared object")
    assert "--excludes" in message and "ocr-excludes.txt" in message and "opencv-python-headless" in message
    fake_imports(monkeypatch, {"rapidocr_onnxruntime": ModuleNotFoundError("x", name="rapidocr_onnxruntime"),
                               "rapidocr": ModuleNotFoundError("y", name="rapidocr")})
    assert sources.ocr_status().startswith("OCR is not installed (rapidocr_onnxruntime is missing)")
    fake_imports(monkeypatch, {"rapidocr_onnxruntime": ModuleNotFoundError("No module named 'cv2'", name="cv2"),
                               "rapidocr": ModuleNotFoundError("y", name="rapidocr")})
    assert "does not load (ModuleNotFoundError: No module named 'cv2')" in sources.ocr_status()


def test_the_ocr_extra_uses_headless_opencv():
    data = tomllib.loads((HERE / "pyproject.toml").read_text())
    extra = data["project"]["optional-dependencies"]["ocr"]
    assert any(r.startswith("opencv-python-headless") for r in extra) and any(r.startswith("rapidocr") for r in extra)
    assert data["tool"]["uv"]["exclude-dependencies"] == ["opencv-python"]
    excluded = [line for line in (HERE / "ocr-excludes.txt").read_text().splitlines() if line and not line.startswith("#")]
    assert excluded == ["opencv-python"] and "ocr-excludes.txt" in sources.OCR_INSTALL


def test_an_unrecognised_scan_is_marked_unreadable(tmp_path):
    sha = "a" * 64
    tmp_path.mkdir(exist_ok=True)
    scan = sources.Extraction(["Digitised by GDZ", "", ""], "pdftotext -layout", "image-only scan (3 of 3 pages have "
                              "no text layer): enable [sources] ocr to recognise it", unreadable=True)
    assert sources.unreadable(sources.write_text(tmp_path, sha, scan))
    ocred = sources.Extraction(["Satz 2.3 " * 40], "OCR (RapidOCR, 300 dpi)", "image-only scan (1 of 1 pages had no text layer)")
    assert not sources.unreadable(sources.write_text(tmp_path, "b" * 64, ocred))
    # A text extracted before the mark existed is recognised by its note.
    legacy = tmp_path / "legacy.txt"
    note = "Note: image-only scan (1 of 1 pages have no text layer): enable [sources] ocr to recognise it"
    legacy.write_text(sources.numbered_text(["x"], ["Text of c (pdftotext -layout; 1 pages).", note]))
    assert sources.unreadable(legacy) and not sources.unreadable(tmp_path / "absent.txt")
