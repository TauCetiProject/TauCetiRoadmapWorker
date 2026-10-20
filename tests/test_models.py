"""Reviews and red-team passes run only on the review models (Fable on Claude, Astra on Codex);
authoring lanes keep the family defaults."""

import dataclasses

from tauceti_roadmap_worker import board, cli, worker
from tauceti_roadmap_worker.board import Outcome, decode_spec, encode_spec, new_job
from tauceti_roadmap_worker.lanes import Context, lane_for

from .fakes import FakeBoard, example_config


def base(**over) -> Context:
    cfg = example_config()
    ctx = Context(cfg=cfg, gh=None, board=FakeBoard(), claims=None, worker_id="w1", operator="alice",
                  agent=cfg.agents.default, model=cfg.model_for(cfg.agents.default), agent_fixed=False)
    return dataclasses.replace(ctx, **over)


def test_a_lane_pins_a_model_per_family():
    cfg = example_config()
    assert cfg.lane_models("review") == {"codex": "gpt-6-astra", "claude": "fable"}
    assert cfg.lane_agent("review") == ("codex", "gpt-6-astra") and cfg.lane_agent("design") == ("codex", "gpt-6.1-sol")
    assert [(p.agent, p.model) for p in cfg.redteam.passes] == [("codex", "gpt-6-astra"), ("claude", "fable")]


def test_reviews_get_the_review_model_of_the_workers_family(monkeypatch):
    monkeypatch.setattr(worker.shutil, "which", lambda agent: f"/usr/bin/{agent}")
    review, design = new_job("REV-PKG-Foo"), new_job("DESIGN-Foo")
    ctx, why = worker.for_job(base(), review)
    assert (ctx.agent, ctx.model, why) == ("codex", "gpt-6-astra", "")
    assert worker.for_job(base(), design)[0].model == "gpt-6.1-sol"
    claude = base(agent="claude", model="claude-opus-5-5", agent_fixed=True)       # --agent claude
    assert (worker.for_job(claude, review)[0].model, worker.for_job(claude, design)[0].model) == ("fable", "claude-opus-5-5")


def test_a_worker_pinned_to_another_model_leaves_reviews(monkeypatch):
    pinned = base(model="gpt-6.1-sol", agent_fixed=True, model_fixed=True)        # --agent codex --model gpt-6.1-sol
    ctx, why = worker.for_job(pinned, new_job("REV-PKG-Foo"))
    assert ctx is None and why == "REV-PKG-Foo needs gpt-6-astra or fable; this worker is pinned to gpt-6.1-sol"
    assert worker.for_job(pinned, new_job("PKG-Foo"))[0] is pinned
    astra = base(model="gpt-6-astra", agent_fixed=True, model_fixed=True)
    assert worker.for_job(astra, new_job("REV-PKG-Foo"))[0] is astra
    rt = new_job("RT-Foo~1b", requires_agent="claude", requires_model="fable")
    assert worker.for_job(astra, rt) == (None, "needs agent claude")
    fable = base(agent="claude", model="fable", agent_fixed=True, model_fixed=True)
    assert worker.for_job(fable, rt)[0] is fable
    board_ = FakeBoard(jobs=[dataclasses.replace(new_job("REV-PKG-Foo"), state="available")])
    lines = []
    assert worker.work_once(dataclasses.replace(pinned, board=board_), None, log=lines.append).status == "idle"
    assert lines == ["skip REV-PKG-Foo: REV-PKG-Foo needs gpt-6-astra or fable; this worker is pinned to gpt-6.1-sol"]


def test_red_team_passes_carry_their_model_into_the_job():
    cfg = example_config()
    review = new_job("REV-PKG-Foo")
    passes = board.successors(review, Outcome(verdict="accepted", pr=12, operator="alice"),
                              redteam_agents=("codex", "claude"), redteam_models=tuple(p.model for p in cfg.redteam.passes))
    assert [(j.id, j.requires_agent, j.requires_model) for j in passes] == [
        ("RT-Foo~1a", "codex", "gpt-6-astra"), ("RT-Foo~1b", "claude", "fable")]
    assert decode_spec(encode_spec(passes[1])).requires_model == "fable"
    fix = new_job("FIX-RT-Foo~1", inputs={"board_pr": 12, "operator": "alice", "reviews": []})
    nxt = board.successors(fix, Outcome(), redteam_agents=("codex", "claude"), redteam_models=("gpt-6-astra",))
    assert [j.requires_model for j in nxt] == ["gpt-6-astra", ""]
    job = passes[1]
    assert "needs model fable" in lane_for(job).eligible(job, base(agent="claude", model="claude-opus-5-5"))


def test_doctor_says_whether_this_host_can_run_the_review_models():
    cfg = example_config()
    rows = cli.review_model_rows(cfg, ["claude"])
    assert [(r[0], r[1]) for r in rows] == [(cli.OK, "review models"), (cli.OK, "redteam models")]
    [warn, _] = cli.review_model_rows(cfg, [])
    assert warn[0] == cli.WARN and "gpt-6-astra (codex) or fable (claude)" in warn[2]
