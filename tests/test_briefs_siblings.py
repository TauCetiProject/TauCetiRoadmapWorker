"""The brief's list of open roadmap pull requests: written out when short, a scratch file when long."""
from pathlib import Path

from tauceti_roadmap_worker import config
from tauceti_roadmap_worker.lanes import base


class WS:
    def __init__(self, scratch: Path):
        self.scratch = scratch


def test_a_handful_of_open_pull_requests_is_written_into_the_brief(tmp_path):
    sibs = [(n, f"R{n}", f"/s/R{n}") for n in range(3)]
    assert base.siblings_text(WS(tmp_path / "scratch"), sibs, []) == "R0 at `/s/R0`; R1 at `/s/R1`; R2 at `/s/R2`"
    assert base.siblings_text(WS(tmp_path / "scratch"), [], [{"name": "A"}, {"name": "B"}]) == "A; B"
    assert base.siblings_text(WS(tmp_path / "scratch"), [], []) == "none"
    assert not (tmp_path / "scratch").exists()


def test_a_long_list_of_open_pull_requests_goes_to_a_scratch_file(tmp_path):
    sibs = [(n, f"R{n:03}", f"/s/R{n:03}") for n in range(400)]
    text = base.siblings_text(WS(tmp_path / "scratch"), sibs, [])
    listing = tmp_path / "scratch" / base.SIBLINGS_FILE
    assert str(listing) in text and "the 400 listed" in text and len(text) < 400
    lines = listing.read_text().splitlines()
    assert "- R007 at `/s/R007`" in lines and sum(line.startswith("- ") for line in lines) == 400
    names = base.siblings_text(WS(tmp_path / "scratch"), [], [{"name": f"N{n}"} for n in range(50)])
    assert "the 50 listed" in names and "- N49" in listing.read_text().splitlines()


def test_repair_models_are_read_and_checked():
    from tauceti_roadmap_worker.paths import HERE
    text = (HERE / "tauceti-roadmap.toml.example").read_text()
    assert config.parse(text, "example").prreview.repair_models == {}
    cfg = config.parse(text.replace("repair_models = {}", 'repair_models = { fable = "claude-opus-5-5" }'), "example")
    assert cfg.prreview.repair_models == {"fable": "claude-opus-5-5"}
    try:
        config.parse(text.replace("repair_models = {}", "repair_models = { fable = 3 }"), "example")
    except config.ConfigError as exc:
        assert "repair_models" in str(exc)
    else:
        raise AssertionError("a non-string repair model was accepted")
