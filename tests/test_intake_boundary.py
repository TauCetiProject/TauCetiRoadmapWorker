"""What the intake takes in is the change it checked: every route (a submission, a board pull request
about to merge, an explorer import) refuses a rename (its source is deleted) and a path that is not a
regular file at the head, reads the file listing of one head only, and merges that head and no
other. Markers read from pull request and issue bodies are parsed totally: a malformed one is not a
marker, and the pull requests after it are still taken in."""

import json

import pytest

from tauceti_roadmap_worker import board, boardpr, explorer, intake
from tauceti_roadmap_worker.board import encode_submission, new_job

from .fakes import bot_context, example_config, head_tree
from .test_boardpr import GH, Board

HEAD = "e" * 40
REPORT = "packages/Foo/reports/REV-PKG-Foo.md"
WORKFLOW = ".github/workflows/board.yml"


class ListingGH(GH):
    """A submission of `REV-PKG-Foo` (a review merged by the intake) whose file listing, head tree and
    re-read head the test sets."""

    def __init__(self, files=None, modes=None, head_after=HEAD, changed_files=None):
        super().__init__(HEAD)
        self.pr["body"] = "Refs #7\n\n" + encode_submission("REV-PKG-Foo", "w2", "bob", "claude", "fable")
        self.pr["user"] = {"login": "bob"}
        self.listing = files or [{"filename": REPORT, "status": "added"}]
        self.modes, self.head_after, self.changed_files = modes or {}, head_after, changed_files

    def api(self, path, paginate=False):
        if "/pulls/12/files" in path:
            return [dict(f) for f in self.listing]
        if path.endswith("/pulls/12"):
            return {**self.pr, "head": {**self.pr["head"], "sha": self.head_after},
                    **({"changed_files": self.changed_files} if self.changed_files is not None else {})}
        if "/git/trees/" in path:
            tree = head_tree(f["filename"] for f in self.listing if f["status"] != "removed")
            for entry in tree["tree"]:
                entry["mode"] = self.modes.get(entry["path"], entry["mode"])
            return tree
        return super().api(path, paginate)


def review_context(gh):
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    rev = new_job("REV-PKG-Foo")
    rev.number, rev.state = 7, "submitted"
    return bot_context(cfg=cfg, gh=gh, board=Board([rev]), operator="board-bot"), rev


RENAME = [{"filename": REPORT, "previous_filename": WORKFLOW, "status": "renamed"}]


def test_a_rename_into_a_deliverable_is_refused_because_it_deletes_its_source():
    gh = ListingGH(RENAME)
    ctx, rev = review_context(gh)
    line = intake.intake(ctx, 12, yes=True)
    assert "not merged" in line and f"{WORKFLOW} is renamed to {REPORT}, which deletes it" in line, line
    assert not gh.merged and rev.state == "submitted"


def test_a_board_pull_request_and_an_import_refuse_the_same_rename():
    gh = ListingGH(RENAME)
    ctx, rev = review_context(gh)
    assert any("which deletes it" in p for p in boardpr.merge_problems(ctx, 12, gh.pr, [rev]))
    gh.pr["author_association"] = "MEMBER"
    found = {"jobs": [new_job("PORT-Foo")], "files": [REPORT]}
    line = explorer.intake_import(ctx, 12, gh.pr, found, yes=False)
    assert "import not merged" in line and "which deletes it" in line, line


@pytest.mark.parametrize("mode,what", [("120000", "a symbolic link"), ("160000", "a submodule"),
                                       ("100755", "an executable file")])
def test_a_deliverable_that_is_not_a_regular_file_is_refused(mode, what):
    gh = ListingGH(modes={REPORT: mode})
    ctx, _rev = review_context(gh)
    line = intake.intake(ctx, 12, yes=True)
    assert f"{REPORT} is {what}, not a regular file" in line and not gh.merged, line


def test_a_listing_read_while_the_head_moved_is_not_used_and_the_next_run_reads_it_again(tmp_path):
    gh = ListingGH(head_after="f" * 40)
    ctx, rev = review_context(gh)
    with pytest.raises(intake.HeadMoved):
        intake.intake(ctx, 12, yes=True, pr=gh.pr)      # as the listing showed it
    assert not gh.merged and rev.state == "submitted" and gh.writes == []
    ctx.state = tmp_path
    gh.pr["labels"] = []
    logged = []
    assert intake.intake_open(ctx, yes=True, only=12, log=logged.append) == [12]
    assert "the head moved" in logged[0] and not gh.merged


def test_an_incomplete_listing_is_refused():
    gh = ListingGH(changed_files=3001)
    ctx, _rev = review_context(gh)
    line = intake.intake(ctx, 12, yes=True)
    assert "the file listing is incomplete (1 of 3001 files)" in line and not gh.merged, line


def test_the_merge_names_the_head_that_was_checked():
    gh = ListingGH()
    ctx, rev = review_context(gh)
    assert intake.intake(ctx, 12, yes=True).startswith("#12: merged REV-PKG-Foo")
    [merge] = [p for _m, path, p in gh.writes if path.endswith("/merge")]
    assert merge == {"merge_method": "squash", "sha": HEAD} and rev.state == "done"


def test_an_import_merges_the_head_that_was_checked():
    gh = ListingGH(files=[{"filename": "packages/Alpha/README.md", "status": "added"}])
    gh.pr["author_association"] = "MEMBER"
    ctx, _rev = review_context(gh)
    found = {"jobs": [new_job("PORT-Alpha")], "files": ["packages/Alpha/README.md"]}
    assert explorer.intake_import(ctx, 12, gh.pr, found, yes=True).startswith("#12: merged the import")
    [merge] = [p for _m, path, p in gh.writes if path.endswith("/merge")]
    assert merge == {"merge_method": "squash", "sha": HEAD}


# --- markers ----------------------------------------------------------------------------------------

def import_marker(data) -> str:
    return f"<!-- {explorer.IMPORT_MARK} {json.dumps(data)} -->"


@pytest.mark.parametrize("data", [1, None, [], {"jobs": 1}, {"jobs": None}, {"jobs": {"id": "PORT-Foo"}},
                                  {"jobs": [1]}, {"jobs": [None]}, {"jobs": [["PORT-Foo"]]},
                                  {"jobs": [{"id": 3}]}, {"jobs": [{"id": "PORT-Foo", "after": 1}]},
                                  {"jobs": [{"id": "PORT-Foo", "after": [1]}]},
                                  {"jobs": [{"id": "PORT-Foo", "inputs": []}]},
                                  {"jobs": [{"id": "PORT-Foo", "requires_operator": ["bob"]}]},
                                  {"jobs": [{"id": "PORT-Foo"}], "files": "packages/Foo/README.md"},
                                  {"jobs": [{"id": "PORT-Foo"}], "files": [1]},
                                  {"jobs": [{"id": "PORT-Foo"}], "files": None}])
def test_a_malformed_import_marker_is_no_marker(data):
    assert explorer.decode_import(import_marker(data)) is None


def test_a_well_formed_import_marker_is_read():
    found = explorer.decode_import(import_marker({"jobs": [{"id": "PORT-Foo", "after": ["REV-PKG-Foo"]}],
                                                  "files": ["packages/Foo/README.md"]}))
    assert [j.id for j in found["jobs"]] == ["PORT-Foo"] and found["jobs"][0].after == ["REV-PKG-Foo"]
    assert found["files"] == ["packages/Foo/README.md"]


@pytest.mark.parametrize("data", [1, None, [], {"job": 1}, {"job": None}, {"job": ["PKG-Foo"]},
                                  {"job": "PKG-Foo", "agent": ["codex"]}, {"job": "PKG-Foo", "model": 5},
                                  {"job": "PKG-Foo", "operator": {"login": "bob"}}])
def test_a_malformed_submission_marker_is_no_marker(data):
    assert board.decode_submission(f"<!-- tauceti-roadmap-submission {json.dumps(data)} -->") is None


def test_a_job_spec_with_a_field_of_the_wrong_type_is_no_job():
    assert board.decode_spec(f"<!-- tauceti-roadmap-job {json.dumps({'id': 'PKG-Foo', 'after': 'DESIGN-Foo'})} -->") is None
    job = board.decode_spec(board.encode_spec(new_job("PKG-Foo", after=["REV-DESIGN-Foo"], inputs={"board_pr": 3})))
    assert job.after == ["REV-DESIGN-Foo"] and job.inputs == {"board_pr": 3}


def test_a_fix_pushers_marker_of_the_wrong_shape_is_ignored():
    fix = new_job("FIX-RT-Foo~1")
    body = f"Fix `{fix.id}` pushed\n\n<!-- tauceti-roadmap-review {{\"agent\": [1], \"model\": \"m\"}} -->"
    assert boardpr.pusher([{"body": body, "user": {"login": "alice"}}], fix) == {"login": "alice", "model": "m"}


class TwoPullsGH(ListingGH):
    """A malformed import marker on #11, listed before the valid submission #12."""

    def api(self, path, paginate=False):
        if "/pulls?state=open" in path:
            bad = {**self.pr, "number": 11, "body": import_marker({"jobs": 1})}
            return [bad, self.pr]
        return super().api(path, paginate)


def test_a_malformed_marker_does_not_stop_the_pull_requests_after_it(tmp_path):
    gh = TwoPullsGH()
    ctx, rev = review_context(gh)
    ctx.state = tmp_path
    logged = []
    assert intake.intake_open(ctx, yes=True, log=logged.append) == []
    assert any(line.startswith("#12: merged REV-PKG-Foo") for line in logged), logged
    assert gh.merged and rev.state == "done"
