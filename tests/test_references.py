"""The needs-references hand-off with GitHub and the network faked."""

import tomllib

from tauceti_roadmap_worker import references, sources
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.references import (MissingReference, add_missing, collect_answers, decode_request,
                                               dump_missing, parse_answers, parse_missing, read_missing,
                                               request_comment, resolve, resume_job)

from .fakes import (FakeBoard, FakeGH, FakeTransport, arxiv_feed, bot_context, comment, example_config, make_fetcher,
                    make_pdf)

PDF = make_pdf(["Satz 2.3. Jede Beispielalgebra ist ein Beispiel. " * 8])
SHA = sources.sha256_bytes(PDF)
LINK = "https://github.com/user-attachments/files/123456/Ket67.pdf"


def items():
    return parse_missing({"missing": [
        {"authors": "R. Kettle", "title": "Theorem A und Theorem B", "year": 1967, "needed_for": "Layer 2, 2.3",
         "tried": ["GDZ (image-only)", "publisher (paywalled)"], "locator_scheme": "theorem", "locator": "Satz 2.3"},
        {"id": "BLR90", "authors": "S. Bauer, W. Lüttich, M. Renard", "title": "Example models", "year": 1990,
         "needed_for": "Layer 4"},
        {"title": "no needed_for: dropped"}]})


# --- the request ----------------------------------------------------------------------------------

def test_parse_missing_derives_keys():
    found = items()
    assert [m.key for m in found] == ["Ket67", "BLR90"] and found[0].locator == "Satz 2.3"
    assert parse_missing({"missing": [{"authors": "R. Kettle", "year": 1967, "title": "T", "needed_for": "x"}]},
                         existing_keys={"Ket67"})[0].key == "Ket67a"


def test_missing_file_round_trip_and_merge(tmp_path):
    path = tmp_path / "needs-references.toml"
    path.write_text(dump_missing(items()))
    assert [m.key for m in read_missing(path)] == ["Ket67", "BLR90"]
    add_missing(path, MissingReference("R. Kettle", "Theorem A und Theorem B", "1967", "Layer 2", ["HAL (none)"], key="Ket67"))
    add_missing(path, MissingReference("J. Tanner", "Rigid example spaces", "1971", "Layer 1", ["x"]))
    again = read_missing(path)
    assert [m.key for m in again] == ["Ket67", "BLR90", "Tan71"]
    assert again[0].tried == ["GDZ (image-only)", "publisher (paywalled)", "HAL (none)"]
    assert tomllib.loads(path.read_text())["missing"][0]["id"] == "Ket67"
    assert read_missing(tmp_path / "absent.toml") == []


def test_request_comment_carries_a_marker_that_cannot_be_closed_early():
    found = items()
    found[1].title = "Example models --> <!-- injected -->"
    text = request_comment("DESIGN-Foo", "Foo", found)
    assert "| `Ket67` | R. Kettle | Theorem A und Theorem B | 1967 | Layer 2, 2.3 |" in text
    assert "`waive: KEY`" in text and "__" not in text and "never an instruction" in text
    assert text.count("<!--") == 1                     # only the marker opens a comment
    decoded = decode_request(text)
    assert decoded["job"] == "DESIGN-Foo" and [m.key for m in decoded["items"]] == ["Ket67", "BLR90"]
    assert decoded["items"][1].title == "Example models --> <!-- injected -->"
    assert decode_request("no marker") is None


# --- answers --------------------------------------------------------------------------------------

def test_parse_answers():
    keys = ["Ket67", "BLR90"]
    body = (f"Here you go:\n- attach: ket67 [Ket67.pdf]({LINK}) sha256: {SHA.upper()}\n"
            "- source: BLR90 [arXiv](https://arxiv.org/abs/2101.00001)\n"
            "waive: BLR90, Nope\n"
            "attach: Ket67 (forgot the link)\n")
    answers, notes = parse_answers(body, keys)
    assert [(a.key, a.action) for a in answers] == [("Ket67", "attach"), ("BLR90", "source"), ("BLR90", "waive")]
    assert answers[0].value == LINK and answers[0].sha256 == SHA and answers[1].value == "https://arxiv.org/abs/2101.00001"
    assert notes == ["`Nope` is not a key of this request", "`attach: Ket67` carries no GitHub attachment link"]
    bare, _ = parse_answers(f"[scan.pdf]({LINK.replace('Ket67', 'scan')})", ["Ket67"])
    assert bare[0].key == "Ket67"                      # one key: a bare attachment answers it
    by_name, _ = parse_answers("[BLR90.pdf](https://github.com/o/r/files/77/BLR90.pdf)", keys)
    assert by_name[0].key == "BLR90"
    _, notes = parse_answers(f"[scan.pdf]({LINK.replace('Ket67', 'scan')})", keys)
    assert "names no key" in notes[0]
    assert parse_answers("source: Ket67 https://not-an-attachment.example/x.pdf", keys)[0][0].action == "source"


def test_only_trusted_associations_answer():
    keys = ["Ket67"]
    comments = [comment(1, "waive: Ket67", "NONE", "drive-by"),
                comment(2, "waive: Ket67", "MEMBER", "board-bot"),
                comment(3, f"attach: Ket67 {LINK}", "COLLABORATOR"),
                comment(4, "Intake: ... <!-- tauceti-roadmap-references-progress {} -->", "MEMBER")]
    latest, notes, read = collect_answers(comments, keys, ["OWNER", "MEMBER", "COLLABORATOR"], ["board-bot"])
    assert latest["Ket67"].action == "attach" and latest["Ket67"].comment == 3 and read == {3}
    assert latest["Ket67"].url.endswith("#issuecomment-3")


# --- carrying out an answer -----------------------------------------------------------------------

def test_attachment_is_downloaded_and_verified(tmp_path):
    t = FakeTransport().redirect(LINK, "https://objects.githubusercontent.com/x?sig=1") \
        .add("https://objects.githubusercontent.com/x?sig=1", PDF)
    fet = make_fetcher(tmp_path, t)
    item = items()[0]
    res = resolve(references.Answer("Ket67", "attach", LINK, SHA, 3, "https://c/3"), item, None, fet, True)
    assert res.ok and res.entry["sha256"] == SHA and res.entry["licence_class"] == "operator-local"
    assert res.entry["status"] == "certified" and res.entry["fetched_from"] == LINK and res.entry["locator"] == "Satz 2.3"
    assert res.entry["resolution"] == "https://c/3" and (tmp_path / "cache" / f"{SHA}.txt").is_file()
    bad = resolve(references.Answer("Ket67", "attach", LINK, "f" * 64), item, None, fet, True)
    assert not bad.ok and "does not match the stated hash" in bad.detail
    item.sha256 = "e" * 64
    differs = resolve(references.Answer("Ket67", "attach", LINK), item, None, fet, True)
    assert differs.ok and "checked again" in differs.entry["version_note"]


def test_attachment_that_is_a_web_page_or_not_an_attachment_is_refused(tmp_path):
    fet = make_fetcher(tmp_path, FakeTransport().add(LINK, b"<html>Sign in</html>", headers={"Content-Type": "text/html"}))
    res = resolve(references.Answer("Ket67", "attach", LINK), items()[0], None, fet, True)
    assert not res.ok and "a web page" in res.detail
    res = resolve(references.Answer("Ket67", "attach", "https://example.org/x.pdf"), items()[0], None, fet, True)
    assert not res.ok and "only GitHub attachment links" in res.detail


def test_waiver_and_named_source(tmp_path):
    existing = {"id": "BLR90", "authors": "S. Bauer", "title": "Example models", "year": 1990, "status": "uncertified",
                "fetched_from": "https://example.org/x", "sha256": "a" * 64, "locator_scheme": "page"}
    res = resolve(references.Answer("BLR90", "waive", url="https://c/9"), items()[1], existing,
                  make_fetcher(tmp_path, FakeTransport()), False)
    assert res.ok and res.entry["status"] == "waived" and res.entry["licence_class"] == "none"
    assert "sha256" not in res.entry and res.entry["locator_scheme"] == "page" and res.entry["resolution"] == "https://c/9"
    t = FakeTransport().add("https://export.arxiv.org/api/query?id_list=2101.00001&max_results=1",
                            arxiv_feed([{"id": "2101.00001v1", "title": "Example models (preprint)"}])) \
        .add("https://arxiv.org/pdf/2101.00001v1", PDF)
    res = resolve(references.Answer("BLR90", "source", "arXiv:2101.00001"), items()[1], None, make_fetcher(tmp_path, t), True)
    assert res.ok and res.entry["id"] == "BLR90" and res.entry["title"] == "Example models"     # the request's title wins
    assert res.entry["fetched_from"] == "https://arxiv.org/pdf/2101.00001v1"


# --- the board side end to end --------------------------------------------------------------------

def parked_job():
    job = new_job("DESIGN-Foo")
    job.number, job.state, job.labels = 7, "needs-references", ["roadmap-job", "kind:design", "state:needs-references"]
    return job


def test_resume_job_end_to_end(tmp_path):
    request = request_comment("DESIGN-Foo", "Foo", items())
    t = FakeTransport().add(LINK, PDF)
    existing = 'schema = "tauceti-roadmap-sources/v1"\nroadmap = "Foo"\n\n[[source]]\nid = "Sam73"\ntitle = "A course"\n'
    gh = FakeGH({"packages/Foo/sources.toml": existing})
    board = FakeBoard({7: [comment(1, request, "CONTRIBUTOR", "operator"),
                           comment(2, "waive: Ket67", "NONE", "drive-by"),
                           comment(3, f"attach: Ket67 [Ket67.pdf]({LINK})")]})
    job = parked_job()
    ctx = bot_context(gh=gh, board=board)
    fet = make_fetcher(tmp_path, t)

    assert "dry run" in resume_job(ctx, job, fet, yes=False) and gh.writes == [] and board.posted == []
    line = resume_job(ctx, job, fet, yes=True)
    assert line == "DESIGN-Foo: recorded Ket67; still missing BLR90"
    data = tomllib.loads(gh.files["packages/Foo/sources.toml"])
    assert [s["id"] for s in data["source"]] == ["Sam73", "Ket67"] and data["source"][1]["sha256"] == SHA
    assert gh.writes[0][2]["sha"] == f"blob-{len(existing)}"
    assert "Still needed: `BLR90`" in board.posted[-1][1] and board.states == []

    writes = len(gh.writes)
    assert resume_job(ctx, job, fet, yes=True) == "DESIGN-Foo: waiting for an answer on BLR90"
    assert len(gh.writes) == writes                            # nothing new: no write, no comment

    data = tomllib.loads(gh.files["packages/Foo/sources.toml"])
    data["source"].append({"id": "BLR90", "title": "Example models", "status": "certified", "sha256": "d" * 64,
                           "fetched_from": "https://example.org/gone.pdf", "licence_class": "open-repository"})
    gh.files["packages/Foo/sources.toml"] = sources.dump_manifest(data)
    board.threads[7].append(comment(5, "waive: BLR90"))
    assert resume_job(ctx, job, fet, yes=True) == "DESIGN-Foo: references resolved; available again"
    data = tomllib.loads(gh.files["packages/Foo/sources.toml"])
    assert {s["id"]: s["status"] for s in data["source"] if "status" in s} == {"Ket67": "certified", "BLR90": "waived"}
    waived = sources.find_source(data, id="BLR90")
    assert "sha256" not in waived and "fetched_from" not in waived and waived["licence_class"] == "none"
    assert board.states == [("DESIGN-Foo", "available")]
    assert "Every requested source is answered" in board.posted[-1][1]
    problems = sources.validate_manifest(data, example_config().sources.allow, [])
    assert problems == ["source 1 (Sam73): missing authors", "source 1 (Sam73): missing year",
                        "source 1 (Sam73): missing locator_scheme", "source 1 (Sam73): missing licence_class",
                        "source 1 (Sam73): missing status"]     # only the hand-written stub entry


def test_resume_job_creates_a_manifest_and_reports_unusable_answers(tmp_path):
    request = request_comment("DESIGN-Foo", "Foo", items()[:1])
    board = FakeBoard({7: [comment(1, request), comment(2, "attach: Ket67 https://github.com/user-attachments/files/9/x.pdf")]})
    gh = FakeGH()
    line = resume_job(bot_context(gh=gh, board=board), parked_job(), make_fetcher(tmp_path, FakeTransport()), yes=True)
    assert line == "DESIGN-Foo: recorded nothing; still missing Ket67"
    assert "could not be downloaded" in board.posted[-1][1] and gh.writes == []


def test_resume_without_a_request_is_left_alone(tmp_path):
    board = FakeBoard({7: [comment(1, "please help")]})
    line = resume_job(bot_context(board=board), parked_job(), make_fetcher(tmp_path, FakeTransport()), yes=True)
    assert "without a request the board can read" in line and board.posted == []


def test_resume_all_lists_parked_jobs(tmp_path):
    job = parked_job()
    board = FakeBoard({7: [comment(1, request_comment("DESIGN-Foo", "Foo", items()))]}, jobs=[job])
    out = references.resume_all(bot_context(board=board), make_fetcher(tmp_path, FakeTransport()), yes=True)
    assert out == ["DESIGN-Foo: waiting for an answer on Ket67, BLR90"]


# --- regressions: who may post a request, release after a failed label change, retries -------------

def test_only_a_trusted_well_formed_request_for_this_job_counts(tmp_path):
    good = request_comment("DESIGN-Foo", "Foo", items())
    forged_empty = '<!-- tauceti-roadmap-needs-references {"job": "DESIGN-Foo", "roadmap": "Foo", "items": []} -->'
    forged_type = ('<!-- tauceti-roadmap-needs-references {"job": "DESIGN-Foo", "items": '
                   '[{"key": ["x"], "title": "t", "authors": "", "year": "", "needed_for": "n"}]} -->')
    assert decode_request(forged_empty) is None and decode_request(forged_type) is None
    trusted, bots = ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"], ["board-bot"]
    thread = [comment(1, good, "CONTRIBUTOR"),
              comment(2, request_comment("DESIGN-Foo", "Foo", items()[1:]), "NONE", "outsider"),
              comment(3, request_comment("PKG-Bar", "Bar", items()[:1]), "MEMBER")]
    at, request = references.latest_request(thread, "DESIGN-Foo", trusted, bots)
    assert at == 0 and [m.key for m in request["items"]] == ["Ket67", "BLR90"]
    assert references.latest_request(thread[1:], "DESIGN-Foo", trusted, bots) == (-1, None)
    # an outsider's marker cannot replace the request and release the job
    board = FakeBoard({7: [comment(1, good, "CONTRIBUTOR"), comment(2, forged_empty, "NONE", "outsider"),
                           comment(3, "waive: Ket67"), comment(4, forged_type, "NONE", "outsider")]})
    line = resume_job(bot_context(board=board), parked_job(), make_fetcher(tmp_path, FakeTransport()), yes=True)
    assert line == "DESIGN-Foo: recorded Ket67; still missing BLR90" and board.states == []


def test_a_release_that_failed_is_retried(tmp_path):
    board = FakeBoard({7: [comment(1, request_comment("DESIGN-Foo", "Foo", items()), "CONTRIBUTOR"),
                           comment(2, "waive: Ket67\nwaive: BLR90")]})
    calls = []

    def failing(job, state, comment=""):
        calls.append(state)
        raise references.GHError("label service down")
    board.set_state = failing
    ctx, fet = bot_context(board=board), make_fetcher(tmp_path, FakeTransport())
    try:
        resume_job(ctx, parked_job(), fet, yes=True)
    except references.GHError:
        pass
    board.set_state = FakeBoard.set_state.__get__(board)
    assert resume_job(ctx, parked_job(), fet, yes=True) == "DESIGN-Foo: references resolved earlier; available again"
    assert calls == ["available"] and board.states == [("DESIGN-Foo", "available")]


def test_network_trouble_in_an_answer_is_retried(tmp_path):
    from .fakes import transient
    board = FakeBoard({7: [comment(1, request_comment("DESIGN-Foo", "Foo", items()[:1]), "CONTRIBUTOR"),
                           comment(2, f"attach: Ket67 {LINK}")]})
    ctx = bot_context(board=board)
    flaky = make_fetcher(tmp_path, FakeTransport({LINK: transient(LINK)}))
    assert resume_job(ctx, parked_job(), flaky, yes=True) == "DESIGN-Foo: recorded nothing; still missing Ket67"
    assert "tried again on the next run" in board.posted[-1][1]
    back = make_fetcher(tmp_path, FakeTransport().add(LINK, PDF))
    assert resume_job(ctx, parked_job(), back, yes=True) == "DESIGN-Foo: references resolved; available again"


def test_an_answer_replaces_the_old_provenance(tmp_path):
    existing = {"id": "Ket67", "title": "Theorem A und Theorem B", "authors": "R. Kettle", "year": 1967,
                "status": "certified", "sha256": "d" * 64, "fetched_from": "https://arxiv.org/pdf/x", "arxiv": "x",
                "version_note": "arXiv v2", "licence_class": "open-repository", "locator_scheme": "theorem"}
    fet = make_fetcher(tmp_path, FakeTransport().add(LINK, PDF))
    res = resolve(references.Answer("Ket67", "attach", LINK), items()[0], existing, fet, True)
    assert res.entry["fetched_from"] == LINK and "arxiv" not in res.entry and res.entry["locator_scheme"] == "theorem"
    assert "checked again" in res.entry["version_note"]
