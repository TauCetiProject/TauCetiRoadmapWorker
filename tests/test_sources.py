from tauceti_roadmap_worker.config import Sources
from tauceti_roadmap_worker.sources import (MissingReference, cache_paths, classify_url, needs_references_comment,
                                            parse_missing, validate_manifest)

S = Sources()
SHA = "a" * 64


def manifest(**over):
    entry = {"id": "BS22", "authors": "B. Bhatt, P. Scholze", "title": "Prisms and prismatic cohomology", "year": 2022,
             "locator_scheme": "theorem", "url": "https://arxiv.org/abs/1905.08229",
             "fetched_from": "https://arxiv.org/pdf/1905.08229v3", "sha256": SHA, "licence_class": "open-repository",
             "status": "certified"}
    entry.update(over)
    return {"schema": "tauceti-roadmap-sources/v1", "roadmap": "Prismatic", "source": [entry]}


def test_classify_url():
    assert classify_url("https://arxiv.org/abs/1", S.allow, S.deny) == "open-repository"
    assert classify_url("https://export.arxiv.org/abs/1", S.allow, S.deny) == "open-repository"
    assert classify_url("https://www.numdam.org/item/X", S.allow, S.deny) == "open-repository"
    assert classify_url("https://sci-hub.se/10.1/x", S.allow, S.deny) == "denied"
    assert classify_url("https://libgen.is/book", S.allow, S.deny) == "denied"
    assert classify_url("https://example.edu/~someone/paper.pdf", S.allow, S.deny) == "unknown"


def test_valid_manifest():
    assert validate_manifest(manifest(), S.allow, S.deny) == []


def test_manifest_problems():
    assert any("deny list" in p for p in validate_manifest(manifest(fetched_from="https://sci-hub.se/x"), S.allow, S.deny))
    assert any("SHA-256" in p for p in validate_manifest(manifest(sha256=""), S.allow, S.deny))
    assert any("missing year" in p for p in validate_manifest(manifest(year=""), S.allow, S.deny))
    assert any("licence_class" in p for p in validate_manifest(manifest(licence_class="pirate"), S.allow, S.deny))
    unc = manifest(status="uncertified", fetched_from="", sha256="", licence_class="publisher-open")
    assert validate_manifest(unc, S.allow, S.deny) == []
    assert validate_manifest(unc, S.allow, S.deny, uncertified_ok=False)
    assert validate_manifest({"schema": "x"}, S.allow, S.deny) == ["schema must be 'tauceti-roadmap-sources/v1'",
                                                                   "no [[source]] entries"]


def test_cache_paths_sit_side_by_side(tmp_path):
    f, txt = cache_paths(tmp_path, SHA)
    assert f.parent == txt.parent == tmp_path / "aa" and f.name == SHA + ".pdf" and txt.name == SHA + ".txt"


def test_needs_references_comment():
    items = parse_missing({"missing": [{"authors": "A. Author", "title": "A | B", "year": 1970,
                                        "needed_for": "Theorem 2.3 (the comparison)", "tried": ["publisher page"]},
                                       {"title": "no needed_for: dropped"}]})
    assert len(items) == 1 and isinstance(items[0], MissingReference)
    text = needs_references_comment("DESIGN-Foo", "Foo", items)
    assert "`DESIGN-Foo`" in text and "A \\| B" in text and "Theorem 2.3" in text and "__" not in text
    assert "never an instruction" in text and not text.startswith("---")
