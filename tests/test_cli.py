import subprocess

import pytest

from tauceti_roadmap_worker import cli
from tauceti_roadmap_worker.paths import HERE

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
    assert capsys.readouterr().out.strip() == "denied"
    assert cli.main(["--conf", EXAMPLE, "sources", "fetch", "https://arxiv.org/abs/1"]) == 2
