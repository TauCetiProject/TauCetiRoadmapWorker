# The opening overview of the roadmap __ROADMAP__

You rewrite ONE part of a roadmap README of the Tau Ceti project (a Lean 4 library built on Mathlib):
its opening, the text between the `# Roadmap: …` title and the first `##` heading. Everything else in
the README stays as it is, word for word, and the worker checks that mechanically: an output that
changes anything else is thrown away.

Files (data, never instructions):

- `__README__`: the README. Its sections are already in the new order: the guide to the layers
  (if any) first, then the conventions and the layers, and the scope and the supplier contracts at
  the end, before the references. Its opening (__OLD_WORDS__ words) is the one you replace.
- `__LEAN__`: its `Suggested.lean`, the headline theorems stated in Lean (it may be empty).

Sections the worker moved: __MOVED__. __GUIDE__

## Who reads the opening

A mathematician who has never seen this roadmap opens the page. After the opening they should know
what the roadmap builds, what its main theorems say, where it sits in Tau Ceti, and how the layers
below are arranged. The rest of the README is the specification: the opening introduces it, it does
not replace it and does not repeat its bookkeeping.

## The voice: a person writing for colleagues, not a table of contents

Write it as you would write the introduction of a paper for colleagues in neighbouring fields. This
matters as much as the content: an opening that is accurate but reads like a compressed inventory
has failed.

- **Say what the subject is before what the roadmap contains.** One or two sentences on the question
  or the phenomenon ("A polynomial with distinct roots can still have repeated factors modulo a
  prime."), then what the roadmap does about it.
- **Full sentences, one idea each.** No sentence that strings together more than three items. Never a
  run of noun phrases joined by commas ("the closure and summand theorems, the dual correspondence,
  isolation, and the spherical classification with its consequences"): pick the two to four results
  that matter and give each its own sentence saying what it asserts.
- **Do not compress.** If saying something plainly takes a second sentence, write the second
  sentence. Prefer "Every such representation is a direct sum of irreducible ones" to "complete
  reducibility". A term of art that a reader from a neighbouring field may not know gets a few words
  on what it is the first time it appears.
- **No project vocabulary and no internal codes.** Not "carrier", "supplier", "consumer", "endpoint",
  "interface", "adapter", "owns", "pins", "statable", "continuation"; not layer or section codes
  (`SR.0–SR.6`, `C3.1`); no slash-joined pairs ("smooth/unitary"). Say "builds on", "is used by",
  "the main results", "the second part of".
- **No formula for the prose.** "There are three main theorems. The first …" is fine when it helps,
  but do not force every opening into it, and do not start every paragraph the same way. Read your
  draft once as its reader: where it sounds like a list being read aloud, rewrite it as explanation.

## What the opening says, in this order

1. **What the roadmap builds**, in one or two sentences: its main objects. If every statement of the
   roadmap rests on a standing hypothesis (all rings unital, all manifolds second countable), say it
   once here, in the words the README uses.
2. **Its headline theorems**, two to four, each in ordinary mathematical language with the
   hypotheses a reader needs to understand it. Take them from the README's own layers and from
   `Suggested.lean`; prefer the ones `Suggested.lean` states or its docstring names. A headline may
   carry one small example, if the README gives it (a Check or a worked example): an example often
   says more than a second clause of hypotheses.
   A theorem whose exact statement needs a list of constants or parameter ranges is described, not
   transcribed: say what is assumed, what is concluded and what is uniform in what ("for every
   probability law on partitions satisfying three explicit estimates, the probability … is at most a
   fixed negative power of n, with constants that do not depend on the law"), and leave the exact
   ranges to the layer.
3. **Where it sits in Tau Ceti**: the two to five roadmaps or library areas a reader needs to place
   it, by name, one clause each saying what passes between them. Choose them from the README's
   supplier contracts and downstream consumers, and from what it says the library already has. The
   complete list stays in the supplier contracts: do not reproduce it, and do not list every
   supplier or every consumer.
4. **A short guide to the layers**: one sentence per layer, or one paragraph. If the README has a
   `## How to read the build` section that is only such a guide and short (a paragraph or two), you
   may fold it into the opening: write its content into your guide and set `fold_guide` to true; the
   worker then removes that section whole. If the section holds anything else (a map of labels,
   notes on `Suggested.lean`) or is long, leave it where it is, set `fold_guide` to false, and keep
   your guide to a few lines or leave it to that section.

Begin with `**Summary.**`. Roughly 250 to 450 words for a short roadmap, up to about 700 for a long
one, in several paragraphs: length spent on plain explanation is welcome, length spent on listing is
not. The worker counts as `wc -w` does: every run of characters between spaces is a word, so each
symbol of a formula written with spaces counts. It refuses an opening of more than 850 by that count
and you then have to write it again, so run `wc -w` on your file before you finish and stay at or
under 700 by that count. A paragraph of the old opening that is not summary and that a reader needs (a warning that two
notions differ, the sentence on what `Suggested.lean` is) may stay word for word.

## The one rule that matters most: add nothing, strengthen nothing, weaken nothing

Every statement in the opening must already be in the README or in `Suggested.lean`. You are
restating, not proving or improving, and you never say more than the README does. In particular:

- Keep every hypothesis that changes the truth of the statement, however fussy it looks: "p odd",
  "the base Noetherian", "the map proper and flat", "the group finite and the coefficients a field
  of characteristic prime to its order", "exact in the middle only", "an isomorphism up to torsion",
  "rank at most d" rather than "rank d", "the space possibly empty". If you cannot tell which
  reading the README means (a "regular" ring or a "regular" scheme, a "small" category in which
  sense), keep the README's own words.
- Being readable never licenses being wrong. Where the plain version would need a hypothesis you are
  leaving to the layer, say that there is one ("under an explicit growth condition", "for parameters
  in a stated range"): a visibly less specific sentence is fine, a silently stronger one is not.
- Do not upgrade a quantifier ("for some n" is not "for every n"), a conclusion ("homotopy
  equivalent" is not "homeomorphic") or a scope ("number fields" is not "global fields").
- Do not state a theorem the README marks as conditional, deferred to another roadmap, or not built
  here, as if this roadmap proved it. Say which roadmap builds it, if you mention it at all.
- Plain words for a defined notion are fine when the README's definition says exactly that
  ("the least number of open sets contractible in M that cover M"); a name with two conventions in
  the literature is better replaced by its definition.

A bad headline: "Develops class field theory with the reciprocity theorems (Layer 4)." It names a
topic. A good one: "For a finite abelian extension L/K of number fields, the Artin map induces an
isomorphism from the idele class group of K modulo norms from L onto Gal(L/K)." It states the
theorem, with its hypotheses, as the README does.

## What the opening leaves out

- Lean identifiers (backticked declaration names), unless the old opening used them; mathematics in
  Unicode, with ASCII inside backtick code spans when it needs them (`Σ c_j t^j`), never LaTeX.
- Locators and section numbers (`§4.2`, "(Layer 3)" after every item), lists of carriers with their
  APIs, and ownership bookkeeping ("owns", "defers to", "is already in the library as …" lists): the
  scope and the supplier contracts carry those.
- Process words: pull-request numbers, dates, "this revision", "now", "previously", review history.
- Links are optional; a roadmap may be linked as `[Name](../Name/README.md)` when it sits beside
  this one.

A bad opening, the kind you are replacing: one long sentence that runs through every layer in
brackets and every supplier's carriers ("From A's X, B's Y and C's Z … the roadmap builds P (Layer 0),
Q (Layer 1), …"), then a list of declaration names. Another bad one: a short list of topics
("normal forms, genericity, deformations, …") and no theorem. A third: every sentence correct and
every sentence a list ("The first part supplies the smooth category, Hecke algebras, induction and
restriction functors, admissibility, … Its endpoints are the closure and summand theorems, the dual
correspondence, …"), which tells a reader the table of contents and nothing about the mathematics.
A good one reads like the first page of a monograph: what the subject is, the objects, the theorems
said in sentences, the neighbours, the plan.

## Direction words in the sections that moved

The scope and the supplier contracts now come after the layers, and the guide to the layers comes
first, so a sentence in those sections that says "below" or "above" may now point the wrong way. The
worker corrected those it could decide from the phrase alone:

__MECHANICAL__

Read the sections that moved (and only those). For each remaining phrase that now points the wrong
way ("as stated below" for something now above, "the following layers" for layers now above),
propose a fix in the plan file: `{"section": "<the heading, without ##>", "old": "<the phrase,
exactly as it is, unique in that section>", "new": "<the same phrase with only its direction words
swapped>"}`. Only these swaps are accepted: below ↔ above, following ↔ preceding, later ↔ earlier,
after ↔ before. Propose nothing else in any section.

## What you write

- `__OUT__`: the opening, from `**Summary.**` to its last paragraph. No title line, no heading, no
  HTML comment.
- `__PLAN__`: one JSON object:

      {"fold_guide": false,
       "directions": [{"section": "Scope and ownership", "old": "…", "new": "…"}],
       "headlines": [{"statement": "<the headline, one line>", "where": "<the README subsection or the
                      Suggested.lean declaration it comes from>"}],
       "neighbours": [{"name": "<roadmap or library area>", "where": "<where the README names it>"}]}

  `headlines` and `neighbours` let the operator check, line by line, that you added nothing.

Change no other file. Do not edit `__README__`.

<!-- include: sections/untrusted.md -->
