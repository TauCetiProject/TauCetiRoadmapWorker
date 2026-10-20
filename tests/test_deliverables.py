"""The sources manifest is a deliverable of every job that works on the roadmap's sources
(regression: the first pilot's third round, where a target lane that had extended the manifest
holding the board's waivers was refused: "the agent changed files outside the job's deliverables:
packages/<Name>/sources.toml"). A review may add to the manifest but leaves the author's entries."""

import dataclasses

import pytest

from tauceti_roadmap_worker import agents, gitops, sources
from tauceti_roadmap_worker.board import allowed_paths, new_job
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.review import manifest_record_problems

from .fakes import FakeGH, FakeTransport
from .test_checkout import NAME, PKG, commit, live, needs_git, run  # noqa: F401 - the shared fixture

JOB = f"TARGET-{NAME}"
WAIVED = {"id": "GasperRahman2004", "authors": "G. Gasper and M. Rahman", "title": "Basic Hypergeometric Series",
          "year": 2004, "locator_scheme": "section", "locator": "Section 1.2", "status": "waived",
          "licence_class": "none", "cited_for": ["F1"]}
FOUND = {"id": "Zwegers2002", "authors": "S. Zwegers", "title": "Mock Theta Functions", "year": 2002,
         "arxiv": "0807.4834v1", "locator_scheme": "theorem", "locator": "Proposition 1.4", "status": "uncertified",
         "licence_class": "none", "cited_for": ["F5"]}


def manifest(*entries) -> str:
    data = sources.new_manifest(NAME)
    data["source"] = [dict(e) for e in entries]
    return sources.dump_manifest(data)


@pytest.mark.parametrize("job_id", ["TARGET-Foo", "DESIGN-Foo", "PKG-Foo", "REV-DESIGN-Foo", "REV-PKG-Foo~2",
                                    "FIX-DESIGN-Foo~2", "FIX-PKG-Foo~3"])
def test_the_manifest_is_a_deliverable_of_every_job_on_the_sources(job_id):
    assert "packages/Foo/sources.toml" in allowed_paths(new_job(job_id), "packages")


@pytest.mark.parametrize("job_id", ["PORT-Foo", "RT-Foo~1a", "FIX-RT-Foo~1", "GATE-Foo~1"])
def test_jobs_on_the_content_repository_submit_only_their_report(job_id):
    assert allowed_paths(new_job(job_id), "packages") == {f"packages/Foo/reports/{job_id}.md"}


@needs_git
def test_a_target_lane_submits_the_manifest_it_extended(live, tmp_path, monkeypatch):  # noqa: F811
    ctx, board_origin = live
    commit(board_origin, {f"{PKG}/sources.toml": manifest(WAIVED)}, "Sources: answers")   # `board references`
    fork, fork_repo = tmp_path / "fork.git", "alice/" + ctx.cfg.repositories.packages_repo().split("/")[1]
    run(tmp_path, "init", "-q", "--bare", str(fork))
    origin_url = gitops.url
    monkeypatch.setattr(gitops, "url", lambda repo: fork.as_uri() if repo == fork_repo else origin_url(repo))

    def agent(task, base_env=None):
        pkg = task.workdir / PKG
        (pkg / "target.md").write_text("Verdict: design\nName: QSeries\nBase: none\nOwner: none\n\n## Scope\n")
        (pkg / "sources.toml").write_text(manifest({**WAIVED, "cited_for": ["F1", "F6"]}, FOUND))
        (pkg / "reports" / f"{JOB}.md").write_text("# Triage of QSeries\n")
        return agents.AgentResult(exit_code=0, seconds=1.0)
    monkeypatch.setattr(agents, "run_agent", agent)
    gh = FakeGH()
    ctx = dataclasses.replace(ctx, gh=gh, transport=FakeTransport())
    job = new_job(JOB, inputs={"form": {"statement": "q-series identities", "why": "partitions"}})
    job.number = 1

    result = lane_for(job).run(job, ctx)

    assert (result.status, result.url) == ("submitted", "https://example.invalid/99"), result.detail
    [pr] = [payload for method, path, payload in gh.writes if path.endswith("/pulls")]
    assert f"`{PKG}/sources.toml`" in pr["body"] and f"`{PKG}/target.md`" in pr["body"]
    pushed = gitops.git(fork, "show", f"w1/{JOB}:{PKG}/sources.toml")
    assert "Zwegers2002" in pushed and ("state", "submitted") in ctx.board.calls


def test_altered_entries():
    before = {"source": [WAIVED, FOUND]}
    grown = {"source": [{**WAIVED, "cited_for": ["F1", "F2"]},
                        {**FOUND, "status": "certified", "sha256": "a" * 64, "licence_class": "open-repository",
                         "fetched_from": "https://arxiv.org/pdf/0807.4834v1"},
                        {**FOUND, "id": "New2020"}]}
    assert sources.altered_entries(before, grown) == []
    rewritten = {"source": [{**WAIVED, "locator": "Section 9", "cited_for": []}]}
    assert sources.altered_entries(before, rewritten) == ["GasperRahman2004: locator, cited_for changed",
                                                         "Zwegers2002: removed"]


@needs_git
def test_a_review_records_sources_but_keeps_the_authors_entries(live):  # noqa: F811
    ctx, board_origin = live
    commit(board_origin, {f"{PKG}/sources.toml": manifest(WAIVED)}, "the author's manifest")
    job = new_job(f"REV-PKG-{NAME}")
    ws = lane_for(job).prepare(job, ctx)
    path = ws.board / PKG / "sources.toml"
    path.write_text(manifest({**WAIVED, "cited_for": ["F1", "F6"]}, FOUND))
    assert manifest_record_problems(ctx, ws, f"{PKG}/sources.toml") == []
    path.write_text(manifest({**WAIVED, "locator": "Section 5.1"}))
    assert manifest_record_problems(ctx, ws, f"{PKG}/sources.toml") == [
        "sources.toml: a review keeps the author's entries: GasperRahman2004: locator changed"]
    path.unlink()
    assert manifest_record_problems(ctx, ws, f"{PKG}/sources.toml") == [
        "a review keeps the sources manifest: sources.toml was deleted"]
