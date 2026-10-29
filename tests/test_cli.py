import json
import subprocess
import tomllib

import pytest

from tauceti_roadmap_worker import cli, fetcher, sources
from tauceti_roadmap_worker.paths import HERE

from .fakes import FakeTransport, arxiv_feed, crossref, make_pdf
from .test_gate_scanners import CLEAN_LEAN, CLEAN_README

EXAMPLE = str(HERE / "tauceti-roadmap.toml.example")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    wt = tmp_path / "wt"
    folder = wt / "TauCetiRoadmap" / "Toy"
    folder.mkdir(parents=True)
    git(wt, "init", "-q")
    git(wt, "commit", "-q", "--allow-empty", "-m", "base")
    (folder / "README.md").write_text(CLEAN_README + "\n*Needs:* Alpha §1.\n", encoding="utf-8")
    (folder / "Suggested.lean").write_text(CLEAN_LEAN, encoding="utf-8")
    (folder / "metadata.toml").write_text('topic = "math.GR"\n', encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "toy")
    (tmp_path / "ORDER.txt").write_text("Alpha\nToy\n", encoding="utf-8")
    return wt


def test_gate_cli_exit_codes(worktree, tmp_path, capsys):
    order = str(tmp_path / "ORDER.txt")
    args = ["--conf", EXAMPLE, "gate", str(worktree), "Toy", "--base", "HEAD~1", "--order", order]
    assert cli.main(args + ["--out", str(tmp_path / "gate.json")]) == 0
    assert capsys.readouterr().out.startswith("gate Toy:")
    assert '"ok": true' in (tmp_path / "gate.json").read_text()
    (worktree / "README.md").write_text("outside the folder\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "outside")
    assert cli.main(args) == 1
    assert "outside-folder=1" in capsys.readouterr().out


def test_depcheck_cli(worktree, tmp_path, capsys):
    upward = tmp_path / "README.md"
    upward.write_text("*Needs:* Toy §1.\n", encoding="utf-8")
    order = str(tmp_path / "ORDER.txt")
    assert cli.main(["--conf", EXAMPLE, "gate", "--depcheck", str(upward), "--name", "Alpha", "--order", order]) == 1
    assert "UPWARD: Alpha (#1) -> Toy (#2)" in capsys.readouterr().out


def test_sources_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    assert cli.main(["--conf", EXAMPLE, "sources", "classify", "https://libgen.is/x"]) == 0
    assert capsys.readouterr().out.strip().startswith("denied (")
    assert cli.main(["--conf", EXAMPLE, "sources", "classify", "https://pastebin.com/x"]) == 0   # the sandbox list
    assert capsys.readouterr().out.strip().startswith("denied (")
    url = "https://www.ams.org/journals/mcom/1970-24-110/S0025-5718-1970-0277473-2/S0025-5718-1970-0277473-2.pdf"
    assert cli.main(["--conf", EXAMPLE, "sources", "classify", url]) == 0                    # the matching entry
    assert capsys.readouterr().out.strip() == "publisher-open (www.ams.org/journals/)"
    assert cli.main(["--conf", EXAMPLE, "sources", "classify", "https://www.ams.org/books/gsm/052"]) == 0
    assert capsys.readouterr().out.strip() == "unknown"


@pytest.fixture
def fake_net(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    transport = FakeTransport().add("https://export.arxiv.org/api/query?id_list=2101.00001&max_results=1",
                                    arxiv_feed([{"id": "2101.00001v2", "title": "An example paper", "authors": ["A. Author"]}]))
    transport.add("https://arxiv.org/pdf/2101.00001v2", make_pdf(["Theorem 2.3 " * 40]))
    monkeypatch.setattr(fetcher, "UrllibTransport", lambda **kw: transport)
    return transport


def test_sources_fetch_records_the_entry(tmp_path, capsys, fake_net):
    manifest = tmp_path / "packages" / "Foo" / "sources.toml"
    args = ["--conf", EXAMPLE, "sources", "fetch", "arXiv:2101.00001", "--manifest", str(manifest), "--id", "Aut21",
            "--locator-scheme", "theorem", "--locator", "Theorem 2.3", "--cited-for", "1.2", "--cited-for", "Layer 2"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "added Aut21" in out and "text: " in out
    data = tomllib.loads(manifest.read_text())
    [entry] = data["source"]
    assert data["roadmap"] == "Foo" and entry["id"] == "Aut21" and entry["cited_for"] == ["1.2", "Layer 2"]
    assert entry["fetched_from"] == "https://arxiv.org/pdf/2101.00001v2" and entry["locator"] == "Theorem 2.3"
    assert cli.main(["--conf", EXAMPLE, "sources", "check", str(manifest)]) == 0
    capsys.readouterr()
    assert cli.main(["--conf", EXAMPLE, "sources", "cache", str(manifest)]) == 0
    assert capsys.readouterr().out.startswith("Aut21\t") and len(fake_net.requests) == 2      # cached: no new request


def test_a_reviewers_fetch_adds_but_never_rewrites(tmp_path, capsys, fake_net):
    manifest = tmp_path / "packages" / "Foo" / "sources.toml"
    data = sources.new_manifest("Foo")
    data["source"] = [{"id": "Aut21", "authors": "A. Author (as the author wrote it)", "title": "An example paper",
                       "year": 2021, "arxiv": "2101.00001v2", "locator_scheme": "theorem", "locator": "Theorem 2.3",
                       "status": "uncertified", "licence_class": "none", "cited_for": ["1.2"]}]
    sources.save_manifest(manifest, data)
    before = tomllib.loads(manifest.read_text())
    args = ["--conf", EXAMPLE, "sources", "fetch", "arXiv:2101.00001", "--manifest", str(manifest), "--add-only",
            "--locator", "Theorem 9.9", "--cited-for", "Layer 2"]
    assert cli.main(args) == 0
    assert "updated Aut21" in capsys.readouterr().out
    after = tomllib.loads(manifest.read_text())
    [entry] = after["source"]
    assert entry == {**before["source"][0], "cited_for": ["1.2", "Layer 2"]}
    assert sources.altered_entries(before, after) == []


def test_sources_fetch_failure_writes_the_request(tmp_path, capsys, fake_net):
    request = tmp_path / "scratch" / "needs-references.toml"
    args = ["--conf", EXAMPLE, "sources", "fetch", "R. Kettle, \"Theorem A und Theorem B\", 1967", "--authors", "R. Kettle",
            "--missing-out", str(request), "--cited-for", "Layer 2", "--json"]
    assert cli.main(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed" and result["entry"]["id"] == "Ket67"
    [missing] = tomllib.loads(request.read_text())["missing"]
    assert missing["id"] == "Ket67" and missing["needed_for"] == "Layer 2" and missing["tried"]


def test_sources_fetch_paywalled_doi_is_uncertified(tmp_path, capsys, fake_net):
    doi = "10.1016/j.example.2020.1"
    fake_net.add(f"https://api.crossref.org/works/{doi}", crossref(doi, "A paywalled theorem", [("C.", "Writer")], 2020))
    manifest = tmp_path / "Foo" / "sources.toml"
    assert cli.main(["--conf", EXAMPLE, "sources", "fetch", doi, "--manifest", str(manifest), "--locator", "Thm 4"]) == 0
    [entry] = tomllib.loads(manifest.read_text())["source"]
    assert entry["status"] == "uncertified" and entry["doi"] == doi and entry["locator"] == "Thm 4" and entry["id"] == "Wri20"


def test_sources_ocr_without_the_extra(tmp_path, capsys):
    if sources.ocr_status() == "":
        pytest.skip("OCR is installed here")
    (tmp_path / "scan.pdf").write_bytes(make_pdf([""]))
    assert cli.main(["--conf", EXAMPLE, "sources", "ocr", str(tmp_path / "scan.pdf")]) == 2
    assert "OCR is not installed" in capsys.readouterr().err


def test_depcheck_movable_checks_a_listed_roadmap_as_moved_after_its_last_supplier(worktree, tmp_path, capsys):
    """The agents' check (`--movable`) agrees with the worker, which moves the roadmap when it submits."""
    upward = tmp_path / "README.md"
    upward.write_text("*Needs:* Toy §1.\n", encoding="utf-8")
    order = str(tmp_path / "ORDER.txt")
    args = ["--conf", EXAMPLE, "gate", "--depcheck", str(upward), "--name", "Alpha", "--order", order, "--movable"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("moved: Alpha moved in the merge order from #1 to #2, right after its last supplier Toy; "
                          "checked as moved (the worker moves it there when it submits)\n") and "UPWARD" not in out
    # A real cycle: the UPWARD line stands, with why it cannot move.
    lines, problems = cli.depcheck_lines("Alpha", "*Needs:* Toy §1.\n", {"Toy": "*Needs:* Alpha §2.\n"},
                                         ["Alpha", "Toy"], set(), movable=True)
    assert lines[0].startswith("not movable: Alpha consumes Toy (#2) and is consumed by Toy (#2)") and "cycle" in lines[0]
    assert "UPWARD: Alpha (#1) -> Toy (#2) at README lines [1]" in problems
    # A consumer listed before the supplier rides along instead of blocking the move.
    lines, problems = cli.depcheck_lines("Alpha", "*Needs:* Toy §1.\n", {"Beta": "*Needs:* Alpha §2.\n", "Toy": "x\n"},
                                         ["Alpha", "Beta", "Toy"], set(), movable=True)
    assert lines[0].startswith("moved: Toy moved in the merge order up before Alpha (supplier of it listed after it); "
                               "checked as moved") and problems == []
