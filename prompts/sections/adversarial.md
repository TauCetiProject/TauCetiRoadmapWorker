## The adversarial mathematics pass

A `sorry` build checks elaboration, not truth. Past roadmaps reached review with false identities, a
0/0, units and non-units reversed, ℤ_p-only formulas stated over a general discrete valuation ring,
a wrong determinant parity, statements quantified over arbitrary data with no hypothesis tying the
data to the construction, and whole API blocks restating lemmas the pinned libraries already have.
Locator checks, test counts and the build see none of these. Almost every such defect that reached
the roadmap repository was in a class this list already names: the pass failed because nobody
computed, not because nobody was told. So the pass is computed, and its record shows the values.

For EVERY theorem, identity, definition and convention:

1. **Degenerate cases.** Instantiate and compute: the trivial group, rank 0 and 1, n = 0, 1, 2, the
   zero module, the zero ring, the unit ideal, the empty set, cover, index set or fan, a field
   versus a DVR, ramified, unramified and equal-characteristic coefficients, p = 2 and 3,
   characteristic p, residue field 𝔽₂, q = 1, split versus non-split, an inseparable extension, a
   nonreduced or square-zero thickening of a reduced test object, a non-compact group where
   compactness is assumed, an object outside the intended universe or cardinality bound, the
   identity map. Include the junk values of total functions: natural-number subtraction, division
   by zero, an out-of-range index, the degree of the zero polynomial. Test an "iff" or an equality
   of sets in both directions, on a non-abelian example (S₃, Q₈) and a cyclic one. If an instance
   makes the statement false, add the hypothesis or fix the claim, and add the instance as a
   negative-control Check (and `example` where statable).
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
   add the law or the Check that does. This applies to every opaque or `sorry`'d map, every
   structure whose fields are candidates for an operation, and every existential hypothesis meant
   to fix a parameter. Every constructed map names its source and its target. Every denominator has
   a non-vanishing argument; every "canonical" map a domain.
6. **Vacuity.** A statement quantified over arbitrary data (a complex, a subgroup, a functor) with
   no hypothesis relating it to the construction is vacuous or false: fix it or delete it. A
   hypothesis that contains the conclusion is a defect, and so is a headline whose statement
   mentions none of the objects the roadmap defines.
7. **Hypotheses earn their place.** For each hypothesis, give the instance that fails without it. A
   hypothesis that no instance and no consumer needs is cut: an over-strong hypothesis is found
   later by the library, when it proves the weaker statement.
8. **Imported lemmas and proof routes.** A step taken from a source's proof (not from its
   statement) is re-checked at the generality restated here; a source proves its lemma in its own
   setting. For each cited proof route, list the inputs it uses with their hypotheses: each
   hypothesis appears in the statement, and each input is a target here, a named supplier section,
   or a pinned declaration that is not itself derived from this target (no circularity through the
   source's own proof). A citation is not a supplier.
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
