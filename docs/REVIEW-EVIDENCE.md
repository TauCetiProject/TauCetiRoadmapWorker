# The evidence behind the review checklists

The adversarial pass, the defect classes, the Lean form and the review, red-team and fix briefs are
the product's quality control. This document records what reviews of the roadmap repository
actually found, what they missed, what a missed defect costs once the library tries to formalize
it, and what each check in the briefs costs to run. Each change to the briefs is justified from
that evidence, and so is each alternative that was considered and rejected.

Examples name the roadmap they come from. The survey's data (per-finding records, per-pull-request
dossiers, the Tau Ceti sample) is kept outside the repository.

## 1. In short

- **The checklists already name the defects that get through, but nobody computes the checks.**
  - Only 6% of 2,659 review findings fall outside every existing checklist section.
  - Defects found later in merged roadmaps sat, in 67 of 111 traceable cases, in a passage the
    original review had read and passed, typically with a one-line summary.
  - Of 50 defects that formalizers met in Tau Ceti, the original review had raised none.
- **What finds defects is computation:** instantiating a small case, recomputing a sign, opening the
  pinned declaration, resolving a citation at the cited head. The reviews that missed defects
  summarised.
- **Review rounds do not converge.** On roadmap pull requests that reach a later round, each round
  finds about 1.5 findings per pull request, 0.6–0.8 of them mathematical, from round 3 to round
  12. Each round finds only a few of the defects that remain.
- **Reviewers are rarely wrong.** About 1% of findings with a known outcome were shown to be
  mistaken (28 of 2,417).
- **A defect that reaches Tau Ceti usually costs little, but sometimes costs a great deal.**
  - Formalizers worked around 62% of defects in the same pull request.
  - A pull request that meets a defect takes twice the review rounds (median 8 against 4).
  - About one defect in twelve costs 10–30 times a normal pull request, almost always because a
    rubric enforces the roadmap's text literally.
  - An estimated 15–35% of defects are met more than once before they are repaired.
- **A check's cost is turns times context, plus thinking, not the brief's length.** A median
  roadmap in the current style is about 100k tokens of README and 20k of Lean, and every turn of a
  whole-document review re-reads them.

The changes therefore do four things:

1. Make the adversarial pass a computed record.
2. Add the few classes the checklists did not name.
3. Run the expensive checks once at full depth rather than every round, and narrow them to where
   they pay.
4. Leave to formalization the defects it handles cheaply.

Checks that are mechanical (signatures, citations, open-PR ownership) belong in the harness. They
are listed as follow-ups in section 8.

## 2. Data and method

**Sources**
- All 1,092 pull requests of the roadmap repository up to the survey, with their
  3,387 comments, 2,586 reviews and 1,492 inline review comments, fetched through the GraphQL API.
- The repository's history and its 253 issues.
- About 1,900 Tau Ceti pull requests that mention a roadmap.

**Classification**
- A pull request is a **new roadmap** when it adds a README.md for a folder that did not exist on
  main when the pull request was opened (365 pull requests).
- It is a **revision** when it changes the README of a roadmap already on main (179).
- Progress reports and other pull requests (548) were set aside.
- Each roadmap pull request became a chronological dossier: its description, commits, reviews and
  comments, each tagged as the author's or another account's.

**Extraction**
- Twelve agents read the dossiers in batches. They recorded every substantive finding in a fixed
  schema:
  - reviewer and reviewer type (human, AI review, the author's own red team, a maintainer's agent);
  - round, and whether the finding concerns text an earlier round had passed (`escape`);
  - category, severity and outcome (applied, applied differently, disputed and upheld, disputed and
    unresolved, ignored);
  - a summary and a quote of at most 30 words;
  - the existing brief section that should have caught it, or none.
- Two agents read the revision pull requests for defects in merged text: who found each one, how
  long after the merge, and whether the original review had examined the passage.
- One agent traced defects reported by formalizers in Tau Ceti back to the roadmap pull request
  that introduced them.
- One agent compared 109 Tau Ceti pull requests that met a roadmap defect with 100 random clean
  ones from the same period.

**Totals:** 2,659 review findings, 345 records about merged text, 50 formalizer-reported defects,
209 Tau Ceti pull requests.

**Limits**
- Category, escape and outcome are an agent's reading of each review, not a human's. Quotes were
  spot-checked against the raw data. One attribution was wrong: the extraction credited a
  maintainer's review with the Dieudonné-module counterexample of the finite flat group schemes
  roadmap, but a later review from the author's account found it. That attribution is not used here.
- "Escape" depends on reconstructing rounds from comment order. It undercounts on pull requests
  whose earlier rounds are summarised only in commit messages.
- Tau Ceti pull requests that meet a defect are also 1.8 times larger than the baseline, so part
  of the extra cost reflects harder targets.
- Neither repository records token usage. The costs of review below are estimates in tokens, from
  token counts measured on the survey's own agents.

## 3. What the reviews find

| Category | Share of 2,659 findings | Escape rate |
|---|---|---|
| False as written, with a counterexample | 23% | 30% |
| Supplier drift, upward citation, cycle, two owners | 12% | 14% |
| Missing or wrong hypothesis | 11% | 31% |
| Unowned prerequisite, leap in a proof route | 10% | 29% |
| Duplicates the library or another roadmap | 8% | 28% |
| Sign, normalisation or convention | 6% | 21% |
| Lean signature (binder lost, wrong carrier) | 5% | 31% |
| Source locator | 5% | 36% |
| Generality mismatch | 4% | 32% |
| Vacuous, or a hypothesis containing the conclusion | 3% | 21% |
| Checks, style, Lean API, scope, design, other | 13% | 18–38% |

The escape rate is the share of findings about text that an earlier round, or the author's own red
team, had already examined and passed.

**Reviewer types:** AI reviews posted by the author or by other accounts 1,674; maintainers' agents
755; humans 164; the author's red team, where it was itemised, 66.

**Who wrote the roadmaps:** the roadmaps written by the pipeline this harness inherits (the author
of 154 of the 365 new-roadmap pull requests) needed a median of 8 external review rounds and 21
commits to merge, after the author-side red-team passes.

**Rounds**

| Round | Pull requests reaching it | Findings per pull request | Mathematical (false, hypothesis, convention, vacuity, generality) | Ownership (supplier, duplication, unowned) |
|---|---|---|---|---|
| 1 | 274 | 2.97 | 1.54 | 0.84 |
| 2 | 185 | 1.89 | 0.99 | 0.55 |
| 3 | 134 | 1.66 | 0.72 | 0.54 |
| 5 | 82 | 1.70 | 0.61 | 0.61 |
| 8 | 45 | 1.53 | 0.71 | 0.58 |
| 12 | 19 | 1.53 | 0.74 | 0.47 |

The pull requests that keep going are partly the ones with more defects, but the plateau is the
signature of a low-recall sampling process: each reviewer finds a few of a large remaining stock.

**What worked.** Every batch reported the same thing: the findings that held came from reviews that
computed something. Examples:
- a 2×2 identity matrix satisfying every listed hypothesis of a determinant lemma;
- an A₄ quotient lifted to GL₄(ℚ₅) giving coefficient 2 where the roadmap claimed 1;
- the Riemann–von Mangoldt constant checked numerically at ℚ(√−163);
- the D₁₆ quotient checked by script;
- curves over 𝔽₂(t).

Reviews that read the proof sketch again and wrote "no blocking issue" found little.

## 4. What gets through, and how it is found later

### Defects in merged roadmaps (revision pull requests)

- Of 111 traceable genuine misses, the original review had read the passage in 67 and passed it.
- They were found a median of 23–41 days after the merge (range 0–136), by:
  - later AI reviews, mostly fresh whole-roadmap passes made on unrelated revision pull requests;
  - Tau Ceti formalization (errata written when a roadmap is archived, and departure notes);
  - maintainer audits;
  - and rarely by humans.

The classes, with the change in the briefs that addresses each:

1. **False at a degenerate case the list already names.** Rank 1, an empty fan, m = 0, k = 0,
   p = ∞, q = 0, a finite ℤ/2, imperfect fields, an out-of-range index returning a junk value.
   - A profinite-groups statement was found false at m = 0 three days after four approving rounds.
   - *Change:* a table row without computed values does not count. The list is extended with empty
     structures, junk values of total functions, nilpotent thickenings and universe bounds.
2. **Underdetermined objects:** a plausible wrong model satisfies every stated law.
   - The geometric Artin map instead of the arithmetic one, and the zero pairing (class field
     theory); a rescaled character or basis; a level condition every larger level also
     meets.
   - *Change:* the wrong-model test (adversarial pass, item 5).
3. **Lemmas imported at the wrong generality, and circular routes.**
   - A step lifted from inside a source's proof and restated beyond the source's setting.
   - A route circular through the source's own proof (class field theory).
   - A citation standing in for a supplier.
   - *Change:* adversarial item 8.
4. **The roadmap contradicts itself.**
   - A worked example refutes a theorem of the same roadmap: the biquadratic example of class
     field theory, found in round 7; the Δ = 12 example of integral lattices.
   - Two Checks on one object cannot both hold.
   - *Change:* adversarial item 10.
5. **Lean binder loss.** A `sorry`'d declaration, a `def` or an `instance` keeps only the section
   variables its type mentions.
   - The rule was already in the Lean form, but it recurs after being "fixed": 25–45% escape in
     several batches; three times in one pull request; six false declarations in one audit of 138.
   - *Change:* the rule covers every `def` and `instance`, and the check is one batched `#check`
     file whose result goes in the report.
6. **Wrong beliefs about native definitions.**
   - `CWComplex` without a separation axiom; `IndepFun` without measurability.
   - "Proven upstream", where the upstream lemma assumes separability.
   - A nonexistent lemma called "genuinely available" (universal covers).
   - *Change:* adversarial item 9 quotes the definition at the pin and says what it omits.
7. **Over-strong hypotheses**, found only when Tau Ceti proved the weaker statement. Three of these
   had been requested by a pre-merge review.
   - *Change:* the briefs only flag unused hypotheses; the rest is left to formalization (section
     6).
8. **Dependency granularity and mandatory routes** that block implementers, found only by
   formalization.
   - *Change:* a proof sketch is not a mandatory route, and `*Needs:*` lines are given item by item.

### How the review process lets them through

- **Diff-scoped and summary reviews pass what whole-roadmap reviews catch.** On five revisions
  (stable reduction, algebraic curves, modular curves, orthogonal and spin groups, reductive groups),
  one review approved the same head on which another found blockers.
- **Repairs introduce defects, and the same defect survives elsewhere.**
  - GL₂ modularity lifting fixed a twist error in one section; it recurred verbatim in another two rounds
    later.
  - Coleman integration's fix covered one case of an exterior-power comparison and not the next.
  - Reviewers' own proposed repairs were false in several cases (three of seven in one review of
    classical étale cohomology),
    and at least five defects later corrected on main had been requested by a review.
- **Citations rot.** Sibling roadmaps are renumbered while a pull request is under review, so a
  citation correct in one round is wrong in the next. In the other direction, reviewers reported
  sections as missing nine times when the section existed and the reviewer was reading a stale copy.
- **Two open pull requests own the same object.** This is the largest category by review rounds
  (supplier plus duplication, 20% of findings), and it is invisible from main and the merge order.
  Formal connections and opers and a sibling both declared the same fully qualified Lean name with different
  bodies.
- **Errata parked in closed or unmerged fix pull requests stay on main** and are found again later.
- **Findings get skipped.** A fix addressed other findings and left one open for weeks (zeros of
  L-functions, six rounds with the same proposed repair), or pushed unrelated material instead
  (Heegaard Floer, tensor product combinatorics).

### Defects met by formalizers

- In 50 defects that Tau Ceti formalizers reported against a roadmap passage, the original review
  had raised none.
- 16 of the 50 are still on main. One reason is that Tau Ceti agents are not permitted to open
  roadmap issues, so the reports live only in Tau Ceti pull request descriptions. An example is the
  set equality `frobeniusPrimeSet_preimage_fixedField` in Chebotarev's Suggested.lean, which a Tau
  Ceti pull request showed false in both directions.
- Classes: false 10, hypothesis 8, generality 8, Lean signature 4, design 3, unowned 3, supplier 3,
  duplication 3, other 8.

### What humans add

Human comments were 6% of findings, and what they contributed differs from the AI reviews:
- invented terminology ("made up AI nonsense", elliptic curves);
- scope and taste ("far too general for FLT, and not general enough for a general theory of
  automorphic representations", weight-two automorphic forms);
- one-line pointers to existing Mathlib API (exchangeability, graph regularity);
- missing named variants of a conjecture;
- checks of a hypothesis against the roadmap's actual consumers;
- a few genuine counterexamples, among them one from a 1967 Grothendieck letter.

*Change:* standard vocabulary in the README shape, and a defect class for invented vocabulary.

## 5. What it costs

### A defect that reaches Tau Ceti

109 pull requests that met a roadmap defect, against 100 random clean merged pull requests from the
same months:

| Per pull request | Met a defect | Baseline | Ratio |
|---|---|---|---|
| review events (mostly rubric rounds), median / mean | 8 / 13.2 | 4 / 6.6 | 2.0× |
| hours open, median / mean | 7.5 / 25.9 | 2.9 / 8.2 | 2.6× / 3.2× |
| commits, median | 4 | 3 | 1.2× |
| closed without merging | 8.3% | 0% | — |

- **What the formalizer did:**
  - worked around the defect in the same pull request: 62%;
  - proved the corrected or negated statement: 21%;
  - the pull request stopped or was closed: 8%;
  - fought a rubric over several rounds: 5%;
  - needed a human roadmap edit: 4%.
- **The tail.** About one defect in twelve, mostly ordering or unowned-prerequisite defects that a
  rubric enforced literally, costs 10–30 times a normal pull request:
  - a mandatory proof route in DG Floer theory: 124 review events, still open;
  - a scope ruling in conformal mapping: 18 days, closed unmerged;
  - a forgotten hypothesis in profinite cohomology: 71 review events.
- **Repeats.** At least 9% of defects were met by more than one pull request; 15–35% is the
  estimate.
- **The direct rubric spend is small.** The real costs are formalizer sessions, elapsed time,
  discarded work and repeated attempts.

### A defect caught in roadmap review

Fixing a defect after the merge is cheap once someone has found it. A revision pull request has a
median of one comment and two commits, about 1.4k tokens of discussion per defect. Before the
merge, a review spends about 1.4k visible tokens per finding.

### What a check costs

Output tokens, thinking included, are priced several times input tokens, and a cache read is a
small fraction of an input token. Prices change; the estimates below are in tokens.

Measured on the survey's own agents: each read 30–100 million cached tokens, wrote 1–4 million to
the cache, and produced 0.03–0.16 million output tokens. Every tool call re-reads the whole
context, so **the number of turns times the context size** is a first-order cost, alongside
thinking.

A roadmap in the current style (20 roadmaps on main with `*Needs:*` lines) has a median of:
- 100k tokens of README (p75 250k) and 20k of Lean;
- 120 `*Needs:*` lines, about 300 Check bullets;
- 128 Lean declarations, 46 of them definitions;
- 40 cross-roadmap citations.

Estimated cost of one review round of such a roadmap:
- 60–150 turns at about 200k average context: 12–30 million cache reads;
- about 12k visible output tokens (the worker's design reviews on the board are 7–12k), with an
  assumed 3–10 times that in thinking: 50–130k output tokens;
- and as much again for the fix that follows.

## 6. The changes, check by check

| Change | Evidence | Estimated marginal cost per median roadmap | Decision |
|---|---|---|---|
| Adversarial table with computed values; a row without values does not count | 94% of escapes in named classes; 67 of 111 merged misses passed by a summary; computing is what found defects | 1–2k thinking per target, about 120–240k output tokens, once | Keep, once at full depth (author pass and first review), then on changed text only |
| Degenerate cases extended: empty structures, junk values, nilpotent thickenings, universe bounds, both directions of an iff | Merged misses at rank 1, empty fan, m = 0, p = ∞; Tau Ceti counterexamples on S₃ and C₄; ULift and cardinal-cutoff findings | inside the table | Keep |
| Conventions recomputed in every layer and supplier | Convention errors 6% of findings; cross-layer sign contradictions in merged roadmaps | few conventions times their users | Keep |
| Wrong-model test | Underdetermined maps and definitions in merged roadmaps; "elaborates but means something else" in Tau Ceti | 1–3k per object | Keep, restricted to opaque or sorried maps, candidate-operation structures and parameter-fixing hypotheses |
| Unused hypotheses: a hypothesis that neither the statement's truth, the proof sketch nor any consumer needs is a P3 note unless an instance shows the statement holds without it | Over-strong hypotheses relaxed cheaply by formalizers; a short proof sketch omits hypotheses the statement needs | a by-product of the table | Keep as a note only (see rejected alternatives) |
| Proof-route inputs owned; steps lifted from inside a source's proof re-checked there | Unowned prerequisites 10%; imported-generality falsity escapes the author's red team | reading sources costs input and turns | Keep, restricted to steps from inside a proof |
| Native definitions and has/lacks/proves claims checked at the pin | Wrong beliefs about native definitions; false "Mathlib lacks" claims | a few greps per claim with a local pin | Keep |
| Checks and worked examples against the theorems of their section | Self-refuting roadmaps among the merged misses | about 300 tokens per Check | Keep, inside the table |
| Colloquial milestones stated exactly, including quantifier order | Quantifier-order and "the standard bound" findings | small | Keep |
| Binder rule for every `def` and `instance`; one batched `#check` file, reported | 25–45% re-escape; six false declarations in one audit of 138 | one elaboration | Keep (and move to the harness, section 8) |
| One example per definition computes a known value | Definitions identically zero, a radical taken in ℝ | one elaboration | Keep |
| A headline names the roadmap's objects | Headline theorems that were algebra on free variables | none | Keep |
| Supplier drift includes the sibling's head, the pin, two open owners and colliding Lean names | Supplier plus duplication 20% of findings; identical fully qualified names | none in the brief | Keep |
| Ownership against open roadmap pull requests whose files the brief gives; citations resolved at the cited head every round, library declarations at the pins | Largest driver of rounds; citation rot | turn-heavy for agents | Keep in the brief, only over the files the brief gives (an agent never goes looking for pull requests); move to the harness (section 8) |
| Missing citations reported only after checking the copy is current (stated once, with the citation rules) | Nine false "missing" findings from stale copies | none | Keep |
| A proof sketch is not a mandatory route; `*Needs:*` per item | The 10–30 times tail in Tau Ceti; ordering blocks | none | Keep |
| Standard vocabulary | Humans' most frequent catch | none | Keep |
| Later rounds: repairs re-reviewed, their pattern searched elsewhere, and sections earlier passed without computed values reviewed in full | Repairs introduce defects; recurrence elsewhere; the round plateau | proportional to the diff | Keep, in place of reviewing the whole document again |
| A proposed repair is tested on the refuting instance | False reviewer repairs; review-requested defects on main | small | Keep |
| Acceptance carries its evidence (computed table, declarations opened, citations resolved) | Approvals that verified nothing were followed by false statements in the next round | 5–15k output | Keep (and refuse in the harness, section 8) |
| Fix and gate: repair the pattern, not the line; adversarial pass on added lemmas; one report section per finding | Recurring defects; skipped findings | proportional to the repairs | Keep |

The briefs grow by 22–26% in words: review 4,671 → 5,875, design 3,701 → 4,520, package 3,550 →
4,494, fix 3,575 → 4,599, red team 4,160 → 5,163. The rules carry no history (that is in this
document), and each is stated once. The brief is read once per turn as cached input,
so this costs less than the whole-document re-review it replaces.

## 7. Alternatives considered and rejected

1. **Making late-round "narrower than the source" findings non-blocking**, to stop review loops that
   do not converge.
   - Rejected. Late findings are almost always real: about 1% of disputes were upheld, and no
     dispute was upheld in two batches.
   - Most defects that reached main sat in text an earlier round had passed.
   - What should change is the depth of the early rounds, not the severity of the late ones. The
     `max_rounds` park stays as the backstop.
2. **A separating counterexample for every hypothesis** (the first draft's "hypotheses earn their
   place").
   - Rejected on cost: about 300 hypotheses at 1–2k thinking each is 300–600k output tokens a pass.
   - The defect it prevents is the one formalizers handle most cheaply: they prove the weaker
     statement and note the departure.
   - A hypothesis that neither the statement's truth, the proof sketch nor any consumer needs is a
     P3 note, not a finding, unless an instance shows the statement holds without it: a short proof
     sketch can omit a hypothesis the statement needs.
3. **Reviewing the whole document again in every round** (the briefs' previous rule).
   - Replaced. It is the largest cost multiplier (full context, times turns, times rounds), and the
     plateau shows each round finds little of what remains.
   - Coverage now comes from one computed pass, ideally sharded by section. Later rounds review the
     repairs, the repaired patterns elsewhere, and whatever an earlier round passed without
     computed values.
4. **The wrong-model test for every definition, and opening every cited source for every route.**
   - Restricted. A definition with a real body is pinned by its two examples, and a theorem
     statement cited from a source is checked by the locator sample.
   - The expensive checks apply where the escapes were: opaque and sorried maps, existential
     parameter-fixing hypotheses, and steps lifted from inside a proof.
5. **Leaving defects to formalization altogether**, since 62% of defects are worked around cheaply.
   - Rejected for the classes that are expensive downstream. False statements and missing
     hypotheses make up half of the Tau Ceti defect pull requests, double their review rounds, are
     met again in 15–35% of cases, and produce most of the 10–30 times tail. Roadmaps that consume
     them build on them.
   - Accepted for the classes that are cheap downstream: over-strong hypotheses, shape and naming
     departures, page-level locator slips, and granularity beyond per-item `*Needs:*` lines.
6. **Agents reading every open roadmap pull request for ownership, and grepping each citation
   themselves.**
   - Rejected as the long-term mechanism. 282 open roadmap pull requests are tens of millions of
     tokens, and per-citation greps add a turn each.
   - The briefs ask for the check over the open pull requests whose files the brief gives, and tell
     the agent not to look for others; where it has none, the report says ownership against open
     pull requests was not checked. The harness should supply an index and a resolver so the agent
     reads only the hits (section 8).
7. **A dedicated late "final deep review"**, which in the existing campaign caught locator and name
   tails after several approvals.
   - Not adopted as an extra round. What it caught is mostly mechanical (stale names, page slips)
     and belongs to the citation resolver.
   - Its mathematical catches came from computing, which the first full pass now requires.
   - Its other value, a different model family seeing what another approved, is better obtained by
     separating review families (section 8).
8. **Adding subject-specific traps to the defect classes.** The survey found subject-specific ones,
   such as the Shimura-datum central quotient that one reviewer found in three roadmaps.
   - Rejected. Such lists grow without bound and bloat every brief. The general checks catch these
     traps: the wrong-model test, imported lemmas, recomputed conventions.
   - A project that wants them can add them through `[prompts] extra_rules_file`.
9. **More degenerate cases without the computed-values rule.**
   - Rejected. The list already named most of the escapes. Lengthening a list that is not executed
     changes nothing; requiring the values does.
10. **Escalating a finding re-raised many times to a human.**
    - Not added: the board already does this, with `Re-raised:` and the `max_rounds` park.
    - The new rule that a fix report has one section per finding addresses the skipped-finding
      loops the survey found.

## 8. Follow-ups outside the briefs

In order of cost-effectiveness:

1. **Record token usage per agent run, lane and job.** `claude -p --output-format json` reports
   usage and cost, and Codex's JSON event stream reports token counts. Until this exists, the costs
   in section 5 are estimates. It is the cheapest way to confirm or overturn the decisions above.
2. **The sibling list sees almost none of the open roadmap pull requests.** `open_roadmap_prs` keeps
   only pull requests labelled `roadmap.pr_label`, which at the survey was 1 of 282. The ownership
   and citation checks need the list of every open pull request that adds or changes a roadmap
   folder: names, folders and heads, which are cheap even where full checkouts are limited.
3. **Mechanical checks in the harness:**
   - an elaborated-signature transcript (`#check @name` for every declaration) given to authors and
     reviewers;
   - a citation and name resolver against the sibling heads, main and the pinned libraries;
   - an object and name index over open roadmaps;
   - a check that a fix report has a section for every finding id of the review;
   - refusing an `accepted` verdict whose report has no adversarial table with computed values.
4. **Make fix-later cheaper on the Tau Ceti side.**
   - Let rubrics accept a documented departure from Suggested.lean. This removes the 10–30 times
     tail.
   - Turn formalizers' departure notes into revision jobs on the board, so that a defect is met
     once.
5. **Shard the full-depth pass** with `[redteam] sharded = true`. Section passes have smaller
   contexts, so each turn is cheaper, and the survey shows they catch pinning gaps that
   whole-document passes miss.
6. **Separate review families.** A different model family repeatedly found what approving rounds of
   another had passed. `[review] separation = "agent"` accepts a sibling model of the same family.
