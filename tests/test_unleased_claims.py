"""A claim nobody holds is released (regression: after the full-disk crash PORT-ColemanIntegrationPartII
(#525) stayed `state:claimed` for 45 minutes with no lease ref: the restarted chris-2 released only
jobs it had leases for, and `board sync` left claims without a lease to `board stale`)."""

import datetime as dt
from types import SimpleNamespace

from tauceti_roadmap_worker import claims, intake, worker
from tauceti_roadmap_worker.board import last_claim, new_job

from .fakes import FakeBoard, FakeClaims, example_config

NOW = dt.datetime.now(dt.UTC)


def at(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def claimed(number, job_id, body, minutes_ago):
    job = new_job(job_id)
    job.number, job.state = number, "claimed"
    return job, [{"body": body, "created_at": at(minutes_ago), "user": {"login": "CBirkbeck"}}]


class Board(FakeBoard):
    def set_state(self, job, state, comment="", expect=""):
        written = super().set_state(job, state, comment, expect)
        self.notes = [*getattr(self, "notes", []), (job.number, comment)]
        return written


def board_with(*rows):
    return Board(comments={j.number: c for j, c in rows}, jobs=[j for j, _c in rows])


def test_the_claim_comment_is_read():
    body = "Claimed for `PORT-Coleman` by worker `chris-2` (@CBirkbeck, codex)."
    found = last_claim([{"body": "unrelated"}, {"body": body, "created_at": "2026-10-21T09:00:00Z"}], "PORT-Coleman")
    assert (found["worker"], found["operator"], found["comment"]) == ("chris-2", "CBirkbeck", False)
    review = "Claimed for `REV-PKG-Foo` by @roed-math (chatgpt), by a comment on #12."
    assert last_claim([{"body": review}], "REV-PKG-Foo")["comment"] is True
    assert last_claim([{"body": body}], "PORT-Other") is None


def test_board_sync_releases_a_claim_with_no_live_lease():
    rows = [claimed(525, "PORT-Coleman", "Claimed for `PORT-Coleman` by worker `chris-2` (@CBirkbeck, codex).", 45),
            claimed(526, "RT-Fresh~1a", "Claimed for `RT-Fresh~1a` by worker `chris-3` (@CBirkbeck, claude).", 10),
            claimed(527, "REV-PKG-Browser", "Claimed for `REV-PKG-Browser` by @roed-math (chatgpt), by a comment on #12.", 90)]
    board = board_with(*rows)
    ctx = SimpleNamespace(cfg=example_config(), board=board)
    out = intake.sync_expired(ctx, FakeClaims())
    assert out == ["PORT-Coleman: released (claimed by worker `chris-2` (@CBirkbeck, codex), no lease)"]
    assert board.states == [("PORT-Coleman", "available")]                      # 10 min: within the lease's 25
    [(number, note)] = board.notes
    assert number == 525 and note.startswith("Released: claimed by worker `chris-2` (@CBirkbeck, codex) at ")
    assert note.endswith("with no live lease.")
    # A namespace that cannot be read now never looks like one without leases.
    board = board_with(*rows)
    unreadable = FakeClaims()
    unreadable.readable = False
    assert intake.sync_expired(SimpleNamespace(cfg=example_config(), board=board), unreadable) == []


def test_a_claim_submitted_since_the_listing_is_not_released():
    """Board 2026-10-21: PKG-SelmerIwasawaCohomologyPartII (#582) submitted #589 at 14:44:51Z (its
    28-minute lane outlived the 25-minute lease, released at its end); the sync's listing still
    read `claimed`, it released the job at 14:45:00Z, and a worker rebuilt the package."""
    rows = [claimed(582, "PKG-Selmer", "Claimed for `PKG-Selmer` by worker `chris-1` (@CBirkbeck, codex).", 28)]
    board = board_with(*rows)
    board.current_state = lambda job: "submitted"          # what the issue carries now
    out = intake.sync_expired(SimpleNamespace(cfg=example_config(), board=board), FakeClaims())
    assert out == ["PKG-Selmer: submitted now; left as it is"] and board.states == []


def test_a_restarted_worker_releases_what_its_previous_process_claimed():
    rows = [claimed(525, "PORT-Coleman", "Claimed for `PORT-Coleman` by worker `chris-2` (@CBirkbeck, codex).", 3),
            claimed(526, "RT-Other~1a", "Claimed for `RT-Other~1a` by worker `chris-3` (@CBirkbeck, claude).", 3),
            claimed(527, "RT-Taken~1a", "Claimed for `RT-Taken~1a` by worker `chris-2` (@CBirkbeck, codex).", 3)]
    board = board_with(*rows)
    held = FakeClaims(owner="CBirkbeck/chris-2")
    cfg = example_config()
    held.leases = {claims.job_key(cfg.project.name, "RT-Taken~1a"): {"owner": "CBirkbeck/chris-9",
                                                                      "expires_at": 4_102_444_800}}
    ctx = SimpleNamespace(cfg=cfg, board=board, claims=held, worker_id="chris-2", operator="CBirkbeck", dry_run=False)
    lines = []
    assert worker.release_leftovers(ctx, lines.append) == ["PORT-Coleman"]
    assert board.states == [("PORT-Coleman", "available")]
    assert lines == ["released PORT-Coleman: claimed by this worker's previous process, with no lease"]
