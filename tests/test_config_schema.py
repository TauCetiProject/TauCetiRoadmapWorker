"""A worker refuses a configuration whose `schema_version` it does not read, before it reads anything
else (design review R10, 2026-10-24: `schema_version = 999` parsed); unknown keys stay warnings, so an
older worker keeps running against a newer file of the same version."""

import io
import re

import pytest

from tauceti_roadmap_worker import cli, config
from tauceti_roadmap_worker.paths import HERE

TEXT = (HERE / "tauceti-roadmap.toml.example").read_text()
URL = "https://raw.githubusercontent.com/TauCetiProject/TauCetiRoadmapJobs/main/tauceti-roadmap.toml"


def versioned(value: str) -> str:
    return re.sub(r"(?m)^schema_version = 1$", f"schema_version = {value}", TEXT, count=1)


@pytest.mark.parametrize("value", ["999", "2", "0", '"1"', "true"])
def test_an_unsupported_schema_version_is_refused(value):
    with pytest.raises(config.ConfigError, match=r"conf\.toml: schema_version .* is not supported by this worker "
                                                 r"\(it reads 1\): upgrade tauceti-roadmap-worker"):
        config.parse(versioned(value), "conf.toml")


def test_the_version_is_read_before_the_tables():
    with pytest.raises(config.ConfigError, match=r"schema_version 2 is not supported"):
        config.parse("schema_version = 2\n[project]\nname = 3\n", "conf.toml")    # not `project.name: expected str`


def test_a_supported_or_absent_version_parses_and_unknown_keys_stay_warnings():
    assert config.parse(TEXT, "conf.toml").schema_version == 1
    bare = config.parse('[project]\nname = "P"\n\n[repositories]\ncontent = "o/c"\nnewer = 1\n', "bare.toml")
    assert bare.schema_version == 1 and bare.warnings == ["unknown key repositories.newer"]


def test_every_command_stops_on_it_and_a_fetched_copy_never_replaces_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    conf = tmp_path / "tauceti-roadmap.toml"
    conf.write_text(versioned("999"))
    with pytest.raises(SystemExit, match="configuration: .*schema_version 999 is not supported"):
        cli._load(type("Args", (), {"conf": str(conf)})())
    config.load(URL, opener=lambda url: io.BytesIO(TEXT.encode()), sleep=lambda s: None)
    with pytest.raises(config.ConfigError, match="schema_version 999"):
        config.load(URL, opener=lambda url: io.BytesIO(versioned("999").encode()), sleep=lambda s: None)
    assert config.url_cache(URL).read_text() == TEXT
