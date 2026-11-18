🤖 AI review (claude, fable) of 6ca84836d373dba1fbd1aa92ea8695f4abe43db6

Verdict: CHANGES NEEDED

Reviewed head: 6ca84836d373dba1fbd1aa92ea8695f4abe43db6

Roadmap folder: `TauCetiRoadmap/MarkovDecisionProcesses/`

Changes needed before a human's final approval: the findings below block it at this head (every finding, P3 included, is a change to make).

At `6ca8483`: the targeted build succeeded with only `sorry` warnings; README identifiers missing from the Lean: 0; dependency check problems: 0; gate passed.

**Source independence:** 13 passages compared with 5 sources (Tsitsiklis1994, WatkinsDayan1992, Sutton1988, KakadeLangford2002, JaakkolaJordanSingh1994), none shared; largest share Puterman1994, 20 of 37 locators; contents of Puterman1994 could not be read, so the arrangement was not compared with it.

**Research link:** Layer 6 states Theorems 1, 3 and 4(a) of Tsitsiklis 1994 and the Q-learning theorem of Watkins and Dayan 1992; 3.3 is Lemma 6.1 of Kakade and Langford 2002, the starting point of conservative policy iteration. Rests on the README's sources, read at Tsitsiklis1994 lines 167–172 and KakadeLangford2002 line 360.

**For the human reviewers:** this review raises 3 questions that a human decides (the scope of the roadmap, or a licence). They are in the section of that name at the end and do not change the verdict.

1. **F01** (P2) Layers 1 to 3 are stated for finite `S` only, below the generality of their suppliers and source

   **Location:** `TauCetiRoadmap/MarkovDecisionProcesses/README.md` line 39, "countable and general state or action spaces, and measurable selection" (out of scope), and line 56, "`S` and `A` are nonempty finite types"; `Suggested.lean` line 50.

   **Claim:** The trajectory law, the Bellman contractions, the fixed-point values, 1.3, 2.1, 3.1 to 3.3 and the bounds of 4.1 and 4.2 use no finiteness of `S`: they hold for countable `S`, bounded `reward` and finite `A`. Restricting them to `Fintype S` is the easy case only; the cited chapter of Puterman 1994 (Chapter 6) works with a countable state space.

   **Witness:** `S = ℕ`, one action, `trans n = pure (n+1)`, `reward n = (−1)^n`: `T` is a `γ`-contraction with fixed point `(−1)^n/(1+γ)`, yet `bellman` needs `[Fintype S]`. Pinned `Kernel.ofPMF` (`TauCeti/Probability/Kernel/PMF.lean:44`) needs only `[Countable α]`.

   **Repair:** In Layers 1 to 3 and the norm bounds of 4.1 and 4.2, take `S` countable with measurable singletons, `|reward s a| ≤ R` as a standing hypothesis and `A` finite; value functions are bounded functions `S → ℝ` with the sup norm (Mathlib `BoundedContinuousFunction` on the discrete topology, or `lp ∞`), and `M.expect v s a = ∫ v ∂(trans s a)`. Keep `[Fintype S]` exactly where it is used: `policyMatrix`, `value_eq_inverse`, the matrix form of `occupancy`, `eventually_optimal_greedy_valueIteration`, `policyIteration_terminates`, Layer 5 and 6.3 to 6.4. Move "countable state spaces" from the out-of-scope list to the Conventions; general (uncountable) spaces and measurable selection stay out of scope. Add the witness above as a Check of 1.2.

2. **F02** (P3) 1.3 and 2.1 use a conditional law of the actions that 1.1 does not state

   **Location:** `README.md` lines 252–253, "by 1.1 the action at time `n` has conditional law `π sₙ` given the states"; lines 165–167 list only the action at time `0` and the conditional law of the states.

   **Claim:** The route of 1.3 (and the induction of 2.1, and the fifth Check of 1.1) uses the conditional law of the action at time `n + 1`; 1.1 proves it nowhere, and `trajLaw_ofStationary` gives the state process only, while the return integrates `reward (ω n).1 (ω n).2` over state-action pairs.

   **Witness:** `S = A` two elements, `trans s a = pure a`, `reward s a = if a = 1 then 1 else 0`: the return depends on the later actions, whose law 1.1 does not state.

   **Repair:** Replace the first bullet of 1.1 by: "`trajLaw M π s` is a probability measure under which the state at time `0` is `s`, the action at time `0` has law `π.first s`, the state at time `n + 1` has conditional law `trans sₙ aₙ` given the history up to time `n`, and the action at time `n + 1` has conditional law `π.next n (h, sₙ₊₁)` given the history `h` up to time `n` and the state `sₙ₊₁` (`Kernel.condDistrib_trajMeasure` and the definition of `Kernel.compProd`); for `ofMarkov π` this law is `π (n + 1) sₙ₊₁`." In 1.3 replace "given the states" by "given the history and the state `sₙ`".

## Verified sound

| Statement | Instances tried | Values computed on each side | Result | Change |
| --- | --- | --- | --- | --- |
| 1.1 tail bound | `N = 0`; one state reward `c`; `S` or `A` empty | `Σ_{n≥N} γ^n R = γ^N R/(1−γ)`; `c/(1−γ) − c(1−γ^N)/(1−γ) = cγ^N/(1−γ)`; `hR` forces `R ≥ 0` | holds | none |
| 1.1 `trajLaw`, `ofMarkov` | `trans s a = pure a`, deterministic Markov `π` | `κ n : Kernel (Π Iic n, S×A) (S×A)` by `compProd`; action at `n+1` is `π (n+1)` of the new state | holds | R02 |
| 1.2 contractions | one state one action; `γ = 0`; `γ = 1` | `T v − T w = γ(v − w)`; `T v = max_a reward`; `T^k 0 = k`, no limit, `hγ` in every statement | holds | R01 |
| 1.2 `value_eq_inverse` | two-state swap | `(1−γP)⁻¹ = (1+γP)/(1−γ²)`, rows `(1,γ)/(1−γ²)`, `(γ,1)/(1−γ²)`; `‖γP‖ = γ` | holds | none |
| 1.2 `norm_value_le` | `S = ∅` with `R = −1` | elaborated type keeps `[Nonempty S] [Nonempty A]`, so `R ≥ 0`; `‖V^π‖ ≤ Σ γ^n R` | holds | none |
| 1.3 `expectedReturn_eq_value` | alternating chain, rewards `1, 0`; `γ = 0` | `V₀ = 1 + γV₁`, `V₁ = γV₀` give `(1/(1−γ²), γ/(1−γ²))`; both sides `r^π s` | holds | R02 |
| 2.1 `backwardInduction` | `γ = 1`, `N = 2`, `g = (2,0)` on `trans s a = pure a` | `T g = 2`, `T² g = 3`; play `1` then `0`: `1+0+2 = 3`; wrong index plays `0,1`: `0+1+0 = 1` | holds | none |
| 3.1 optimality | same MDP; alternating Markov policy | `V⋆ = 1/(1−γ)`; alternating return `Σ γ^{2k} = 1/(1−γ²) < 1/(1−γ)` for `γ > 0` | holds | none |
| 3.2 `qBellman_optimalQ` | same MDP; `γ = 0` | `Q⋆(s,1) = 1 + γ/(1−γ) = 1/(1−γ)`, `Q⋆(s,0) = γ/(1−γ)`; `H q = reward` | holds | none |
| 3.3 `value_le_of_improve` | strict state | `V^(π') = lim (T^(π'))^k V^π ≥ T^(π') V^π > V^π` at `s` | holds | none |
| 3.3 `performanceDifference` | `π = 0`, `π' = 1`; `π' = π`; alternating chain occupancy | left `1/(1−γ) − 0`; right `1/(1−γ)·Σ occ·1 = 1/(1−γ)`; `0 = 0`; occupancy sums `(1−γ)(1+γ)/(1−γ²) = 1` | holds | none |
| 4.1 `value_greedy_le`, eventual optimality | `v = V⋆`; `γ = 0`; `v₀ = 0` on the two-action MDP | bound `0`; `v_k = (1−γ^k)/(1−γ)` constant in `s`, only greedy action `1`, `k₀ = 0` | holds | none |
| 4.2 `policyIteration_rate` | general | `0 ≤ V⋆ − V^(π_{k+1}) ≤ T V⋆ − T V^(π_k) ≤ γ‖V⋆ − V^(π_k)‖` | holds | none |
| 4.2 `policyIteration_terminates` | `|S| = |A| = 1`; `S = ∅`; no tie rule | bound `1`; `|A|^0 = 1`, trivial; non-optimal iterates have distinct values, at most `|A|^|S| − 1` of them | holds | none |
| 5.1 primal | `v = R/(1−γ)`; `μ s₁ = 0` | `r + γR/(1−γ) ≤ R/(1−γ)`; raising `v s₁` keeps objective, uniqueness fails without `μ > 0` | holds | none |
| 5.2 `exists_eq_stateActionOccupancy` | `μ` with a negative entry; `A = ∅` | `d = Σ_a x ≥ 0` solves `d − γ d P^π = μ` uniquely; signature keeps `[Nonempty A]` | holds | none |
| 5.2 reward identity, weak duality | two-action MDP, `μ = (1/2,1/2)`, `π = 1` | `x(0,1) = 1/2`, `x(1,1) = 1/2 + γ/(1−γ)`; reward `1/(1−γ) = Σ μ V⋆` | holds | none |
| 6.1 `ae_tendsto_zero_noise` | `w = 0`; `α = 1`; `α t = 1/(t+2)²`; non-measurable `B` | stopped martingale `E M² ≤ k·k`; `W(t+1) = w t`; `Π_{n≥2}(1−1/n²) = 1/2`; proof uses `P[(w t)²|ℱ t]`, not `B` | holds | none |
| 6.2 boundedness | `β = 1`, `F x = x+1`; `β < 0`; non-measurable `τ` | `x(t+1) = x t + α t` unbounded; `β < 0` makes `hF` unsatisfiable; proof uses `τ` only pathwise, `G t` is `ℱ t`-measurable | holds | none |
| 6.2 convergence | `F = id`, `β = 1`; `τ = 0`, `F x = x/2` | limit `Σ w_t/(t+1)` random; `x t = 1/2 ≠ 0`; `D = (1+β)‖x⋆‖_ξ` reduces to boundedness | holds | none |
| 6.3 `qLearning_ae_tendsto` | noise moments; wrong-law sampler; `rew = reward` | `w² ≤ 2(rew−reward)² + 8γ²‖q‖²`, so `≤ 2C + 8γ²‖q t‖²`; `Q⋆(0,1) = γ/(1−γ)` versus limit `0`; `R + γm ≤ m` | holds | none |
| 6.4 `td0_ae_tendsto` | alternating chain; `γ = 0` | limit `(1/(1−γ²), γ/(1−γ²))` as 1.3; `v t s → r^π s` | holds | none |

- Source locators checked against the text: 14 of 37 (Tsitsiklis1994 lines 129–172, 177–182, 339–346, 420–422, 440–446; WatkinsDayan1992 lines 45–71, 102–109; Sutton1988 lines 304, 1299; KakadeLangford2002 line 360; JaakkolaJordanSingh1994 lines 215, 325).
- Library declarations opened at the pins: 24, all present with the stated hypotheses (`Contracting.lean:259,286,295,302`; `Traj.lean:687,763,785`; `Convergence.lean:195`; `Units.lean:97`; `Normed.lean:419`; `MarkovChain.lean:161,340,437`; `MarkovProperty.lean:157`; `PMF.lean:44,50`; `Matrix.lean:44,94,114`).
- Cross-roadmap citations resolved: 2 (MarkovChainsAndRenewal on main, scope lines 20 and 36; FirstOrderOptimization at its open head, Layer 5, 5.1); neither is a supplier.
- Dependency check: `depcheck: 237 roadmaps, 2516 edges, 0 problem(s)`; no UPWARD or CYCLE line.
- Gate: every check passed. Elaboration of `Suggested.lean`: 33 `sorry` warnings, nothing else.
- Signatures: all 57 compared with the README; the 16 dropped binders are `[Nonempty S]`/`[Nonempty A]` on definitions that do not use them; every theorem keeps both.
- One-tactic probe (`rfl`, `simp`, `infer_instance`, `aesop`) on a scratch copy: none of the 33 theorems closes; no padding found.
- Pull-request text read against the head: no sentence contradicted.

Overlap check: read FirstOrderOptimization, DenoisingDiffusion, PointProcesses, MarkovChainsAndRenewal, StochasticCalculus, OptimalTransport; nothing shared (Robbins–Siegmund and stochastic gradient descent are FirstOrderOptimization's, chain classification is MarkovChainsAndRenewal's, and this README defers both).

Source independence: 13 passages compared with 5 sources (Tsitsiklis1994, WatkinsDayan1992, Sutton1988, KakadeLangford2002, JaakkolaJordanSingh1994), none shared; largest share Puterman1994, 20 of 37 locators; contents of Puterman1994 could not be read, so the arrangement was not compared with it.

Research link: Layer 6 states Theorems 1, 3 and 4(a) of Tsitsiklis 1994 and the Q-learning theorem of Watkins and Dayan 1992; 3.3 is Lemma 6.1 of Kakade and Langford 2002, the starting point of conservative policy iteration. Rests on the README's sources, read at Tsitsiklis1994 lines 167–172 and KakadeLangford2002 line 360.

## Not verifiable

- Puterman 1994 (20 locators: Sections 2.1.6, 5.3, 6.9.1; Theorems 4.5.1, 6.1.1, 6.2.2, 6.2.5, 6.2.6, 6.2.7, 6.2.10, 6.3.1, 6.4.2, 6.4.6, 6.9.1, 6.9.4; Propositions 6.2.4, 6.4.1): the publisher's page is paywalled and no legal copy was found.
- Bertsekas and Tsitsiklis 1996, Proposition 4.4: no legal copy found.
- Howard 1960, Chapter 7: no legal copy found.
- Singh and Yee 1994, Main Theorem: the publisher's page is paywalled; the author's page did not resolve.
- Jaakkola, Jordan and Singh 1994, Theorems 2 and 3: read in the technical-report version on an author's page; that its numbering equals the journal's was not checked.

## Source issues

- none

## For the human reviewers

- **Not tested.** Puterman1994 has the largest share of the locators (20 of 37); neither its text nor its table of contents could be read legally, so wording and arrangement were not compared with it.
- **Licence.** `README.md` lines 720–722 name rl-theory-in-lean (Lean 4, MIT) as formalizing the Q-Bellman contraction and Q-learning; `Suggested.lean` was not compared with that repository.
- **Scope.** The subject is discounted finite Markov decision processes and tabular reinforcement-learning convergence (`metadata.toml`: `math.OC`); 4.1, 4.2, 6.3 and 6.4 are convergence theorems about value iteration, policy iteration, Q-learning and TD(0), stated as sequences, with no procedures.

<!-- tauceti-roadmap-prreview {"agent": "claude", "head": "6ca84836d373dba1fbd1aa92ea8695f4abe43db6", "job": "PRREVIEW-MarkovDecisionProcesses~1", "mode": "full", "model": "fable", "verdict": "needs_changes", "worker": "host-d4"} -->
