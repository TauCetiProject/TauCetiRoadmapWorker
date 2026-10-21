## Suggested.lean: states, does not develop

The README carries the full detail; `Suggested.lean` states a compact set of signatures that
contributors will be held to, so every statement in it must be one we are sure of.

- At most __MAX_LEAN_LINES__ lines, written to this shape from the start. It holds only: (a) light
  definitions with real bodies (predicates, structures bundling data that exists at the pins,
  specialisations of library notions); (b) the end theorems and the milestone of each layer,
  proved by `sorry`, and only when every carrier in the statement exists in Mathlib or Tau Ceti at
  the pins or is a kept real-bodied definition; (c) at most two proved, discriminating `example`s
  per kept definition, testing a definition this roadmap introduces and exhibiting one of its main
  themes. No admitted (`sorry`) examples; no examples about library objects unrelated to the
  roadmap; no random numerical calculations. A boundary case that cannot be proved yet becomes a
  named `sorry`'d theorem target listed in the README.
- Headline theorems appear only when they are easy to state on existing carriers. Never force a
  headline through a weakened substitute, a bounded stand-in, a long ad hoc encoding or a sorry'd
  data definition; a headline whose objects are not formalised yet stays a README target. A missing
  easy headline and a forced headline are both major defects.
- No sorry'd data: no `def`, `abbrev`, `instance` or `opaque` whose body is `sorry`, and nothing that
  mentions one. A heavy definition (a site or topos, a building, a tilt, a moduli stack, an
  analytification functor, a completion whose construction is itself a theorem) is a README target
  with its intended signature: formalising it is the library's work, the roadmap is the deliverable.
- A `sorry`'d declaration keeps only the section `variable`s its type mentions, so hypotheses such
  as `[CompleteSpace X]` or `[Module.Finite A M]` silently disappear. Carry every needed binder
  explicitly in the declaration's own signature (or an `include` line) and check the elaborated
  type with `#check @name` in a scratch file.
- Imports: the single line `import Mathlib` followed only by the individual `TauCeti.*` modules
  actually used (the library's root module re-exports nothing; every module named must exist at
  the pinned Tau Ceti); never `Mathlib.<submodule>` lines, never another roadmap's module, nothing
  from a roadmap that is still a pull request. The gate refuses any other import.
- Never `set_option` of any kind (no file-level `set_option`, no `set_option … in`). A statement
  that only elaborates with an override is restated so that it elaborates under the defaults.
- Form: one module docstring, "<Name>: representative target signatures", saying that the roadmap
  is README.md and that the file records statements suggested against the pinned APIs and is not
  exhaustive; `namespace TauCetiRoadmap.<Name>`; layers as `/-! ## Layer k: <title> -/` section
  comments in README order; a docstring on every declaration stating the mathematics (and, where
  useful, the source); `theorem`, never `lemma`; `#check` only to pin a library declaration.
- Never `#print axioms`, `#eval`, `#synth`, `True` placeholders, `def P : Prop := sorry`, a
  `Prop`-valued field standing for a condition you cannot state, a hypothesis that contains the
  conclusion, catalogues of names, or process comments. Use `sorry` honestly: a condition that
  cannot be stated yet is omitted, never named emptily.
- README and Lean agree: every Lean name appears in the README, every README Check that is
  statable at the pins has its `example`, and the README's generality equals the Lean's.
