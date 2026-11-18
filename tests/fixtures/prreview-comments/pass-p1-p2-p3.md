🤖 AI review (claude, claude-opus-5-5) of 9c8fbb0b937a14ca234d55040258a2294a4a1474

Verdict: CHANGES NEEDED

Reviewed head: 9c8fbb0b937a14ca234d55040258a2294a4a1474

This is a pass before the final review. It does not decide `ai-found-no-errors`: the final review, on another model, follows it.

Roadmap folder: `TauCetiRoadmap/RepresentationTheory/CompactGroups/`

Changes needed before a human's final approval: the findings below block it at this head (every finding, P3 included, is a change to make).

At `9c8fbb0`: the targeted build succeeded with only `sorry` warnings; README identifiers missing from the Lean: 0; dependency check problems: 0; gate passed.

**Source independence:** 24 passages compared with 1 source: the 19 verified Knapp locators, plus README Conventions on coefficients, §3.2, the §4.1 proof route, the §5.1 compactness routes and the Layer 4-5 docstrings of `Suggested.lean`. The longest shared run is 4 words ("of all matrix coefficients", `README.md:393-394` against `knapp line 10320`). The largest share of locators is `knapp`, 18 of 26. Contents compared with Knapp §§I.9, IV.1-IV.5, V.6 and VIII.5: independent. Layers 0-6 draw on two sections (IV.2-IV.3) and put characters after Peter–Weyl. The order of Layers 0-5 is forced by the Needs lines of §§1.2, 2.2, 4.1, 5.4 and 6.3.

**Research link:** Layers 0, 5 and 6 and the `SU(2)` engine supply the Haar measure, character orthonormality, class-function density and `(2/π) sin²θ`. DeligneWeightsAndPurity (main, its `README.md:537-539`) uses them for equidistribution after Deligne, Weil II (1980). SatoTateGroups (open) uses them for Sato–Tate moments. This rests on those READMEs and on Knapp (2002), Chs. IV and VIII.

**For the human reviewers:** this review raises 2 questions that a human decides (the scope of the roadmap, or a licence). They are in the section of that name at the end and do not change the verdict.

1. **F01** (P1) A character is the conjugate of the sum of the diagonal coefficients, so the block projection in §5.5 has the wrong kernel

   **Location:** `README.md:597-598` ("in an orthonormal basis it is the sum of the diagonal coefficients `π_ii`"); `README.md:563-564` ("convolution with `dim · conj(Σ_i π_ii)`, where `Σ_i π_ii` is the sum of the diagonal coefficients of the model (its character, §6.1)").

   **Claim:** The README sets `π_ij = g ↦ ⟪π g e_j, e_i⟫` with the inner product conjugate-linear in its first slot (`:69-71`). Under that convention `Σ_i π_ii = conj χ`, not `χ`. So §6.1 is false as written, and the §5.5 kernel works out to `dim · χ`, which projects onto the block of the dual model.

   **Witness:** For the circle model `π z = zⁿ`, `n ≠ 0`: `Σπ_ii = z⁻ⁿ` but `χ = zⁿ` (`:603-604`). The kernel `conj(z⁻ⁿ) = zⁿ` keeps mode `n`; the block is `ℂ·z⁻ⁿ` (`:573-574`). Tau Ceti: `star_character` (`Continuous/Character.lean:159`); kernel `dim • star χ` (`Compact/IsotypicBlock/Basic.lean:653`).

   **Repair:**
   - At `:597-598`, write "and in an orthonormal basis it is `conj(Σ_i π_ii) = Σ_i ⟪e_i, π g e_i⟫`, the conjugate of the sum of the diagonal coefficients (`ContRepresentation.star_character`; Knapp, (4.11)-(4.15), pp. 242-243, whose inner product is linear in its first slot)".
   - At `:563-565`, write "is convolution with `dim · conj χ = dim · Σ_i π_ii`, where `χ` is the character of the model (§6.1)". Test on circle model `n`: the kernel is `z⁻ⁿ`, which keeps mode `−n`, the block `ℂ·z⁻ⁿ`.
   - Add `ContRepresentation.star_character` (`Continuous/Character.lean`) to the §6.1 row of "From Tau Ceti".

2. **F02** (P2) §4.2 does not assume that `ρ` is irreducible

   **Location:** `README.md:441-443` ("For irreducible `π` on a finite-dimensional space and `ρ` on a complete space, with `ρ` unitary and no equivalence between them, every matrix coefficient of `π` is `L²`-orthogonal to every matrix coefficient of `ρ`").

   **Claim:** As written, `ρ` may be reducible, and then the statement is false. `ContRepresentation.schur_orthogonality` assumes `ρ.toRepresentation.IsIrreducible` (elaborated signature).

   **Witness:** Take `G = ZMod 2`, `π = sgn` on `ℂ`, and `ρ = sgn ⊕ 1` on `ℂ²`. `ρ` is unitary, and it is not equivalent to `π` (its dimension is 2). Its coefficient at `(e₁, e₁)` is `sgn`, and `⟪sgn, sgn⟫ = 1 ≠ 0`.

   **Repair:** Write "For irreducible `π` on a finite-dimensional space and irreducible `ρ` on a complete space, with `ρ` unitary and no equivalence between them, …". Add the Check: "Irreducibility of `ρ` is needed: for `ZMod 2`, `π = sgn` and `ρ = sgn ⊕ 1` on `ℂ²` are inequivalent, yet the coefficient of `ρ` at `(e₁, e₁)` is `sgn`, of norm `1`."

3. **F03** (P3) The scope does not say who owns the gap that four roadmaps on main record against Layer 5

   **Location:** `README.md:18-30` ("This roadmap owns Layers 0-6 … Tau Ceti at the pin proves every target of Layers 0-6"). "Left to other roadmaps" (`:32-49`) has no entry for Banach representations.

   **Claim:** AutomorphicFormsOnReductiveGroups, SmoothBanachRepresentations, IndexTheory and TopologicalKTheory (main) cite "CompactGroups Layer 5 (`gap:banach-gfinite-density`)" for G-finite vectors and their density in Banach representations. Neither this Layer 5 nor Tau Ceti at the pin states these, and the README does not say who does.

   **Witness:** AutomorphicFormsOnReductiveGroups `README.md:53, :309-310` (main). Tau Ceti `2fd255d` has no `IsGFinite`. CompactGroupsPartII (open) `README.md:31-32` claims "compact strongly continuous Banach density", stated in its §2.1 (`:772, :805`).

   **Repair:** Add to "Left to other roadmaps": "- **G-finite vectors in Banach representations.** The orbit-finiteness predicate, the submodule of G-finite vectors, and its density for a strongly continuous representation on a Banach space are not stated here: every representation in Layers 0-6 is finite-dimensional or on a Hilbert space. AutomorphicFormsOnReductiveGroups §2.1 and others record them against Layer 5 as `gap:banach-gfinite-density`; CompactGroupsPartII §2.1 states them from §§5.1-5.2."

4. **F04** (P3) Some targets lack locators, and two cite finite-dimensional statements for Hilbert-space claims

   **Location:** §6.3 `README.md:626-630`, §6.4 `:643-647`, the E.2 classification `:705-708`, and §5.5's projection and `G × G` statements `:562-568`: no locator. §6.5 `:664`: "(Bröcker-tom Dieck, Ch. II)". §1.2 `:253` and §4.2 `:444` cite Knapp for complete-space statements.

   **Claim:** Every theorem needs a theorem-level locator. Knapp Prop. 4.6 and Cor. 4.10(a) assume finite-dimensional spaces, which the README's statements in §1.2 and §4.2 drop.

   **Witness:** Knapp Prop. 4.6 and Cor. 4.10(a) are finite-dimensional (`knapp line 10093`, `:10129-10130`). Knapp has no class-function basis, isotypic projector or Frobenius–Schur indicator. Thm 1.66, p. 62 (`:2992`), with p. 314 (`:13248-13253`), gives the `SU(2)` classification.

   **Repair:**
   - E.2: append "(Knapp, Thm 1.66, p. 62, through the passage from `SU(2)` to `sl(2, ℂ)` on p. 314; irreducibility also Knapp, Problem IV.1, pp. 269-270)".
   - §1.2: write "(…; Knapp, Prop. 4.6, p. 240, for finite-dimensional `V`)".
   - §4.2: write "(…; Knapp, Cor. 4.10(a), p. 241, for finite-dimensional `ρ`)".
   - §6.3, §6.4, §6.5 and §5.5's projection and equivariance: give theorem number and page in Bump or Bröcker–tom Dieck (keys `bump`, `brockerTomDieck`) at each claim.

5. **F05** (P3) A Mathlib name without its namespace, a wrong file, and undefined notation

   **Location:** `README.md:89, :177, :182` (`CompactSpace.isFiniteMeasure`); `:103-104` ("`IsCompactOperator` and `ContinuousLinearMap.finite_dimensional_eigenspace` (`Analysis/InnerProductSpace/Spectrum.lean`)"); `:397, :510` (`𝓡(G)`).

   **Claim:** At the pin, the first name resolves only with its namespace. `IsCompactOperator` is not in the file named. `𝓡(G)` is used but never defined.

   **Witness:** At Mathlib `1b1ccf4`, `#check @CompactSpace.isFiniteMeasure` fails and `MeasureTheory.CompactSpace.isFiniteMeasure` elaborates (`Typeclasses/Finite.lean:397`). `IsCompactOperator` is at `Analysis/Normed/Operator/Compact/Basic.lean:69`. `𝓡` occurs only at `:397` and `:510`.

   **Repair:**
   - Write `MeasureTheory.CompactSpace.isFiniteMeasure` at `:89, :177, :182`.
   - At `:103-104`, write "`IsCompactOperator` (`Analysis/Normed/Operator/Compact/Basic.lean`) and `ContinuousLinearMap.finite_dimensional_eigenspace` (`Analysis/InnerProductSpace/Spectrum.lean`)".
   - End the first sentence of §3.2 with "; write `𝓡(G)` for its carrier".

6. **F06** (P3) Ten subsections give a definition's API in prose instead of bullets, and one Check computes nothing

   **Location:** the definitions in §§3.2, 5.1, 5.3, 5.5, 6.1, 6.3, 6.4, 6.5, E.1 and E.2 (`README.md:393-398, :478-489, :524-530, :560-569, :595-598, :623-626, :643-647, :658, :690, :703-705`). `:555-556`: "The theorem pins the elements, not only the existence of a basis".

   **Claim:** The house form lists each definition's API as bullets. Here the API is in running prose or absent (r11 R14, partly repaired). The §5.4 Check computes no value.

   **Witness:** API bullets exist only for `haarProb`, `IsUnitary`, `gramOperator` and `matrixCoeff` (`:184-188, :236-238, :258-259, :373-379`).

   **Repair:**
   - Move each API sentence into bullets under its definition. For §6.1: "- `χ_π` is a class function (`ContRepresentation.character_conj`). - `χ_π(1) = dim V`. - In an orthonormal basis `χ_π = conj(Σ_i π_ii)` (`ContRepresentation.star_character`)."
   - Replace `:555-556` by "For `ZMod 2` the basis is `{1, sgn}`. The Hilbert basis `{(1 + sgn)/√2, (1 − sgn)/√2}` has the same index set and is excluded by the element formula."

7. **F07** (P3) The description's summary and References bullet describe an earlier head

   **Location:** The description opens: "**What this revision changes.** In the Schur orthogonality layer, the coordinate-free relation `schur_orthogonality_self` now reads …". It adds that the "headline targets, among them … `peterWeylBasis` … are unchanged". The References bullet says Knapp "is cited at each target by theorem and page."

   **Claim:** This head rewrites the README, replaces the 56 declarations of `Suggested.lean` by 26 `#check` pins, and adds §§2.4, 5.5, 6.4 and 6.5. Knapp is not cited in §§2.4, 6.3, 6.4 or 6.5.

   **Witness:** The folder diff is 897 insertions and 972 deletions, with 14 headings removed and 46 added. See `Suggested.lean:52-117` and `README.md:626-630, :643-647, :664`.

   **Repair:** Replace the summary block with: "**What this revision changes.** It recasts the roadmap in the house form and names, for every target, the Tau Ceti declaration that proves it. It adds `Continuous π` throughout, corrects the conjugate in the first Schur relation, and replaces the 56 restated declarations of `Suggested.lean` by `#check` pins of 26 Tau Ceti headline theorems. **What the roadmap keeps.** Layers 0-6, the engine case and their heading texts." In the References bullet, write "cited by theorem and page at every target it covers".

## Previous findings (r11, at `2f447db`)

- R01 continuity: repaired. See Conventions `:61-64`; every pin carries `Continuous π`; negative controls at `:266-270, :311-312, :536-538`.
- R02 Tau Ceti restated: repaired. See the From Tau Ceti table `:114-138`; `Suggested.lean` holds pins only.
- R03 sorry'd `def`s, R04 `set_option`, R11 section instances, R12 padding: repaired (none remain).
- R05 missing headlines: repaired. `stdCharacterBasis`, `coe_stdCharacterBasis` and `range_isotypicProjector` are pinned (`Suggested.lean:97-99`); the docstring is at `:30-33`.
- R06 separability: repaired (`:75-76`, Check `:199-201`).
- R07 `char_orthonormal`: repaired (`:771-777`).
- R08 Mathlib name: repaired (`:103, :484`).
- R09 torus character: repaired (`:723-736, :785`).
- R10 `character_orthonormal`: repaired (mapping table `:146`).
- R13 sources: partly repaired. Ranges are corrected (`:814-819`) and most targets carry theorem and page; the rest is R04.
- R14 house form: partly repaired. Headings, Checks, Examples and Dependencies are present; the API bullets are R06.
- R15 process narrative and R16 separable deliverables: repaired. No quoted passage remains, and a grep for process words finds none.

## Verified sound

| Statement | Instances tried | Values computed on each side | Result; change |
| --- | --- | --- | --- |
| §0.1 `haarProb` | `ZMod 2`; circle; trivial group; `(ZMod 2)^ℝ` | `haarProb{0} = ½` (`haarProb_def`); arc `α/2π`; Dirac at `1`; uncountably many characters, pairwise `√2` apart | consistent; Knapp §IV.2 `knapp line 10035-10092` supports |
| §0.2 averaging | circle `zⁿ`; `S₃` indicators of `σ`, `σ⁻¹`; constant `c` | `0` for `n ≠ 0`, `1` for `n = 0`; `1/6 = 1/6`; `c` | consistent |
| §1.1 `IsUnitary` | `M = [[1,1],[0,−1]]` on `ℂ²` | `M² = 1`; `‖Me₂‖ = √2 ≠ 1`; Tau Ceti def `⟪π g v, π g w⟫ = ⟪v, w⟫` | consistent; definition, no locator needed |
| §1.2 `isUnitarizable` | `M`; reversed average; continuous `φ : Circle → ℝ` | `S = [[1,½],[½,3/2]]`, det `5/4`, `MᴴSM = S`; reversed gives `[[3/2,2],[2,7/2]]`; image compact subgroup `= 0` | true; locator scope R04 |
| §2.1, §2.3 | swap with `U = ℂ(e₁+e₂)`; `M`; `ZMod 3` on `ℝ²` | `Uᗮ = ℂ(e₁−e₂)` invariant; `ℂe₂` not invariant under `M`; rotation by `π/2` commutes, not scalar | consistent; Knapp (4.5), Prop. 4.8, Cor. 4.9 |
| §2.2 | `V = 0`; trivial `ℂ²`; `M`; `[[1,φ],[0,1]]` | empty family; two lines; lines `ℂe₁`, `ℂ(1,−2)` with `⟪e₁,(1,−2)⟫ = 1`; `(a,1) ↦ (a+φ,1)`, no complement | consistent; Knapp Cor. 4.7 p. 240 |
| §2.4 | trivial `ℂ → ℂ²`; regular `ZMod 2` | both Hom spaces have dimension `2`; `((x+y)/2,(x−y)/2)` intertwines swap with `1 ⊕ sgn` | consistent; elementary, no locator needed |
| §3.1 `matrixCoeff` | circle `zⁿ`; `χ`, `v = i`; `S₃` `π_11` at a 3-cycle | `z⁻ⁿ`; `−i·conj χ`; `−½`; Tau Ceti `⟪π g v, w⟫` | consistent |
| §4.1 `schur_orthogonality_self` | `χ`, `v₁ = i`; `S₃` `π_11`; `ZMod 3` on `ℝ²` | `i = conj(conj i)`, other placement `−i`; `3/6 = ½ = d⁻¹`; `⟪π_11,π_22⟫ = ½` against `0` | true; Knapp Cor. 4.10(b) |
| §4.1 proof route | `T u = ⟪v₂,u⟫v₁` | `tr T = conj⟪v₁,v₂⟫`; `⟪w₁, (tr T/d) w₂⟫ = d⁻¹conj⟪v₁,v₂⟫⟪w₁,w₂⟫` | consistent |
| §4.2 `schur_orthogonality` | `ZMod 2` `1` against `sgn`; `sgn` against `sgn ⊕ 1` | `(1−1)/2 = 0`; `⟪sgn,sgn⟫ = 1` under README hypotheses | R02 |
| §4.3 | `S₃`; unnormalized 2-dimensional block | `1+1+4 = 6 = |G|`; norm `1/√2` | consistent; Knapp p. 242 `knapp line 10184-10192` |
| §5.1 convolution | circle `zᵐ * zⁿ`; `k = 1`; `ZMod 3`, `k = δ_g` | `xᵐδ_mn`; constant `∫f`; `f(g⁻¹·)/3`, adjoint is translation by `−g` | consistent; Tau Ceti kernel `k(x y⁻¹)` |
| §5.2 density, separation | indiscrete 2-point group; `ZMod 2` | closure `⊤`, separation fails, not `T1`; `ℂ² = span{1, sgn}` | consistent; Lean dense has no `T2`; Knapp Thm 4.20 p. 245 |
| §5.3 skeleton | circle `{zⁿ}`; duplicate; `e^{iφ}` with discontinuous `φ` | exhausts; pairwise inequivalence fails; excluded by `continuous_rep` | consistent; Lean fields match |
| §5.4 `stdPeterWeylBasis` | `ZMod 2`; `S₃`; circle | `{1, sgn}`; `6` elements; model `n` ↦ mode `−n`; `peterWeylFamily = √dim • matrixCoeffLp` | consistent; Knapp Cor. 4.21 p. 247 |
| §5.5 block projection | circle model `n`; `S₃` | README kernel `dim·conj(Σπ_ii) = zⁿ` keeps mode `n`; Lean `dim•star χ = z⁻ⁿ` keeps `−n`; blocks `1,1,4` | R01 |
| §5.5 `G × G` | `biRegularLp (g,h) f = f(g⁻¹·h)` on `⟪π x v, w⟫` | `⟪π x (π h v), π g w⟫`, the action `T ↦ π g T π h⁻¹` | consistent |
| §6.1 character | circle; `S₃`; trivial `ℂ²` | `Σπ_ii = z⁻ⁿ ≠ zⁿ = χ`; `2, 0, −1`; `2` | R01 |
| §6.2 orthonormality | `S₃`; trivial `ℂ²`; circle | `(4+0+2)/6 = 1`; `4`; `δ_mn` | true; Knapp Cor. 4.16 p. 243 |
| §6.3 `stdCharacterBasis` | `S₃`; transposition indicator; circle | dimension `3` = number of classes; conjugate is the third indicator; same functions as Peter–Weyl via `n ↦ −n` | true; locator R04 |
| §6.4 projector | swap on `ℂ²`; `S₃` without `dim σ` | `(1−swap)/2` onto `ℂ(1,−1)`, `(1+swap)/2`; scalar `c` with `2c = 1` | true; Tau Ceti kernel `dim•star χ`; locator R04 |
| §6.5 Frobenius–Schur | trivial; circle `zⁿ`; `S₃`; `SU(2)` `Sym¹` | `1`; `∫z²ⁿ = 0`; `(2+6−2)/6 = 1`; `0 − 1 = −1` | true; Tau Ceti `∫ χ(g·g)` |
| E.1 torus conjugacy | `w = [[0,1],[−1,0]]`; `θ = π/2` | `det w = 1`, swaps diagonal; trace `2cos θ` injective on `[0,π]`; order `4` | true; Knapp Thm 4.36 p. 255 |
| E.2 classification | `n = 0, 1`; `Sym²` at `diag(i,−i)` | trivial, defining; eigenvalues `−1, 1, −1`, sum `−1` | true; locator R04 |
| E.3 Weyl character | `n = 1`, `θ = π/3`; `θ = 0, π`; `⟪χ₀,χ₂⟫` on torus | `1 = 2cos(π/3)`; `n+1`, `(−1)ⁿ(n+1) = (n+1)cosⁿθ`; `1` | true; Knapp pp. 314-315, Thm 5.113 |
| E.4 Weyl integration | `f = 1`, `χ₁`, `|χ₁|²`; Knapp Thm 8.60 | `1`, `(4/π)[sin³θ/3] = 0`, `(8/π)(π/8) = 1`; `½∫₀^{2π}·4sin²θ dθ/2π = (2π)⁻¹∫₀^π` | true |
| E.4 character orthonormality | `(m,n) = (0,0), (0,1)` | `(2/π)∫₀^π sin²θ = 1`; `(2/π)∫₀^π sinθ sin2θ = 0` | true |

- **Gate (rerun at this head):** every scanner reports `0`, `OK`. The worker's gate, including `build`, is `OK`.
- **Dependency check:** `depcheck: 236 roadmaps, 2516 edges, 0 problem(s)`. No UPWARD, CYCLE or moved line.
- **Elaboration:** `lake env lean` on `Suggested.lean` at Mathlib `1b1ccf4` and Tau Ceti `2fd255d` exits 0 with no error and no warning, printing 26 `#check` signatures. All 17 imported modules have object files. Every signature carries `Continuous π` where the README requires it. The one mismatch is R02.
- **Signature probe:** a scratch file with 57 `#check`/`#print` of README-named declarations. All elaborate except `CompactSpace.isFiniteMeasure` (R05) and `TauCeti.SU2.symPowerModel`, whose module the probe did not import (it exists at `SU2/Exhaustion.lean:97`).
- **Library:** all 83 Tau Ceti names of the supplier table were found in the files named. 23 Mathlib declarations were found at the files named, except R05. Definitions read: `IsUnitary`, `IrrepModel`, `IsIrrepSkeleton`, `IsRepresentative`, `isotypicKernel`, `frobeniusSchurIndicator`, `IsMollifier`, `peterWeylBlockAveraging`, `convolutionOperator`.
- **Sources:** Knapp is read from the author's page (cache `5ba1b91d`; printed page = PDF page − 18). Locators checked:
  - §IV.2 pp. 239-240 (`knapp line 10035`), (4.5) `:10031`, Prop. 4.6 `:10093`, Cor. 4.7 `:10101`, Prop. 4.8 and Cor. 4.9 `:10109-10117`, Cor. 4.10 `:10128`, p. 242 `:10184`, (4.11)-(4.15) `:10199-10214`, Cor. 4.16 `:10215`;
  - proof of Thm 4.20 `:10334-10385`, Thm 4.20 `:10319`, Cor. 4.21 `:10439`;
  - Example 4 p. 235 `:9854`, Thm 4.36 `:10755`, pp. 314-315 `:13246-13291`, Thm 5.113 `:14311`, Thm 8.60 `:23045`, Thm 1.66 `:2992`.

  All 19 are correct as cited; the scope caveats are R04.
- **Cross-roadmap citations, all on main:** Completed/OrthogonalL2Bases §B2 (`:275`), OperatorIdeals Part C (OI-C14 `:563`), LieGroups Layer 6 (`:673`), LieHighestWeight Layer 5 (`:1438`), CharacterTheory (its `:231-235`), and the 12 downstream consumers all resolve. The old "What is not consumable…" paragraph, which CompactGroupsPartII cites (its `:24, :460, :559`), is mapped at `:340-343`. The service grep finds no citation of any other dropped heading.
- **Padding probe:** not applicable. There is no sorried declaration, and each of the 26 pins is a proved Tau Ceti theorem, not a restated definition or a Check.

Overlap check: read CompactGroupsPartII, SatoTateGroups and SmoothRepresentationsOfLocalGroupsPartII (open), and CharacterTheory and LieGroups (main). Shared objects and their owners:
- Banach G-finite density: CompactGroupsPartII §2.1 (R03).
- General Weyl integration: LieGroups Layer 6; the `SU(2)` case is this roadmap's, proved in Tau Ceti.
- Finite-group characters: CharacterTheory.
- Unitarizability of smooth representations of locally profinite groups: SmoothRepresentationsOfLocalGroupsPartII. This is a different object.
- SatoTateGroups consumes `haarProb`, the character basis and `SU(2)` Weyl integration through Tau Ceti and owns none of them.

Source independence: 24 passages compared with 1 source: the 19 verified Knapp locators, plus README Conventions on coefficients, §3.2, the §4.1 proof route, the §5.1 compactness routes and the Layer 4-5 docstrings of `Suggested.lean`. The longest shared run is 4 words ("of all matrix coefficients", `README.md:393-394` against `knapp line 10320`). The largest share of locators is `knapp`, 18 of 26. Contents compared with Knapp §§I.9, IV.1-IV.5, V.6 and VIII.5: independent. Layers 0-6 draw on two sections (IV.2-IV.3) and put characters after Peter–Weyl. The order of Layers 0-5 is forced by the Needs lines of §§1.2, 2.2, 4.1, 5.4 and 6.3.

Research link: Layers 0, 5 and 6 and the `SU(2)` engine supply the Haar measure, character orthonormality, class-function density and `(2/π) sin²θ`. DeligneWeightsAndPurity (main, its `README.md:537-539`) uses them for equidistribution after Deligne, Weil II (1980). SatoTateGroups (open) uses them for Sato–Tate moments. This rests on those READMEs and on Knapp (2002), Chs. IV and VIII.

## Not verifiable

- Bump, *Lie Groups* (key `bump`, uncertified): Chs. 1, 2, 3, 4, 16 and 17, cited at §0.1, §4.1, §5.1, §5.2, E.1 and E.4. The text was not read.
- Bröcker–tom Dieck (key `brockerTomDieck`, uncertified): Ch. III at §5.2 and Ch. II at §6.5, a sampled target. The text was not read; Knapp's text has no Frobenius–Schur statement (searched for "Frobenius", "indicator", "quaternionic").
- Ownership against open pull requests was checked through the roadmap service's grep of the indexed heads (index of 2026-11-18T00:12:08Z) and the SatoTateGroups checkout.

## Source issues

- none

## For the human reviewers

- `RepresentationTheory/STATUS.md:43` lists CompactGroups as complete, and `README.md:27-30` states that Tau Ceti `2fd255d` proves every target. Whether the folder moves to `Completed/`, as OrthogonalL2Bases did, is open; the description leaves it to maintainers.
- Not tested: Bump and Bröcker–tom Dieck carry 8 of 26 locators, all at chapter level, and are uncertified. The wording and arrangement checks used Knapp alone.

<!-- tauceti-roadmap-prreview {"agent": "claude", "head": "9c8fbb0b937a14ca234d55040258a2294a4a1474", "job": "PRREVIEW-CompactGroups~12", "mode": "full", "model": "claude-opus-5-5", "verdict": "needs_changes", "worker": "host-c3"} -->
