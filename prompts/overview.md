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

## What the opening says, in this order

1. **What the roadmap builds**, in one or two sentences: its main objects. If every statement of the
   roadmap rests on a standing hypothesis (all rings unital, all manifolds second countable), say it
   once here, in the words the README uses.
2. **Its headline theorems**, two to four, each in ordinary mathematical language with its real
   hypotheses: "There are three headline theorems. The first …". Take them from the README's own
   layers and from `Suggested.lean`; prefer the ones `Suggested.lean` states or its docstring names.
   A headline may carry one small example, if the README gives it (a Check or a worked example).
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

Begin with `**Summary.**`. Roughly 150 to 350 words for a short roadmap, up to about 500 for a long
one; several paragraphs are fine. A paragraph of the old opening that is not summary and that a
reader needs (a warning that two notions differ, the sentence on what `Suggested.lean` is) may stay
word for word.

## The one rule that matters most: add nothing, strengthen nothing, weaken nothing

Every statement in the opening must already be in the README or in `Suggested.lean`, with the same
hypotheses. You are restating, not proving or improving. In particular:

- Keep every hypothesis that changes the truth of the statement, however fussy it looks: "p odd",
  "the base Noetherian", "the map proper and flat", "the group finite and the coefficients a field
  of characteristic prime to its order", "exact in the middle only", "an isomorphism up to torsion",
  "rank at most d" rather than "rank d", "the space possibly empty". If you cannot tell which
  reading the README means (a "regular" ring or a "regular" scheme, a "small" category in which
  sense), keep the README's own words.
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
("normal forms, genericity, deformations, …") and no theorem. A good one reads like the first page of
a monograph: the objects, the theorems with their hypotheses, the neighbours, the plan.

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
