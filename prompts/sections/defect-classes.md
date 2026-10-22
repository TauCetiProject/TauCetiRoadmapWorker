## Defect classes that reviewers keep finding

Test every class on every target.

1. **Missing finiteness, flatness or smoothness hypotheses**: a finite set of places, a Noetherian
   base, proper and flat rather than merely proper, the whole group scheme smooth rather than its
   identity component, boundedness that holds only in finite type.
2. **False "iff" or false identification**: a converse that fails in characteristic p, "vanishing
   for the trivial character implies vanishing for all twists", normalised versus unnormalised
   induction (q^{1/2} need not exist in the coefficients when p = 2).
3. **Degenerate cases**: rank 0 makes a condition vacuous; p = 2 breaks divided powers or sign
   conditions; inseparable extensions; small n with exceptional stabilisers; 0/0 in a product
   formula; the zero ring; a space that is not quasi-separated.
4. **Sign and normalisation**: residue versus period factors of 2πi, Chern-class signs, modulus
   characters in adjunctions, duals of Tate modules, the direction of a comparison map.
5. **Unowned prerequisites**: a proof plan uses a result that no layer, no named supplier section
   and no pinned declaration provides; a standard theorem restated instead of cited.
6. **Supplier drift**: a cited supplier declaration or section that does not exist where it is
   cited (a roadmap's section on main or at its open head, a library declaration at the pin), a
   supplier cited for a statement it does not make, two owners for one object (including
   two open pull requests that each build it, and two `Suggested.lean` files that declare the same
   fully qualified name), a cycle between two roadmaps' layers, a `*Needs:*` edge that uses
   something the scope section excludes.
7. **Convention mismatches between roadmaps**: row versus column conventions, indexing by `Fin d`
   versus ℕ with junk values, additive versus multiplicative normalisations, different hypotheses
   for the same notion.
8. **Checks**: a Check that is false, vacuous (it also holds for the wrong object), or a
   restatement of the definition; fewer than three Checks for a definition; a Check about a carrier
   that does not exist in the file.
9. **Lean signature defects**: a truncated type, a sorry'd data field, a Lean statement at a
   different generality from the README (a missing Noetherian, topological-group or open-subgroup
   hypothesis), arbitrary-topology carriers that make a statement vacuous or false; a `def` or
   `instance` that lost a section variable its type does not mention; a quantifier order that lets
   a "uniform" bound depend on what it should not; natural-number subtraction or division where
   the junk value matters; `ULift` used to move morphisms where objects had to change universe.
10. **Source locators**: the wrong chapter or page, a source whose hypothesis the target drops, a
    misprint in the source copied into the roadmap, a locator into a text that does not contain the
    statement, a survey cited for a theorem that needs the primary source.
11. **Build order**: a target that needs a later one, a layer-level dependency that creates a
    coarse cycle, one `*Needs:*` line covering several theorems with different needs.
12. **Policy**: process words or pull-request numbers in the README or docstrings, `lemma`, a target
    without a locator, a `set_option`, an edit outside the roadmap folder.
13. **Overclaims in prose**: "every", "canonical", "unique", "for any scheme" where the source
    proves less; a scope paragraph that excludes something the proof plan then uses.
14. **Upward dependency**: a `*Needs:*`, supplier-table or "Supplied by" citation of a roadmap later
    in the merge order, or two roadmaps citing each other. Repair by repointing to the earliest
    owner or moving the consumed target down.
15. **Invented vocabulary**: a term that is neither standard nor defined in the roadmap. Use the
    literature's and Mathlib's name for the object, or define the new one.
16. **Padding in Suggested.lean**: declarations that restate definitions, follow in one tactic from
    the library, mirror Checks or compute small values (and boundary-case theorems, "controls",
    examples per definition): a P3 finding whose repair is deleting them (the README keeps the
    statement as a target or Check), never proving them in place. And the converse: a headline
    theorem that is statable at the pins but absent (P2).
