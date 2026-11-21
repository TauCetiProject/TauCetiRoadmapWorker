## The roadmap as a record

A roadmap is read in two ways: as the plan for what remains, and as the record of what the library
now contains. The project site's structure page and each roadmap's generated `STATUS.md` read the
layers of its README, and the coverage block of `STATUS.md` is keyed by the layer ids ("Layer 0",
"Layer 1", …). Deleting the layers the library has completed destroys that record; reusing a layer
id for another subject falsifies it. For a roadmap already on the content repository's main branch,
whatever the pinned libraries contain:

1. **Completed material stays.** A layer, definition, theorem, Check or worked example stays in the
   README after the library proves it: in its place in the layer order, with its full readable
   statement (hypotheses and conclusion as mathematics). It is never deleted, never moved wholesale
   into `## Exact supplier contracts`, and never replaced by a list of declaration names.
2. **Layer ids are stable.** Every layer of the README on main keeps its number (or letter) and its
   subject. A new layer gets a new id; no layer is renumbered so that an existing id changes
   meaning, and no id is reused. The same holds for the numbered milestones, parts and sections the
   README on main uses as its tracked structure.
3. **A completed item names what realises it.** Where the pinned Tau Ceti has the declarations that
   realise an item, the item names them exactly, after its statement, on a line
   `` *In Tau Ceti:* `Foo.bar`, `Foo.baz`. `` The names help the reader; the statement stays. They
   are library declarations, not claims that `Suggested.lean` declares them, and the gate reads them
   so. A layer the library has completed keeps every statement; only its proof routes may shrink, a
   route the library has realised to a sentence or two.
4. **What "cite, do not restate" removes.** The duplication rule applies to what Mathlib has and to
   what another roadmap owns (stated there). It never applies to the Tau Ceti declarations that
   realise this roadmap's own items: those items are its record. An item the README on main has as a
   layer item is this roadmap's own, whatever the library has proved since. A new roadmap (one not
   on main) whose intended statement Tau Ceti already has cites it: that was never its work.
5. **`Suggested.lean`** follows the roadmap's existing practice: a statement the pinned library
   proves is not restated there with `sorry`. The README is the record.
6. **Timeless text still holds.** The README does not narrate ("done", "merged", "so far", dates,
   pull-request numbers). An `*In Tau Ceti:*` line states a fact at the pin, as a supplier contract
   does, and is allowed.

A review of a revision compares the README on main (at the pull request's merge base) with the head,
layer by layer and item by item. A layer or item the README on main has that the head removes, a
layer id whose subject the head changes, and a completed item reduced to a declaration name are each
a finding (P1), whatever the library contains: a finding although no stated result becomes false,
and not a matter of shape or headings. The repair is "restore it under its id with its statement".
"The pinned Tau Ceti already proves this target" is not a duplication finding when the target is
the roadmap's own: the repair is to name the declaration in the item, never to delete the item. A
repair settles such a remark the same way, and a remark that asks it to delete such a layer or item
is applied by naming the declaration instead (`applied differently`).
