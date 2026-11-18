🤖 AI review (claude, fable) of 20b3f6ba2f031c346ef26657035fb7eb88720746 (incremental re-review since afd8629)

Verdict: CHANGES NEEDED

Reviewed head: 20b3f6ba2f031c346ef26657035fb7eb88720746

Roadmap folder: `TauCetiRoadmap/DenoisingDiffusion/`

Changes needed before a human's final approval: the findings below block it at this head (every finding, P3 included, is a change to make).

At `20b3f6b`: the targeted build succeeded with only `sorry` warnings; README identifiers missing from the Lean: 0; dependency check problems: 0; gate passed.

**Source independence:** changed passages only; 3 passages compared with 1 source (Vincent2011), none shared; largest share HoJainAbbeel2020, 6 of 12 locators; contents compared with HoJainAbbeel2020: independent (no layer added, removed or reordered). Robbins1956 could not be read.

**Research link:** Layers 2 and 4 state the forward process and variational bound of Sohl-Dickstein et al. 2015 and Ho, Jain and Abbeel 2020; Layer 3 is Tweedie's formula (Robbins 1956, Efron 2011) and the score-matching equivalence of Hyvärinen 2005 and Vincent 2011. Rests on the README's references and the five texts read.

**For the human reviewers:** this review raises 3 questions that a human decides (the scope of the roadmap, or a licence). They are in the section of that name at the end and do not change the verdict.

1. **F01** (P3) `## How to read the build`: "everything else uses" contradicts the third sentence

   **Location:** `TauCetiRoadmap/DenoisingDiffusion/README.md` lines 109–112: "Layer 1 proves the two Gaussian facts everything else uses: … Layer 3 is independent of Layers 1 and 2".

   **Claim:** The first sentence says every later layer uses 1.1 and 1.2; Layer 3 uses neither, as the same paragraph and Layer 3's Dependencies say. The wording is the one the previous review proposed; the slip is in that proposal.

   **Witness:** Lines 312, 346, 376: the *Needs:* lines of 3.1–3.3 name no Layer 1 target. Line 385: "No other layer is used."

   **Repair:** Replace the first sentence by: "Layer 1 proves the two Gaussian facts that Layers 2 and 4 use: the divergence between isotropic Gaussian laws and the Bayes update." (Lines 247, 273, 483, 513: 2.2 needs 1.1, 2.3 needs 1.2, 4.2 and 4.3 need 1.1; Layer 3 is no longer covered.)

## Previous findings

- R01 — repaired: `Suggested.lean` lines 245 and 256 carry `(hβ : ∀ t, 1 ≤ t → 0 < β t ∧ β t < 1)`, the docstrings end before "this holds for every `β`"; README lines 416–419 restrict "every `β`" to the variational bound.
- R02 — partly repaired: README lines 109–113 give the layer order and what each layer delivers; the first sentence overstates the use of Layer 1 (R01 of this report).
- R03 — repaired: README line 366 "Vincent 2011 (technical report numbering), Section 4.2, equation (11), and the Appendix"; lines 548–549 cite Technical Report 1358 with the journal reference.

## Verified sound

| Statement | Instances tried | Values computed on each side | Result | Change |
|---|---|---|---|---|
| 4.1 `forwardPath_reverse` (with `hβ`) | `T=0`; `T=1, d=1, μ₀=dirac 0, β 1=1/2`; previous witness `β 2=−1` | both sides the law of `x₀~μ₀`; `q₁=N(0,1/2)`, `rev 1=dirac 0` a.e., both sides `N(0,1/2)⊗dirac 0`; excluded by `0 < β 2` | holds | none |
| 4.1 `klDiv_forwardPath_eq` (with `hβ`) | `T=0`; `T=2, β=1/2, μ₀=dirac 0` | both sides `klDiv μ₀ π`; path marginal at 2: `1/4+1/2=3/4`, `1−ᾱ 2=3/4`, equal to `q₂`, so `IsReverseKernel` is satisfiable | holds, no longer vacuous | none |
| 4.1 `klDiv_le_klDiv_forwardPath` (no `hβ`, "every `β`" kept) | `β 1=1/2, β 2=−1`; both sides `∞` | time-`0` marginal of `forwardPath` is `μ₀` although `x₂~N(0,1) ≠ dirac 0 = q₂`; `∞ ≤ ∞` | holds for every `β` | none |
| 3.3 locator (Vincent) | technical report at the cited numbering | Vincent2011 line 12 (Technical Report 1358), line 271 (Section 4.2), line 290 (equation (11)), line 513 (Appendix) | agrees | none |
| `## How to read the build`, first sentence | Layer 3 against Layer 1 | *Needs:* of 3.1–3.3 cite 3.1, 3.2 and libraries only; 2.2, 2.3, 4.2, 4.3 cite 1.1 or 1.2 | false for Layer 3 | R01 |

The rows for the untouched statements stand as in the previous review, of `afd8629`.

- Incremental re-review of the 42 lines changed since `afd8629`; changed passages only.
- Source locators checked against the text: 4 (Vincent2011 lines 12–16, 271, 290, 513). The seeded sample falls in the change only at 3.3 and 4.1; the 4.1 locators (HoJainAbbeel2020 equations (3), (16)) are unchanged.
- Library declarations opened at the pins: 3 — Tau Ceti `multivariateGaussianPDFReal` (`Density.lean` 82), `multivariateGaussian_eq_withDensity` (265), `stdGaussian_eq_withDensity` (164; exists, no longer cited by any target or *Needs:* line, so dropping it from the contracts is consistent). The four removed imports name modules whose declarations appear only in README supplier citations, not in any statement.
- Cross-roadmap citations resolved: 1 — MathematicalStatisticsNonparametric 1.3 `klDiv_multivariateGaussian` on main (its README line 506).
- Gate at this head: every check passed; no change outside the folder. Dependency check: `depcheck: 238 roadmaps, 2526 edges, 0 problem(s)`, no UPWARD or CYCLE line.
- Elaboration: 24 `sorry` warnings for 24 theorems, no error and no other warning. Elaborated types of `forwardPath_reverse` and `klDiv_forwardPath_eq` compared with README 4.1: `hβ` present, no section binder lost.
- Pull request text read against the head: title and description agree with it (new roadmap, one roadmap supplier, 24 theorems, 42 declarations); the change since `afd8629` alters nothing they state.

Overlap check: read FirstOrderOptimization, MathematicalStatisticsSemiparametric, MathematicalStatisticsFoundations, MathematicalStatisticsHighDimensional; nothing shared (as in the previous review; the change adds no object).

Source independence: changed passages only; 3 passages compared with 1 source (Vincent2011), none shared; largest share HoJainAbbeel2020, 6 of 12 locators; contents compared with HoJainAbbeel2020: independent (no layer added, removed or reordered). Robbins1956 could not be read.

Research link: Layers 2 and 4 state the forward process and variational bound of Sohl-Dickstein et al. 2015 and Ho, Jain and Abbeel 2020; Layer 3 is Tweedie's formula (Robbins 1956, Efron 2011) and the score-matching equivalence of Hyvärinen 2005 and Vincent 2011. Rests on the README's references and the five texts read.

## Not verifiable

- Robbins 1956, equations (44)–(46), p. 162 (3.2): the cached text of Robbins1956 is the host's block page; unchanged since the previous review.
- Vincent 2011 in its Neural Computation numbering (3.3): only the technical report was read; the README now cites that numbering.

## Source issues

- none

## For the human reviewers

- **Scope.** Layers 2 and 4 rest on two machine-learning conference papers (HoJainAbbeel2020, NeurIPS; SohlDickstein2015, ICML); Layer 3 on journal articles (Efron2011, Hyvarinen2005, Vincent2011) and Robbins1956. README line 528: no roadmap on main consumes this one.
- **Licence.** Efron2011 and Vincent2011 were read from author-hosted copies (a preprint and a technical report); their licence status was not settled.
- **Not tested.** Robbins1956 could not be read; its one locator (3.2) is unchecked. The largest-share source, HoJainAbbeel2020, was read in full in the previous review.

<!-- tauceti-roadmap-prreview {"agent": "claude", "head": "20b3f6ba2f031c346ef26657035fb7eb88720746", "job": "PRREVIEW-DenoisingDiffusion~3", "mode": "incremental", "model": "fable", "verdict": "needs_changes", "worker": "host-b2"} -->
