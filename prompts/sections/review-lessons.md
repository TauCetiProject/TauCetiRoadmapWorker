## What the pull-request reviews keep finding on accepted packages

A package accepted here is ported to a pull request and reviewed again there, on the same rubric.
Across the ported packages, two in three failed that first pull-request review, mostly on points this
review can settle now. Check each of these explicitly and report it here, so the pull request meets
its review without a round:

- **Padding in `Suggested.lean`** (the most frequent finding): run the one-tactic probe on every
  sorried theorem, not a sample; a statement that `rfl`, `infer_instance` or `simp` closes, that
  restates a definition, mirrors a README Check, computes a small value or treats a boundary case
  is a finding with the repair "delete it".
- **Statable headline theorems absent from `Suggested.lean`** (P2): list the headline theorems from
  the README's summary and check each is stated at the pins or named in the module docstring as
  not statable, with the reason.
- **Backticked names presented as this roadmap's own that `Suggested.lean` does not declare**:
  resolve every backticked identifier the README attributes to this roadmap.
- **Names that do not exist at the pin**: open each cited Mathlib or Tau Ceti declaration at the
  pinned libraries (file and line), in the README as well as in the Lean file.
- **README shape**: layers with dependencies, every definition with its API and at least three
  discriminating Checks, every theorem with its exact hypotheses and a locator.
- **Locator slips and the source manifest**: the sampled locators checked against the text, and
  every cited work recorded in the manifest.
- **Process residue**: pull-request numbers, dates, hand-off notes, HTML anchors or machine paths
  in the roadmap text; the import header (`import Mathlib`, then only the `TauCeti.*` modules used).
- **Supplier drift**: a cited section or declaration of a supplier that no longer exists at the
  supplier's current head (the open pull requests move); resolve each citation now.
