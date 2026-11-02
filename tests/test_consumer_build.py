"""A roadmap's targeted build includes its consumers in the tree: the modules outside its folder that
import one of its modules. A change that passes its own build but renames a declaration a sibling
imports is a gate failure, not a surprise on main."""

from tauceti_roadmap_worker import gates


def _lean(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(tmp_path):
    root = tmp_path / "tree"
    _lean(root, "TauCetiRoadmap/LocalFields/Suggested.lean", "import Mathlib\n\ntheorem a : True := trivial\n")
    _lean(root, "TauCetiRoadmap/LocalFields/Extra.lean", "import Mathlib\nimport TauCetiRoadmap.LocalFields.Suggested\n")
    _lean(root, "TauCetiRoadmap/MassFormula/Suggested.lean",
          "import Mathlib\nimport TauCetiRoadmap.LocalFields.Suggested\n\ntheorem b : True := trivial\n")
    _lean(root, "TauCetiRoadmap/ClassFieldTheory/Suggested.lean",
          "import Mathlib\npublic import TauCetiRoadmap.LocalFields.Extra\n")
    _lean(root, "TauCetiRoadmap/Unrelated/Suggested.lean",
          "import Mathlib\n-- import TauCetiRoadmap.LocalFields.Suggested (a comment, not an import)\n"
          "import TauCetiRoadmap.LocalFieldsPartII.Suggested\n")
    _lean(root, "TauCetiRoadmap/LocalFieldsPartII/Suggested.lean", "import Mathlib\n")
    _lean(root, "TauCetiRoadmap/Family/Index.lean", "import Mathlib\nimport TauCetiRoadmap.Family.Member.Suggested\n")
    _lean(root, "TauCetiRoadmap/Family/Member/Suggested.lean", "import Mathlib\n")
    return root


def test_consumers_are_the_importers_outside_the_folder(tmp_path):
    root = _tree(tmp_path)
    assert gates.consumer_modules(root, "TauCetiRoadmap", "LocalFields") == [
        "TauCetiRoadmap.ClassFieldTheory.Suggested", "TauCetiRoadmap.MassFormula.Suggested"]
    # the roadmap's own second file, a commented-out import and a roadmap with a longer name are not consumers
    assert gates.consumer_modules(root, "TauCetiRoadmap", "Unrelated") == []
    assert gates.consumer_modules(root, "TauCetiRoadmap", "LocalFieldsPartII") == ["TauCetiRoadmap.Unrelated.Suggested"]


def test_nested_roadmap_consumers_include_the_family_index(tmp_path):
    root = _tree(tmp_path)
    assert gates.consumer_modules(root, "TauCetiRoadmap", "Family/Member") == ["TauCetiRoadmap.Family.Index"]
    assert gates.consumer_modules(root, "TauCetiRoadmap", "Family") == []


def test_build_targets_lead_with_the_roadmap_module(tmp_path):
    root = _tree(tmp_path)
    assert gates.build_targets(root, "TauCetiRoadmap", "LocalFields") == (
        "TauCetiRoadmap.LocalFields.Suggested TauCetiRoadmap.ClassFieldTheory.Suggested "
        "TauCetiRoadmap.MassFormula.Suggested")
    assert gates.build_targets(root, "TauCetiRoadmap", "Nowhere") == "TauCetiRoadmap.Nowhere.Suggested"
    assert gates.build_targets(tmp_path / "missing", "TauCetiRoadmap", "X") == "TauCetiRoadmap.X.Suggested"
    assert "lake build {module}".format(module=gates.build_targets(root, "TauCetiRoadmap", "LocalFields")).startswith(
        "lake build TauCetiRoadmap.LocalFields.Suggested TauCetiRoadmap.")


def test_consumer_warnings_are_not_the_roadmaps_problem():
    text = ("warning: TauCetiRoadmap/ProfiniteCohomology/Suggested.lean:567:0: unused section variable\n"
            "warning: TauCetiRoadmap/LocalFields/Suggested.lean:12:0: unused variable `x`\n"
            "warning: TauCetiRoadmap/LocalFields/Suggested.lean:20:8: declaration uses `sorry`\n"
            "error: TauCetiRoadmap/MassFormula/Suggested.lean:30:2: unknown identifier `normGroup`\n")
    out = gates.build_output_problems(text, 0, folder="TauCetiRoadmap/LocalFields")
    assert any("normGroup" in p for p in out) and not any("unused section" in p for p in out)
    # without an error, only the roadmap's own warning survives
    out = gates.build_output_problems("\n".join(text.splitlines()[:3]), 0, folder="TauCetiRoadmap/LocalFields")
    assert out == [p for p in out if "LocalFields" in p] and len(out) == 1
    # no folder: the old behaviour
    assert len(gates.build_output_problems("\n".join(text.splitlines()[:3]), 0)) == 2


def test_gate_problems_pass_a_nested_folder_into_the_gate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tauceti_roadmap_worker.lanes import gate as gate_lane
    seen = {}

    def run_gate(worktree, name, opts, base="", build_cmd=""):
        seen["folder"] = opts.folder
        r = gates.GateResult(name)
        return r
    monkeypatch.setattr(gates, "run_gate", run_gate)
    monkeypatch.setattr(gate_lane, "fetch_pr_base", lambda ctx, ws: "origin/main")
    monkeypatch.setattr(gate_lane, "worktree_elaboration_problems", lambda ctx, ws, folder: [])
    ctx = SimpleNamespace(cfg=SimpleNamespace(gates=SimpleNamespace(build="")))
    ws = SimpleNamespace(workdir=tmp_path)
    gate_lane.gate_problems(ctx, ws, "SemisimpleAlgebras", "RepresentationTheory/SemisimpleAlgebras", gates.GateOptions())
    assert seen["folder"] == "RepresentationTheory/SemisimpleAlgebras"
    gate_lane.gate_problems(ctx, ws, "Foo", "Foo", gates.GateOptions())
    assert seen["folder"] == ""
