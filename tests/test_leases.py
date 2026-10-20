"""Leases left behind: a worker's previous process killed mid-job (the pilot's chris-1 at 20:53Z),
and leases that expired without a release."""

from tauceti_roadmap_worker import claims, intake, worker
from tauceti_roadmap_worker.board import Board
from tauceti_roadmap_worker.lanes import Context

from .fakes import FakeClaims, FakeIssuesGH, example_config, job_issue

FUTURE, PAST = 4_102_444_800, 1_700_000_000


def ctx_with(gh, shared, cfg=None):
    cfg = cfg or example_config()
    return Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=shared, worker_id="chris-1", operator="CBirkbeck",
                   agent="codex", model="m")


def test_a_worker_releases_what_its_previous_process_left_claimed():
    cfg = example_config()
    key = claims.job_key(cfg.project.name, "TARGET-QSeries")
    other = claims.job_key(cfg.project.name, "TARGET-Other")
    shared = FakeClaims(leases={key: {"owner": "CBirkbeck/chris-1", "expires_at": FUTURE},
                                other: {"owner": "CBirkbeck/chris-2", "expires_at": FUTURE}}, owner="CBirkbeck/chris-1")
    gh = FakeIssuesGH([job_issue(1, "TARGET-QSeries", "claimed"), job_issue(2, "TARGET-Other", "claimed")])
    lines = []
    assert worker.release_leftovers(ctx_with(gh, shared, cfg), lines.append) == ["TARGET-QSeries"]
    assert shared.released == [key] and gh.states(1) == ["available"] and gh.states(2) == ["claimed"]
    assert lines == ["released TARGET-QSeries: left claimed by this worker's previous process"]
    assert "previous process ended without releasing" in gh.comments[1][0]


def test_board_sync_releases_a_claim_whose_lease_expired():
    cfg = example_config()
    leases = {claims.job_key(cfg.project.name, "TARGET-Expired"): {"owner": "CBirkbeck/chris-1", "expires_at": PAST},
              claims.job_key(cfg.project.name, "TARGET-Live"): {"owner": "CBirkbeck/chris-2", "expires_at": FUTURE}}
    gh = FakeIssuesGH([job_issue(1, "TARGET-Expired", "claimed"), job_issue(2, "TARGET-Live", "claimed"),
                       job_issue(3, "TARGET-Unleased", "claimed")])
    out = intake.sync_expired(ctx_with(gh, None, cfg), FakeClaims(leases=leases))
    assert out == ["TARGET-Expired: released (lease of CBirkbeck/chris-1 expired)"]
    assert [gh.states(n) for n in (1, 2, 3)] == [["available"], ["claimed"], ["claimed"]]
    assert intake.sync_expired(ctx_with(gh, None, cfg), None) == []
