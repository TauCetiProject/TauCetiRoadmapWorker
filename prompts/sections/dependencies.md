## Suppliers and the merge order

The merge order is `__ORDER__`: one roadmap per line, suppliers before consumers. This roadmap may
consume only:

- Mathlib and Tau Ceti at the pins;
- roadmaps on the content repository's main branch (active and completed);
- roadmaps earlier than it in the merge order: __SUPPLIERS__;
- other authors' open pull requests only when they add a new roadmap folder (a pull request that
  revises or reports progress on an existing roadmap is never a dependency: cite that roadmap as it
  is on main), or when they drop an id this roadmap cites (below).

A roadmap already on main (this pull request revises it) consumes only the first two: it never names
a roadmap that is not on main as a supplier, whatever the merge order says (the dependency check
reports that edge UPWARD, and it is never waited for).

Never cite these roadmaps as suppliers: __DROPPED__.

**Ownership against open work.** Before this roadmap states a construction another roadmap could
own (a generic carrier, a comparison, a foundational theorem), search for the same object, by object
rather than by name, in the roadmaps on main, in the board's packages, and in the open roadmap pull
requests whose files this brief gives you (the sibling checkouts listed with a path: __SIBLINGS__).
Cite an existing owner, or say in your report why this roadmap is the owner, and give no declaration
of `Suggested.lean` the fully qualified name of a declaration in a roadmap you can read. Where the
brief names open pull requests without their files, or none, do not look for them yourself: say in
your report that ownership against open pull requests was not checked.

**Overlap with open pull requests.** `scratch/overlaps.md`, when the tools list it, says what the open
pull requests do to this roadmap and it to them, read from their heads (check each item before you
act on it):

- a second copy of a roadmap on main (the pull request adds this folder, and main has gained the
  roadmap since the pull request branched): read main's folder and confirm it (a branch that has
  merged main since, so that its text is main's with changes, is a revision and no copy). While it holds,
  nothing else about this text matters, because it cannot merge: the pull request becomes a revision
  of main's text (main merged into the branch, the README main's with this pull request's changes
  made in it, every id main defines kept or mapped) or is closed in its favour, and which is the
  author's decision. Repairing the statements of the copy is wasted work: a reviewer reports the
  second copy and what this text has that main lacks, and stops there; nobody else edits the copy;
- an id this roadmap cites that an open pull request drops (a revision renumbers or deletes the
  section, layer or target, or renames the heading): the citation breaks when that pull request
  merges. Cite the id as it is at that head (where the content now is); when the content is gone,
  cite another owner or state it here. A revision that maps the old id to its new place (a table
  whose header names the former numbering, or "Layer 1.5 is now §1.3") keeps the citation valid:
  the report does not list it, and the citation needs no change and no dependency. Citing a
  revision's numbering makes it a dependency: the report lists it under "only an open revision
  defines", and the worker names it in the merge-order paragraph of the pull requests it opens. A
  pull request opened any other way names it in its description;
- an id this roadmap's pull request drops that other roadmaps cite: keep the id, or give the old →
  new numbering in the README (a table whose header names the former numbering, one row per moved
  id, or a sentence "Layer 1.5 is now §1.3") so that the citing roadmaps can follow; the overlap
  check reads such a mapping and no longer lists the id. A roadmap on main that cites it breaks at
  the merge otherwise;
- another open pull request that changes this roadmap: when the heads conflict, or the two change
  the same targets differently, one is rebased on the other before either merges; say which, and do
  not restate what the other already changes;
- the closest roadmaps (three lists: open ones by vocabulary, those on main by vocabulary, and open
  or on main by the results they name and their headings, where the higher the score the likelier a
  second plan of the same subject under another title): read their scope and ownership and compare
  what each says it owns with what this roadmap owns. An object two roadmaps both own, or that each
  hands to the other, has no single owner: settle it as any other duplication, with one owner
  earlier in the merge order and a citation from the other. A roadmap on main is earlier than every
  open one: when it and an open roadmap both own something, the open roadmap cites the one on main
  (or narrows its own statement), never the reverse, whichever pull request is older.

**Citations are resolved, not remembered.** Check every cross-roadmap citation (roadmap, section or
target, declaration name) against the cited roadmap as it is now, every round: on main, or at the
head of its open pull request where this brief gives you that head. Check every library declaration
at the pins, not on a newer Mathlib or Tau Ceti. Before you report a citation or a declaration as
missing, confirm that the copy you searched is that main, that head or that pin.

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
