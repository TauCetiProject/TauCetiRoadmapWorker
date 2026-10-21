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


def test_independence_by_family_of_models():
    sol = ("chris", "codex", "gpt-6.1-sol")
    assert claims.family("codex") == claims.family("ChatGPT") == "openai" and claims.family("claude") == "anthropic"
    assert claims.independent("family", ("chris", "claude", "fable"), sol)              # same operator, other family
    assert not claims.independent("family", ("bob", "codex", "gpt-6-astra"), sol)        # other operator, same family
    assert not claims.independent("family", ("bob", "chatgpt", ""), sol)                 # a browser model of that family
    assert claims.independent("family", ("bob", "codex", "x"), ("chris", "", ""))        # no agent recorded: by operator
    assert not claims.independent("family", ("chris", "claude", "x"), ("chris", "", ""))
    assert claims.separation_problem(["PKG-Foo"], "bob", {"PKG-Foo": {"operator": "chris", "agent": "codex"}},
                                     "family", "codex", "gpt-6-astra") == \
        "PKG-Foo was written by the openai family of models too, or by this operator"
    review = new_job("REV-PKG-Foo", independent_of=["PKG-Foo"])
    assert separation_problems(review, "bob", {"PKG-Foo": [{"login": "chris", "agent": "codex"}]}, "family", "codex") == \
        ["bob also did PKG-Foo with the same family of models, or as the same operator"]


def test_a_worker_with_both_families_takes_the_review_with_the_other(monkeypatch):
    from tauceti_roadmap_worker import worker
    cfg = example_config()
    cfg.review.separation = "family"
    shared = FakeClaims()
    shared.authors = {"PKG-Foo": {"operator": "chris", "agent": "codex", "model": "gpt-6.1-sol"}}
    review = new_job("REV-PKG-Foo", independent_of=["PKG-Foo"])
    review.number = 3
    ctx = Context(cfg=cfg, gh=None, board=FakeBoard(jobs=[review]), claims=shared, worker_id="chris-2",
                  operator="chris", agent="codex", model="gpt-6-astra", agent_fixed=False, dry_run=True)
    monkeypatch.setattr(worker, "usable_families", lambda ctx: {"codex", "claude"})
    lane = lane_for(review)
    other = worker.other_family(ctx, review, lane)
    assert other is not None and other.agent == "claude" and other.model == cfg.lane_model("review", "claude")
    assert worker.other_family(dataclasses.replace(ctx, agent_fixed=True), review, lane) is None
    assert worker.other_family(ctx, review, lane, paused={"claude": None}) is None


def test_the_configuration_accepts_family():
    from tauceti_roadmap_worker import config
    from tauceti_roadmap_worker.paths import HERE
    text = (HERE / "tauceti-roadmap.toml.example").read_text().replace('separation = "operator"', 'separation = "family"')
    assert config.parse(text, "example").review.separation == "family"
