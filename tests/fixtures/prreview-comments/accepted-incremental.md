🤖 AI review (claude, fable) of ed022504da466b22ec8e8f54db9e96e9abe1c74b (incremental re-review since ed02250)

Verdict: APPROVE

Reviewed head: ed022504da466b22ec8e8f54db9e96e9abe1c74b

Roadmap folder: `TauCetiRoadmap/MatesDoubleCategories/`

The AI review accepts this head: the pull request is ready for a human's final approval. The AI does not approve or merge; a human does.

At `ed02250`: the targeted build succeeded with only `sorry` warnings; README identifiers missing from the Lean: 0; dependency check problems: 0; gate passed.

**Source independence:** changed passages only; the description's 3 paragraphs compared with 2 sources (Schanuel–Street, Riehl–Verity §3.3), none shared; largest share FairbanksShulman, 16 of 23 locators; contents compared with FairbanksShulman: independent, no layer added, removed or reordered.

**Research link:** M3.3 formalises the mates correspondence for bicategories of Fairbanks–Shulman (2026), Example 3.10; M4 is Kelly–Street (1974), Proposition 2.2, as restated by Cheng–Gurski–Riehl (2014), Theorem 1.2; M1.4 is the free adjunction of Schanuel–Street (1986) used by Riehl–Verity (2016). Rests on the README's references and the four texts read.

**For the human reviewers:** this review raises 5 questions that a human decides (the scope of the roadmap, or a licence). They are in the section of that name at the end and do not change the verdict.

No findings. The folder is unchanged since the previous review of `ed02250`; the rewritten pull-request description was read against the head.

## Previous findings

- R01 — repaired: the description now says "M3.2 constructs the 2-cell dual of a bicategory, absent from the pinned Mathlib" and "constructs the Schanuel–Street walking adjunction with its universal property and four hom-category isomorphisms", with both sources and the simplex suppliers.

## Verified sound

| Statement | Instances tried | Values computed on each side | Result | Change |
|---|---|---|---|---|
| Description: "four hom-category isomorphisms: `Δ₊`, `Δ₊ᵒᵖ`, and the top- and bottom-preserving subcategories of `Δ`" (M1.4 Hom-category theorem) | `[1] → [2]` in `Δ_top`, `Δ_bot`; `1 → 2` in `Δ₊` against `[2] → [1]` in `Δ_∂` | `#Δ_top([1],[2]) = 3 = #Δ_bot([1],[2])`; `#Δ₊(1,2) = 2 = #Δ_∂([2],[1])`, evaluated in Lean | matches README lines 352–357 | none |
| Description: "the existing cut functor `SimplexCategory.II`" (M1.4 Simplex categories and conventions) | `α : 1 → 2`, both values of `α(0)`, cuts `j = 0, 1, 2` | README formula `#{i | α(i) < j}`: `0,1,1` and `0,0,1`; `II.map'` evaluated: `[0,1,1]` and `[0,0,1]` | equal; endpoints `0 ↦ 0`, `2 ↦ 1` preserved | none |
| Description: "M3.3 supplies the identity-adjunction mate formula with unitors" (M3.3 API) | general `S, T : a ⟶ b`, `σ : S ⟶ T`; `S = T = 𝟙`, `σ = 𝟙` | `Bicategory.mateEquiv (id a) (id b) (ρ_S.hom ≫ σ ≫ λ_T.inv) = λ_S.hom ≫ σ ≫ ρ_T.inv` proved in a scratch file (reduces to `σ ≫ coherence = σ`); special case gives `𝟙 = 𝟙` | true as stated at README lines 681–685 | none |
| Description: "M3.2 constructs the 2-cell dual of a bicategory, absent from the pinned Mathlib" (M3.2 API) | `Bicategory/Opposites.lean` at the pin | lines 23–29: only the 1-cell dual is defined; the 2-cell dual is listed as not yet defined; `Hom2` (line 47) keeps the 2-cell direction | claim holds; README lines 616–624 make it a target | none |
| Description: "is not a prerequisite for the general mates theorem" (M1.4) | every `*Needs:*` and Dependencies line of M2–M4 | M3.1 needs M1.3; M3.2 needs M1.1–M1.2, M3.1; M3.3 needs M3.2; none names M1.4; README lines 427, 740 say so | claim holds | none |
| `representable_of_representation` (the one `sorry`) | elaborated type | `∀ D, D.Representation → D.Representable`, binders `O, H, V, D` all present | unchanged since the previous review | none |

The rows for the other statements stand as in the previous review, of `ed02250`.

- Pull-request text against the head, every claim: title; adds only `TauCetiRoadmap/MatesDoubleCategories/` (no file changed outside it); `topic = "math.CT"`; toolchain `v4.36.0-rc2`; Mathlib pin `1b1ccf4b088b32fa3f23f2de1127433254a81e1e`; one `sorry` (line 168); the `#check` of `Bicategory.mateEquiv` (line 174); 221 roadmap folders with a topic at this head (191 + 30); the two source links equal references 4 and 5; Riehl–Verity 3.3.4, 3.3.6, 3.3.8 lie in §3.3. None contradicted, no change left unstated.
- Library declarations opened at the pins: `Bicategory/Opposites.lean` 23–29, 47, 157; `Bicategory/Adjunction/Mate.lean` 425, 453; `SimplicialObject/II.lean` 73, 106, 111, 116, 122, 226; `SimplexCategory/Augmented/Basic.lean` 33; `Bicategory/Functor/StrictPseudofunctor.lean` 43; `TauCeti/CategoryTheory/Adjunction/Mates.lean` 69.
- Duplication at the pins: no double category, walking adjunction or 2-cell dual in Mathlib or Tau Ceti; the overlap lists are unchanged.
- Cross-roadmap citations: none in the README; no declared supplier gaps.
- Dependency check: `depcheck: 222 roadmaps, 2394 edges, 0 problem(s)`; no UPWARD or CYCLE line.
- Gate at ed02250: every check 0, targeted build passes. Elaboration: one warning (`sorry`, line 168).
- Seeded sample: the two M1.4 targets the description cites are the rows above; the rest fall in untouched text and stand as previously checked.

Overlap check: read HomotopyTheoryOfCategories, GrothendieckInfinityGroupoids, Foams (the third list is empty); nothing shared.

Source independence: changed passages only; the description's 3 paragraphs compared with 2 sources (Schanuel–Street, Riehl–Verity §3.3), none shared; largest share FairbanksShulman, 16 of 23 locators; contents compared with FairbanksShulman: independent, no layer added, removed or reordered.

Research link: M3.3 formalises the mates correspondence for bicategories of Fairbanks–Shulman (2026), Example 3.10; M4 is Kelly–Street (1974), Proposition 2.2, as restated by Cheng–Gurski–Riehl (2014), Theorem 1.2; M1.4 is the free adjunction of Schanuel–Street (1986) used by Riehl–Verity (2016). Rests on the README's references and the four texts read.

## Not verifiable

- Description: "GitHub's full build passed" for `ed02250` at the linked run: the run page was not read; the targeted build of `Suggested.lean` passes locally at this head.
- Description: the second local Lean check (the two parenthesizations in `FreeBicategory`) is the author's own and is not in the pull request; the first (the identity-adjunction formula) was reproduced.
- Kelly–Street, Proposition 2.2, pp. 86–87, and its diagrams (2.4)–(2.5) (README lines 59, 842–844): recorded as uncertified, text not read.
- Published page numbers of Riehl–Verity and of Cheng–Gurski–Riehl: only the arXiv texts were read; the item numbers agree.
- Ownership against open Mathlib pull requests (a 2-cell dual, double categories) was not checked.

## Source issues

- none

## For the human reviewers

- Scope: the headline of M3 (README lines 665–674) rests on FairbanksShulman Example 3.10 (lines 878–882), an example stated without proof in a 2026 article; the strict case is Kelly–Street 1974.
- Not tested: Kelly–Street's text was not read; the convention comparison at README lines 58–59 is unchecked.
- Licence: README line 806 links Geb's `GebLean/Utilities/DoubleCategory.lean` (GPL-3.0 per line 813). That file was not re-read in this round; `Suggested.lean` is unchanged.
- Policy: `Suggested.lean` lines 10–15 and README line 14 call the Lean file suggested forms and a prototype; its one theorem is an M1.2 target, and all four headlines are listed as not statable.
- Sources: the roadmap's source manifest records references 1–3 only; Schanuel–Street and Riehl–Verity (references 4, 5) were read from open copies (Numdam, arXiv:1310.8279v4) and have no manifest entry.

<!-- tauceti-roadmap-prreview {"agent": "claude", "head": "ed022504da466b22ec8e8f54db9e96e9abe1c74b", "job": "PRREVIEW-MatesDoubleCategories~5", "mode": "incremental", "model": "fable", "verdict": "accepted", "worker": "host-a1"} -->
