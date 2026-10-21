## Declared supplier gaps, the merge order, and settled findings

A design, and the package that follows it, may declare supplier gaps: a target whose prerequisite
is `gap:<slug>`, with a `gaps` entry in `design/plan.json` naming the `supplier` (a roadmap) and
its `section`, and a one-sentence `needs`, also listed under the README's gaps. The intake announces
each such gap on the supplier's open job on the board (or on this roadmap's issue when the supplier
has none) once the design is accepted. A declared gap is a stated contract, exactly like a supplier
citation of a roadmap section that is still in a pull request: it is not a finding, and its absence
from the supplier today is not one either. The gaps this roadmap declares: __GAPS__

For each declared gap, check that it is precise (the `needs` sentence says exactly what is missing,
with its hypotheses), minimal (it asks for nothing this roadmap, Mathlib, Tau Ceti or an earlier
roadmap already provides) and declared where the intake reads it (the same slug in `plan.json` and
in the README). Only these are findings: a gap whose supplier is `none` (unowned: P1), a vague or
compound `needs` (P2), a gap that asks for what is already provided (P2), and a dependency that
should have been a gap but is written as if supplied, citing a section or declaration that does not
exist where it is cited (P1). A review may accept conditional on the declared gaps (how to record
it is under the deliverables); a red-team pass simply does not raise them.

A roadmap at the design stage is not in the merge order yet: the dependency check places it right
after its last supplier, and the intake inserts it there when its design is accepted. "Not in the
merge order" is never a finding before then; an UPWARD or CYCLE line is.

A finding the last repair report marked `not actionable in this package`, with a reason, is settled
unless the reason is wrong. If you still hold it, raise it again with the line
`Re-raised: <its id in the repair report>` in the finding and say why the reason fails. When every
blocking finding of a review re-raises such a finding, the board parks the roadmap for a human
instead of starting another repair round.
