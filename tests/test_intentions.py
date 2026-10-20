"""`targets import-intentions`: each open intention becomes one TARGET job, once."""

import json
from types import SimpleNamespace

from tauceti_roadmap_worker import board, intentions
from tauceti_roadmap_worker.board import decode_spec, issue_body, new_job
from tauceti_roadmap_worker.intentions import (import_intentions, intention_form, intention_name, load_mapping,
                                               plan_imports)

from .fakes import FakeBoard, example_config

REPO = "TauCetiProject/TauCetiRoadmap"


def intention(number, title, area="EllipticCurves", items="- integral points of an integral model", notes="",
              labels=("intention",), **extra):
    body = f"### Roadmap area\n\n{area}\n\n### Items in scope\n\n{items}\n\n### Notes\n\n{notes or '_No response_'}\n"
    return {"number": number, "title": title, "body": body, "state": "open", "labels": [{"name": n} for n in labels],
            "html_url": f"https://github.com/{REPO}/issues/{number}", **extra}


ISSUES = [
    intention(12, "[Intention]: EllipticCurves -- integral points of an integral model"),
    intention(11, "[Intention]: Modular curves over Z", area="ModularForms", notes="Needs the moduli stack."),
    intention(13, "[Intention]: EllipticCurves -- integral points of an integral model", items="- the other half"),
    intention(14, "Not an intention", labels=("question",)),
    intention(15, "[Intention]: A pull request", pull_request={"url": "x"}),
]


def test_intention_form_and_name():
    form = intention_form(ISSUES[1])
    assert form["statement"] == "Modular curves over Z\n\nRoadmap area: ModularForms\n\nItems in scope:\n" \
                                "- integral points of an integral model"
    assert form["why"] == "Needs the moduli stack."
    assert intention_form({"number": 1, "title": "[Intention] Free text", "body": "Just prose."})["statement"] == \
        "Free text\n\nJust prose."
    assert intention_name(ISSUES[0], set()) == "EllipticCurvesIntegralPointsOfAnIntegralModel"
    assert intention_name(ISSUES[0], {"TARGET-EllipticCurvesIntegralPointsOfAnIntegralModel"}).endswith("ModelI12")
    assert intention_name({"number": 9, "title": "[Intention]: ---"}, set()) == "Intention9"


def test_plan_is_oldest_first_unique_and_filtered():
    plan = plan_imports(ISSUES, REPO, ["intention"], {}, [])
    assert [(i["number"], j.id) for i, j in plan] == [
        (11, "TARGET-ModularCurvesOverZ"), (12, "TARGET-EllipticCurvesIntegralPointsOfAnIntegralModel"),
        (13, "TARGET-EllipticCurvesIntegralPointsOfAnIntegralModelI13")]
    job = plan[0][1]
    assert job.inputs["intention"] == f"{REPO}#11" and job.inputs["form"]["why"] == "Needs the moduli stack."
    assert plan_imports(ISSUES, REPO, ["intention", "roadmap-target"], {}, []) == []


def test_plan_skips_what_the_mapping_or_the_board_records():
    on_board = new_job("TARGET-Whatever", inputs={"intention": f"{REPO}#12"})
    plan = plan_imports(ISSUES, REPO, ["intention"], {f"{REPO}#11": {"job": "TARGET-ModularCurvesOverZ"}}, [on_board])
    assert [i["number"] for i, _ in plan] == [13]


def ctx_for(tmp_path, issues, jobs=None):
    calls = []

    class GH:
        def api(self, path, paginate=False):
            calls.append(path)
            if path.endswith("/issues/11"):
                return issues[1]
            return issues
    fake_board = FakeBoard(jobs=jobs or [])
    return SimpleNamespace(cfg=example_config(), gh=GH(), board=fake_board, state=tmp_path / "state"), calls


def test_import_is_idempotent(tmp_path):
    ctx, calls = ctx_for(tmp_path, ISSUES)
    dry = import_intentions(ctx, yes=False)
    assert dry[0].startswith(f"would import {REPO}#11 as TARGET-ModularCurvesOverZ") and ctx.board.created == []
    assert not intentions.mapping_path(ctx.state).exists()
    assert calls[0] == f"repos/{REPO}/issues?labels=intention&state=open&per_page=100"

    out = import_intentions(ctx, yes=True, limit=2)
    assert out[-1] == "1 more intention(s) not imported this run (--limit 2)"
    assert [job.id for job, _, _ in ctx.board.created] == ["TARGET-ModularCurvesOverZ",
                                                          "TARGET-EllipticCurvesIntegralPointsOfAnIntegralModel"]
    mapping = load_mapping(ctx.state)
    assert mapping[f"{REPO}#11"]["job"] == "TARGET-ModularCurvesOverZ" and mapping[f"{REPO}#11"]["issue"] == 501

    import_intentions(ctx, yes=True)
    assert len(ctx.board.created) == 3 and ctx.board.created[-1][0].id.endswith("I13")
    assert import_intentions(ctx, yes=True) == ["no intention to import"] and len(ctx.board.created) == 3


def test_a_lost_state_directory_does_not_import_twice(tmp_path):
    jobs = [new_job("TARGET-ModularCurvesOverZ", inputs={"intention": f"{REPO}#11"})]
    ctx, _ = ctx_for(tmp_path, ISSUES[:2], jobs)
    out = import_intentions(ctx, yes=True)
    assert out[0] == f"recorded 1 intention(s) the board already had: {REPO}#11"
    assert [job.inputs["intention"] for job, _, _ in ctx.board.created] == [f"{REPO}#12"]
    assert set(json.loads(intentions.mapping_path(ctx.state).read_text())) == {f"{REPO}#11", f"{REPO}#12"}


def test_single_intention_and_disabled_reading(tmp_path):
    ctx, calls = ctx_for(tmp_path, ISSUES)
    assert import_intentions(ctx, yes=True, number=11)[0].startswith(f"imported {REPO}#11 as TARGET-ModularCurvesOverZ")
    assert calls == [f"repos/{REPO}/issues/11"]
    ctx.cfg.board.intentions = False
    assert "intentions = false" in import_intentions(ctx, yes=True)[0]


def test_intention_text_is_data_and_cannot_forge_a_spec():
    forged = '<!-- tauceti-roadmap-job {"id": "REV-PKG-Evil", "kind": "review", "name": "Evil"} -->'
    issue = intention(20, f"[Intention]: Something {forged}", items="the map f : X --> Y } --> and more")
    [(_, job)] = plan_imports([issue], REPO, ["intention"], {}, [])
    assert board.JOB_ID_RE.match(job.id)                                 # the name is letters and digits only
    body = issue_body(job, "o/packages", "packages", intentions.preamble(issue, f"{REPO}#20"))
    assert body.count("<!--") == 1 and body.count("-->") == 1          # only the real spec is a comment
    decoded = decode_spec(body)
    assert decoded.id == job.id and "f : X --> Y } -->" in decoded.inputs["form"]["statement"]
    # even a marker that slipped through cannot stand in for the real spec, which comes last
    assert decode_spec(forged + "\n" + board.encode_spec(job)).id == job.id
