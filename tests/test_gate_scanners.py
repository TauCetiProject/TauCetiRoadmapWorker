import re
import textwrap

from tauceti_roadmap_worker import gates
from tauceti_roadmap_worker.gates import GateOptions, scan_folder

from .fakes import example_config

CLEAN_README = textwrap.dedent("""\
    # Roadmap: toy groups

    **Summary.** Builds `Toy.order` over Mathlib's `Group` and proves `Toy.order_one`.

    ## Layer 1: orders

    ### 1.1 The order

    Define `Toy.order` as the cardinality of the carrier. Prove `Toy.order_one` (Author, *Example Book*, Thm I.2.1, p. 23).
    The value is `Nat.card G` written in a code span, and `f $ x` in code is fine.

    **Checks.**
    - the trivial group has order one (*order-trivial*);

    ### Dependencies

    Mathlib `Nat.card`.
    """)

CLEAN_LEAN = textwrap.dedent("""\
    import Mathlib

    /-! # Toy: representative target signatures

    The roadmap is README.md. We never write set_option here. -/

    namespace TauCetiRoadmap.Toy

    /-! ## Layer 1: orders -/

    /-- The order of a group. -/
    noncomputable def Toy.order (G : Type*) [Group G] : ℕ := Nat.card G

    -- a comment that mentions sorry and set_option
    theorem Toy.order_one : Toy.order PUnit = 1 := sorry

    example : Toy.order PUnit = 1 := by simp [Toy.order] -- order-trivial

    end TauCetiRoadmap.Toy
    """)


def opts(**kw):
    return GateOptions(content_repo="TauCetiProject/TauCetiRoadmap", **kw)


def test_clean_fixture_passes():
    r = scan_folder("Toy", CLEAN_README, CLEAN_LEAN, opts())
    assert r.ok, r.details
    assert r.exit_code == 0 and r.summary().endswith("OK")


def test_blob_links():
    pinned = "see https://github.com/TauCetiProject/TauCetiRoadmap/blob/0123abc/TauCetiRoadmap/X/README.md"
    floating = "see https://github.com/TauCetiProject/TauCetiRoadmap/blob/main/README.md"
    assert len(gates.scan_blob_links(pinned, "TauCetiProject/TauCetiRoadmap")) == 1
    assert gates.scan_blob_links(floating, "TauCetiProject/TauCetiRoadmap") == []


def test_process_residue_and_pr_numbers():
    text = "Layer 2 (removed).\nThe packet says so far nothing.\nAs in pull request #812 and (#813).\n"
    residue = gates.scan_process_residue(text)
    assert [h.line for h in residue] == [1, 2, 2]
    assert len(gates.scan_pr_numbers(text)) == 2
    assert gates.scan_process_residue("Analytic continuation of L(s, χ).") == []


def test_latex_only_outside_code():
    text = "Prose with $x^2$ and \\(y\\).\n\n```text\n$ok$ \\(ok\\)\n```\n\nCode `f $ x` and `x_{i}`.\n\n    $indented code$\n"
    hits = gates.scan_latex(text)
    assert [h.line for h in hits] == [1, 1, 1]          # two $ and one \( on line 1, nothing in code
    assert gates.scan_tex_residue("bad x_{i} here\n`fine x_{i}`") and not gates.scan_tex_residue("`fine x_{i}`")
    # a list item's continuation is prose, not an indented code block
    assert gates.scan_latex("- item\n    continued with $x$\n")


def test_set_option_counts_code_only():
    lean = "/-- never set_option -/\n-- set_option in a comment\nset_option maxHeartbeats 400000 in\ntheorem t : True := trivial\n"
    hits = gates.scan_set_option(lean)
    assert [h.line for h in hits] == [3]
    assert gates.scan_set_option("/- nested /- set_option -/ still comment -/\n") == []


GATE_SH_SORRY_DATA = r"^(noncomputable )?(def|abbrev|instance)[^:]*:=\s*sorry"


def test_sorry_data_catches_typed_and_multiline_definitions():
    lean = textwrap.dedent("""\
        def a : ℕ := sorry
        noncomputable def b (x : ℕ) :
            ℕ :=
          sorry
        abbrev c := sorry
        instance : Inhabited PUnit := sorry
        def d : ℕ := 3
        theorem e : 1 = 1 := sorry
        def f (n : ℕ) : ℕ := if n = 0 then 0 else by sorry
        """)
    hits = gates.scan_sorry_data(lean)
    assert [h.line for h in hits] == [1, 2, 5, 6, 9]
    # the ported gate.sh regex missed every typed definition: this is why the scanner is per declaration
    assert re.search(GATE_SH_SORRY_DATA, "def a : ℕ := sorry") is None


def test_sorry_examples_and_lean_residue():
    lean = "example : 1 + 1 = 2 := by sorry\nexample : 1 + 1 = 2 := rfl\nlemma x : True := trivial\n#eval 1\ndef P : Prop := sorry\n"
    assert [h.line for h in gates.scan_sorry_examples(lean)] == [1]
    assert [h.line for h in gates.scan_lean_residue(lean)] == [3, 3, 4, 5]   # `lemma` and a `True` placeholder


def test_imports():
    assert len(gates.scan_mathlib_subimports("import Mathlib.Data.Nat.Basic\nimport Mathlib\n")) == 1
    assert gates.scan_mathlib_subimports("import Mathlib\nimport TauCeti.Foo\n") == []
    assert gates.scan_missing_mathlib_import("import TauCeti.Foo\n")
    assert gates.scan_missing_mathlib_import("import Mathlib\n") == []


def test_configured_patterns_and_private_paths():
    text = "We cite DroppedRoadmap here.\nSee /home/someone/notes.\nAnchor forbidden-anchor-1."
    assert len(gates.scan_dropped_names(text, ["DroppedRoadmap"])) == 1
    assert gates.scan_dropped_names("DroppedRoadmapPartII", ["DroppedRoadmap"]) == []
    assert len(gates.scan_forbidden(text, [r"forbidden-anchor-\d"])) == 1
    assert [h.line for h in gates.scan_private_paths(text)] == [2]


def test_mechanical_agreement():
    readme = "Prove `Toy.order_one` and `Toy.order_mul` and Mathlib's `Nat.card_eq`. Check (*order-trivial*), (*order-two*)."
    assert gates.readme_identifiers_missing(readme, CLEAN_LEAN) == ["Toy.order_mul"]
    assert gates.check_labels_missing(readme, CLEAN_LEAN) == ["order-two"]
    empty = "/-! ## Layer 1: a -/\n\n/-! ## Layer 2: b -/\ntheorem t : True := trivial\n"
    assert [h.text for h in gates.empty_lean_sections(empty)] == ["Layer 1: a"]
    assert gates.round_labels("label continuation-2 and gate3") and not gates.round_labels("analytic continuation")


def test_dirty_fixture_fails_with_counts():
    readme = CLEAN_README + "\nSee pull request #900 and $x$.\n"
    lean = CLEAN_LEAN.replace("import Mathlib\n", "import Mathlib.Data.Nat.Basic\nset_option autoImplicit true\n")
    lean += "def bad : ℕ := sorry\n"
    r = scan_folder("Toy", readme, lean, opts())
    assert not r.ok and r.exit_code == 1
    for key in ("prnum", "latex", "mathlib-subimports", "no-import-mathlib", "set_option", "sorry-data"):
        assert r.counts[key] >= 1, key
    assert "FAIL" in r.summary()


def test_line_cap_and_outside_folder():
    assert gates.scan_lean_length("x\n" * 11, 10) and not gates.scan_lean_length("x\n" * 10, 10)
    changed = ["TauCetiRoadmap/Toy/README.md", "README.md", "TauCetiRoadmap/Other/README.md"]
    assert gates.outside_folder(changed, "TauCetiRoadmap/Toy", []) == ["README.md", "TauCetiRoadmap/Other/README.md"]
    assert gates.outside_folder(changed, "TauCetiRoadmap/Toy", [r"^README\.md$"]) == ["TauCetiRoadmap/Other/README.md"]


def test_build_output():
    ok = "warning: ./TauCetiRoadmap/Toy/Suggested.lean:3:8: declaration uses 'sorry'\nBuild completed successfully.\n"
    assert gates.build_output_problems(ok, 0) == []
    bad = "warning: Suggested.lean:4:0: unused variable `x`\n"
    assert gates.build_output_problems(bad, 0) == ["warning: Suggested.lean:4:0: unused variable `x`"]
    assert gates.build_output_problems("error: Suggested.lean:1:0: unknown identifier\n", 1)[0] == "build exited 1"


LONG_PATH = "/home/chris/.local/state/tauceti/projects/TauCetiRoadmap/workers/chris-4/board/packages/" \
            "QSeriesPartitionsAndMockModularForms/Suggested.lean"
OBJECT_FILE_ERROR = (f"{LONG_PATH}:1:0: error: object file '/home/chris/.local/state/tauceti/projects/TauCetiRoadmap/"
                     "workers/chris-4/content/.lake/packages/TauCeti/.lake/build/lib/lean/TauCeti/Combinatorics/Enumerative/"
                     "Partition/Conjugate.olean' of module TauCeti.Combinatorics.Enumerative.Partition.Conjugate does not exist")


def test_elaboration_errors_are_kept_whole_and_warnings_drop_out_when_an_error_is_present():
    """Regression (2026-10-21, chris-4, FIX-RT-QSeriesPartitionsAndMockModularForms~2): the first error
    was cut at 200 characters, mid-path, and buried under eleven linter warnings."""
    out = (OBJECT_FILE_ERROR + "\n"
           "warning: TauCetiRoadmap/QSeriesPartitionsAndMockModularForms/Suggested.lean:122:70: this tactic is never executed\n"
           "warning: TauCetiRoadmap/QSeriesPartitionsAndMockModularForms/Suggested.lean:735:36: Variable name `hτ` is not "
           "explicitly referenced.\nHint: The binding can be removed (if unused) or named `_` (if used implicitly).\n")
    problems = gates.build_output_problems(out, 1)
    assert problems == ["build exited 1", OBJECT_FILE_ERROR]       # whole, over 200 characters, and no warning
    assert len(OBJECT_FILE_ERROR) > 200
    # Without an error the warnings stay, each with its continuation lines; `sorry` is never one.
    warnings_only = out.split("\n", 1)[1] + "warning: Suggested.lean:3:8: declaration uses 'sorry'\nBuild completed successfully.\n"
    problems = gates.build_output_problems(warnings_only, 0)
    assert len(problems) == 2 and problems[0].endswith("this tactic is never executed")
    assert problems[1].endswith("explicitly referenced.\n    Hint: The binding can be removed (if unused) or named `_` (if used implicitly).")
    # A goal is part of its error, and a message is capped at 1000 characters, never a line at 200.
    goal = "Suggested.lean:12:4: error: unsolved goals\nx : ℕ\n⊢ x = x\nerror: Lean exited with code 1\n"
    assert gates.build_output_problems(goal, 1)[1] == "Suggested.lean:12:4: error: unsolved goals\n    x : ℕ\n    ⊢ x = x"
    huge = "Suggested.lean:1:0: error: " + "y" * 2000 + "\n"
    assert len(gates.build_output_problems(huge, 1)[1]) == 1000
    # The cap is on the number of diagnostics; the first error of every file survives it.
    many = "".join(f"A.lean:{k}:0: error: e{k}\n" for k in range(1, 60)) + "B.lean:7:0: error: first of B\n"
    problems = gates.build_output_problems(many, 1)
    assert len(problems) == 1 + gates.MAX_DIAGNOSTICS + 1 and "B.lean:7:0: error: first of B" in problems
    assert problems[-1] == "… and 20 more errors not shown"


def test_the_imports_are_mathlib_then_the_tauceti_modules_used():
    """The maintainer's rule (2026-10-19): `import Mathlib`, then only the individual `TauCeti.*`
    modules actually used; never `Mathlib.<submodule>`. (Commit 4c7f9ba had wrongly forbidden the
    `TauCeti.*` lines.)"""
    assert gates.scan_lean_imports(CLEAN_LEAN) == []
    assert gates.scan_lean_imports("") == []           # no import at all is `no-import-mathlib`'s finding
    assert gates.scan_lean_imports("-- import TauCeti.Foo\n/-! import Mathlib.Data.Nat.Basic -/\nimport Mathlib\n") == []
    assert gates.scan_lean_imports("public import Mathlib\n") == []
    with_tauceti = CLEAN_LEAN.replace("import Mathlib\n", "import Mathlib\nimport TauCeti.Combinatorics.Enumerative.Partition.Conjugate\n"
                                                           "import TauCeti.NumberTheory.ModularForms.Eta\n")
    assert gates.scan_lean_imports(with_tauceti) == []
    assert gates.tauceti_imports(with_tauceti) == ["TauCeti.Combinatorics.Enumerative.Partition.Conjugate",
                                                   "TauCeti.NumberTheory.ModularForms.Eta"]
    assert gates.tauceti_imports(CLEAN_LEAN) == []
    assert scan_folder("Toy", CLEAN_README, with_tauceti, opts()).counts["imports"] == 0
    # A Mathlib submodule, a roadmap module, a duplicate, Tau Ceti before Mathlib, an import below the header.
    sub = CLEAN_LEAN.replace("import Mathlib\n", "import Mathlib\nimport Mathlib.Tactic\n")
    hits = gates.scan_lean_imports(sub)
    assert [h.line for h in hits] == [1]
    assert hits[0].text == ("Suggested.lean imports: `import Mathlib` first, then only the individual `TauCeti.*` modules "
                            "used (never `Mathlib.<submodule>`); found: `import Mathlib` (line 1), `import Mathlib.Tactic` (line 2)")
    roadmap = CLEAN_LEAN.replace("import Mathlib\n", "import Mathlib\nimport TauCetiRoadmap.Other.Suggested\n")
    assert len(gates.scan_lean_imports(roadmap)) == 1
    assert len(gates.scan_lean_imports("import Mathlib\nimport TauCeti.A\nimport TauCeti.A\n")) == 1
    assert len(gates.scan_lean_imports("import TauCeti.A\nimport Mathlib\n")) == 1
    inside = CLEAN_LEAN.replace("-- a comment that mentions sorry and set_option\n", "import TauCeti.A\n")
    hits = gates.scan_lean_imports(inside)
    assert len(hits) == 1 and hits[0].text.endswith("`import TauCeti.A` (line 14, after the header)")
    for lean, count in ((CLEAN_LEAN, 0), (with_tauceti, 0), (sub, 1), (roadmap, 1), (inside, 1)):
        assert scan_folder("Toy", CLEAN_README, lean, opts()).counts["imports"] == count
    assert not scan_folder("Toy", CLEAN_README, sub, opts()).ok
    assert gates.lean_imports(sub) == [(1, "Mathlib", "import Mathlib"), (2, "Mathlib.Tactic", "import Mathlib.Tactic")]


def test_a_tauceti_module_must_exist_at_the_pinned_tau_ceti_when_its_sources_are_at_hand(tmp_path):
    checkout = tmp_path / "content"
    sources = checkout / ".lake" / "packages" / "TauCeti" / "TauCeti"
    (sources / "Combinatorics" / "Enumerative" / "Partition").mkdir(parents=True)
    (sources / "Combinatorics" / "Enumerative" / "Partition" / "Conjugate.lean").write_text("-- x\n")
    assert gates.tauceti_sources(checkout) == sources
    assert gates.tauceti_sources(tmp_path / "nowhere") is None and gates.tauceti_sources(None) is None
    lean = "import Mathlib\nimport TauCeti.Combinatorics.Enumerative.Partition.Conjugate\nimport TauCeti.Nope.Missing\n"
    hits = gates.scan_lean_imports(lean, sources)
    assert [(h.line, h.text) for h in hits] == [
        (3, "Suggested.lean imports: TauCeti module TauCeti.Nope.Missing does not exist at the pinned Tau Ceti")]
    assert gates.scan_lean_imports(lean, None) == []                     # without the sources: accepted
    assert gates.scan_lean_imports("import Mathlib\nimport TauCeti.Combinatorics.Enumerative.Partition.Conjugate\n", sources) == []
    assert not gates.tauceti_module_exists("TauCeti.", sources) and gates.tauceti_module_exists("TauCeti.Anything", None)
    assert scan_folder("Toy", CLEAN_README, lean, opts(tauceti_sources=sources)).counts["imports"] == 1
    assert scan_folder("Toy", CLEAN_README, lean, opts()).counts["imports"] == 0
    assert GateOptions.from_config(example_config(), tauceti=sources).tauceti_sources == sources


def test_a_missing_object_file_is_explained_only_for_an_import_that_is_neither_mathlib_nor_tau_ceti(tmp_path):
    problems = ["build exited 1", OBJECT_FILE_ERROR]
    tauceti = "import Mathlib\nimport TauCeti.Combinatorics.Enumerative.Partition.Conjugate\n"
    foreign = "import Mathlib\nimport TauCetiRoadmap.Other.Suggested\n"
    assert gates.explain_build_problems(foreign, problems) == problems + [gates.IMPORT_EXPLANATION]
    assert gates.explain_build_problems(tauceti, problems) == problems       # a Tau Ceti module: the build makes it
    assert gates.explain_build_problems("import Mathlib\n", problems) == problems        # a real missing olean
    other = ["build exited 1", "Suggested.lean:4:0: error: unknown identifier `foo`"]
    assert gates.explain_build_problems(foreign, other) == other                        # not an import's error
    assert gates.explain_build_problems(foreign, []) == []
    # With the pinned sources at hand a Tau Ceti module that does not exist there is foreign too.
    sources = tmp_path / "TauCeti"
    sources.mkdir()
    assert gates.explain_build_problems(tauceti, problems, sources) == problems + [gates.IMPORT_EXPLANATION]
    assert gates.foreign_imports(tauceti, sources) == ["TauCeti.Combinatorics.Enumerative.Partition.Conjugate"]
    assert gates.foreign_imports(tauceti) == [] and gates.foreign_imports(foreign) == ["TauCetiRoadmap.Other.Suggested"]


def test_gate_runs_depcheck_when_order_given():
    readme = CLEAN_README + "\n*Needs:* Later §2.\n"
    sib = {"Later": "# Roadmap: later\n"}
    r = scan_folder("Toy", readme, CLEAN_LEAN, opts(order=["Toy", "Later"], sibling_readmes=sib))
    assert r.counts["depcheck"] == 1 and r.details["depcheck"][0].startswith("UPWARD: Toy")
