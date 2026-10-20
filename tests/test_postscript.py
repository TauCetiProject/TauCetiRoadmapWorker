"""PostScript and compressed sources (the pilot's first needs-references request: an author page's
`reg.ps.gz` read as "unrecognised file, not a document")."""

import gzip
import shutil

import pytest

from tauceti_roadmap_worker import fetcher, sources

from .fakes import FakeTransport, make_fetcher, make_pdf

PS = b"%!PS-Adobe-3.0\n/Helvetica findfont 14 scalefont setfont\n72 720 moveto (Satz 2.3 Jede Algebra) show\nshowpage\n"
needs_gs = pytest.mark.skipif(not (shutil.which("gs") and shutil.which("pdftotext")), reason="gs and pdftotext needed")


def test_a_gzipped_body_is_classified_by_what_it_holds():
    assert sources.sniff(gzip.compress(PS)) == "bin"
    assert sources.sniff(sources.unwrap(gzip.compress(PS))) == "ps"
    assert sources.unwrap(b"%PDF-1.4 plain") == b"%PDF-1.4 plain"
    assert sources.unwrap(b"\x1f\x8b not really gzip") == b"\x1f\x8b not really gzip"


def test_glyph_names_are_not_text():
    words = "Let K be a number field and p a prime of K above the rational prime under study. " * 4
    names = " ".join(f"/g{k} /C{100 + k}" for k in range(60))
    assert not sources.garbled([words]) and sources.garbled([names]) and not sources.garbled(["short"])


def test_without_ghostscript_the_service_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "ghostscript", lambda: None)
    url = "https://www.math.example.edu/~besser/reg.ps.gz"
    fet = make_fetcher(tmp_path, FakeTransport().add(url, gzip.compress(PS)))
    out = fet.fetch(fetcher.parse_citation(url), licence_class="author-page")
    assert out.status == "failed" and any("needs Ghostscript (gs)" in t for t in out.tried)
    assert not any("unrecognised file" in t for t in out.tried)
    assert sources.extract(tmp_path / "x.ps", "ps").note.startswith("needs Ghostscript")


@needs_gs
def test_postscript_is_read_through_ghostscript(tmp_path):
    probe = tmp_path / "probe.ps"
    probe.write_bytes(PS)
    if not sources.gs_pdf(probe, tmp_path / "probe.pdf"):
        pytest.skip("this Ghostscript has no standard fonts (a dvips file embeds its own)")
    url = "https://www.math.example.edu/~besser/reg.ps.gz"
    fet = make_fetcher(tmp_path, FakeTransport().add(url, gzip.compress(PS)))
    out = fet.fetch(fetcher.parse_citation(url), licence_class="author-page")
    assert out.status == "fetched", out.tried
    assert out.file.suffix == ".ps" and out.entry["sha256"] == sources.sha256_bytes(PS)
    text = out.text.read_text()
    assert "Satz 2.3 Jede Algebra" in text and "Ghostscript pdfwrite" in text


def test_a_readable_pdf_is_left_alone(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(make_pdf(["Theorem 2.3 holds for every prime. " * 20]))
    if shutil.which("pdftotext") or sources.pdf_pages_pypdf(path) is not None:
        assert "Ghostscript" not in sources.extract(path, "pdf").method
