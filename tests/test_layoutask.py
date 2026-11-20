"""Asking an author for the README layout (`layoutask`), and the label that waits for it (`intake`)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from tauceti_roadmap_worker import intake, layout, layoutask

from .fakes import FakeBoard, example_config
from .test_prreview import H1, H2, REPO, ContentGH, node, review_job, verdict_file

GOOD = ("# Roadmap: toy groups\n\n**Summary.** Builds the order of an element.\n\n## Conventions\n\nMultiplicative.\n\n"
        "## Layer 0: orders\n\nDefine the order.\n\n## Scope and ownership\n\nOwns the order.\n\n"
        "## Exact supplier contracts\n\n### From Mathlib\n\n`orderOf`.\n\n## References\n\nLang.\n")
# The older form: no overview, the scope first, no conventions and no supplier contracts.
OLD = ("# Toy groups\n\nThis roadmap builds the order of an element.\n\n## Scope and ownership\n\nOwns the order.\n\n"
       "## Layer 0: orders\n\nDefine the order.\n\n## References\n\nLang.\n")
# Everything there, in another order.
MOVED = GOOD.replace("## Conventions\n\nMultiplicative.\n\n## Layer 0: orders\n\nDefine the order.\n\n## Scope and ownership\n\nOwns the order.\n\n",
                     "## Scope and ownership\n\nOwns the order.\n\n## Conventions\n\nMultiplicative.\n\n## Layer 0: orders\n\nDefine the order.\n\n")
README = "TauCetiRoadmap/Foo/README.md"
LABEL = "ai-found-no-errors"


def context(tmp_path, gh, jobs=(), **over):
    cfg = example_config()
    cfg.prreview.enabled = True
    cfg.prreview.text_per_run = 0
    for key, value in over.items():
        setattr(cfg.prreview, key, value)
    return SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(jobs=list(jobs)), operator="board-bot", dry_run=False, state=tmp_path)


def test_lacks_names_what_a_readme_is_short_of():
    assert layout.lacks(GOOD) == {}
    found = layout.lacks(OLD)
    assert set(found) == {"overview", "sections", "order"}
    assert "`## Conventions` and `## Exact supplier contracts`" in found["sections"] and "Summary" in found["overview"]
    assert set(layout.lacks(MOVED)) == {"order"}
    assert set(layout.lacks(GOOD.replace("## Layer 0: orders", "## Orders"))) == {"sections"}
    assert "`## Layer k: …`" in layout.lacks(GOOD.replace("## Layer 0: orders", "## Orders"))["sections"]
    assert set(layout.lacks("no title, no sections")) == {"overview", "sections"}


def test_the_request_is_one_comment_and_is_asked_once(tmp_path):
    gh = ContentGH([node(12, H1, author="outsider"), node(13, H1, author="outsider", paths=("TauCetiRoadmap/Bar/README.md",))],
                   blobs={(H1, README): OLD, (H1, "TauCetiRoadmap/Bar/README.md"): GOOD})
    ctx = context(tmp_path, gh)
    dry = layoutask.run(ctx, others=True, report=lambda _line: None)
    assert dry == ["[dry run] #12 (outsider): would ask: Foo lacks overview, sections, order",
                   "#13 (outsider): nothing to ask: in the layout"] and gh.writes == []
    lines = layoutask.run(ctx, others=True, write=True, report=lambda _line: None, sleep=lambda _s: None,
                          now=lambda: "2026-11-20T10:00:00Z")
    assert lines[0] == "#12 (outsider): asked: Foo lacks overview, sections, order" and lines[-1] == "1 pull request asked"
    [(method, path, payload)] = gh.writes
    assert (method, path) == ("POST", f"repos/{REPO}/issues/12/comments")
    body = payload["body"]
    assert body.startswith("**The README layout.**") and "`TauCetiRoadmap/Foo/README.md`: an overview that begins" in body
    assert "`ai-found-no-errors` label stays off" in body and "nothing here questions the mathematics" in body
    record = json.loads(body.split(layoutask.REQUEST_MARK, 1)[1].split("-->")[0])
    assert record == {"head": H1, "needs": {"Foo": ["order", "overview", "sections"]}}
    assert layoutask.load(tmp_path)["12"]["needs"] == {"Foo": ["order", "overview", "sections"]}
    # Asked once: a second run posts nothing; --needs narrows what is asked for.
    assert layoutask.run(ctx, others=True, write=True, report=lambda _line: None)[0] == f"#12 (outsider): already asked at {H1[:7]}"
    assert len(gh.writes) == 1
    only = layoutask.run(context(tmp_path / "x", gh), numbers=[12], kinds=["order"], report=lambda _line: None)
    assert only == ["[dry run] #12 (outsider): would ask: Foo lacks order"]


def test_nothing_is_asked_of_the_pool_nor_of_an_author_the_reviews_leave_out(tmp_path):
    gh = ContentGH([node(12, H1, author="carol"), node(14, H1, author="silent"), node(15, H1, author="skipped")],
                   blobs={(H1, README): OLD})
    ctx = context(tmp_path, gh, skip_authors=["skipped"])
    ctx.cfg.roadmap.pool = ["carol"]
    ctx.cfg.worker.no_review_authors = ["silent"]
    assert layoutask.run(ctx, others=True, report=lambda _line: None) == ["no pull request selected"]
    assert layoutask.run(ctx, numbers=[12], report=lambda _line: None) == [
        "#12 (carol): refused: a pull request of the pool is reordered, not asked (`layout`, `overview`)"]
    ctx.cfg.worker.no_review_except = [14]
    assert layoutask.run(ctx, others=True, report=lambda _line: None) == [
        "[dry run] #14 (silent): would ask: Foo lacks overview, sections, order"]


def test_the_label_waits_for_the_layout_and_follows_the_reviews_again(tmp_path):
    labels = f"repos/{REPO}/issues/12/labels"
    done = review_job(41, 1, 12, H1, "done")
    layoutask.save(tmp_path, {"12": {"head": H1, "needs": {"Foo": ["order", "overview", "sections"]}}})
    # Accepted and labelled, and the README still in the older form: the label goes, with the approval label.
    gh = ContentGH([node(12, H1, author="outsider", labels=[LABEL, "awaiting-human-approval"])],
                   files=verdict_file(done, "accepted", H1), blobs={(H1, README): OLD})
    out = intake.sync_pr_reviews(context(tmp_path, gh, [done], approval_label="awaiting-human-approval"))
    assert gh.writes == [("DELETE", f"{labels}/{LABEL}", {}), ("DELETE", f"{labels}/awaiting-human-approval", {})]
    assert out[0] == f"#12: {LABEL} removed (a layout request stands: Foo lacks overview, sections, order)"
    # Accepted, not labelled, the request standing: no label is added, and no review is filed for the head.
    gh = ContentGH([node(12, H1, author="outsider")], files=verdict_file(done, "accepted", H1), blobs={(H1, README): OLD})
    ctx = context(tmp_path, gh, [done])
    assert intake.sync_pr_reviews(ctx) == [] and gh.writes == [] and ctx.board.created == []
    # Only the kinds `layout_hold` names hold the label: with the order left out, a README short of the order alone
    # settles the request.
    gh = ContentGH([node(12, H1, author="outsider")], files=verdict_file(done, "accepted", H1), blobs={(H1, README): MOVED})
    out = intake.sync_pr_reviews(context(tmp_path, gh, [done], layout_hold=["overview", "sections"]))
    assert out[0] == f"#12: layout request settled (the README is in the layout at {H1[:7]})"
    assert gh.writes == [("POST", labels, {"labels": [LABEL]})] and layoutask.load(tmp_path) == {}
    # The README is brought into the layout at a new head: the request is settled, and the label waits for that
    # head's review like any other.
    layoutask.save(tmp_path, {"12": {"head": H1, "needs": {"Foo": ["order", "overview", "sections"]}}})
    gh = ContentGH([node(12, H2, author="outsider")], files=verdict_file(done, "accepted", H1), blobs={(H2, README): GOOD})
    ctx = context(tmp_path, gh, [done])
    out = intake.sync_pr_reviews(ctx)
    assert out[0] == f"#12: layout request settled (the README is in the layout at {H2[:7]})" and layoutask.load(tmp_path) == {}
    assert [job.id for job, _state, _ in ctx.board.created] == ["PRREVIEW-Foo~2"] and gh.writes == []
    # A README GitHub did not give leaves the request standing; a closed pull request drops it.
    layoutask.save(tmp_path, {"12": {"head": H1, "needs": {"Foo": ["order"]}}, "99": {"head": H1, "needs": {"Foo": ["order"]}}})
    gh = ContentGH([node(12, H1, author="outsider", labels=[LABEL])], files=verdict_file(done, "accepted", H1), blobs="unread")
    out = intake.sync_pr_reviews(context(tmp_path, gh, [done]))
    assert out[0] == "#99: layout request dropped (the pull request is no longer open)"
    assert out[1] == f"#12: {LABEL} removed (a layout request stands (its READMEs could not be read this time))"
    assert set(layoutask.load(tmp_path)) == {"12"}


def test_a_run_without_a_state_directory_holds_nothing():
    done = review_job(41, 1, 12, H1, "done")
    gh = ContentGH([node(12, H1, author="outsider")], files=verdict_file(done, "accepted", H1))
    cfg = example_config()
    cfg.prreview.enabled, cfg.prreview.text_per_run = True, 0
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(jobs=[done]), operator="board-bot", dry_run=False)
    assert intake.sync_pr_reviews(ctx) == [f"#12: {LABEL} added (the AI review of {H1[:7]} accepts it)"]
