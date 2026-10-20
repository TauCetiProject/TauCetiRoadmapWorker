"""The sources service on canned responses: no test touches the network."""

import json

import pytest

from tauceti_roadmap_worker import fetcher, sources
from tauceti_roadmap_worker.fetcher import (Client, FetchError, crossref_meta, direct_pdf_url, landing_pdf,
                                            local_match, parse_arxiv_feed, parse_citation, parse_hal,
                                            surnames_match, title_matches)

from .fakes import HAL_EMPTY, FakeTransport, arxiv_feed, crossref, example_config, make_fetcher, make_pdf, transient

PDF = make_pdf(["Theorem 2.3. Every example ring is an example. " * 8, "Proof. Compute. " * 30])
ARXIV_API = "https://export.arxiv.org/api/query?id_list=2101.00001&max_results=1"


# --- citations ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind,value", [
    ("10.1007/BF01390187", "doi", "10.1007/BF01390187"),
    ("doi: 10.1007/BF01390187.", "doi", "10.1007/BF01390187"),
    ("https://doi.org/10.24033/asens.1234", "doi", "10.24033/asens.1234"),
    ("https://dx.doi.org/10.1215%2Fs0012-7094", "doi", "10.1215/s0012-7094"),
    ("2101.00001v2", "arxiv", "2101.00001v2"),
    ("arXiv:2101.00001", "arxiv", "2101.00001"),
    ("math/0601001", "arxiv", "math/0601001"),
    ("math.AG/0601001v3", "arxiv", "math.AG/0601001v3"),
    ("https://arxiv.org/abs/2101.00001v1", "arxiv", "2101.00001v1"),
    ("https://arxiv.org/pdf/2101.00001.pdf", "arxiv", "2101.00001"),
    ("<https://www.numdam.org/item/AIHPB_2007__43_1_1_0/>", "url", "https://www.numdam.org/item/AIHPB_2007__43_1_1_0/"),
    ("~/books/kiehl.pdf", "file", "~/books/kiehl.pdf"),
])
def test_parse_citation(text, kind, value):
    c = parse_citation(text)
    assert (c.kind, c.value) == (kind, value)


def test_parse_free_text_citation():
    c = parse_citation('R. Kettle, "Theorem A und Theorem B für Beispielräume", Invent. Math. 2 (1967)')
    assert c.kind == "text" and c.title.startswith("Theorem A und") and c.year == "1967"
    assert parse_citation("Sample, A course in examples", year="1973").year == "1973"


def test_title_and_author_matching():
    assert title_matches("Théorèmes de finitude", "Theoremes de finitude")
    assert title_matches("Theorem A und Theorem B", "theorem a und theorem b")
    assert not title_matches("Theorem A und Theorem B", "Theorem C und Theorem D")
    assert not title_matches("Theorem A", "A different paper about modular forms")
    assert title_matches("On the thing and its cousin", "A. Author, On the Thing and its Cousin, 1971", free_text=True)
    assert not title_matches("On the thing", "A. Author, On the Thing, 1971", free_text=True)   # one word: too weak
    assert surnames_match(["Jean-Pierre Sample"], "J.-P. Sample") and surnames_match("anyone", "")
    assert not surnames_match(["John Other"], "J.-P. Sample")


# --- the policy client ----------------------------------------------------------------------------

def client(transport, **kw):
    cfg = example_config().sources
    return Client(transport, allow=cfg.allow, deny=cfg.deny + ["pastebin.com"], paywalled=cfg.paywalled, **kw)


def test_denied_and_paywalled_hosts_are_never_contacted():
    t = FakeTransport()
    c = client(t, pace={})
    with pytest.raises(FetchError) as exc:
        c.get("https://sci-hub.se/10.1/x")
    assert exc.value.refused and t.requests == []
    with pytest.raises(FetchError) as exc:
        c.get("https://link.springer.com/article/10.1007/x")
    assert exc.value.paywalled and t.requests == []
    with pytest.raises(FetchError) as exc:
        c.get("file:///etc/passwd")
    assert exc.value.refused


def test_redirects_are_checked_hop_by_hop():
    t = FakeTransport().redirect("https://doi.org/10.1/a", "https://example.org/landing") \
        .redirect("https://example.org/landing", "https://www.sciencedirect.com/science/article/pii/1")
    with pytest.raises(FetchError) as exc:
        client(t, pace={}).get("https://doi.org/10.1/a")
    assert exc.value.paywalled and t.requests == ["https://doi.org/10.1/a", "https://example.org/landing"]
    t = FakeTransport().redirect("https://example.org/a", "/b").add("https://example.org/b", b"ok")
    r = client(t, pace={}).get("https://example.org/a")
    assert r.url == "https://example.org/b" and r.body == b"ok"
    t = FakeTransport().redirect("https://example.org/a", "https://libgen.is/x")
    with pytest.raises(FetchError, match="deny list"):
        client(t, pace={}).get("https://example.org/a")


def test_status_codes():
    t = FakeTransport().add("https://example.org/gone", status=410).add("https://example.org/busy", status=503) \
        .add("https://example.org/pay", status=402)
    c = client(t, pace={})
    with pytest.raises(FetchError) as exc:
        c.get("https://example.org/gone")
    assert exc.value.permanent and not exc.value.paywalled
    with pytest.raises(FetchError) as exc:
        c.get("https://example.org/busy")
    assert not exc.value.permanent
    with pytest.raises(FetchError) as exc:
        c.get("https://example.org/pay")
    assert exc.value.paywalled


def test_arxiv_is_paced():
    waits, now = [], [100.0]
    t = FakeTransport().add("https://arxiv.org/a", b"x").add("https://export.arxiv.org/b", b"y")
    c = client(t, sleep=waits.append, clock=lambda: now[0])
    c.get("https://arxiv.org/a")
    c.get("https://export.arxiv.org/b")
    assert waits == [3.0]


# --- resolvers ------------------------------------------------------------------------------------

def test_landing_pages():
    meta = '<html><head><meta content="/item/X.pdf" name="citation_pdf_url"></head></html>'
    assert landing_pdf(meta, "https://www.numdam.org/item/X/") == ("https://www.numdam.org/item/X.pdf", "")
    one = '<a href="/download/pdf/PPN1/LOG_0012.pdf">PDF</a><a href="/about">about</a>'
    assert landing_pdf(one, "https://gdz.sub.uni-goettingen.de/id/PPN1")[0].endswith("/download/pdf/PPN1/LOG_0012.pdf")
    assert landing_pdf('<a href="a.pdf">1</a><a href="b.pdf">2</a>', "https://x.org/") == ("", "2 PDF links (name the right one)")
    assert landing_pdf("<p>nothing</p>", "https://x.org/") == ("", "no PDF link")


def test_direct_pdf_urls():
    assert direct_pdf_url("https://arxiv.org/abs/2101.00001v2") == "https://arxiv.org/pdf/2101.00001v2"
    assert direct_pdf_url("https://www.numdam.org/item/AIHPB_2007__43_1_1_0/") == \
        "https://www.numdam.org/item/AIHPB_2007__43_1_1_0.pdf"
    assert direct_pdf_url("https://www.numdam.org/item/ASNSP_2009_5_8_1_143_0.pdf").endswith("_0.pdf")
    assert direct_pdf_url("https://msp.org/pjm/1967/21-2/p.pdf") == "https://msp.org/pjm/1967/21-2/p.pdf"


def test_metadata_parsers():
    feed = arxiv_feed([{"id": "2101.00001v2", "title": "An  example\n paper", "authors": ["A. Author", "B. Author"],
                        "doi": "10.1234/x"}]).replace(b"<feed", b"<feed").replace(
        b"</feed>", b"<entry><id>http://arxiv.org/api/errors#bad</id><title>Error</title></entry></feed>")
    [entry] = parse_arxiv_feed(feed)
    assert entry == {"arxiv": "2101.00001v2", "title": "An example paper", "authors": ["A. Author", "B. Author"],
                     "year": "2021", "doi": "10.1234/x", "venue": ""}
    assert parse_arxiv_feed(b"not xml") == []
    msg = json.loads(crossref("10.1/x", "A title", [("A.", "Author")], 1999, ["arXiv:2101.00001"]))["message"]
    msg["relation"]["has-preprint"].append({"id-type": "doi", "id": "10.48550/arXiv.2102.00002"})
    meta = crossref_meta(msg)
    assert meta["preprints"] == ["2101.00001", "2102.00002"] and meta["year"] == "1999" and meta["authors"] == "A. Author"
    assert meta["venue"] == "J. Example Math. 12"
    hal = json.dumps({"response": {"docs": [{"title_s": ["T"], "authFullName_s": ["A B"], "producedDateY_i": 2001,
                                             "fileMain_s": "https://hal.science/hal-1/document", "uri_s": "https://hal.science/hal-1"}]}})
    assert parse_hal(hal.encode())[0]["file"] == "https://hal.science/hal-1/document"
    assert parse_hal(b"{}") == []


def test_local_match(tmp_path):
    files = [tmp_path / "2101.00001v2.pdf", tmp_path / "kiehl-1967-theorem-a-und-theorem-b.pdf",
             tmp_path / "s10240-014-0064-7.pdf", tmp_path / "math-0601001.pdf"]
    assert local_match(files, arxiv="2101.00001") == files[0]
    assert local_match(files, arxiv="math/0601001v2") == files[3]
    assert local_match(files, doi="10.1007/s10240-014-0064-7") == files[2]
    assert local_match(files, title="Theorem A und Theorem B", year="1967") == files[1]
    assert local_match(files, title="Theorem A und Theorem B", year="1970") is None


# --- fetching -------------------------------------------------------------------------------------

def test_fetch_arxiv_records_the_version_and_a_numbered_text(tmp_path):
    t = FakeTransport().add(ARXIV_API, arxiv_feed([{"id": "2101.00001v2", "title": "An example paper",
                                                    "authors": ["A. Author", "B. Author"]}])) \
        .add("https://arxiv.org/pdf/2101.00001v2", PDF, headers={"Content-Type": "application/pdf"})
    out = make_fetcher(tmp_path, t).fetch(parse_citation("arXiv:2101.00001"))
    assert out.status == "fetched", out.tried
    e = out.entry
    assert e["fetched_from"] == "https://arxiv.org/pdf/2101.00001v2" and e["url"] == "https://arxiv.org/abs/2101.00001v2"
    assert e["licence_class"] == "open-repository" and e["status"] == "certified" and e["year"] == 2021
    assert e["arxiv"] == "2101.00001v2" and e["version_note"] == "arXiv v2" and e["authors"] == "A. Author, B. Author"
    assert e["sha256"] == sources.sha256_bytes(PDF) and e["fetched_at"].endswith("Z")
    assert out.file == tmp_path / "cache" / f"{e['sha256']}.pdf" and out.text.name == f"{e['sha256']}.txt"
    head = out.text.read_text().splitlines()
    assert head[0].lstrip().split("  ", 1)[1].startswith(f"# Text of {e['sha256']} (") and "2 pages" in head[0]
    assert any(line.endswith("=== page 2 ===") for line in head)
    entry = {**e, "id": "AB21", "locator_scheme": "theorem"}
    assert sources.validate_manifest({"schema": sources.SCHEMA, "source": [entry]}, example_config().sources.allow, []) == []


def test_paywalled_doi_prefix_is_uncertified_without_contacting_the_publisher(tmp_path):
    doi = "10.1016/j.jalgebra.2020.01.001"
    t = FakeTransport().add(f"https://api.crossref.org/works/{doi}", crossref(doi, "A paywalled theorem",
                                                                             [("C.", "Writer")], 2020)) \
        .add(fetcher.hal_query_url(doi=doi), HAL_EMPTY)
    out = make_fetcher(tmp_path, t).fetch(parse_citation(doi))
    assert out.status == "uncertified" and out.entry["doi"] == doi and out.entry["licence_class"] == "none"
    assert out.entry["url"] == f"https://doi.org/{doi}" and "sha256" not in out.entry
    assert not any("doi.org/" + doi in r or "sciencedirect" in r for r in t.requests)
    assert any("paywalled publisher" in x for x in out.tried)


def test_doi_landing_on_a_paywalled_host_is_uncertified(tmp_path):
    doi = "10.4007/annals.2000.1"
    t = FakeTransport().add(f"https://api.crossref.org/works/{doi}", crossref(doi, "Some title words here", [], 2000)) \
        .add(fetcher.hal_query_url(doi=doi), HAL_EMPTY) \
        .redirect(f"https://doi.org/{doi}", "https://link.springer.com/article/x")
    out = make_fetcher(tmp_path, t).fetch(parse_citation(doi))
    assert out.status == "uncertified"
    assert "https://link.springer.com/article/x" not in t.requests


def test_doi_with_a_listed_preprint_fetches_arxiv(tmp_path):
    doi = "10.1007/s00222-019-00001-1"
    t = FakeTransport().add(f"https://api.crossref.org/works/{doi}",
                            crossref(doi, "An example paper", [("A.", "Author")], 2019, ["2101.00001"])) \
        .add("https://arxiv.org/pdf/2101.00001", PDF)
    out = make_fetcher(tmp_path, t).fetch(parse_citation(doi))
    assert out.status == "fetched" and out.entry["doi"] == doi and out.entry["licence_class"] == "open-repository"
    assert out.entry["title"] == "An example paper" and out.entry["version_note"] == "arXiv, latest version"


def test_doi_landing_on_an_open_repository(tmp_path):
    doi = "10.24033/asens.1234"
    landing = b'<html><meta name="citation_pdf_url" content="https://www.numdam.org/item/ASENS_1967_4_1_1_0.pdf"></html>'
    t = FakeTransport().add(f"https://api.crossref.org/works/{doi}", crossref(doi, "Sur les choses", [("A.", "Auteur")], 1967)) \
        .add(fetcher.hal_query_url(doi=doi), HAL_EMPTY) \
        .redirect(f"https://doi.org/{doi}", "https://www.numdam.org/item/ASENS_1967_4_1_1_0/") \
        .add("https://www.numdam.org/item/ASENS_1967_4_1_1_0/", landing, headers={"Content-Type": "text/html"}) \
        .add("https://www.numdam.org/item/ASENS_1967_4_1_1_0.pdf", PDF)
    t.routes[fetcher.arxiv_query_url(title="Sur les choses", authors="A. Auteur")] = \
        fetcher.Response(200, "", {}, arxiv_feed([]))
    out = make_fetcher(tmp_path, t).fetch(parse_citation(doi))
    assert out.status == "fetched", out.tried
    assert out.entry["fetched_from"] == "https://www.numdam.org/item/ASENS_1967_4_1_1_0.pdf"
    assert out.entry["url"] == f"https://doi.org/{doi}" and out.entry["licence_class"] == "open-repository"


def test_landing_whose_pdf_link_is_a_login_page_counts_as_paywalled(tmp_path):
    url = "https://projecteuclid.org/journals/x/1.full"
    t = FakeTransport().add(url, b'<html><meta name="citation_pdf_url" content="https://projecteuclid.org/x.pdf"></html>') \
        .add("https://projecteuclid.org/x.pdf", b"<html>Sign in to read</html>", headers={"Content-Type": "text/html"})
    out = make_fetcher(tmp_path, t).fetch(parse_citation(url))
    assert out.status == "uncertified" and "login or subscription" in " ".join(out.tried)


def test_unknown_hosts_need_a_named_licence_class(tmp_path):
    url = "https://www.math.example.edu/~someone/notes.pdf"
    t = FakeTransport().add(url, PDF)
    out = make_fetcher(tmp_path, t).fetch(parse_citation(url))
    assert out.status == "failed" and "in no licence class" in " ".join(out.tried)
    out = make_fetcher(tmp_path, t).fetch(parse_citation(url), licence_class="author-page")
    assert out.status == "fetched" and out.entry["licence_class"] == "author-page" and out.entry["url"] == url


def test_a_named_class_never_vouches_for_discovered_urls(tmp_path):
    doi = "10.9999/unknown.1"
    t = FakeTransport().add(f"https://api.crossref.org/works/{doi}", crossref(doi, "Words of a title", [], 2001)) \
        .add(fetcher.hal_query_url(doi=doi), HAL_EMPTY) \
        .redirect(f"https://doi.org/{doi}", "https://journal.example.com/article/1") \
        .add("https://journal.example.com/article/1", b'<html><meta name="citation_pdf_url" content="/a.pdf"></html>') \
        .add("https://journal.example.com/a.pdf", PDF)
    out = make_fetcher(tmp_path, t).fetch(parse_citation(doi), licence_class="author-page")
    assert out.status == "failed" and "https://journal.example.com/a.pdf" not in t.requests


def test_pirate_mirrors_are_refused_without_a_request(tmp_path):
    t = FakeTransport()
    out = make_fetcher(tmp_path, t).fetch(parse_citation("https://libgen.is/book/index.php?md5=x"), licence_class="author-page")
    assert out.status == "failed" and t.requests == [] and "deny list" in " ".join(out.tried)


def test_network_trouble_is_transient_not_uncertified(tmp_path):
    t = FakeTransport({ARXIV_API: transient(ARXIV_API), "https://arxiv.org/pdf/2101.00001": transient("x")})
    out = make_fetcher(tmp_path, t).fetch(parse_citation("2101.00001"))
    assert out.status == "failed" and out.transient and out.note.startswith("network trouble")


def test_local_texts_come_first_and_need_no_network(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "2101.00001v1.pdf").write_bytes(PDF)
    t = FakeTransport().add(ARXIV_API, arxiv_feed([{"id": "2101.00001v1", "title": "An example paper"}]))
    out = make_fetcher(tmp_path, t, local_dirs=[str(lib)]).fetch(parse_citation("2101.00001"))
    assert out.status == "fetched" and out.entry["licence_class"] == "operator-local" and "fetched_from" not in out.entry
    assert t.requests == [ARXIV_API]
    explicit = make_fetcher(tmp_path, FakeTransport(), local_dirs=[str(lib)]).fetch(parse_citation(str(lib / "2101.00001v1.pdf")))
    assert explicit.status == "fetched"
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(PDF)
    refused = make_fetcher(tmp_path, FakeTransport(), local_dirs=[str(lib)]).fetch(parse_citation(str(outside)))
    assert refused.status == "failed" and "local_dirs" in " ".join(refused.tried)


def test_free_text_search_through_crossref_and_arxiv(tmp_path):
    text = "A. Author, An example paper, 2021"
    t = FakeTransport()
    t.routes[fetcher.arxiv_query_url(text=text, authors="")] = fetcher.Response(200, "", {}, arxiv_feed(
        [{"id": "2101.00001v3", "title": "An example paper", "authors": ["A. Author"]}]))
    t.add("https://arxiv.org/pdf/2101.00001v3", PDF)
    out = make_fetcher(tmp_path, t).fetch(parse_citation(text))
    assert out.status == "fetched" and out.entry["arxiv"] == "2101.00001v3" and out.entry["title"] == "An example paper"


# --- texts the manifest records -------------------------------------------------------------------

def test_materialize_downloads_and_verifies(tmp_path):
    sha = sources.sha256_bytes(PDF)
    entry = {"id": "AB21", "title": "An example paper", "status": "certified", "sha256": sha,
             "fetched_from": "https://arxiv.org/pdf/2101.00001v2", "licence_class": "open-repository"}
    data = {"source": [entry, {"id": "Unc", "status": "uncertified"}]}
    t = FakeTransport().add("https://arxiv.org/pdf/2101.00001v2", PDF)
    fet = make_fetcher(tmp_path, t)
    [dry] = fet.materialize(data, offline=True)
    assert dry.text is None and not dry.permanent and t.requests == []
    [got] = fet.materialize(data)
    assert got.text == tmp_path / "cache" / f"{sha}.txt" and got.text.is_file()
    t.requests.clear()
    assert fet.materialize(data)[0].text and t.requests == []          # cached: no request
    changed = make_fetcher(tmp_path / "other", FakeTransport().add("https://arxiv.org/pdf/2101.00001v2", PDF + b"x"))
    [bad] = changed.materialize(data)
    assert bad.text is None and bad.permanent and "changed" in bad.problem
    gone = make_fetcher(tmp_path / "gone", FakeTransport())
    assert gone.materialize(data)[0].permanent
    flaky = make_fetcher(tmp_path / "flaky", FakeTransport({"https://arxiv.org/pdf/2101.00001v2": transient("x")}))
    assert not flaky.materialize(data)[0].permanent


def test_materialize_finds_operator_texts_by_hash(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "renamed-scan.pdf").write_bytes(PDF)
    sha = sources.sha256_bytes(PDF)
    data = {"source": [{"id": "Ket67", "status": "certified", "sha256": sha, "licence_class": "operator-local"}]}
    [got] = make_fetcher(tmp_path, FakeTransport(), local_dirs=[str(lib)]).materialize(data)
    assert got.text and (tmp_path / "cache" / "local-index.json").is_file()
    [missing] = make_fetcher(tmp_path / "x", FakeTransport(), local_dirs=[]).materialize(data)
    assert missing.permanent and "does not hold" in missing.problem
