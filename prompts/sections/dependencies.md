## Suppliers and the merge order

The merge order is `__ORDER__`: one roadmap per line, suppliers before consumers. This roadmap may
consume only:

- Mathlib and Tau Ceti at the pins;
- roadmaps on the content repository's main branch (active and completed);
- roadmaps earlier than it in the merge order: __SUPPLIERS__;
- other authors' open pull requests only when they add a new roadmap folder (a pull request that
  revises or reports progress on an existing roadmap is never a dependency: cite that roadmap as it
  is on main).

Never cite these roadmaps as suppliers: __DROPPED__.

**Ownership is checked against open work, not only against main.** Two open roadmaps independently
specifying the same reusable construction is the most frequent finding in roadmap reviews, and the
costliest in rounds. Before this roadmap states a construction another roadmap could own (a generic
carrier, a comparison, a foundational theorem), search the open roadmap pull requests (__SIBLINGS__)
and the board's packages for the same object, by object rather than by name; cite an existing owner,
or say in your report why this roadmap is the owner. Check that no declaration in `Suggested.lean`
has the fully qualified name of a declaration in another open roadmap.

**Citations are resolved, not remembered.** Every cross-roadmap citation (roadmap, section or
target, declaration name) is checked against the cited roadmap as it is now: on main, or at the
head of its open pull request. Sections are renumbered and moved during review; a citation that was
right last round is checked again. A citation correct on a newer main but absent at the pin is a
defect.

A notion this roadmap needs from a roadmap later in the order is stated here as a target instead
(the notion moves down), and the move is recorded in your report. Never add a `*Needs:*` line, a
supplier-table row or a "Supplied by" citation of a later roadmap. Cite suppliers by roadmap name
and section or target, never by pull-request number. What another roadmap plans is imported, never
planned again; an extension in an existing roadmap's direction is "<that roadmap>, Part II", whose
introduction says where the base roadmap stops.

Check the direction mechanically after your edits:

    __DEPCHECK__

It must show no UPWARD or CYCLE line for this roadmap; include its output in your report. A
roadmap at the design stage is not in the merge order yet: the check places it right after its last
supplier (the intake inserts it there when its design is accepted), so "not in the merge order" is
not a defect before then. If you
believe it reports a false positive, say so with the exact line; never edit the tooling or the
merge-order file.
