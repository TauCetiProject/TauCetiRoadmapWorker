## The downstream-use check

A roadmap is read by the people who will state something with it. A review that checks each target
against its source and its neighbours can pass every target and still leave the statement the roadmap
exists for unwritable: a clause nobody specifies, or an interface that does not fit the supplier's. In
a full review (not an incremental one), test that directly, once:

1. **Name the statements.** Take the one to three statements the roadmap is for: the definition or
   theorem its target issue asks for, what its "Why Tau Ceti wants it" and downstream consumers name
   (the plan's `consumers`), the result of the research paper it cites as its goal. Say which you
   took and from where. When the README names none, that is the first remark: ask for it.
2. **Clause by clause.** Write each statement out as its clauses: every object, hypothesis and
   quantifier. For each clause name what provides it: a target of this README by its id, a supplier's
   section or declaration (resolved as that supplier is now), or a pinned declaration. A clause with
   no provider is a gap: a finding (P2) whose witness is the clause and whose repair is the target
   that would provide it, or the supplier contract that should. A clause the README leaves open
   between two readings (for some weak differential or for every one; which measure "almost
   everywhere" refers to; how a dimension is tied to a rank) is weighed like any other remark (the
   section on writing remarks): a finding when the two readings give different objects or make a
   stated result true under one and false under the other (the witness is an instance on which they
   differ), a small correction that asks the README to choose when they provably agree.
3. **In Lean.** Is the statement, or the definition it rests on, in `Suggested.lean`? When it is
   statable at the pins and absent, that is the finding of the rule on headline theorems. When the
   module docstring calls it not statable, check the reason against the suppliers as they are now: a
   carrier the docstring says nobody has, which a roadmap on main now declares in its own
   `Suggested.lean`, makes the docstring wrong (a small correction), and whether the statement is then
   written is decided by the rule on imports. Where elaboration is available, write the statement in
   your scratch directory against the files that hold its carriers and report what the attempt
   showed: the instance arguments it needed, a hypothesis the README does not mention, a universe or
   coercion the prose hides. The probe finds; the section on writing remarks weighs, as for anything
   else. A hypothesis the statement needs and the README's statement lacks (verify it: an instance
   on which the README's statement fails, or the supplier's declaration that requires it), a README
   statement at a different generality from what can be written, and a supplier interface the
   statement cannot be written against are findings. A detail of representation that changes no
   statement (which instance carries a structure, an explicit coercion) is a small correction or a
   format note. What you suspect and could not verify is a question for the author. The scratch
   statement (at most 15 lines) is the witness in each case.
4. **Record it.** Under `## Verified sound`, one line per statement: `Downstream use: <statement>,
   <n> clauses: <clause → provider; …>`. The line is evidence like a row of the adversarial table: a
   line that names no provider for a clause is a remark above it.
