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


def test_packet_as_a_term():
    assert [h.line for h in gates.scan_process_residue("Intro.\nSee the packet.\n")] == [2]
    assert gates.scan_process_residue("Its L-packet.\nThe knowl `lf.packet`.\n") == []
    assert gates.scan_process_residue("Define the **packet** of K.\nEach packet is finite.\n") == []


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
    empty = "/-! ## Layer 1: a -/\n\n/-! ## Layer 2: b -/\ntheorem t : True := trivial\n"
    assert [h.text for h in gates.empty_lean_sections(empty)] == ["Layer 1: a"]
    assert gates.round_labels("label continuation-2 and gate3") and not gates.round_labels("analytic continuation")


def test_no_gate_asks_for_lean_counterparts_of_checks_or_counts_declarations():
    """Suggested.lean states the headline theorems: README Checks stay prose, and a file that states
    one theorem, or nothing at all, passes."""
    readme = CLEAN_README.replace("(*order-trivial*);", "(*order-trivial*);\n- the group of order two (*order-two*);")
    r = scan_folder("Toy", readme, CLEAN_LEAN, opts())
    assert r.ok, r.details
    assert "check-labels-missing" not in r.counts and "check-labels-missing" not in gates.GATE_RULES
    assert "never by adding a declaration" in gates.GATE_RULES["readme-ids-missing"]
    headline = CLEAN_LEAN.replace(
        "example : Toy.order PUnit = 1 := by simp [Toy.order] -- order-trivial\n", "")
    assert scan_folder("Toy", readme, headline, opts()).ok
    empty = textwrap.dedent("""\
        import Mathlib

        /-! # Toy: target signatures

        The roadmap is README.md. No headline theorem is statable at the pins. -/

        namespace TauCetiRoadmap.Toy

        end TauCetiRoadmap.Toy
        """)
    prose = "# Roadmap: toy groups\n\n**Summary.** Builds the order of a group.\n"
    assert scan_folder("Toy", prose, empty, opts()).ok


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


# --- the anchor residue, repaired mechanically (explorer imports, the fix lane before its agent) -----

def test_github_slugs():
    assert gates.github_slug("Layer 0: Entire functions and quantitative prime estimates") == \
        "layer-0-entire-functions-and-quantitative-prime-estimates"
    # Mathematics in a heading: Greek letters stay (lower-cased), TeX braces, bars and operators go.
    assert gates.github_slug("0.11 Mertens's first theorem and Σ_{p | N} log p/p = O(log log N)") == \
        "011-mertenss-first-theorem-and-σ_p--n-log-pp--olog-log-n"
    assert gates.github_slug("6.43 The Gibbs states φ_{β,u}") == "643-the-gibbs-states-φ_βu"
    assert gates.github_slug("Foo — Bar") == "foo--bar"
    assert gates.github_slug("The `snake_case` lemma and *emphasis* and _also_") == \
        "the-snake_case-lemma-and-emphasis-and-also"
    assert gates.github_slug("x² and a₀ in [a link](https://example.org) &amp; more") == "x-and-a-in-a-link--more"


ANCHORED = textwrap.dedent("""\
    # Roadmap

    See [the estimate](#chebyshev-estimate-legacy), [layer 0](#layer-0-primes),
    [the remark](#remark-legacy) and [a missing one](#nowhere).

    <a id="layer-0-primes"></a>

    ## Layer 0: Primes

    <a id="chebyshev-estimate-legacy"></a>
    <a id="second-legacy-id"></a>

    ### 0.1 Chebyshev's estimate for θ(x)

    Text with \\[#I(κ)\\](#I(κ)/2) as mathematics, and `[code](#chebyshev-estimate-legacy)` in a span.

    <a id="remark-legacy"></a>

    A remark under 0.1, then an inline <a id="inline-legacy"></a> tag.

    ### <a id="dup-legacy"></a>0.1 Chebyshev's estimate for θ(x)

    [again][ref]

    [ref]: #dup-legacy
    """)


def test_strip_anchors_removes_the_tags_and_points_links_at_heading_slugs():
    fixed = gates.strip_anchors(ANCHORED)
    assert "<a " not in fixed.text and gates.scan_process_residue(fixed.text) == []
    assert fixed.removed == 6 and fixed.unresolved == ["nowhere"]
    first = "01-chebyshevs-estimate-for-θx"
    assert f"[the estimate](#{first})" in fixed.text
    assert "[layer 0](#layer-0-primes)" in fixed.text                     # the tag's id is already the slug
    assert f"[the remark](#{first})" in fixed.text                        # a tag before a paragraph: its section
    assert "[ref]: #01-chebyshevs-estimate-for-θx-1" in fixed.text        # the second heading of that name
    assert fixed.rewritten == 3
    # Nothing else changes: the mathematics, the code span, a link to no tag, the headings' text.
    assert "\\[#I(κ)\\](#I(κ)/2)" in fixed.text and "`[code](#chebyshev-estimate-legacy)`" in fixed.text
    assert "[a missing one](#nowhere)" in fixed.text
    assert "### 0.1 Chebyshev's estimate for θ(x)\n\n[again][ref]" in fixed.text
    assert "an inline tag." in fixed.text
    # A line that held only tags goes with the blank line after it.
    assert "## Layer 0: Primes\n\n### 0.1" in fixed.text and "\n\n\n" not in fixed.text
    assert fixed.summary() == ("6 anchor tags removed from README.md, 3 internal links pointed at heading slugs, "
                               "internal links to no heading: #nowhere")


def test_strip_anchors_leaves_a_readme_without_tags_alone():
    text = "# R\n\n[x](#nowhere)\n\n```\n<a id=\"in-code\">\n```\n"
    fixed = gates.strip_anchors(text)
    assert fixed.text == text and fixed.removed == 0 and fixed.rewritten == 0
    wrapped = gates.strip_anchors('A <a id="t">Theorem</a> named.\n')
    assert wrapped.text == "A Theorem named.\n" and wrapped.removed == 1


def test_target_ids():
    # Prefixed subsection ids (`### PA.0.12`, `### NR0.1`) make the target reader see nothing: each heading is reported.
    bad = ("# Roadmap\n\n## Layer PA.0: Foundations\n\n### PA.0.1 Resolvability\n\nText.\n\n### PA.0.2 Covers\n\n"
           "## Layer NR1: Hecke images\n\n### NR1.1 Selected images\n\n### Examples\n\n- one\n")
    hits = gates.scan_target_ids(bad)
    assert [h.line for h in hits] == [5, 9, 13], hits
    assert "'PA.0.1'" in hits[0].text and "### k.n Title" in hits[0].text
    # A layered README with no target heading at all is reported once, on its first layer line.
    empty = "# Roadmap\n\n## Scope\n\ntext\n\n## Layer 1: orders\n\nProse only.\n"
    hits = gates.scan_target_ids(empty)
    assert [h.line for h in hits] == [7] and "states no targets" in hits[0].text, hits
    # The required form passes, bold target names pass, and a README without layers is not checked.
    assert gates.scan_target_ids(CLEAN_README) == []
    assert gates.scan_target_ids("## Layer 1: x\n\n**The order.** Define it.\n") == []
    assert gates.scan_target_ids("# Roadmap\n\nNo layers here.\n") == []
    r = scan_folder("Toy", CLEAN_README.replace("### 1.1", "### T1.1"), CLEAN_LEAN, opts())
    assert r.counts["target-ids"] >= 1 and not r.ok, r.details


# --- roadmaps on the content repository's main are never upward suppliers ---------------------------

def content_clone(tmp_path):
    """A clone of a content repository whose main holds the roadmaps Later and, nested, Family/Deep,
    an archived one under Completed/, and a references folder; the clone adds Toy, which cites Later."""
    import subprocess
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env={**__import__("os").environ, **env})
    origin, seed, wt = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    for rel in ("TauCetiRoadmap/Later/README.md", "TauCetiRoadmap/Family/README.md", "TauCetiRoadmap/Family/Deep/README.md",
                "Completed/Old/README.md", "TauCetiRoadmap/Later/references/README.md", "TauCetiRoadmap/README.md"):
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text("# Roadmap\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "main")
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/main")
    subprocess.run(["git", "clone", "-q", str(origin), str(wt)], check=True)
    toy = wt / "TauCetiRoadmap" / "Toy"
    toy.mkdir(parents=True)
    (toy / "README.md").write_text(CLEAN_README + "\n*Needs:* Later §2.\n")
    (toy / "Suggested.lean").write_text(CLEAN_LEAN)
    (toy / "metadata.toml").write_text('topic = "math.GR"\n')
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "Toy")
    return wt


def test_the_gate_takes_a_roadmap_on_main_as_a_supplier_whatever_the_order_says(tmp_path):
    wt = content_clone(tmp_path)
    merged = gates.merged_roadmaps(wt, "TauCetiRoadmap", "Completed", "main")
    assert merged == {"Later", "Family", "Deep", "Old"}
    assert gates.merged_roadmaps(tmp_path, "TauCetiRoadmap", "Completed") == set()        # not a checkout
    sib = {"Later": "# Roadmap: later\n"}
    stale = opts(order=["Toy", "Later"], sibling_readmes=sib)
    assert gates.run_gate(wt, "Toy", stale).details["depcheck"][0].startswith("UPWARD: Toy (#1) -> Later (#2)")
    assert gates.run_gate(wt, "Toy", opts(order=["Toy", "Later"], sibling_readmes=sib, merged=merged)).counts["depcheck"] == 0
    cfg = example_config()
    assert GateOptions.from_config(cfg, order=["Toy"], merged=merged).merged == merged
    assert GateOptions.from_config(cfg).merged == set()


# --- a stacked pull request is gated against its own base ---------------------------------------------

def stacked_clone(tmp_path):
    """A content repository whose branch roadmap/parent adds the roadmap Parent and roadmap/child, on top
    of it, adds Child; a clone of the child branch that has not fetched roadmap/parent."""
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}

    def git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout

    def roadmap(root, name):
        folder = root / "TauCetiRoadmap" / name
        folder.mkdir(parents=True)
        (folder / "README.md").write_text(CLEAN_README)
        (folder / "Suggested.lean").write_text(CLEAN_LEAN)
        (folder / "metadata.toml").write_text('topic = "math.GR"\n')
    origin, seed, wt = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    roadmap(seed, "Other")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "main")
    roadmap(seed, "Parent")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "Parent")
    git(seed, "push", "-q", str(origin), "HEAD~1:refs/heads/main", "HEAD:refs/heads/roadmap/parent")
    roadmap(seed, "Child")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "Child")
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/roadmap/child")
    subprocess.run(["git", "clone", "-q", "-b", "roadmap/child", str(origin), str(wt)], check=True, capture_output=True)
    git(wt, "update-ref", "-d", "refs/remotes/origin/roadmap/parent")
    return wt


def test_a_stacked_pull_request_changes_only_its_own_folder_against_its_base(tmp_path, monkeypatch):
    from tauceti_roadmap_worker.lanes import Context
    from tauceti_roadmap_worker.lanes import base as lane_base
    from tauceti_roadmap_worker.lanes import gate as lane_gate
    from tauceti_roadmap_worker.lanes.base import Workspace

    from .fakes import FakeBoard
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    wt = stacked_clone(tmp_path)
    against_main = gates.run_gate(wt, "Child", opts(), base="origin/main")
    assert against_main.counts["outside-folder"] == 3 and all(
        "TauCetiRoadmap/Parent/" in str(h) for h in against_main.details["outside-folder"])
    cfg = example_config()
    cfg.gates.build = cfg.gates.elaborate = ""
    ctx = Context(cfg=cfg, gh=None, board=FakeBoard(), claims=None, worker_id="w1", operator="alice", agent="codex",
                  model="m")
    ws = Workspace(job_dir=tmp_path / "job", scratch=tmp_path / "job" / "s", board=tmp_path / "board", content=wt,
                   workdir=wt, report=tmp_path / "r.md", branch="b", extra={"base_ref": "roadmap/parent"})
    assert lane_base.pr_base(ctx, ws) == "origin/roadmap/parent"
    problems, result = lane_gate.gate_problems(ctx, ws, "Child", "Child", opts())    # fetches the base first
    assert result.counts["outside-folder"] == 0 and not any(p.startswith("outside-folder") for p in problems)
    assert lane_base.pr_base(ctx, Workspace(**{**ws.__dict__, "extra": {}})) == "origin/main"
