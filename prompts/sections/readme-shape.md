## The README in TauCetiRoadmap form

Read two or three roadmaps on the content repository's main branch (under `__ACTIVE_DIR__/`, the
completed ones under `__COMPLETED_DIR__/`) before writing. A README is read by mathematicians first:
it opens with an overview they can read cold, then the specification, then the bookkeeping.

Shape (headings as upstream uses them), in this order:

1. `# Roadmap: <lower-case title>`.
2. **The opening overview**, before any `##` heading, beginning `**Summary.**`, for a mathematician
   who has never seen the roadmap. It may run to several paragraphs (roughly 150 to 350 words for a
   short roadmap, up to about 500 for a long one) and says, in ordinary mathematical language:
   - what the roadmap builds: its main objects, and the standing hypotheses every statement shares;
   - its headline theorems, two to four, each stated with its real hypotheses ("If G is a finite
     group and k a field of characteristic prime to |G|, every k[G]-module is semisimple"), not as
     topics ("semisimplicity results, Layer 3"); every statement here is one the layers make, with
     the same hypotheses;
   - how it sits in Tau Ceti: the two to five roadmaps or library areas a reader needs to place it,
     by name, one clause each saying what passes between them. The complete list is in the supplier
     contracts at the end and is not reproduced here;
   - a short guide to the layers, one line per layer or one paragraph. A long guide may stand under
     `## How to read the build` directly after the overview instead.
   No Lean identifiers are needed here, no locators, no `(§x.y)` lists, no ownership bookkeeping, no
   inventory of carriers: the rest of the README carries those.
3. `## Conventions`: standing hypotheses, notation, normalisations, universe and coefficient choices.
4. `## Layer k: <title>` in build order, with `### k.n <topic>` subsections written as prose:
   "Define `fooBar` as … . Prove that `fooBar` is multiplicative on coprime arguments and vanishes
   at zero (Source, Thm 3.2, p. 41). *Needs:* Mathlib `X`, Layer 1 `Y`, OtherRoadmap §2.1."
   Library and supplier declarations in backticks; hypotheses in the sentence; the locator in
   parentheses at the claim. Each definition lists its API as bullets that state each fact in prose,
   hypotheses and conclusion. An API bullet carries a backticked Lean name only when that
   declaration exists in `Suggested.lean` (a headline statement or one of the few definitions it
   needs): a backticked name of this roadmap's own is a claim that `Suggested.lean` declares it,
   and the gate checks every such claim. A name on a completed item's `*In Tau Ceti:*` line is a
   library declaration, not such a claim (the section on the roadmap as a record). A proposed name
   that is not declared is written in prose ("a map we call the transfer"), so a reader can tell a
   proposal from a declaration. Every layer ends with `### Examples` and `### Dependencies`.
5. Optionally `## Worked examples` and `## Downstream consumers`.
6. `## Scope and ownership`: what this roadmap owns and what it leaves to named roadmaps, by section.
   This is the only place that says "stated by roadmap X", and the only place that says "already in
   the library" of a target this roadmap does not own: one Mathlib has, one another roadmap owns, or,
   for a new roadmap, one Tau Ceti already has. Such a target is deleted, not left as a "(removed)"
   stub, and listed here as a deferral. A completed item of this roadmap is never deleted: it stays
   in its layer and names the declarations that realise it (the section on the roadmap as a
   record). A kept variant says in one clause how it differs.
7. `## Exact supplier contracts`: per supplier a `### From …` subsection (`From Mathlib`, `From Tau
   Ceti`, `From TauCetiRoadmap.<X>`), the declarations or sections consumed, by name, and what is
   assumed of them.
8. `## References` last.

Because the scope and the supplier contracts now come after the layers, a sentence in them that
points at the layers says "above", and the guide to the layers, which comes first, says "below".

### Checks

A definition or theorem may carry a `**Checks.**` bullet list: sanity checks, unit tests for the KEY
definitions and theorems, one bullet per test, each saying in prose what is computed and what a
plausible wrong definition would get wrong (a computed value, the degenerate case, agreement with the
library's notion, a non-example); a Check never names or claims a Lean counterpart. Give Checks where
they earn their place: there is no minimum per definition, per section or per roadmap, and a
definition with no natural test has none. Every Check you write must be true and must test something
(a Check that every candidate passes, the identity or every equivalence say, tests nothing); a key
definition with an evident plausible wrong variant deserves one Check that tells the two apart.

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
- Roadmaps on main and in open pull requests written before this shape have the scope and the
  supplier contracts near the top. That order is not a defect: a review never raises it as a
  finding, and a roadmap is brought into this order when it is next revised (`tauceti-roadmap
  layout` does it as a pure reordering).
