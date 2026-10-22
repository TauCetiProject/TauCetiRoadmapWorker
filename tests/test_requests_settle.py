"""A job asks for references once per run, never again for a settled citation, and at most three
times (regression: RT-ArithmeticDynamics~1a parked three times on board #27, the third time for a
key the maintainer had waived after the second). A source is known by its identity, not its key
(regression: RT-SieveMethodsAndPrimePatterns~1a parked three times on jobs #284, each time
re-requesting a waived text under a new key: HR-SIEVE then HR-1974, KM-16RANK-published then
KM-KERNEL-PUBLISHED)."""

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
             {"id": "ms95", "title": "Periodic points", "status": "waived", "authors": "P. Morton", "year": 1995,
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


# --- identity, not key (jobs #284) -------------------------------------------------------------------

HR = {"id": "HR-SIEVE", "authors": "H. Halberstam and H.-E. Richert", "title": "Sieve Methods", "year": 1974,
      "status": "waived", "licence_class": "none", "resolution": "https://github.com/o/jobs/issues/284#issuecomment-1"}
KM = {"id": "KM-16RANK-published", "authors": "Peter Koymans, Djordjo Milovic", "year": 2018, "status": "waived",
      "title": "On the 16-Rank of Class Groups of ℚ(√-2p) for Primes p ≡ 1 mod 4", "doi": "10.1093/imrn/rny010",
      "licence_class": "none", "resolution": "https://github.com/o/jobs/issues/284#issuecomment-1"}
RENAMED = ('[[missing]]\nid = "HR-1974"\nauthors = "H. Halberstam, H.-E. Richert"\n'
           'title = "H. Halberstam and H.-E. Richert, Sieve methods, Academic Press, 1974"\nyear = 1974\n'
           'needed_for = "README §1.1"\ntried = ["title search"]\n\n'
           '[[missing]]\nid = "KM-KERNEL-PUBLISHED"\nauthors = "Peter Koymans and Djordjo Milovic"\n'
           'title = "On the 16-rank of class groups of Q(sqrt(-2p)) for primes p congruent to 1 modulo 4"\n'
           'year = 2019\nneeded_for = "README §5.6"\ntried = ["OUP: 403"]\n\n'
           '[[missing]]\nid = "KM-DOI"\nauthors = "P. Koymans"\ntitle = "Joint spin paper"\nyear = 2019\n'
           'doi = "https://doi.org/10.1093/IMRN/RNY010"\nneeded_for = "README §5.6"\ntried = ["OUP: 403"]\n\n'
           '[[missing]]\nid = "IW-SIEVE"\nauthors = "H. Iwaniec"\ntitle = "Sieve methods"\nyear = 1974\n'
           'needed_for = "README §1.1"\ntried = ["none"]\n')


def test_same_source_by_doi_arxiv_title_and_bibliographic_line():
    same = references.same_source
    assert same(references.MissingReference("X", "Other words", "2018", "x", doi="doi:10.1093/IMRN/rny010"), KM)
    assert same(references.MissingReference("X", "Other", "2018", "x", url="https://arxiv.org/abs/1705.05505v2"),
                {"id": "KM-2018", "arxiv": "1705.05505v1"})
    assert same(references.MissingReference("Halberstam, H. and Richert, H.-E.", "Sieve methods.", "1974", "x"), HR)
    assert same(references.MissingReference("Koymans, P.", "On the 16-rank of class groups of Q(sqrt(-2p)) for primes "
                                            "p congruent to 1 modulo 4", "2019", "x"), KM)
    assert not same(references.MissingReference("H. Iwaniec", "Sieve methods", "1974", "x"), HR)   # another author
    assert not same(references.MissingReference("P. Koymans", "On the 8-rank of class groups", "2019", "x"), KM)
    assert references.normalise_title("Groupes de Brauer et ℚ(√−2p) — ﬁnis") == "groupes de brauer et q sqrt 2p finis"


def test_a_settled_work_is_dropped_under_any_key_and_a_new_work_is_requested(ctx):  # noqa: F811
    run_ctx, job, lane, ws = setup(ctx)
    manifest(ws, run_ctx, HR, KM)
    (ws.scratch / "needs-references.toml").write_text(RENAMED)
    missing = lane.missing_references(job, run_ctx, ws)
    assert [m.key for m in missing] == ["IW-SIEVE"]                                    # genuinely new: requested
    assert ws.extra["notes"] == ["dropped from the request, settled in the manifest as HR-SIEVE: HR-1974 (waived); "
                                 "as KM-16RANK-published: KM-KERNEL-PUBLISHED (waived); "
                                 "as KM-16RANK-published: KM-DOI (waived)"]
    assert "as HR-SIEVE: HR-1974 (waived)" in (ws.job_dir / "requests.log").read_text()


def test_an_answered_text_is_asked_for_at_most_once_more(ctx):  # noqa: F811
    board = FakeBoard()
    run_ctx, job, lane, ws = setup(ctx, board)
    answered = {**HR, "id": "HR-SIEVE", "status": "certified", "licence_class": "answered-by:alice", "sha256": "a" * 64,
                "fetched_from": "https://example.org/hr.pdf"}
    manifest(ws, run_ctx, answered)
    again = ('[[missing]]\nid = "HR-1974"\nauthors = "H. Halberstam and H.-E. Richert"\ntitle = "Sieve methods"\n'
             'year = 1974\nneeded_for = "README §1.1"\ntried = ["unreadable here"]\n')
    item = references.MissingReference("H. Halberstam and H.-E. Richert", "Sieve Methods", "1974", "x", key="HR-SIEVE")
    lane.park(job, run_ctx, [item])                                       # the first request, answered by a human
    (ws.scratch / "needs-references.toml").write_text(again)
    assert [m.key for m in lane.missing_references(job, run_ctx, ws)] == ["HR-1974"]      # once more: requested
    lane_for(job).park(job, run_ctx, [references.MissingReference("H. Halberstam", "Sieve methods", "1974", "x",
                                                                   key="HR-1974")])
    (ws.scratch / "needs-references.toml").write_text(again.replace("HR-1974", "HALBERSTAM-RICHERT"))
    assert lane.missing_references(job, run_ctx, ws) == []                               # a third time: not posted
    assert ws.extra["notes"] == ["not requested again, answered in the manifest as HR-SIEVE: HALBERSTAM-RICHERT "
                                 "(certified); settled with the earlier answer"]
    assert len(board.posted) == 2
