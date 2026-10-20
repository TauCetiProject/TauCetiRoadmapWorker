"""The sources service inside a lane: the manifest's texts are fetched before the agent runs, a text
that is gone for good becomes a needs-references request, and a request parks the job."""

import dataclasses

from tauceti_roadmap_worker import agents, references, sources
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import base as lane_base
from tauceti_roadmap_worker.lanes import lane_for

from .fakes import FakeTransport, make_pdf
from .test_lane_dry_run import ctx  # noqa: F401 - the shared lane fixture

PDF = make_pdf(["Theorem 2.3 " * 40])
SHA = sources.sha256_bytes(PDF)
GONE = "c" * 64


def write_manifest(run_ctx, ws, name="Foo"):
    manifest = ws.package(run_ctx.cfg, name) / "sources.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    data = sources.new_manifest(name)
    data["source"] = [
        {"id": "AB21", "authors": "A. Author", "title": "An example paper", "year": 2021, "status": "certified",
         "sha256": SHA, "fetched_from": "https://arxiv.org/pdf/2101.00001v2", "licence_class": "open-repository",
         "locator_scheme": "theorem", "cited_for": ["1.2"]},
        {"id": "Gon99", "authors": "G. One", "title": "A vanished preprint", "year": 1999, "status": "certified",
         "sha256": GONE, "fetched_from": "https://arxiv.org/pdf/9901.00001v1", "licence_class": "open-repository",
         "locator_scheme": "page", "cited_for": ["Layer 2"]},
        {"id": "Pay20", "authors": "P. Wall", "title": "Paywalled", "year": 2020, "status": "uncertified",
         "licence_class": "none", "locator_scheme": "theorem"}]
    sources.save_manifest(manifest, data)
    return manifest


def live(base, transport):
    return dataclasses.replace(base, dry_run=False, transport=transport)


def test_prepare_sources_fetches_and_requests_what_is_gone(ctx):  # noqa: F811
    run_ctx = live(ctx, FakeTransport().add("https://arxiv.org/pdf/2101.00001v2", PDF))
    lane, job = lane_for(new_job("REV-PKG-Foo")), new_job("REV-PKG-Foo")
    ws = lane.prepare(job, run_ctx)
    write_manifest(run_ctx, ws)
    (ws.scratch / "needs-references.toml").write_text("[[missing]]\ntitle = 'old'\nneeded_for = 'x'\n")
    lane.prepare_sources(job, run_ctx, ws)
    listing = ws.extra["source_texts"]
    assert f"`AB21` (A. Author, An example paper, 2021): `{run_ctx.state / 'sources' / (SHA + '.txt')}`" in listing
    assert "`Gon99` (G. One, A vanished preprint, 1999): unavailable: " in listing and "Pay20" not in listing
    assert (ws.scratch / "needs-references.previous.toml").read_text().startswith("[[missing]]\ntitle = 'old'")
    [missing] = references.read_missing(ws.scratch / "needs-references.toml")
    assert missing.key == "Gon99" and missing.sha256 == GONE and missing.needed_for == "Layer 2"
    assert missing.locator_scheme == "page" and "not found" in missing.tried[0]


def test_transient_trouble_does_not_park(ctx):  # noqa: F811
    from .fakes import transient
    run_ctx = live(ctx, FakeTransport({"https://arxiv.org/pdf/2101.00001v2": transient("x"),
                                       "https://arxiv.org/pdf/9901.00001v1": transient("y")}))
    lane, job = lane_for(new_job("REV-PKG-Foo")), new_job("REV-PKG-Foo")
    ws = lane.prepare(job, run_ctx)
    write_manifest(run_ctx, ws)
    lane.prepare_sources(job, run_ctx, ws)
    assert not (ws.scratch / "needs-references.toml").exists()


def test_fetch_command_edits_the_manifest_only_where_the_job_may(ctx):  # noqa: F811
    design = new_job("DESIGN-Foo")
    ws = lane_base.Lane().prepare(design, ctx)
    cmd = lane_for(design).fetch_command(design, ctx, ws)
    assert cmd.startswith("tauceti-roadmap --conf /conf.toml sources fetch --manifest ")
    assert cmd.endswith("/scratch/needs-references.toml") and "packages/Foo/sources.toml" in cmd
    for job_id in ("TARGET-Foo", "PKG-Foo", "REV-DESIGN-Foo", "REV-PKG-Foo~2", "FIX-PKG-Foo~2"):
        job = new_job(job_id)
        assert "--manifest" in lane_for(job).fetch_command(job, ctx, ws), job_id
    for job_id in ("PORT-Foo", "RT-Foo~1a", "FIX-RT-Foo~1", "GATE-Foo~1"):
        job = new_job(job_id)
        assert "--manifest" not in lane_for(job).fetch_command(job, ctx, ws), job_id


def test_dry_run_brief_lists_cached_texts_without_fetching(ctx):  # noqa: F811
    transport = FakeTransport()
    run_ctx = dataclasses.replace(ctx, transport=transport)
    job = new_job("PKG-Foo")
    job.number = 7
    ws = lane_base.Lane().prepare(job, run_ctx)
    write_manifest(run_ctx, ws)
    assert lane_for(job).run(job, run_ctx).status == "dry-run"
    brief = (ws.job_dir / "brief.md").read_text()
    assert "unavailable: not in the cache (dry run: not fetched)" in brief and transport.requests == []
    assert "sources fetch --manifest" in brief


def test_a_request_parks_the_job_with_its_marker(ctx, monkeypatch):  # noqa: F811
    def agent_asks_for_a_text(run, base_env=None):
        (run.log_out.parent / "scratch" / "needs-references.toml").write_text(
            '[[missing]]\nauthors = "R. Kettle"\ntitle = "Theorem A und Theorem B"\nyear = 1967\n'
            'needed_for = "the locator sample: Satz 2.3"\ntried = ["GDZ: image-only"]\n')
        return agents.AgentResult(exit_code=0, seconds=1.0)
    monkeypatch.setattr(agents, "run_agent", agent_asks_for_a_text)
    run_ctx = live(ctx, FakeTransport())
    job = new_job("REV-PKG-Foo")
    job.number = 7
    result = lane_for(job).run(job, run_ctx)
    assert result.status == "needs-references" and result.detail.endswith("Ket67")
    assert ("comment", 7) in run_ctx.board.calls and ("state", "needs-references") in run_ctx.board.calls
