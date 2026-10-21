import re
import textwrap

from tauceti_roadmap_worker import gates
from tauceti_roadmap_worker.gates import GateOptions, scan_folder

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


def test_layer_ids():
    bad = ("## Layer 0 Discrete convexity\n\n### 0.1 Heights\n\n## Layer 1: valuations\n\n"
           "## Layer 2 Coefficients, Gauss values\n")
    hits = gates.scan_layer_ids(bad)
    assert [h.line for h in hits] == [1, 7], hits
    assert "'Layer 0 Discrete convexity'" in hits[0].text and "'Layer 2 Coefficients'" in hits[1].text
    good = ("### Part A — algebra\n\n### Part B: analysis\n\n### Stage 3 (optional)\n\n"
            "### Part A\n\n### Layer 4.5a: interlude\n")
    assert gates.scan_layer_ids(good) == []
    assert gates.scan_layer_ids("### L0A — sheaves\n\n### S1: the site\n") == []
    assert gates.scan_layer_ids("# Roadmap\n\nNo layers here.\n") == []
    r = scan_folder("Toy", CLEAN_README.replace("## Layer 1: orders", "## Layer 1 orders"), CLEAN_LEAN, opts())
    assert r.counts["layer-ids"] == 1 and not r.ok, r.details


def test_gate_runs_depcheck_when_order_given():
    readme = CLEAN_README + "\n*Needs:* Later §2.\n"
    sib = {"Later": "# Roadmap: later\n"}
    r = scan_folder("Toy", readme, CLEAN_LEAN, opts(order=["Toy", "Later"], sibling_readmes=sib))
    assert r.counts["depcheck"] == 1 and r.details["depcheck"][0].startswith("UPWARD: Toy")
