"""Author/reviewer separation by operator (multi-operator projects) or by agent and model (a
single-operator project, such as the pilot's two workers under one login)."""

import dataclasses

from tauceti_roadmap_worker import board, claims
from tauceti_roadmap_worker.board import authorship_from_comments, new_job, separation_problems
from tauceti_roadmap_worker.lanes import Context, lane_for

from .fakes import FakeBoard, FakeClaims, example_config


def test_independence_by_operator_or_by_agent_and_model():
    sol, opus = ("chris", "codex", "gpt-6.1-sol"), ("chris", "claude", "claude-opus-5-5")
    fable, astra = ("CHRIS", "claude", "fable"), ("chris", "codex", "gpt-6-astra")
    assert claims.independent("operator", ("bob", "codex", "x"), sol)
    assert not claims.independent("operator", fable, sol)
    assert claims.independent("agent", fable, sol) and claims.independent("agent", astra, sol)
    assert not claims.independent("agent", ("chris", "codex", "gpt-6.1-sol"), sol)
    assert not claims.independent("agent", fable, ("chris", "", ""))      # an author record without its agent
    assert not claims.independent("agent", fable, ("chris", "claude", "")) # or without its model
    assert claims.independent("agent", fable, opus)                        # same family, another model


def test_separation_problem_reads_author_records_old_and_new():
    recorded = {"PKG-Foo": {"operator": "chris", "agent": "codex", "model": "gpt-6.1-sol"}, "FIX-PKG-Foo~2": "chris"}
    assert claims.separation_problem(["PKG-Foo"], "chris", recorded) == "operator chris authored PKG-Foo"
    assert claims.separation_problem(["PKG-Foo"], "chris", recorded, "agent", "claude", "fable") == ""
    assert claims.separation_problem(["PKG-Foo", "FIX-PKG-Foo~2"], "chris", recorded, "agent", "claude", "fable") == \
        "operator chris authored FIX-PKG-Foo~2 with the same agent and model as this reviewer (claude, fable), or none recorded"


def test_the_intake_applies_the_same_test_to_its_notes():
    notes = [{"user": {"login": "github-actions[bot]"}, "body": "Intake: merged #5 from @chris (codex, gpt-6.1-sol)."},
             {"user": {"login": "github-actions[bot]"}, "body": "Intake: merged #4 from @dana (claude)."},
             {"user": {"login": "mallory"}, "body": "Intake: merged #6 from @chris (claude, fable)."}]
    recorded = authorship_from_comments(notes, ["github-actions[bot]"])
    assert recorded == [{"login": "chris", "agent": "codex", "model": "gpt-6.1-sol"},
                        {"login": "dana", "agent": "claude", "model": ""}]
    review = new_job("REV-PKG-Foo", independent_of=["PKG-Foo"])
    authors = {"PKG-Foo": recorded}
    assert separation_problems(review, "chris", authors) == ["chris also did PKG-Foo"]
    assert separation_problems(review, "chris", authors, "agent", "claude", "fable") == []
    assert separation_problems(review, "chris", authors, "agent", "codex", "gpt-6.1-sol") == \
        ["chris also did PKG-Foo with the same agent and model as this reviewer (codex, gpt-6.1-sol), or none recorded"]
    assert separation_problems(review, "dana", authors, "agent", "claude", "fable") == \
        ["dana also did PKG-Foo with the same agent and model as this reviewer (claude, fable), or none recorded"]       # its model is unknown
    assert '"model": "fable"' in board.encode_submission("REV-PKG-Foo", "w1", "chris", "claude", "fable")


def test_a_single_operator_project_reviews_its_own_packages_with_another_model():
    cfg = example_config()
    shared = FakeClaims()
    shared.authors = {"PKG-Foo": {"operator": "chris", "agent": "codex", "model": "gpt-6.1-sol"}}
    ctx = Context(cfg=cfg, gh=None, board=FakeBoard(), claims=shared, worker_id="chris-2", operator="chris",
                  agent="claude", model="fable")
    review = new_job("REV-PKG-Foo", independent_of=["PKG-Foo"])
    assert lane_for(review).eligible(review, ctx) == "operator chris authored PKG-Foo"
    cfg.review.separation = "agent"
    assert lane_for(review).eligible(review, ctx) == ""
    same = dataclasses.replace(ctx, agent="codex", model="gpt-6.1-sol")
    assert "with the same agent and model" in lane_for(review).eligible(review, same)
