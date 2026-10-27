"""One review contract for every route (design review R07): a review or red-team pass counts on the
board only when it keeps the contract the review and red-team lanes hold a worker's report to,
whether a worker posted it or a listed login's browser model did. Reproduced first in the live
configuration's shape (separation by agent and model, passes codex/gpt-6-astra and claude/fable): a
listed login's acceptance with a P1 finding and no evidence accepted the package, and both red-team
passes counted as `codex`."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import boardpr, claims, prreviews
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import base, lane_for

from .fakes import bot_context, example_config
from .test_boardpr import GH, TABLE, Board, review

HEAD = "c" * 40
PLAN = json.dumps({"gaps": [{"slug": "frob", "supplier": "LambdaRings", "needs": "a Frobenius lift"}]})


def live_flow():
    """Board pull request #12 at HEAD with REV-PKG-Foo~2 open, configured as the live board is."""
    cfg = example_config()
    cfg.review.separation, cfg.review.comment_reviewers, cfg.board.bot_logins = "agent", ["roed-math"], ["board-bot"]
    rev = new_job("REV-PKG-Foo~2", independent_of=["PKG-Foo", "FIX-PKG-Foo~2"],
                  inputs={"board_pr": 12, "operator": "alice", "after_comment": 0})
    rev.number, rev.state = 5, "available"
    board, gh = Board([rev]), GH(HEAD)
    gh.pr["labels"] = [{"name": "needs-review"}]
    gh.raw_file = lambda repo, path, ref="": PLAN if path.endswith("/design/plan.json") and ref == HEAD else "# Foo\n"
    return bot_context(cfg=cfg, gh=gh, board=board, operator="board-bot"), board, gh, rev


def not_counted(gh):
    return [c["body"] for c in gh.comments if c["body"].startswith("Not counted")]


def red_team(round_, pass_, agent, body=TABLE):
    return prreviews.redteam_head(round_, pass_, agent, HEAD) + "\n\nVerdict: accepted\n\n" + body


def test_an_acceptance_with_a_blocking_finding_and_no_evidence_is_not_counted():
    ctx, board, gh, rev = live_flow()
    gh.say("roed-math", prreviews.review_head("chatgpt", HEAD) + "\n\nVerdict: accepted\n\n1. **F01** (P1) false\n")
    assert boardpr.scan(ctx, yes=True) == [] and rev.state == "available" and board.find("RT-Foo~1a") is None
    [note] = not_counted(gh)
    assert note.startswith("Not counted for `REV-PKG-Foo~2`: @roed-math's review: it accepts with blocking findings "
                           "(F01 (P1)): an acceptance leaves P3 notes only; the report has no adversarial table")
    assert boardpr.scan(ctx, yes=True) == [] and len(not_counted(gh)) == 1             # answered once
    # The browser workflow itself still works: an acceptance with its table and only P3 notes.
    gh.say("roed-math", prreviews.review_head("chatgpt", HEAD) + "\n\nVerdict: accepted\n\n1. **F01** (P3) a locator\n\n"
           + TABLE)
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: REV-PKG-Foo~2 accepted; created RT-Foo~1a, RT-Foo~1b")
    record = rev.inputs["accepted_review"]
    assert (record["status"], record["findings"], record["blocking"]) == ("accepted", 1, 0)


def test_each_red_team_pass_counts_only_from_its_family_of_models():
    ctx, board, gh, _rev = live_flow()
    gh.say("roed-math", review("chatgpt", HEAD, "accepted"))
    boardpr.scan(ctx, yes=True)
    rt_a, rt_b = board.find("RT-Foo~1a"), board.find("RT-Foo~1b")
    assert (rt_b.requires_agent, rt_b.requires_model) == ("claude", "fable")
    # Both passes signed by OpenAI's family (the reproduction): pass b is not counted, nor a pass with no table.
    gh.say("roed-math", red_team(1, "a", "chatgpt"))
    gh.say("roed-math", red_team(1, "b", "codex"))
    gh.say("roed-math", red_team(1, "b", "claude", body="Nothing to report.\n"))
    assert boardpr.scan(ctx, yes=True) == ["#12: RT-Foo~1a done; created nothing"] and rt_b.state != "done"
    signed, untabled = not_counted(gh)
    assert ("it is signed `codex`, but this pass is for the family of `claude` (sign `claude`, `fable`, `opus`, "
            "`sonnet`)") in signed
    assert "a pass with no finding carries its evidence" in untabled
    # A worker of that family on another model is not counted either, and its job goes back to available.
    rt_b.state, board.claimed_by[rt_b.id] = "submitted", "bob"
    gh.say("bob", prreviews.render(prreviews.redteam_head(1, "b", "claude", HEAD), "accepted", TABLE,
                                   {"job": rt_b.id, "worker": "bob-1", "agent": "claude", "model": "opus"}))
    assert boardpr.scan(ctx, yes=True) == [] and rt_b.state == "available"
    assert "its worker ran `opus`, but this pass requires the model `fable`" in not_counted(gh)[-1]
    # The listed login signing a name of the pass's family attests that family; its model is not checked.
    gh.say("roed-math", red_team(1, "b", "fable"))
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: RT-Foo~1b done; created FIX-RT-Foo~1")
    assert rt_a.state == rt_b.state == "done"


def test_an_acceptance_is_conditional_only_on_gaps_the_plan_at_the_head_declares():
    def conditional(gaps):
        return review("chatgpt", HEAD, "accepted").replace(
            "Verdict: accepted\n", "Verdict: accepted\nConditional on the announced gaps: " + gaps + "\n")
    ctx, _board, gh, rev = live_flow()
    gh.say("roed-math", conditional("`frob`, `lift`"))
    assert boardpr.scan(ctx, yes=True) == []
    assert "it is conditional on gaps the plan at this head does not declare: `lift`" in not_counted(gh)[0]
    # A plan GitHub did not serve is read again by the next scan, unanswered; a missing plan declares nothing.
    gh.say("roed-math", conditional("`frob`"))
    served = gh.raw_file
    gh.raw_file = lambda repo, path, ref="": (_ for _ in ()).throw(GHError("gh api: HTTP 502: Bad Gateway"))
    assert boardpr.scan(ctx, yes=True) == [] and len(not_counted(gh)) == 1
    gh.raw_file = lambda repo, path, ref="": (_ for _ in ()).throw(GHError("gh api: Not Found (HTTP 404)"))
    assert boardpr.declared_gaps(ctx, "Foo", HEAD) == set()
    gh.raw_file = served
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: REV-PKG-Foo~2 accepted")
    assert rev.inputs["accepted_review"]["conditional_gaps"] == ["frob"]


def test_a_browser_review_under_its_own_login_counts_only_under_another_agents_name():
    ctx, board, gh, rev = live_flow()
    board.authors = lambda ids, jobs: {"PKG-Foo": [{"login": "roed-math", "agent": "codex", "model": "gpt-6.1-sol"}]}
    gh.say("roed-math", review("codex", HEAD, "accepted"))        # names no model: the same agent is not independent
    assert boardpr.scan(ctx, yes=True) == [] and rev.state == "available"
    assert "`roed-math` (codex) is not independent of its authors (separation by agent)" in not_counted(gh)[0]
    gh.say("roed-math", review("chatgpt", HEAD, "accepted"))      # the maintainer's browser model on its own packages
    assert boardpr.scan(ctx, yes=True)[0].startswith("#12: REV-PKG-Foo~2 accepted")


def test_the_contract_on_its_own():
    rt = new_job("RT-Foo~1b", requires_agent="claude", requires_model="fable")
    unrated = prreviews.parse(prreviews.review_head("chatgpt", HEAD) + "\n\nVerdict: needs_changes\n\n1. **F01** wrong\n")
    assert prreviews.contract_problems(unrated, new_job("REV-PKG-Foo")) == ["finding F01 names no severity (P1, P2 or P3)"]
    gaps = prreviews.parse(review("chatgpt", HEAD, "accepted") + "\nConditional on the announced gaps: `lift`\n")
    assert prreviews.contract_problems(gaps, new_job("REV-PKG-Foo")) == []                 # declared None: unchecked
    worker = prreviews.parse(prreviews.render(prreviews.redteam_head(1, "b", "claude", HEAD), "needs_changes",
                                              "## R01 (P3) — a locator\nbody\n", {"agent": "claude", "model": "fable"}))
    assert worker.provenance == ("claude", "fable") and prreviews.contract_problems(worker, rt) == []
    assert claims.family_names("chatgpt") == ["chatgpt", "codex", "gpt", "openai"]


def test_the_brief_tells_a_browser_model_which_names_each_pass_signs_with():
    ctx, _board, gh, _rev = live_flow()
    text = boardpr.brief(ctx, new_job("RT-Foo~1a", requires_agent="codex"), gh.pr)
    assert "pass a is for the family of `codex` (sign `codex` or `chatgpt` or `gpt` or `openai`)" in text
    assert "pass b is for the family of `claude` (sign `claude` or `fable` or `opus` or `sonnet`)" in text
    assert "for example `chatgpt`" in boardpr.brief(ctx, new_job("REV-PKG-Foo~2"), gh.pr)


# --- the same inputs through a worker's lane and a browser comment -------------------------------------

@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A review or red-team worker on board pull request #12 at HEAD: the board checkout holds the
    package and its plan (declaring `frob`)."""
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    checkout = tmp_path / "board"
    pkg = checkout / "packages" / "Foo"
    (pkg / "design").mkdir(parents=True)
    (pkg / "reports").mkdir()
    for name in ("README.md", "Suggested.lean", "metadata.toml"):
        (pkg / name).write_text("x\n")
    (pkg / "design" / "plan.json").write_text(PLAN)
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")):
        subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)

    def workspace(job):
        return base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "s", board=checkout, content=tmp_path,
                              workdir=checkout, report=pkg / "reports" / f"{job.id}.md", branch="b", job_id=job.id,
                              extra={"head": HEAD, "pr": 12})
    ctx = SimpleNamespace(cfg=example_config(), agent="claude", model="fable", operator="alice", worker_id="alice-1")
    return ctx, pkg, workspace


def browser_outcome(head_line, verdict, report, conditional=None):
    """What the board does with the same review written by the listed login's browser model."""
    ctx, _board, gh, _rev = live_flow()
    gh.say("roed-math", prreviews.render(head_line, verdict, report, {}, conditional))
    return bool(boardpr.scan(ctx, yes=True)), not_counted(gh)


REVIEWS = {   # verdict file (status, blocking, conditional gaps), report, whether it counts
    "accepted with a P1 the verdict file does not count": (("accepted", 0, []), "## R01 (P1) — false\nbody\n\n" + TABLE,
                                                           False),
    "accepted without evidence": (("accepted", 0, []), "The roadmap is sound.\n", False),
    "accepted on an undeclared gap": (("accepted", 0, ["lift"]), TABLE, False),
    "accepted on a declared gap, with evidence": (("accepted", 0, ["frob"]), TABLE, True),
    "needs changes": (("needs_changes", 1, []), "## R01 (P1) — false\nbody\n", True),
}


@pytest.mark.parametrize("case", REVIEWS)
def test_a_worker_review_and_a_browser_review_meet_the_same_contract(worker, case):
    ctx, pkg, workspace = worker
    (status, blocking, gaps), report, counts = REVIEWS[case]
    job = new_job("REV-PKG-Foo~2", inputs={"board_pr": 12})
    ws = workspace(job)
    lane = lane_for(job)
    verdict = {"status": status, "reviewer": f"independent-review-{job.id}", "findings": blocking, "blocking": blocking}
    (pkg / "review.json").write_text(json.dumps({"review": {**verdict, **({"conditional_gaps": gaps} if gaps else {})}}))
    ws.report.write_text("# Review of Foo (package, round 2)\n\n" + report)
    assert (lane.check(job, ctx, ws) == []) is counts
    counted, notes = browser_outcome(prreviews.review_head("chatgpt", HEAD), status, report, gaps)
    assert counted is counts and bool(notes) is not counts


PASSES = {   # report body, the worker's model, whether it counts
    "no finding, no table": ("Nothing found.\n", "fable", False),
    "no finding, with its table": (TABLE, "fable", True),
    "a finding, no table": ("## R01 (P2) — an overclaim\nbody\n", "fable", True),
    "a worker on another model": (TABLE, "opus", False),
}


@pytest.mark.parametrize("case", PASSES)
def test_a_worker_pass_and_a_browser_pass_meet_the_same_contract(worker, case):
    ctx, _pkg, workspace = worker
    body, model, counts = PASSES[case]
    job = new_job("RT-Foo~1b", requires_agent="claude", requires_model="fable", inputs={"board_pr": 12})
    ws = workspace(job)
    ws.report.write_text(f"# Red-team review of #12 Foo at {HEAD[:7]} (pass b (claude))\n\n**Verdict:** x\n\n" + body)
    assert (lane_for(job).check(job, SimpleNamespace(**{**vars(ctx), "model": model}), ws) == []) is counts
    if model != "fable":
        return          # a browser comment carries no model: the worker's marker is what is checked
    ctx_, board, gh, _rev = live_flow()
    gh.say("roed-math", review("chatgpt", HEAD, "accepted"))
    boardpr.scan(ctx_, yes=True)
    verdict = "needs_changes" if prreviews.report_findings(body)[0] else "accepted"
    gh.say("roed-math", prreviews.render(prreviews.redteam_head(1, "b", "claude", HEAD), verdict, body, {}))
    out = boardpr.scan(ctx_, yes=True)
    assert (board.find("RT-Foo~1b").state == "done") is counts and bool(out) is counts
