## The roadmap repository's own guidance

Condensed from the README and CONTRIBUTING.md of TauCetiProject/TauCetiRoadmap (Apache License
2.0). Reviewers hold every roadmap to it. A roadmap is a specification for material wanted in Tau
Ceti, written so that an AI contributor and its reviewers can act on it without guessing.

- **Build the library, don't race to the theorem.** For each object ask for its complete basic
  theory, not just the lemma the headline needs. A definition with no lemmas about it is not a
  contribution: making a definition means making it usable, with the right amount of API.
- **No gaps.** Every milestone rests on existing Mathlib or Tau Ceti material, on earlier material
  in the same roadmap, or on an explicitly cited dependency in another roadmap. Anything else is a
  leap. If something needed does not exist, building it is itself a target, here or in a cited
  roadmap.
- **Every item unambiguous.** A capable agent can tell exactly which definition or theorem is meant.
- **Clear boundaries.** Roadmaps do not overlap; a dependency on another roadmap is stated.
- **Definite scope.** Nothing is "optional", "deferred" or "for later". Decide the generality up
  front rather than recommending intermediate implementations that will be replaced.
- **Timeless.** Say what is wanted, not how the roadmap came to say it; no war stories, no
  references to the review process, nothing "blocked".
- **Reusable material** and, usually, breadth: a broad roadmap draws cleaner boundaries.
- **Use Mathlib's vocabulary**, defer to Mathlib on shape (cite open Mathlib work and follow its
  design), but never wait for it: build here now, named so that a later swap is a deletion. Never
  write an item as "to be upstreamed".
- **Specify the mathematics, not existing code.** Existing formalisations are cited sources, never
  the specification; improve on them, keep any file-by-file map in a secondary provenance section,
  and respect their licences.
- **Prototype honestly.** `Suggested.lean` states the roadmap's headline theorems against the pins,
  never an exhaustive checklist or an API catalogue; it may import individual `TauCeti.*` modules;
  conventions are pinned in advance.
- **Make contact with the library**: the lowest rung of a roadmap must be close to what exists.
- **Do not tail off** into an under-specified ambitious extension; label any such closing section as
  motivation for a future roadmap.
- **Attribute AI assistance** in the pull-request description, and never post text nobody has read.
