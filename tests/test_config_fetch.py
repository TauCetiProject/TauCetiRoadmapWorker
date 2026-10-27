"""A configuration URL read at startup survives a slow network: retries with backoff, then the
last good copy (the pilot's chris-2 crashed on `TimeoutError: The read operation timed out`)."""

import io

import pytest

from tauceti_roadmap_worker import config
from tauceti_roadmap_worker.paths import HERE

URL = "https://raw.githubusercontent.com/TauCetiProject/TauCetiRoadmapJobs/main/tauceti-roadmap.toml"
TEXT = (HERE / "tauceti-roadmap.toml.example").read_text()


class Opener:
    def __init__(self, failures: int):
        self.failures, self.calls = failures, 0

    def __call__(self, url):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("The read operation timed out")
        return io.BytesIO(TEXT.encode())


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))


def test_retries_with_backoff_then_caches():
    sleeps, opener = [], Opener(2)
    cfg = config.load(URL, opener=opener, sleep=sleeps.append)
    assert cfg.project.name == "TauCetiRoadmapWorker" and sleeps == [5, 20] and opener.calls == 3
    assert config.url_cache(URL).read_text() == TEXT and config.url_cache(URL).parent.name == "config"


def test_a_cached_copy_stands_in_when_every_try_fails():
    config.load(URL, opener=Opener(0), sleep=lambda s: None)
    sleeps = []
    cfg = config.load(URL, opener=Opener(99), sleep=sleeps.append)
    assert sleeps == [5, 20, 60] and cfg.project.name == "TauCetiRoadmapWorker"
    assert any("could not be read" in w and "using the copy cached 0.0 h ago" in w for w in cfg.warnings)


def test_no_cache_and_no_network_fails():
    with pytest.raises(config.ConfigError, match="after 4 tries, and no cached copy"):
        config.load(URL, opener=Opener(99), sleep=lambda s: None)


def test_an_overlay_sets_host_and_operator_settings_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    conf = tmp_path / "tauceti-roadmap.toml"
    conf.write_text(TEXT)
    overlay = tmp_path / "overlay.toml"
    overlay.write_text('[sandbox]\nprofile = "netjail"\nnetjail = "/opt/launcher"\n'
                       '[board]\ndispatch = false\n[sources]\nlocal_dirs = ["~/texts"]\n')
    cfg = config.load(str(conf), overlay=str(overlay))
    assert (cfg.sandbox.profile, cfg.sandbox.netjail, cfg.board.dispatch) == ("netjail", "/opt/launcher", False)
    assert cfg.sources.local_dirs == ["~/texts"]
    assert cfg.board.target_filers == config.parse(TEXT).board.target_filers    # the rest of the table stays
    assert cfg.overlaid == ["board.dispatch", "sandbox.netjail", "sandbox.profile", "sources.local_dirs"]
    monkeypatch.setenv("TAUCETI_ROADMAP_OVERLAY", str(overlay))
    assert config.load(str(conf)).sandbox.profile == "netjail" and config.load(str(conf)).overlay_path


def test_an_overlay_never_changes_the_boards_rules(tmp_path):
    conf = tmp_path / "tauceti-roadmap.toml"
    conf.write_text(TEXT)
    for text, refused in (('[review]\nseparation = "agent"\n', "review"),
                          ('[board]\ndispatch = false\ntarget_filers = ["me"]\n', "board.target_filers"),
                          ('sandbox = "off"\n', "sandbox")):
        overlay = tmp_path / "overlay.toml"
        overlay.write_text(text)
        with pytest.raises(config.ConfigError, match=f"refused {refused}"):
            config.load(str(conf), overlay=str(overlay))
    bad = tmp_path / "bad.toml"
    bad.write_text('[sandbox]\nprofile = "jail"\n')
    with pytest.raises(config.ConfigError, match="sandbox.profile"):
        config.load(str(conf), overlay=str(bad))


def test_agents_commands_carry_the_overlay():
    from types import SimpleNamespace

    from tauceti_roadmap_worker.lanes import Context
    ctx = Context(cfg=SimpleNamespace(overlay_path="/home/a/overlay.toml"), gh=None, board=None, claims=None,
                  worker_id="w1", operator="alice", agent="claude", model="m", conf="/home/a/conf.toml")
    assert ctx.cli("gate", "x") == "tauceti-roadmap --conf /home/a/conf.toml --overlay /home/a/overlay.toml gate x"
