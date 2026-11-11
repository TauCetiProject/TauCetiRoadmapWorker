"""Author/reviewer separation by operator (multi-operator projects) or by agent and model (a
single-operator project, such as the pilot's two workers under one login)."""

import dataclasses

from tauceti_roadmap_worker import board, claims
from tauceti_roadmap_worker.board import authorship_from_comments, new_job, separation_problems
from tauceti_roadmap_worker.config import RedTeamPass
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


def test_a_reviewer_without_its_agent_or_model_is_unknown_not_different():
    sol = ("chris", "codex", "gpt-6.1-sol")
    assert claims.independent("agent", ("chris", "chatgpt", ""), sol)       # a browser comment: another agent
    assert not claims.independent("agent", ("chris", "codex", ""), sol)     # the same agent, model unknown
    assert not claims.independent("agent", ("chris", "", "fable"), sol)
    assert not claims.independent("agent", ("chris", "", ""), sol)
    assert claims.independent("agent", ("dana", "chatgpt", ""), sol)        # another operator still is


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


def test_a_pinned_red_team_model_never_writes_what_its_pass_must_be_independent_of():
    passes = [("codex", "gpt-6-astra"), ("claude", "fable")]
    blocked = lambda jid, agent, model, rounds=2: board.blocked_redteam_pass(jid, agent, model, passes, rounds)  # noqa: E731
    for jid in ("PKG-Foo", "FIX-PKG-Foo~2", "FIX-PKG-Foo~5", "FIX-RT-Foo~1"):
        assert blocked(jid, "claude", "fable") == "claude, fable", jid
        assert blocked(jid, "codex", "gpt-6-astra") == "codex, gpt-6-astra", jid
        assert blocked(jid, "claude", "claude-opus-5-5") == "", jid
        assert blocked(jid, "codex", "fable") == "", jid
    # The last round's fix has no pass after it; designs, ports and reviews are not what a pass is independent of.
    for jid in ("FIX-RT-Foo~2", "DESIGN-Foo", "FIX-DESIGN-Foo~2", "PORT-Foo", "REV-PKG-Foo", "RT-Foo~1b", "GATE-Foo"):
        assert blocked(jid, "claude", "fable") == "", jid
    assert blocked("FIX-RT-Foo~2", "claude", "fable", rounds=3) == "claude, fable"
    assert blocked("PKG-Foo", "claude", "fable", rounds=0) == ""
    assert board.blocked_redteam_pass("PKG-Foo", "claude", "fable", [("codex", ""), ("claude", "")], 2) == ""  # unpinned


def test_under_separation_by_agent_a_worker_on_a_pinned_pass_model_leaves_the_package_to_others():
    cfg = example_config()
    cfg.redteam.passes = [RedTeamPass("codex", "gpt-6-astra"), RedTeamPass("claude", "fable")]
    fable = Context(cfg=cfg, gh=None, board=FakeBoard(), claims=FakeClaims(), worker_id="chris-2", operator="chris",
                    agent="claude", model="fable")
    opus = dataclasses.replace(fable, agent="claude", model="claude-opus-5-5")
    fix, last_fix, review = new_job("FIX-PKG-Foo~2"), new_job("FIX-RT-Foo~2"), new_job("REV-PKG-Foo~2")
    assert "pinned to" not in lane_for(fix).eligible(fix, fable)             # several operators: another one can run the pass
    cfg.review.separation = "agent"
    assert "red-team pass pinned to (claude, fable)" in lane_for(fix).eligible(fix, fable)
    assert "pinned to" not in lane_for(fix).eligible(fix, opus)
    assert "pinned to" not in lane_for(last_fix).eligible(last_fix, fable)
    assert "pinned to" not in lane_for(review).eligible(review, fable)


def test_with_fix_rounds_off_a_red_team_pass_is_held_to_the_package_and_the_red_team_fix_only():
    listed = ["PKG-Foo", "FIX-PKG-Foo~2", "FIX-PKG-Foo~3", "FIX-RT-Foo~1"]
    assert board.independence_scope("RT-Foo~2b", listed) == listed
    assert board.independence_scope("RT-Foo~2b", listed, fix_rounds=False) == ["PKG-Foo", "FIX-RT-Foo~1"]
    assert board.independence_scope("RT-Foo~1a", ["PKG-Foo", "FIX-PKG-Foo~2"], fix_rounds=False) == ["PKG-Foo"]
    # Reviews keep every author of the round they review, whatever the setting.
    assert board.independence_scope("REV-PKG-Foo~3", listed[:3], fix_rounds=False) == listed[:3]
    assert board.independence_scope("REV-DESIGN-Foo~2", ["DESIGN-Foo", "FIX-DESIGN-Foo~2"], fix_rounds=False) == [
        "DESIGN-Foo", "FIX-DESIGN-Foo~2"]
    assert board.independence_scope("RT-Foo~1b", ["PKG-Foo", "not a job id"], fix_rounds=False) == ["PKG-Foo", "not a job id"]
    # The guard follows: with fix rounds off, the pinned model may write a package fix round, never the package.
    passes = [("codex", "gpt-6-astra"), ("claude", "fable")]
    assert board.blocked_redteam_pass("FIX-PKG-Foo~2", "claude", "fable", passes, 2, fix_rounds=False) == ""
    assert board.blocked_redteam_pass("PKG-Foo", "claude", "fable", passes, 2, fix_rounds=False) == "claude, fable"
    assert board.blocked_redteam_pass("FIX-RT-Foo~1", "claude", "fable", passes, 2, fix_rounds=False) == "claude, fable"


def test_with_fix_rounds_off_the_pinned_model_takes_a_pass_on_a_package_whose_fix_round_it_wrote():
    cfg = example_config()
    cfg.review.separation = "agent"
    cfg.redteam.passes = [RedTeamPass("codex", "gpt-6-astra"), RedTeamPass("claude", "fable")]
    wrote = {"operator": "chris", "agent": "claude", "model": "fable"}
    other = {"operator": "chris", "agent": "claude", "model": "claude-opus-5-5"}

    class Recorded(FakeClaims):
        def recorded_authors(self, ids):
            return {i: r for i, r in {"PKG-Foo": other, "FIX-PKG-Foo~2": wrote}.items() if i in ids}

    fable = Context(cfg=cfg, gh=None, board=FakeBoard(), claims=Recorded(), worker_id="chris-2", operator="chris",
                    agent="claude", model="fable")
    rt = new_job("RT-Foo~1b", independent_of=["PKG-Foo", "FIX-PKG-Foo~2"], requires_agent="claude",
                 requires_model="fable")
    assert "authored FIX-PKG-Foo~2" in lane_for(rt).eligible(rt, fable)
    cfg.redteam.independent_of_fix_rounds = False
    assert "authored" not in lane_for(rt).eligible(rt, fable)
    # The intake applies the same scope to the submission, and still refuses the package's own author.
    authors = {"PKG-Foo": [other], "FIX-PKG-Foo~2": [wrote]}
    assert board.separation_problems(rt, "chris", authors, "agent", "claude", "fable")
    assert board.separation_problems(rt, "chris", authors, "agent", "claude", "fable", fix_rounds=False) == []
    assert board.separation_problems(rt, "chris", {"PKG-Foo": [wrote]}, "agent", "claude", "fable", fix_rounds=False)
