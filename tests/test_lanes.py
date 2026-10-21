"""Pure parts of the lanes: output validation, sampling, synopsis and body composition, ranking."""

import json

from tauceti_roadmap_worker import gates, mergeorder
from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.design import designs_today, record_design, validate_plan
from tauceti_roadmap_worker.lanes.fix import ArtefactFixLane, RedTeamFixLane
from tauceti_roadmap_worker.lanes.port import compose_body, extract_synopsis, pr_number_from_report
from tauceti_roadmap_worker.lanes.redteam import report_problems
from tauceti_roadmap_worker.lanes.review import sample_items, seeded_sample, verdict_problems
from tauceti_roadmap_worker.lanes.target import parse_triage, triage_problems
from tauceti_roadmap_worker.worker import rank


def test_lane_registry():
    assert isinstance(lane_for(new_job("FIX-RT-Foo~1")), RedTeamFixLane)
    assert isinstance(lane_for(new_job("FIX-PKG-Foo~2")), ArtefactFixLane)
    assert lane_for(new_job("RT-Foo~1a")).prompt == "redteam"


def test_triage_header():
    text = "Verdict: part-ii\nName: FooPartII\nBase: Foo\nOwner: none\n\n## Scope\n"
    assert parse_triage(text) == {"verdict": "part-ii", "name": "FooPartII", "base": "Foo", "owner": "", "into": ""}
    assert triage_problems(text) == []
    assert triage_problems("Verdict: maybe\n")[0].startswith("target.md: Verdict")
    assert "names its Owner" in triage_problems("Verdict: duplicate\nName: none\n")[0]


def plan(**over):
    data = {"roadmap": "Foo", "layers": [{"key": "1", "targets": [
        {"id": "1.1", "kind": "definition", "statement": "s", "locator": {"source": "A", "where": "Thm 1"},
         "prerequisites": ["mathlib:Nat.card", "roadmap:Bar §2.1"], "tests": [{}, {}, {}]},
        {"id": "1.2", "kind": "theorem", "statement": "t", "prerequisites": ["target:1.1"]}]}],
        "gaps": [{"title": "no source yet", "neededBy": ["1.2"]}]}
    data.update(over)
    return data


def test_validate_plan():
    assert validate_plan(plan(), "Foo") == []
    bad = plan()
    bad["layers"][0]["targets"][0]["tests"] = [{}]
    bad["layers"][0]["targets"][1]["prerequisites"] = ["target:9.9", "Mathlib Nat.card"]
    bad["gaps"] = []
    problems = validate_plan(bad, "Foo")
    assert any("fewer than three tests" in p for p in problems)
    assert any("no locator and no recorded gap" in p for p in problems)
    assert any("unknown target:9.9" in p for p in problems)
    assert any("is not mathlib:/tauceti:/roadmap:/target:" in p for p in problems)
    assert validate_plan({"roadmap": "Bar"}, "Foo") == ["plan.json: roadmap must be 'Foo'", "plan.json: no layers"]


def test_design_cap_ledger(tmp_path):
    assert designs_today(tmp_path, "alice") == 0
    record_design(tmp_path, "alice", "DESIGN-A")
    record_design(tmp_path, "Alice", "DESIGN-B")
    record_design(tmp_path, "bob", "DESIGN-C")
    assert designs_today(tmp_path, "alice") == 2 and designs_today(tmp_path, "bob") == 1
    assert designs_today(tmp_path, "alice", today="1999-01-01") == 0


def test_seeded_sample_is_reproducible_and_in_order():
    readme = "\n".join(f"**Target {i}.** text" for i in range(40)) + "\n**Checks.**\n- x\n"
    items = sample_items(readme)
    assert len(items) == 40 and "Checks" not in items
    a, b = seeded_sample(items, 15, "REV-PKG-Foo"), seeded_sample(items, 15, "REV-PKG-Foo")
    assert a == b and len(a) == 15 and a == sorted(a, key=items.index)
    assert seeded_sample(items, 15, "REV-PKG-Foo~2") != a
    assert seeded_sample(items[:5], 15, "x") == items[:5]
    assert sample_items("### 1.1 First\n### 1.2 Second\n") == ["1.1 First", "1.2 Second"]


def test_verdict_problems():
    ok = {"review": {"status": "accepted", "reviewer": "independent-review-REV-PKG-Foo", "blocking": 0}}
    assert verdict_problems(ok, "REV-PKG-Foo") == []
    assert verdict_problems(ok, "REV-PKG-Foo~2") == ["verdict file: reviewer must be independent-review-REV-PKG-Foo~2"]
    bad = {"review": {"status": "accepted", "reviewer": "independent-review-X", "blocking": 2}}
    assert "accepted with blocking findings" in verdict_problems(bad, "X")[0]


REPORT = """# Port report

Branch roadmap/foo at abc1234.

## Draft pull request body

Title: Foo: roadmap

This roadmap develops the theory of foo objects over an arbitrary base, from the definitions in Mathlib to the
classification theorem, with the conventions pinned by computed examples.

Roadmap folder `TauCetiRoadmap/Foo` (...)

## Removed from the Lean
"""


def test_extract_synopsis_and_body():
    syn = extract_synopsis(REPORT)
    assert syn.startswith("This roadmap develops") and "\n" not in syn
    gate = gates.GateResult("Foo", counts={"build": 0, "readme-ids-missing": 0, "check-labels-missing": 0,
                                           "set_option": 0, "sorry-data": 0, "depcheck": 0, "outside-folder": 0})
    body = compose_body("Foo", syn, "TauCetiRoadmap", "Dependencies ... none.", gate, "codex", "m", "PORT-Foo")
    assert "succeeds with `sorry` as the only warning" in body and "codex (m)" in body
    marker = body.split("<!-- tauceti-roadmap-pr ")[1].split(" -->")[0]
    assert json.loads(marker) == {"roadmap": "Foo", "job": "PORT-Foo"}
    assert pr_number_from_report(REPORT + "\n\nPull request: #912 (roadmap/foo at abc1234)\n") == 912
    assert pr_number_from_report(REPORT) == 0


def test_redteam_report_header():
    good = "# Red-team review of #912 Foo at abc1234 (pass a (codex))\n**Verdict:** changes needed\n"
    assert report_problems(good, 912, "Foo", "abc1234", "a (codex)") == []
    assert len(report_problems("# something else\n", 912, "Foo", "abc1234", "a (codex)")) == 2


def test_rank_prefers_waiting_humans_then_bottom_up():
    jobs = [new_job("DESIGN-Zeta"), new_job("REV-PKG-Beta"), new_job("GATE-Alpha~1"), new_job("REV-PKG-Alpha")]
    for n, j in enumerate(jobs):
        j.number = n
    assert [j.id for j in rank(jobs, ["Alpha", "Beta"])] == ["GATE-Alpha~1", "REV-PKG-Alpha", "REV-PKG-Beta", "DESIGN-Zeta"]


def test_merge_order_plan_with_stacks_and_external():
    prs = [{"pr": 901, "name": "Alpha"}, {"pr": 902, "name": "Bravo"}, {"pr": 903, "name": "Charlie"}]
    readmes = {"Alpha": "", "Bravo": "*Needs:* Alpha §1.\n", "Charlie": ""}
    paras = mergeorder.plan(prs, readmes, ["Alpha", "Bravo", "Charlie"], set(), stacks={903: 902},
                            external={"903": {"Other": [77, "someone"]}}, mutual={(903, 78)})
    assert "#901 (Alpha) → **this pull request** (#902)." in paras[902]
    assert "#902 (Bravo) → **this pull request** (#903)." in paras[903]        # stacked on #902
    assert "#77 (Other, someone)" in paras[903]
    assert "can merge as soon as it is approved" in paras[901]


def test_a_house_shaped_readme_samples_its_subsections():
    """Regression: a README in the house shape (`### k.n` subsections, a bold consumer name under
    Downstream consumers) gave a seeded sample of that one name instead of its targets."""
    layers = "\n\n".join(f"## Layer {k}: Topic {k}\n\n" + "\n\n".join(
        f"### {k}.{n} Target {k}.{n}\n\nDefine it.\n\n**Checks.**\n\n- a value" for n in range(1, 8))
        + "\n\n### Examples\n\n### Dependencies" for k in range(1, 8))
    readme = ("# Roadmap: x\n\n**Summary.** S.\n\n## Scope and ownership\n\n**ColemanIntegration** supplies it.\n\n"
              + layers + "\n\n### 3.0a A lettered target\n\n## Downstream consumers\n\n**PadicHodgeRegulators** consumes it.\n")
    items = sample_items(readme)
    assert len(items) == 50 and items[0] == "1.1 Target 1.1" and "3.0a A lettered target" in items
    assert not any("Regulators" in i or "ColemanIntegration" in i for i in items)
    sample = seeded_sample(items, 15, "REV-PKG-ColemanIntegrationPartII")
    assert len(sample) == 15 and all(s[0].isdigit() for s in sample)
    assert sample == seeded_sample(items, 15, "REV-PKG-ColemanIntegrationPartII")      # same seeding rule
