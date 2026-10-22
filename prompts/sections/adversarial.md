## The adversarial mathematics pass

A `sorry` build checks elaboration, not truth. Past roadmaps reached review with false identities, a
0/0, units and non-units reversed, ℤ_p-only formulas stated over a general discrete valuation ring,
a wrong determinant parity, statements quantified over arbitrary data with no hypothesis tying the
data to the construction, and whole API blocks restating lemmas the pinned libraries already have.
Locator checks, test counts and the build see none of these, so the pass is computed, and its
record shows the values.

For EVERY theorem, identity, definition and convention:

1. **Degenerate cases.** Instantiate and compute: the trivial group, rank 0 and 1, n = 0, 1, 2, the
   zero module, the zero ring, the unit ideal, the empty set, cover, index set or fan, a field
   versus a DVR, ramified, unramified and equal-characteristic coefficients, p = 2 and 3,
   characteristic p, residue field 𝔽₂, q = 1, split versus non-split, an inseparable extension, a
   nonreduced or square-zero thickening of a reduced test object, a non-compact group where
   compactness is assumed, an object outside the intended universe or cardinality bound, the
   identity map. Include the junk values of total functions: natural-number subtraction, division
   by zero, an out-of-range index, the degree of the zero polynomial. Test an "iff" or an equality
   of sets in both directions; where a group is involved, on a non-abelian one (S₃, Q₈) and a
   cyclic one. If an instance makes the statement false, add the hypothesis or fix the claim, and
   add the instance as a negative-control Check (and `example` where statable).
2. **Context.** A Lean statement is only as true as its `variable`/`include` block: for each
   statement that needs a standing hypothesis (invertibility, compactness, Hausdorffness, a domain,
   finite residue field, finite generation, a scalar tower), confirm it is actually in scope.
3. **Generality, in both directions.** The README's generality equals the Lean signature's. "For a
   DVR O" in prose with `ℤ_[p]` in Lean, or the reverse, is a defect: scope the statement or
   register the general form separately with its own hypotheses. Never narrow a statement below
   what the sources support; special cases are Checks. Conversely, after choosing a carrier, check
   which wider class of inputs it now admits (a full subcategory that admits a map you meant to
   exclude, a bare homomorphism that cannot carry the structure you meant).
4. **Conventions with a witness.** Every sign, parity, direction of a map and normalisation (δ_P
   versus δ_P⁻¹, ϖ versus p, left versus right cosets, 2πi factors, Tate twists, duals, arithmetic
   versus geometric Frobenius) is pinned by one explicit two-term example whose value is computed,
   as a README Check and a Lean test. Recompute that witness in every layer, and in every supplier,
   that uses the same object.
5. **Definitions determine their object.** Check each defining property against one example that
   satisfies it and one that fails it. Then name the most plausible wrong model — the inverse or
   geometric convention, a rescaling by a unit, the zero map, the trivial structure, a larger value
   of the parameter, an unmarked isomorphism — and show which stated law excludes it; if none does,
   add the law or the Check that does. Do this for every opaque or `sorry`'d map, every structure
   whose fields are candidates for an operation, and every existential hypothesis meant to fix a
   parameter; for an ordinary definition with a real body, the two examples suffice. Every
   constructed map names its source and its target. Every denominator has a non-vanishing
   argument; every "canonical" map a domain.
6. **Vacuity.** A statement quantified over arbitrary data (a complex, a subgroup, a functor) with
   no hypothesis relating it to the construction is vacuous or false: fix it or delete it. A
   hypothesis that contains the conclusion is a defect, and so is a headline whose statement
   mentions none of the objects the roadmap defines.
7. **Unused hypotheses.** A hypothesis that neither the statement's truth, the proof sketch nor any
   consumer needs is a P3 note, not a finding, unless you show with an instance that the statement
   holds without it. Do not search for a separating counterexample for every hypothesis.
8. **Imported lemmas and proof routes.** Every input a proof route uses is a target here, a named
   supplier section, or a pinned declaration, and none of them is derived from the target itself; a
   citation is not a supplier. Where a step is taken from inside a source's proof rather than from
   a stated theorem, open the source there and check the step at the generality restated here: a
   source proves its lemmas in its own setting.
9. **Duplication by object.** Grep the pinned Mathlib and Tau Ceti for the objects (structure
   fields, operators, hypotheses), not only for our declaration names. What exists at the pins is
   deleted and cited in the supplier contracts with its exact name and file. When you rely on what a
   native definition says, quote it at the pin and note what it does not include (a separation
   axiom, nonemptiness, measurability). Every claim that a library "has", "lacks" or "proves"
   something, or that a statement can be ported from an existing formalization, is checked at the
   pin with its file and line; the source's own hypotheses (separability, finite dimension) come
   with it.
10. **The roadmap agrees with itself.** Evaluate every Check and worked example against every
    theorem and formula of its section, and against the other Checks on the same object. A worked
    example that refutes a theorem of the same roadmap is P1.
11. **Interfaces.** Anything the prose promises (an extension map, a duality, a comparison) is a
    named target with hypotheses, or is cut. A milestone named by a colloquial name ("the standard
    bound", "compactness") states the exact quantifiers, their order and which of several conflated
    notions is meant.

Record the pass as a table, one row per statement: statement, instances tried, the values computed
on each side, result, change. A row without computed values ("checked", "fine", "no change") does
not count as a check.
