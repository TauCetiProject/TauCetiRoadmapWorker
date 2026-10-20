"""Only members of the humans team file targets (the user's rule): board sync, and the CLI."""

import base64
import json

import pytest

from tauceti_roadmap_worker import cli, filers, intake
from tauceti_roadmap_worker.gh import GHError

from .fakes import FakeBoard, bot_context, example_config

TEAM = "team:TauCetiProject/humans"
FORM = "### Statement or topic\n\nA statement.\n\n### Why it matters for the library\n\nBecause.\n\n### Suggested name\n\n{}\n"


class GH:
    """The board's issues, the team's members (or a token that may not read them), the contents API."""

    def __init__(self, members=None, issues=(), files=None):
        self.members, self.issues, self.files, self.writes = members, list(issues), dict(files or {}), []

    def api(self, path, paginate=False):
        if path.startswith("orgs/TauCetiProject/teams/humans/members"):
            if self.members is None:
                raise GHError("GET orgs/...: 403 Resource not accessible by integration")
            return [{"login": m} for m in self.members]
        if "/contents/board/filers.json" in path:
            if "board/filers.json" not in self.files:
                raise GHError("404")
            return {"content": base64.b64encode(self.files["board/filers.json"].encode()).decode(), "sha": "s1"}
        if "kind:target" in path:
            return self.issues
        raise AssertionError(path)

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        return {"number": 1}


def target_issue(number, author, name):
    return {"number": number, "title": f"[Target] {name}", "body": FORM.format(name), "user": {"login": author},
            "labels": [{"name": "roadmap-job"}, {"name": "kind:target"}]}


@pytest.fixture
def cfg():
    c = example_config()
    c.board.target_filers = [TEAM]
    return c


def ctx(cfg, gh, tmp_path, board=None):
    out = bot_context(cfg=cfg, gh=gh, board=board or FakeBoard())
    out.state = tmp_path / "state"
    return out


def test_a_member_files_and_a_non_member_is_closed_once(cfg, tmp_path):
    gh = GH(members=["Chris"], issues=[target_issue(2, "chris", "Alpha"), target_issue(3, "mallory", "Beta")])
    board = FakeBoard()
    out = intake.sync_targets(ctx(cfg, gh, tmp_path, board))
    assert out == ["#2: TARGET-Alpha available", "#3: closed, @mallory may not file targets"]
    assert board.states == [("TARGET-Alpha", "available")]
    closing = [(m, p.rsplit("/", 1)[-1], body) for m, p, body in gh.writes if "/issues/3" in p]
    assert closing == [("POST", "comments", {"body": "Targets are filed by members of the TauCetiProject humans team "
                                                       "for now; ask a maintainer."}),
                       ("POST", "labels", {"labels": ["not-a-filer"]}), ("PATCH", "3", {"state": "closed"})]
    published = [p for m, path, p in gh.writes if path.endswith("contents/board/filers.json")]
    assert json.loads(base64.b64decode(published[0]["content"])) == {"TauCetiProject/humans": ["Chris"]}


def test_a_token_that_cannot_read_the_team_uses_the_cached_or_published_list(cfg, tmp_path):
    member = GH(members=["chris"])
    assert filers.allowed(ctx(cfg, member, tmp_path), "chris") == (True, "member of TauCetiProject/humans (api)")
    denied = GH(members=None)
    assert filers.allowed(ctx(cfg, denied, tmp_path), "chris")[0] is True       # the hour-long cache
    cache = filers.cache_path(tmp_path / "state")
    cache.write_text(json.dumps({"TauCetiProject/humans": {"at": 0, "members": ["chris"]}}))
    ok, note = filers.allowed(ctx(cfg, denied, tmp_path), "chris")
    assert ok is True and "cache from before" in note
    cache.unlink()
    published = GH(members=None, files={"board/filers.json": json.dumps({"TauCetiProject/humans": ["dana"]})})
    ok, note = filers.allowed(ctx(cfg, published, tmp_path), "dana")
    assert ok is True and "board/filers.json" in note
    assert filers.allowed(ctx(cfg, published, tmp_path), "mallory")[0] is False


def test_without_any_list_nothing_is_closed(cfg, tmp_path):
    gh = GH(members=None, issues=[target_issue(3, "mallory", "Beta")])
    board = FakeBoard()
    out = intake.sync_targets(ctx(cfg, gh, tmp_path, board))
    assert len(out) == 1 and out[0].startswith("#3: may @mallory file targets? Unknown (team TauCetiProject/humans unreadable")
    assert gh.writes == [] and board.states == []


def test_anyone_files_when_no_filers_are_configured(tmp_path):
    cfg = example_config()
    assert cfg.board.target_filers == [TEAM, "roed-math"]          # the project's setting
    assert filers.allowed(ctx(cfg, GH(), tmp_path), "roed-math") == (True, "listed as roed-math")
    cfg.board.target_filers = []
    assert filers.allowed(ctx(cfg, GH(), tmp_path), "anyone") == (True, "anyone may file targets")


def test_the_cli_refuses_a_login_that_may_not_file(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda args: cfg)
    monkeypatch.setattr(cli, "_bot_context", lambda args, c, dry: ctx(c, GH(members=["chris"]), tmp_path))
    with pytest.raises(SystemExit, match="Targets are filed by members of the TauCetiProject humans team"):
        cli.main(["targets", "file", "--statement", "s", "--why", "w", "--name", "Alpha"])
