## Suggested.lean: the acceptance test

`Suggested.lean` is the roadmap's acceptance test, not an API catalogue. It states the roadmap's
headline theorems: the deep results the finished roadmap will be able to prove, written now against
the pinned Mathlib and Tau Ceti, each with `sorry`. A roadmap toward Fermat's Last Theorem would
state FLT itself: FLT is easy to state in Mathlib, although its proof needs everything the roadmap
builds. The README carries every target, API list and Check; the Lean file carries only what the
finished roadmap is tested against, so every statement in it must be one we are sure of.

- **Headline theorems first.** List the roadmap's headline theorems (its end theorems and the deep
  results it is built to reach), then state each one that is statable at the pins: every carrier in
  its statement exists in Mathlib or Tau Ceti at the pins or is a definition kept by the next rule.
- **Definitions only to make a headline statable.** If a deep theorem needs a few simple
  definitions to be statable, make those definitions, with real bodies on native carriers
  (predicates, structures bundling data that exists at the pins, specialisations of library
  notions), and state it. No other definitions.
- **Not statable: say so.** If a headline theorem is not statable at the pins, the module docstring
  says so (which theorem, and which carrier is missing) and nothing is stated in its place: no
  weakened substitute, bounded stand-in, long ad hoc encoding, free `Prop`-valued predicate or
  sorry'd data definition. A headline that is statable at the pins but absent and a headline forced
  through a stand-in are both defects.
- **No minimum size.** A short file is correct when little is statable; a file that states nothing,
  its docstring saying why, is correct when nothing is. It never exceeds __MAX_LEAN_LINES__ lines.
- **No padding.** Not wanted: API lemmas that restate a definition or follow in one tactic from the
  library; boundary and degenerate-case theorems; "controls"; declarations that mirror README
  Checks; examples that compute values; any quota of examples per definition. These stay in the
  README as targets and Checks. A proved `example` stays only when it tests something nontrivial
  about a kept definition, at most a handful in the file, and an admitted (`sorry`) `example` is
  never written.
- No sorry'd data: no `def`, `abbrev`, `instance` or `opaque` whose body is `sorry`, and nothing that
  mentions one. A heavy definition (a site or topos, a building, a tilt, a moduli stack, an
  analytification functor, a completion whose construction is itself a theorem) is a README target
  with its intended signature: formalising it is the library's work, the roadmap is the deliverable.
- A `sorry`'d declaration, and every `def` and `instance` whatever its body, keeps only the section
  `variable`s its type mentions, so hypotheses such as `[CompleteSpace X]` or `[Module.Finite A M]`
  silently disappear (a `theorem` proved later does not bring them back to the `def` it is about).
  Carry every needed binder explicitly in the declaration's own signature (or an `include` line).
  Check it mechanically: put `#check @name` for every declaration in one scratch file, elaborate it
  once, compare each elaborated type with its README statement, and say in your report that you did
  and what changed.
- A headline names the roadmap's objects. A theorem about free variables only (functions `ℂ → ℂ`,
  bare scalars, an arbitrary type) whose conclusion is algebra on its hypotheses is not a headline:
  state it on the actual carriers or name it in the docstring as not statable.
- Imports: the single line `import Mathlib` followed only by the individual `TauCeti.*` modules
  actually used (the library's root module re-exports nothing; every module named must exist at
  the pinned Tau Ceti); never `Mathlib.<submodule>` lines, never another roadmap's module, nothing
  from a roadmap that is still a pull request. The gate refuses any other import.
- Never `set_option` of any kind (no file-level `set_option`, no `set_option … in`). A statement
  that only elaborates with an override is restated so that it elaborates under the defaults.
- Form: one module docstring, "<title>: target signatures", saying that the roadmap is README.md
  and this file is not the roadmap, that it states the roadmap's headline theorems against the
  pinned APIs, and which headline theorems are not statable at the pins and what each lacks;
  `namespace TauCetiRoadmap.<Name>`; where the file states targets of several layers,
  `/-! ## Layer k: <title> -/` section comments in README order; a docstring on every declaration
  stating the mathematics (and, where useful, the source); `theorem`, never `lemma`; `#check` only
  to pin a library declaration.
- Never `#print axioms`, `#eval`, `#synth`, `True` placeholders, `def P : Prop := sorry`, a
  `Prop`-valued field standing for a condition you cannot state, a hypothesis that contains the
  conclusion, catalogues of names, or process comments. Use `sorry` honestly: a condition that
  cannot be stated yet is omitted, never named emptily.
- README and Lean agree: every Lean name appears in the README, every backticked name of this
  roadmap's own in the README is declared here, and the README's generality equals the Lean's.
  README API bullets and Checks are prose; they are not restated in the Lean.
