"""A job asks for references once per run, never again for a settled citation, and at most three
times (regression: RT-ArithmeticDynamics~1a parked three times on board #27, the third time for a
key the maintainer had waived after the second)."""

import dataclasses

from tauceti_roadmap_worker import agents, references, sources
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for

from .fakes import FakeBoard, FakeTransport
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

REQUEST = ('[[missing]]\nid = "forster-riemann-surfaces"\nauthors = "O. Forster"\ntitle = "Lectures on Riemann Surfaces"\n'
           'year = 1981\nneeded_for = "1.2"\ntried = ["Springer: paywalled"]\n\n'
           '[[missing]]\nid = "ms95"\nauthors = "P. Morton, J. Silverman"\ntitle = "Periodic points"\nyear = 1995\n'
           'needed_for = "2.1"\ntried = ["GDZ: image-only"]\n')


def manifest(ws, run_ctx, *entries):
    path = ws.package(run_ctx.cfg, "ArithmeticDynamics") / "sources.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = sources.new_manifest("ArithmeticDynamics")
    data["source"] = list(entries)
    sources.save_manifest(path, data)


WAIVED = {"id": "forster-riemann-surfaces", "authors": "O. Forster", "title": "Lectures on Riemann Surfaces",
          "year": 1981, "status": "waived", "licence_class": "none", "locator_scheme": "theorem"}


def setup(ctx, board=None):  # noqa: F811
    run_ctx = dataclasses.replace(ctx, dry_run=False, transport=FakeTransport(), board=board or FakeBoard())
    job = new_job("RT-ArithmeticDynamics~1a", inputs={"pr": 1336, "operator": "alice"})
    job.number = 27
    lane = lane_for(job)
    ws = lane_for(new_job("REV-PKG-ArithmeticDynamics")).prepare(new_job("REV-PKG-ArithmeticDynamics"), run_ctx)
    return run_ctx, job, lane, ws


def test_a_waived_key_is_never_requested_again(ctx):  # noqa: F811
    run_ctx, job, lane, ws = setup(ctx)
    manifest(ws, run_ctx, WAIVED)
    (ws.scratch / "needs-references.toml").write_text(REQUEST)
    missing = lane.missing_references(job, run_ctx, ws)
    assert [m.key for m in missing] == ["ms95"]
    assert ws.extra["notes"] == ["dropped from the request, settled in the manifest: forster-riemann-surfaces (waived)"]
    assert "forster-riemann-surfaces (waived)" in (ws.job_dir / "requests.log").read_text()
    # Matched by title too, whatever key the agent gave it; an accepted uncertified citation is settled as well.
    renamed = REQUEST.replace('id = "forster-riemann-surfaces"', 'id = "Forster81"')
    (ws.scratch / "needs-references.toml").write_text(renamed)
    assert [m.key for m in lane.missing_references(job, run_ctx, ws)] == ["ms95"]
    manifest(ws, run_ctx, {**WAIVED, "status": "uncertified", "doi": "10.1007/978-1-4612-5961-9"},
             {"id": "ms95", "title": "Periodic points", "status": "waived", "authors": "M", "year": 1995,
              "licence_class": "none", "locator_scheme": "page"})
    assert lane.missing_references(job, run_ctx, ws) == []                  # nothing left: no park at all


def test_the_request_lists_every_unreadable_manifest_text(ctx):  # noqa: F811
    run_ctx, job, lane, ws = setup(ctx)
    manifest(ws, run_ctx, WAIVED)
    ws.extra["auto_missing"] = [references.MissingReference("J. Zieve", "Cycles of polynomial mappings", "1996", "3.4",
                                                            [sources.UNREADABLE], key="Zieve1996")]
    (ws.scratch / "needs-references.toml").write_text(REQUEST)            # the agent dropped Zieve1996
    assert [m.key for m in lane.missing_references(job, run_ctx, ws)] == ["ms95", "Zieve1996"]
    assert "added to the request, unreadable here: Zieve1996" in ws.extra["notes"]


def test_one_request_per_run_and_at_most_three_per_job(ctx):  # noqa: F811
    board = FakeBoard()
    run_ctx, job, lane, _ws = setup(ctx, board)
    item = [references.MissingReference("P. Morton", "Periodic points", "1995", "2.1", ["GDZ"], key="ms95")]
    first = lane.park(job, run_ctx, item)
    assert first.status == "needs-references" and len(board.posted) == 1
    second = lane.park(job, run_ctx, item)
    assert second.status == "failed" and "second needs-references request in one run is refused" in second.detail
    assert len(board.posted) == 1                                             # nothing posted the second time
    # Two more runs ask again; the fourth is blocked for a human instead of posting a fourth request.
    for _ in range(2):
        assert lane_for(job).park(job, run_ctx, item).status == "needs-references"
    fourth = lane_for(job).park(job, run_ctx, item)
    assert fourth.status == "blocked" and "asked for references 3 times" in fourth.detail and len(board.posted) == 3


def test_a_run_whose_request_was_all_settled_goes_on(ctx, monkeypatch):  # noqa: F811
    def agent(run, base_env=None):
        (run.log_out.parent / "scratch" / "needs-references.toml").write_text(REQUEST.split("\n\n")[0] + "\n")
        return agents.AgentResult(exit_code=0, seconds=1.0)
    monkeypatch.setattr(agents, "run_agent", agent)
    board = FakeBoard()
    run_ctx, _job, _lane, _ws = setup(ctx, board)
    job = new_job("REV-PKG-ArithmeticDynamics")
    job.number = 27
    lane = lane_for(job)
    real_prepare = lane.prepare

    def prepare(job_, ctx_):
        ws_ = real_prepare(job_, ctx_)
        manifest(ws_, ctx_, WAIVED)
        return ws_
    monkeypatch.setattr(lane, "prepare", prepare)
    result = lane.run(job, run_ctx)
    assert result.status != "needs-references" and board.posted == []
    assert "dropped from the request, settled in the manifest: forster-riemann-surfaces (waived)" in result.detail
