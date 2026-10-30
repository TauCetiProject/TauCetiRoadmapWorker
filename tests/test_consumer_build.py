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
