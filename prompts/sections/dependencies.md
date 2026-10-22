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

A notion this roadmap needs from a roadmap later in the order is stated here as a target instead
(the notion moves down), and the move is recorded in your report. Never add a `*Needs:*` line, a
supplier-table row or a "Supplied by" citation of a later roadmap. Cite suppliers by roadmap name
and section or target, never by pull-request number. What another roadmap plans is imported, never
planned again; an extension in an existing roadmap's direction is "<that roadmap>, Part II", whose
introduction says where the base roadmap stops.

Check the direction mechanically after your edits:

    __DEPCHECK__

It must show no UPWARD or CYCLE line for this roadmap; include its output in your report. A
`moved:` line means the merge order lists this roadmap above a supplier it names and the worker moves
it right after the last of them when it submits: that is not a defect. A roadmap at the design stage is not in the merge order yet: the check places it right after its last
supplier (the intake inserts it there when its design is accepted), so "not in the merge order" is
not a defect before then. If you
believe it reports a false positive, say so with the exact line; never edit the tooling or the
merge-order file.
