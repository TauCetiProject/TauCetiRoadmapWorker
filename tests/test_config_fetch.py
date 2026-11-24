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


def test_the_overlay_carries_the_order_and_models_of_the_hosts_own_workers(tmp_path, monkeypatch):
    """`[worker] late_authors`, `late_except` and `author_models` are an operator's own: the overlay sets
    them, and the file at `DEFAULT_OVERLAY` is read when no overlay is named."""
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    conf = tmp_path / "tauceti-roadmap.toml"
    conf.write_text(TEXT)
    overlay = tmp_path / "roadmap-overlay.toml"
    overlay.write_text('[worker]\nlate_authors = ["dave"]\nlate_except = [7]\n'
                       '[worker.author_models]\ndave = "claude-opus-5-5"\n')
    cfg = config.load(str(conf), overlay=str(overlay))
    assert (cfg.worker.late_authors, cfg.worker.late_except, cfg.worker.author_models) == (
        ["dave"], [7], {"dave": "claude-opus-5-5"})
    assert cfg.worker.idle_seconds == config.parse(TEXT).worker.idle_seconds          # the rest of the table stays
    assert config.load(str(conf)).worker.late_authors == []                           # (the suite names no overlay)
    monkeypatch.delenv("TAUCETI_ROADMAP_OVERLAY")
    monkeypatch.setattr(config, "DEFAULT_OVERLAY", str(overlay))
    assert config.load(str(conf)).worker.late_authors == ["dave"]                     # the host's default file
    monkeypatch.setenv("TAUCETI_ROADMAP_OVERLAY", "")
    assert config.load(str(conf)).worker.late_authors == []                           # set and empty: none
    held = tmp_path / "held.toml"
    held.write_text('[worker]\nheld_logins = ["Erin"]\n')
    cfg = config.load(str(conf), overlay=str(held))
    assert cfg.worker.held_logins == ["Erin"] and config.ignored_logins(cfg) == {"github-actions[bot]", "Erin"}
    assert config.ignored_logins(config.load(str(conf))) == {"github-actions[bot]"}
    left = tmp_path / "left.toml"
    left.write_text('[worker]\nno_review_authors = ["dave"]\nno_review_except = [7]\n')
    cfg = config.load(str(conf), overlay=str(left))
    assert (cfg.worker.no_review_authors, cfg.worker.no_review_except) == (["dave"], [7])
    assert config.load(str(conf)).worker.no_review_authors == []
    for bad, why in (('late_authors = ["not a login"]', "worker.late_authors"), ("late_except = [0]", "worker.late_except"),
                     ('author_models = { dave = "" }', "worker.author_models"),
                     ('held_logins = ["not a login"]', "worker.held_logins"),
                     ('no_review_authors = ["not a login"]', "worker.no_review_authors"),
                     ("no_review_except = [0]", "worker.no_review_except")):
        with pytest.raises(config.ConfigError, match=why):
            config.parse(f'[project]\nname = "P"\n[worker]\n{bad}\n', "x.toml")


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


class Serving:
    """An opener that serves the text it is given."""

    def __init__(self, text: str):
        self.text, self.calls = text, 0

    def __call__(self, url):
        self.calls += 1
        return io.BytesIO(self.text.encode())


def test_the_fingerprint_follows_the_text_and_the_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_ROADMAP_OVERLAY", "")           # no overlay, not even the host's default
    first = config.fingerprint(URL, opener=Serving(TEXT))
    assert first and len(first) == 16 and config.fingerprint(URL, opener=Serving(TEXT)) == first
    changed = TEXT.replace("# review_changes_of_merged = true", "review_changes_of_merged = true", 1)
    assert changed != TEXT and config.fingerprint(URL, opener=Serving(changed)) not in ("", first)
    assert not config.url_cache(URL).exists()                    # nothing is cached or written
    path = tmp_path / "conf.toml"
    path.write_text(TEXT)
    assert config.fingerprint(str(path)) == first                # a file and a URL with one text agree
    overlay = tmp_path / "overlay.toml"
    overlay.write_text('[worker]\nkeep_job_days = 3\n')
    assert config.fingerprint(str(path), overlay=str(overlay)) not in ("", first)


def test_a_configuration_that_cannot_be_read_or_does_not_validate_has_no_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_ROADMAP_OVERLAY", "")
    failing = Opener(99)
    assert config.fingerprint(URL, opener=failing, sleep=lambda s: 1 / 0) == "" and failing.calls == 1   # one try, no backoff
    assert config.fingerprint(URL, opener=Serving("this is not = toml [")) == ""
    assert config.fingerprint(URL, opener=Serving(TEXT.replace("schema_version = 1", "schema_version = 99"))) == "" \
        or "schema_version" not in TEXT
    assert config.fingerprint(str(tmp_path / "missing.toml")) == ""
    broken = tmp_path / "overlay.toml"
    broken.write_text("[worker\n")
    path = tmp_path / "conf.toml"
    path.write_text(TEXT)
    assert config.fingerprint(str(path), overlay=str(broken)) == ""
