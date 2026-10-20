## The adversarial mathematics pass

A `sorry` build checks elaboration, not truth. Past roadmaps reached review with false identities, a
0/0, units and non-units reversed, ℤ_p-only formulas stated over a general discrete valuation ring,
a wrong determinant parity, statements quantified over arbitrary data with no hypothesis tying the
data to the construction, and whole API blocks restating lemmas the pinned libraries already have.
Locator checks, test counts and the build see none of these. For EVERY theorem, identity,
definition and convention:

1. **Degenerate cases.** Instantiate and compute: the trivial group, rank 0 and 1, n = 0, 1, 2, the
   zero module, the zero ring, the unit ideal, a field versus a DVR, ramified, unramified and
   equal-characteristic coefficients, p = 2 and 3, characteristic p, residue field 𝔽₂, q = 1, the
   empty set, split versus non-split, an inseparable extension, a non-compact group where
   compactness is assumed, the identity map. If an instance makes the statement false, add the
   hypothesis or fix the claim, and add the instance as a negative-control Check (and `example`
   where statable).
2. **Context.** A Lean statement is only as true as its `variable`/`include` block: for each
   statement that needs a standing hypothesis (invertibility, compactness, Hausdorffness, a domain,
   finite residue field, finite generation, a scalar tower), confirm it is actually in scope.
3. **Generality.** The README's generality equals the Lean signature's. "For a DVR O" in prose with
   `ℤ_[p]` in Lean, or the reverse, is a defect: scope the statement or register the general form
   separately with its own hypotheses. Never narrow a statement below what the sources support;
   special cases are Checks.
4. **Conventions with a witness.** Every sign, parity, direction of a map and normalisation (δ_P
   versus δ_P⁻¹, ϖ versus p, left versus right cosets, 2πi factors, Tate twists, duals) is pinned
   by one explicit two-term example whose value is computed, as a README Check and a Lean test.
5. **Definitions.** Check each defining property against one example that satisfies it and one
   that fails it. Every denominator has a non-vanishing argument; every "canonical" map a domain.
6. **Vacuity.** A statement quantified over arbitrary data (a complex, a subgroup, a functor) with
   no hypothesis relating it to the construction is vacuous or false: fix it or delete it. A
   hypothesis that contains the conclusion is a defect.
7. **Duplication by object.** Grep the pinned Mathlib and Tau Ceti for the objects (structure
   fields, operators, hypotheses), not only for our declaration names. What exists at the pins is
   deleted and cited in the supplier contracts with its exact name and file.
8. **Interfaces.** Anything the prose promises (an extension map, a duality, a comparison) is a
   named target with hypotheses, or is cut.

Record the pass as a table: statement, instances tried, result, change.
