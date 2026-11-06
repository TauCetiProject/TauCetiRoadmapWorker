"""The statement-hygiene scan of Suggested.lean (`gates.scan_lean_statement_hygiene`): how the targets
are stated, one hit per occurrence with its line, its severity and the repair a review quotes; and its
wiring into the push gate (count `statement-hygiene`) under `[prreview] statement_hygiene`."""

import re
import textwrap

from tauceti_roadmap_worker import config, gates
from tauceti_roadmap_worker.gates import GateOptions, scan_folder
from tauceti_roadmap_worker.lanes import prreview as lane_prreview
from tauceti_roadmap_worker.paths import HERE

from .fakes import example_config
from .test_gate_scanners import CLEAN_LEAN, CLEAN_README

EXAMPLE_TOML = (HERE / "tauceti-roadmap.toml.example").read_text(encoding="utf-8")


def scan(lean: str) -> list[tuple[int, str]]:
    return [(h.line, h.text) for h in gates.scan_lean_statement_hygiene(textwrap.dedent(lean))]


def repair(key: str, **kw) -> str:
    return "; repair: " + gates.HYGIENE_REPAIRS[key].format(**kw)


# --- 1. no `Nonempty`-isomorphism headline ----------------------------------------------------------

def test_a_theorem_concluding_that_an_isomorphism_exists_is_reported():
    found = scan("""\
        import Mathlib

        theorem equiv_headline (A B : Type) : Nonempty (A ≃ B) := sorry
        theorem iso_headline {C : Type*} [CategoryTheory.Category C] (X Y : C) :
            Nonempty (X ≅ Y) := sorry
        lemma equivalence_headline (C D : Type*) [CategoryTheory.Category C] [CategoryTheory.Category D] :
            Nonempty (C ≌ D) := sorry
        theorem additive_headline (A B : Type) :
            ∃ e : A ≃+ B, e 0 = 0 := sorry
        theorem iso_given (A B : Type) : ∃ (e : A ≅ B), True := sorry
        """)
    assert [line for line, _ in found] == [3, 5, 7, 9, 10]
    assert found[0][1] == ("P3 theorem `equiv_headline` concludes `Nonempty (A ≃ B)`" + repair("nonempty-iso"))
    assert found[3][1].startswith("P3 theorem `additive_headline` concludes `∃ e : A ≃+ B, e 0 = 0`")
    assert "keep `Nonempty` only as a corollary named `nonempty_*`" in found[4][1]


def test_the_named_corollary_a_conjunction_and_an_automorphism_are_not_headlines():
    assert scan("""\
        import Mathlib

        theorem nonempty_equiv (A B : Type) : Nonempty (A ≃ B) := sorry
        theorem Foo.exists_iso (A B : Type) : ∃ e : A ≃ B, True := sorry
        theorem conj (A B : Type) : Nonempty (A ≃ B) ∧ True := sorry
        theorem exists_nat : ∃ n : ℕ, Nonempty (Fin n ≃ Fin 2) := sorry
        theorem frobenius (K L : Type*) [Field K] [Field L] [Algebra K L] (P : (L ≃ₐ[K] L) → Prop) :
            ∃ σ : L ≃ₐ[K] L, P σ := sorry
        theorem canonical (A B : Type) (f : A → B) : Function.Bijective f := sorry
        def equivData (A B : Type) : Nonempty (A ≃ B) := sorry
        """) == []


# --- 2. no choice in data -------------------------------------------------------------------------

def test_a_definition_whose_body_chooses_is_reported():
    found = scan("""\
        import Mathlib

        noncomputable def pick (h : Nonempty ℕ) : ℕ := h.some
        noncomputable def pick₂ (h : ∃ n : ℕ, 0 < n) : ℕ := Classical.choose h
        noncomputable abbrev pick₃ (X : Type) [Nonempty X] : X := Classical.arbitrary X
        noncomputable instance inst (X : Type) [h : Nonempty X] : Inhabited X where
          default := Classical.choice h
        noncomputable def pick₄ (h : ∀ n : ℕ, ∃ m, n < m) : ℕ → ℕ
          | 0 => (h 0).choose
          | n + 1 => Nonempty.some ⟨n⟩
        """)
    assert [line for line, _ in found] == [3, 4, 5, 7, 9, 10]
    assert found[0][1] == "P3 def `pick` chooses its datum (`h.some`)" + repair("choice-data")
    assert "(`Classical.choose`)" in found[1][1] and "abbrev `pick₃`" in found[2][1]
    assert found[3][1].startswith("P3 instance `inst` chooses its datum (`Classical.choice`)")


def test_sorry_bodies_constructors_and_proofs_do_not_choose():
    assert scan("""\
        import Mathlib

        noncomputable def admitted : ℕ := sorry
        def opt : Option ℕ := Option.some 3
        def opt₂ : Option ℕ := .some 3
        def top : WithTop ℕ := WithTop.some 3
        def sub (h : ∃ n : ℕ, 0 < n) : {n : ℕ // 0 < n} := ⟨1, Nat.one_pos⟩
        theorem chosen (h : ∃ n : ℕ, 0 < n) : 0 < Classical.choose h := Classical.choose_spec h
        """) == []


# --- 3. no `letI`/`haveI` in a signature ------------------------------------------------------------

def test_a_local_instance_in_a_signature_is_reported():
    found = scan("""\
        import Mathlib

        theorem let_in_type (X : Type) [Nonempty X] :
            letI : Inhabited X := ⟨Classical.arbitrary X⟩
            (default : X) = default := sorry
        theorem have_in_type {c : ℝ} (hc : 0 < c) : haveI : NeZero c := ⟨hc.ne'⟩; c ≠ 0 := sorry
        example : letI : Add ℕ := ⟨Nat.add⟩; (1 : ℕ) + 1 = 2 := rfl
        """)
    assert [line for line, _ in found] == [4, 6, 7]
    assert found[0][1] == "P3 `letI` in the signature of theorem `let_in_type`" + repair("local-instance")
    assert found[1][1].startswith("P3 `haveI` in the signature of theorem `have_in_type`")
    assert "an anonymous example" in found[2][1]


def test_a_local_instance_in_a_proof_or_a_body_is_not_in_a_signature():
    assert scan("""\
        import Mathlib

        theorem in_proof (X : Type) [Nonempty X] : True := by
          letI : Inhabited X := ⟨Classical.arbitrary X⟩
          haveI := (inferInstance : Inhabited X)
          trivial
        def in_body : ℕ := letI : Add ℕ := ⟨Nat.add⟩; 1 + 1
        theorem named_binder (X : Type) [inst : Inhabited X] : (default : X) = default := rfl
        """) == []


# --- 4. `[Finite k]`, not `[Fintype k]`, beside `[Field k]` -------------------------------------------

def test_fintype_beside_field_is_reported_in_a_signature_and_through_variables():
    found = scan("""\
        import Mathlib

        theorem own (F : Type*) [Field F] [Fintype F] : Fintype.card F ≠ 0 := sorry

        section
        variable (k : Type*) [Field k] [Fintype k]

        theorem uses_k : Fintype.card k ≠ 0 := sorry
        theorem uses_k_again : 1 < Fintype.card k := sorry
        end

        section
        variable (E : Type*) [Fintype E]
        theorem field_here [Field E] : Fintype.card E ≠ 0 := sorry
        end
        """)
    assert found == [
        (3, "P3 `[Fintype F]` beside `[Field F]` (theorem `own`)" + repair("fintype-field", k="F")),
        (6, "P3 `[Fintype k]` beside `[Field k]` (a `variable` command in force for theorem `uses_k`)"
            + repair("fintype-field", k="k")),
        (13, "P3 `[Fintype E]` beside `[Field E]` (a `variable` command in force for theorem `field_here`)"
             + repair("fintype-field", k="E"))]
    assert "use `[Finite F]`; `Nat.card F` is the cardinality" in found[0][1]


def test_fintype_without_field_finite_beside_field_and_closed_sections_pass():
    assert scan("""\
        import Mathlib

        theorem ring_only (F : Type*) [CommRing F] [Fintype F] : Fintype.card F ≠ 0 := sorry
        theorem finite (F : Type*) [Field F] [Finite F] : Nat.card F ≠ 0 := sorry
        theorem two_names (F G : Type*) [Field F] [Fintype G] : Fintype.card G ≠ 0 := sorry

        section
        variable (k : Type*) [Field k] [Fintype k]
        theorem unrelated : (1 : ℕ) = 1 := sorry
        end

        theorem after_the_section (k : Type*) [Field k] : Nat.card k ≠ 1 := sorry
        """) == []


# --- 5. no `ULift` in a signature ---------------------------------------------------------------------

def test_ulift_in_the_signature_of_a_theorem_or_definition_is_reported():
    found = scan("""\
        import Mathlib

        universe u
        theorem lifted (X : Type) : Nonempty (ULift.{u} X) ↔ Nonempty X := sorry
        def liftedCarrier (X : Type) : Type u := ULift.{u} X
        noncomputable def comparison (X : Type) : ULift.{u} X ≃ X := sorry
        """)
    assert found == [(4, "P3 `ULift` in the signature of theorem `lifted`" + repair("ulift")),
                     (6, "P3 `ULift` in the signature of def `comparison`" + repair("ulift"))]


def test_ulift_in_a_body_or_another_name_is_not_in_a_signature():
    assert scan("""\
        import Mathlib

        theorem body (X : Type) (x : X) : True := by
          have := ULift.up x
          trivial
        def liftable (f : Type → Type) [ULiftable f f] : Type := f ℕ
        """) == []


# --- 6. no instance removals or local derived-category instances -------------------------------------

def test_instance_and_simp_removals_and_local_derived_category_instances_are_reported():
    found = scan("""\
        import Mathlib

        attribute [-instance] Foo.instBar
        attribute [-simp] Nat.add_eq
        attribute [local instance] HasDerivedCategory.standard

        theorem t : True := trivial
        """)
    assert [line for line, _ in found] == [3, 4, 5]
    assert found[0][1] == "P3 `attribute [-instance]`" + repair("instance-removal")
    assert found[2][1].startswith("P3 `attribute [local instance] HasDerivedCategory`")


def test_other_attributes_are_not_removals():
    assert scan("""\
        import Mathlib

        attribute [local instance] Classical.propDecidable
        attribute [simp] Nat.add_zero
        attribute [instance] Foo.instBar
        @[simp] theorem t : True := trivial
        """) == []


# --- 7. no heterogeneous equality or transport in a public conclusion ---------------------------------

def test_heq_and_transport_in_a_public_conclusion_are_reported():
    found = scan("""\
        import Mathlib

        theorem heq (a b : ℕ) : HEq a b := sorry
        theorem heq₂ (a b : ℕ) : a ≍ b := sorry
        theorem casted (A B : Type) (h : A = B) (x : A) : cast h x = cast h x := sorry
        protected theorem Foo.hom {C : Type*} [CategoryTheory.Category C] {X Y : C} (h : X = Y) (f : X ⟶ Y) :
            f = CategoryTheory.eqToHom h := sorry
        """)
    assert [line for line, _ in found] == [3, 4, 5, 5, 7]
    assert found[0][1] == "P3 `HEq` in the conclusion of theorem `heq`" + repair("transport")
    assert found[1][1].startswith("P3 `≍` in the conclusion of theorem `heq₂`")
    assert found[4][1].startswith("P3 `eqToHom` in the conclusion of theorem `Foo.hom`")


def test_private_theorems_hypotheses_and_coercions_pass():
    assert scan("""\
        import Mathlib

        private theorem aux (a b : ℕ) : HEq a b := sorry
        theorem hyp (a b : ℕ) (h : HEq a b) : a = b := sorry
        theorem coe (n : ℕ) : (Nat.cast n : ℝ) = n := sorry
        theorem names (a : ℕ) : foo_eqToHom a = a := sorry
        """) == []


# --- 8. one statement, one declaration (P2) ------------------------------------------------------------

def test_two_theorems_with_one_statement_are_reported_at_the_second():
    found = scan("""\
        import Mathlib

        theorem first (x : ℕ) (h : 0 < x) : x + 0 = x := sorry

        theorem second (y : ℕ)
            (hy : 0 < y) :   y + 0 = y := by
          simp
        theorem third (z : ℕ) (h : 0 < z) : z + 0 = z := sorry
        """)
    assert found == [(5, "P2 theorem `second` states what theorem `first` (line 3) states" + repair("duplicate")),
                     (8, "P2 theorem `third` states what theorem `first` (line 3) states" + repair("duplicate"))]


def test_different_statements_namespaces_variables_and_data_are_not_duplicates():
    assert scan("""\
        import Mathlib

        theorem plus (x : ℕ) : x + 0 = x := sorry
        theorem times (x : ℕ) : x * 1 = x := sorry
        theorem pi_pos : (π : ℝ) > 0 := sorry
        theorem e_pos : (e : ℝ) > 0 := sorry

        namespace A
        theorem same : f 0 = 0 := sorry
        end A
        namespace B
        theorem same : f 0 = 0 := sorry
        end B

        section
        variable (R : Type*) [CommRing R]
        theorem unit_ring : (1 : R) ≠ 0 := sorry
        end
        section
        variable (R : Type*) [Field R]
        theorem unit_field : (1 : R) ≠ 0 := sorry
        end

        def one : ℕ := 1
        def two : ℕ := 2
        """) == []


# --- comments, docstrings and strings ---------------------------------------------------------------

def test_comments_docstrings_and_strings_are_ignored_and_lines_stay_right():
    lean = """\
        import Mathlib

        /-! # Module docs
        theorem in_module_doc (A B : Type) : Nonempty (A ≃ B) := sorry
        attribute [-instance] Foo.bar -/

        /- a block /- nested: `theorem nested : HEq a b := sorry` -/
           letI ULift [Field k] [Fintype k] -/

        /-- The docstring says `Classical.choose`, `h.some`, `letI`, `ULift`, `HEq` and `eqToHom`. -/
        noncomputable def clean (n : ℕ) : ℕ := n -- := Classical.choose h, attribute [-simp] foo

        -- theorem commented (A B : Type) : Nonempty (A ≃ B) := sorry
        def text : String := "Classical.choose h -- letI ULift attribute [-instance] x"

        theorem clean_thm (x : ℕ) : x = x := rfl  /- theorem copy (y : ℕ) : y = y := rfl -/

        theorem bad (A B : Type) : Nonempty (A ≃ B) := sorry
        """
    assert scan(lean) == [(18, "P3 theorem `bad` concludes `Nonempty (A ≃ B)`" + repair("nonempty-iso"))]


# --- the gate and the switch ------------------------------------------------------------------------

BAD_LEAN = CLEAN_LEAN.replace(
    "end TauCetiRoadmap.Toy",
    "theorem Toy.equiv (A B : Type) : Nonempty (A ≃ B) := sorry\n\nend TauCetiRoadmap.Toy")


def test_the_gate_counts_the_hits_and_the_review_sees_their_rule_severity_and_repair():
    assert scan_folder("Toy", CLEAN_README, CLEAN_LEAN, GateOptions()).counts["statement-hygiene"] == 0
    r = scan_folder("Toy", CLEAN_README, BAD_LEAN, GateOptions())
    assert r.counts["statement-hygiene"] == 1 and not r.ok
    report = lane_prreview.gate_document(r, "a" * 40)
    assert "## statement-hygiene (1)" in report and "Rule: Suggested.lean states its targets cleanly" in report
    line = BAD_LEAN.splitlines().index("theorem Toy.equiv (A B : Type) : Nonempty (A ≃ B) := sorry") + 1
    assert re.search(rf"- line {line}: P3 theorem `Toy\.equiv` concludes `Nonempty \(A ≃ B\)`; "
                     r"repair: state the canonical map", report), report
    off = scan_folder("Toy", CLEAN_README, BAD_LEAN, GateOptions(statement_hygiene=False))
    assert off.counts["statement-hygiene"] == 0 and off.ok, off.details


def test_the_switch_is_on_by_default_and_read_from_prreview():
    cfg = example_config()
    assert cfg.prreview.statement_hygiene is True and config.PRReview().statement_hygiene is True
    assert GateOptions.from_config(cfg).statement_hygiene is True
    off = config.parse(EXAMPLE_TOML.replace("statement_hygiene = true", "statement_hygiene = false"), "conf.toml")
    assert off.prreview.statement_hygiene is False and off.warnings == []
    assert GateOptions.from_config(off).statement_hygiene is False
    typo = config.parse(EXAMPLE_TOML.replace("statement_hygiene = true", "statement_hygeine = false"), "conf.toml")
    assert typo.warnings == ["unknown key prreview.statement_hygeine"] and typo.prreview.statement_hygiene is True
