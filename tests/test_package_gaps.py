"""A supplier gap can be declared at the package stage (regression: FIX-PKG-CohomologyComparisonsPartII~3,
board #518, was parked because the review's "a consumed contract is not a declared gap" had been
marked not actionable: "gap registry is outside permitted files")."""

import json
from types import SimpleNamespace

from tauceti_roadmap_worker import board as board_mod
from tauceti_roadmap_worker import intake, prompts
from tauceti_roadmap_worker.board import allowed_paths, encode_submission, new_job, plan_change_problems
from tauceti_roadmap_worker.lanes.package import gap_reference_problems

from .fakes import FakeBoard, bot_context, example_config
from .test_boardpr import GH, Board

PLAN = {"roadmap": "Foo", "layers": [{"key": "1", "targets": [{"id": "1.1"}]}],
        "gaps": [{"slug": "frobenius-lift", "needs": "a Frobenius lift", "supplier": "LambdaRings", "section": "§3"}]}
ADDED = {**PLAN, "gaps": PLAN["gaps"] + [{"slug": "derived-completion", "needs": "derived completion of complexes",
                                           "supplier": "DerivedModuleAlgebra", "section": "§1.3"}]}


def test_the_package_stage_may_add_gaps_and_nothing_else():
    for job_id, inputs in (("PKG-Foo", {}), ("FIX-PKG-Foo~2", {}), ("FIX-RT-Foo~1", {"board_pr": 12})):
        job = new_job(job_id, inputs=inputs)
        assert "packages/Foo/design/plan.json" in allowed_paths(job, "packages") and board_mod.gaps_only(job)
    design_fix = new_job("FIX-DESIGN-Foo~2")
    assert "packages/Foo/design/plan.json" in allowed_paths(design_fix, "packages") and not board_mod.gaps_only(design_fix)
    assert not board_mod.gaps_only(new_job("FIX-RT-Foo~1", inputs={"pr": 905}))    # a roadmap pull request: report only
    path = "packages/Foo/design/plan.json"
    assert plan_change_problems(path, json.dumps(PLAN), json.dumps(ADDED)) == []
    retargeted = {**ADDED, "layers": []}
    assert plan_change_problems(path, json.dumps(PLAN), json.dumps(retargeted)) == [
        "packages/Foo/design/plan.json: at the package stage only its top-level `gaps` list may change"]
    assert "`gaps` is a list of entries with a `slug`" in plan_change_problems(path, json.dumps(PLAN),
                                                                             json.dumps({**PLAN, "gaps": [{}]}))[0]


def test_a_readme_gap_must_be_declared_in_the_plan(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN))
    readme = ("### From TauCetiRoadmap.LambdaRings\n\nWhat LambdaRings must provide (gap:frobenius-lift): a Frobenius lift.\n"
              "The comparison is gap:derived-completion.\n")
    missing = ("README names gap:derived-completion, which design/plan.json `gaps` does not declare (add the entry "
               "there, with `needs`, `supplier` and `section`)")
    assert gap_reference_problems(readme, plan) == [missing]
    plan.write_text(json.dumps(ADDED))
    assert gap_reference_problems(readme, plan) == []
    assert gap_reference_problems(readme, tmp_path / "absent.json") == []


class PackageGH(GH):
    """A package fix's submission pull request whose plan.json differs from its base."""

    def __init__(self, base_plan, head_plan):
        super().__init__("e" * 40)
        self.pr["body"] = "Refs #8\n\n" + encode_submission("FIX-PKG-Foo~2", "w2", "bob", "codex", "gpt-6.1-sol")
        self.pr["base"] = {"sha": "b" * 40}
        self.pr["user"] = {"login": "bob"}
        self.plans = {"b" * 40: json.dumps(base_plan), "e" * 40: json.dumps(head_plan)}

    def api(self, path, paginate=False):
        if "/pulls/12/files" in path:
            return [{"filename": "packages/Foo/design/plan.json", "status": "modified"},
                    {"filename": "packages/Foo/reports/FIX-PKG-Foo~2.md", "status": "added"}]
        return super().api(path, paginate)

    def raw_file(self, repo, path, ref=""):
        return self.plans.get(ref, "") if path.endswith("plan.json") else "# report\n"


def test_the_intake_takes_gaps_and_refuses_other_plan_changes():
    cfg = example_config()
    cfg.board.bot_logins = ["board-bot"]
    fix = new_job("FIX-PKG-Foo~2", inputs={"review": "REV-PKG-Foo"})
    fix.number, fix.state = 8, "submitted"
    line = intake.intake(bot_context(cfg=cfg, gh=PackageGH(PLAN, ADDED), board=Board([fix])), 12, yes=True)
    assert "open for review" in line                                          # gaps added: taken
    line = intake.intake(bot_context(cfg=cfg, gh=PackageGH(PLAN, {**ADDED, "layers": []}), board=Board([fix])), 12, yes=True)
    assert "not merged" in line and "only its top-level `gaps` list may change" in line


def test_gaps_added_by_the_package_are_announced_on_accept():
    supplier = new_job("PKG-DerivedModuleAlgebra")
    supplier.number, supplier.state = 40, "submitted"
    pkg = new_job("PKG-Foo")
    pkg.number = 9
    plans = {"": json.dumps(PLAN), "h" * 40: json.dumps(ADDED)}
    gh = SimpleNamespace(raw_file=lambda repo, path, ref="": plans.get(ref, ""))
    board = FakeBoard(jobs=[supplier, pkg])
    note = intake.announce_package_gaps(bot_context(gh=gh, board=board), pkg, board.jobs, "h" * 40)
    assert note == "1 supplier gap(s) announced"
    [(number, body)] = board.posted
    assert number == 40 and "gap `derived-completion` of `PKG-Foo`" in body and "frobenius-lift" not in body


def test_parked_repairs_the_rule_resolves_are_noted_once():
    stuck = ("Parked for a human: ...\n\n- `F01` (P2) A consumed contract is not a declared gap: re-raises `F01`, which "
             "the repair marked not actionable (gap registry is outside permitted files)\n")
    other = stuck.replace("gap registry is outside permitted files", "the supplier must decide")
    fixes = []
    for n, body in ((518, stuck), (519, other)):
        job = new_job(f"FIX-PKG-Pkg{n}~3")
        job.number, job.state = n, "blocked"
        fixes.append((job, body))

    class Issues:
        def api(self, path, paginate=False):
            return {"body": {j.number: b for j, b in fixes}[int(path.rsplit("/", 1)[1])]}

    class Blocked(FakeBoard):
        def list_jobs(self, states=("available",), kinds=None):
            return [j for j, _b in fixes]
    board = Blocked()
    ctx = bot_context(gh=Issues(), board=board)
    assert intake.note_gap_registry_blocks(ctx) == ["FIX-PKG-Pkg518~3: noted as resolvable by package-stage gaps"]
    assert board.posted[0][0] == 518 and "move this job to state:available" in board.posted[0][1]
    assert intake.note_gap_registry_blocks(ctx) == []                          # once


def test_the_briefs_say_how_to_declare_a_package_gap():
    package = prompts.template("package")
    assert "`__PACKAGE__/design/plan.json`, its top-level `gaps` list only" in package
    assert "What <Supplier> must provide (gap:<slug>)" in package
    assert "counts as declared when the plan's `gaps` has the same" in prompts.template("review")
    assert "What <Supplier> must provide (gap:<slug>)" in prompts.template("fix")
