## The README in TauCetiRoadmap form

Read two or three roadmaps on the content repository's main branch (under `__ACTIVE_DIR__/`, the
completed ones under `__COMPLETED_DIR__/`) before writing, and match their shape. Ours are more
detailed, never thinner: every definition keeps its API list and at least three discriminating
Checks, every theorem its exact hypotheses, every claim its locator, every layer its dependencies.

Shape (headings as upstream uses them):

1. `# Roadmap: <lower-case title>` and an opening `**Summary.**` paragraph: what is built, from what,
   to what end theorems.
2. `## Scope and ownership`: what this roadmap owns and what it leaves to named roadmaps, by section.
   This is the only place that says "already in the library" or "stated by roadmap X"; a target that
   exists elsewhere is deleted, not left as a "(removed)" stub, and listed here as a deferral. A kept
   variant says in one clause how it differs.
3. `## Conventions`: standing hypotheses, notation, normalisations, universe and coefficient choices.
4. `## Exact supplier contracts`: per supplier (`From Mathlib`, `From Tau Ceti`, `From
   TauCetiRoadmap.<X>`), the declarations or sections consumed, by name, and what is assumed of them.
5. `## How to read the build`: one paragraph on the layer order and what each layer delivers.
6. `## Layer k: <title>` in build order, with `### k.n <topic>` subsections written as prose:
   "Define `fooBar` as … . Prove `fooBar_mul`, `fooBar_zero` (Source, Thm 3.2, p. 41). *Needs:*
   Mathlib `X`, Layer 1 `Y`, OtherRoadmap §2.1." Lean names in backticks; hypotheses in the
   sentence; the locator in parentheses at the claim. Each definition lists its API as named lemmas
   and its unit tests as a `**Checks.**` bullet list, one bullet per test, each saying what is
   computed and what a plausible wrong definition would get wrong (a computed value, the degenerate
   case, agreement with the library notion, a non-example). Every layer ends with `### Examples` and
   `### Dependencies`.
7. `## Downstream consumers`, optionally `## Worked examples`, and `## References`.

House style and rules:

- Mathematics in Unicode, with ASCII inside backtick code spans (`H¹(X_K̄, ℚ_ℓ)`,
  `det(1 − ρ(F_v) T)`). Never LaTeX delimiters `\( … \)`, `\[ … \]`, `$…$`, `$$…$$`; a display
  formula becomes an inline code span or an indented code block.
- Timeless: no process narrative ("so far", "to be checked", "current", "removed", "checkpoint",
  "handoff", "packet", job ids, review or pull-request references, round labels, local paths). A
  reader a year from now cannot tell which parts were contentious.
- A proof sketch names a route and its inputs; it does not make the route mandatory. A target's
  `*Needs:*` line lists what its statement and its route need, item by item, so that an implementer
  who proves it another way is not blocked behind an unrelated layer.
- Definite about scope: nothing is "optional", "deferred" or "for later". Every item lives in some
  layer. A roadmap that is unwieldy is split into Parts.
- No target-numbering tables (`**T017**` blocks) and no `Hypotheses:/API:/Tests:/Sources:` field
  lists: the catalogue is an input, the README is prose for mathematicians.
- Standard vocabulary: every term is either the literature's or Mathlib's name for the object, or
  is defined where it first appears.
- Mathematics, not algorithms: no executable procedures, certificates, implementations or large
  computations. The mathematical background of an algorithm and small worked examples as Checks
  are welcome.
- Cross-roadmap citations name the roadmap and its section or target, never a pull-request number,
  including for roadmaps still in pull requests.
- Do not edit the content repository's top-level README roadmap list: it is regenerated after merge.
