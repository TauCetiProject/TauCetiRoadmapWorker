"""The board's second identity (`[worker] board_token_command`): its own bookkeeping on job issues is
written under it, everything else and every fallback under the operator's own login."""
import sys
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import board, cli, throttle
from tauceti_roadmap_worker import gh as ghmod
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.gh import GH, GHError, GHHalt, GHRateLimited

from .fakes import example_config


class Rec:
    """A GH that records its writes; `fail` is raised by every write."""

    def __init__(self, fail=None, dry_run=False):
        self.writes, self.reads, self.fail, self.dry_run, self.fence = [], [], fail, dry_run, None

    def api(self, path, paginate=False):
        self.reads.append(path)
        return []

    def write(self, method, path, payload=None):
        if self.fail is not None:
            raise self.fail
        self.writes.append((method, path, payload))
        if method == "POST" and path.endswith("/labels"):
            return [{"name": "roadmap-job"}, {"name": "state:claimed"}, {"name": payload["labels"][0]}]
        if method == "POST" and path.endswith("/issues"):
            return {"number": 41, "updated_at": "2026-11-15T17:00:00Z"}
        return {}


def job(number=7):
    j = new_job("REV-PKG-Widgets~2")
    j.number, j.labels = number, ["roadmap-job", "state:claimed"]
    return j


def test_the_boards_bookkeeping_goes_out_under_the_second_identity():
    own, second = Rec(), Rec()
    b = board.Board(example_config(), own, second)
    j = job()
    assert b.set_state(j, "done", comment="Intake: merged #9 from `alice` (claude, fable).")
    b.add_label(j, "superseded")
    b.close(j, "Superseded: a later round took its place", "superseded")
    made = b.create(new_job("FIX-PKG-Widgets~3"))
    assert made == 41 and own.writes == []
    repo = b.repo
    assert [(m, p) for m, p, _ in second.writes] == [
        ("POST", f"repos/{repo}/issues/7/labels"),
        ("DELETE", f"repos/{repo}/issues/7/labels/state:claimed"),
        ("POST", f"repos/{repo}/issues/7/comments"),
        ("POST", f"repos/{repo}/issues/7/labels"),
        ("POST", f"repos/{repo}/issues/7/comments"),
        ("POST", f"repos/{repo}/issues/7/labels"),
        ("PATCH", f"repos/{repo}/issues/7"),
        ("POST", f"repos/{repo}/issues"),
    ]
    assert second.reads == [] and own.reads          # the board still reads as the operator does
    assert b.refused == 0


def test_without_a_second_identity_the_operator_writes_as_before():
    own = Rec()
    b = board.Board(example_config(), own)
    assert b.writer is own
    b.comment(7, "a note")
    assert own.writes == [("POST", f"repos/{b.repo}/issues/7/comments", {"body": "a note"})]


@pytest.mark.parametrize("failure", [
    GHError("gh api -X POST: HTTP 403: Resource not accessible by integration"),
    GHError("token command: exit 1, no token"),
    GHHalt("gh api -X POST: HTTP 401: Bad credentials"),
    GHRateLimited("secondary rate limit", 0.0),
])
def test_a_refused_second_identity_falls_back_to_the_operator(failure):
    own, second = Rec(), Rec(fail=failure)
    b = board.Board(example_config(), own, second)
    b.comment(7, "a note")
    assert own.writes == [("POST", f"repos/{b.repo}/issues/7/comments", {"body": "a note"})]
    assert b.refused == 1


def test_a_failure_of_the_request_itself_is_not_retried_as_the_operator():
    own, second = Rec(), Rec(fail=GHError("gh api -X POST: HTTP 422: Validation Failed"))
    b = board.Board(example_config(), own, second)
    with pytest.raises(GHError, match="422"):
        b.comment(7, "a note")
    assert own.writes == [] and b.refused == 0


def test_a_dry_run_and_a_lost_lease_stop_the_second_identity_too():
    own, second = Rec(dry_run=True), Rec()
    b = board.Board(example_config(), own, second)
    b.comment(7, "a note")
    assert second.writes == [] and len(own.writes) == 1          # the operator's GH prints a dry run's writes

    own, second = Rec(), Rec()

    def lost():
        raise GHError("the lease is lost: nothing more is written")
    own.fence = lost
    b = board.Board(example_config(), own, second)
    with pytest.raises(GHError, match="lease is lost"):
        b.comment(7, "a note")
    assert second.writes == [] and own.writes == []


def test_a_token_command_gives_gh_its_token_and_is_asked_again_only_when_it_is_old(tmp_path, monkeypatch):
    count = tmp_path / "count"
    count.write_text("0")
    script = tmp_path / "token.py"
    script.write_text(
        "import pathlib\n"
        f"p = pathlib.Path({str(count)!r})\n"
        "n = int(p.read_text()) + 1\n"
        "p.write_text(str(n))\n"
        "print(f'tok-{n}')\n")
    now = [1000.0]
    g = GH(token_command=f"{sys.executable} {script}")
    g.clock = lambda: now[0]
    assert g._env()["GH_TOKEN"] == "tok-1" and g._env()["GH_TOKEN"] == "tok-1"
    now[0] += ghmod.TOKEN_SECONDS + 1
    assert g._env()["GH_TOKEN"] == "tok-2"
    assert GH()._env() is None                                    # the caller's own identity: its own environment

    seen = {}

    def run(args, **kw):
        seen["env"] = kw.get("env")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(ghmod.subprocess, "run", run)
    g._token, g._token_at = "tok-9", now[0]
    g.write("POST", "repos/o/r/issues/7/comments", {"body": "x"})
    assert seen["env"]["GH_TOKEN"] == "tok-9"
    GH().write("POST", "repos/o/r/issues/7/comments", {"body": "x"})
    assert seen["env"] is None


def test_a_token_command_that_fails_says_so_without_its_output(tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("import sys\nprint('secret-on-stdout')\nsys.stderr.write('secret-on-stderr')\nsys.exit(3)\n")
    g = GH(token_command=f"{sys.executable} {script}")
    with pytest.raises(GHError) as caught:
        g._env()
    assert "token command" in str(caught.value) and "secret" not in str(caught.value)
    with pytest.raises(GHError, match="token command"):
        GH(token_command=str(tmp_path / "no-such-command"))._env()


def test_the_cli_builds_the_board_with_its_writer_only_when_the_host_names_one(tmp_path, monkeypatch):
    cfg = example_config()
    monkeypatch.setattr(type(cfg), "state_dir", lambda self: tmp_path)
    own = GH()
    assert cli._board(cfg, own).writer is own
    cfg.worker.board_token_command = "print-a-token"
    b = cli._board(cfg, own)
    assert b.gh is own and b.writer is not own and b.writer.token_command == "print-a-token"
    assert b.writer.throttle.path == tmp_path / "gh-writes-board.json"
    assert throttle.for_config(cfg).path == tmp_path / "gh-writes.json"
    assert cli._board(cfg, GH(dry_run=True)).writer.token_command == ""          # a dry run writes nothing
    cfg.worker.writes_per_minute = cfg.worker.writes_per_hour = 0
    assert cli._board(cfg, own).writer.throttle is None
