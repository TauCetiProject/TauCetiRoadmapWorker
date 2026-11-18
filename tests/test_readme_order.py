"""Every reader of a README finds its sections by heading, wherever they stand: a README in the new
order (opening overview, conventions, layers, consumers, then scope and supplier contracts, then
references) reads as the same README in the old order (scope and supplier contracts near the top)."""

from __future__ import annotations

import json

from tauceti_roadmap_worker import depgraph, gates, layout, overlaps
from tauceti_roadmap_worker.lanes.package import gap_reference_problems
from tauceti_roadmap_worker.lanes.review import sample_items

OLD_ORDER = """# Roadmap: toy cohomology

**Summary.** This roadmap develops the cohomology of a finite group with coefficients in a module and
proves that it vanishes in positive degrees when the order of the group is invertible in the
coefficients. It builds on GroupAlgebras for the group ring and feeds SpectralSequences.

## Scope and ownership

This roadmap owns group cohomology in low degrees. Shapiro's lemma is stated by InducedModules §2.1.

## Conventions

Modules are left modules over the group ring.

## Exact supplier contracts

### From Mathlib

- `groupCohomology`, the cohomology of a representation.

### From TauCetiRoadmap.GroupAlgebras

- §1.2, the group ring of a finite group and its augmentation ideal.
- What GroupAlgebras must provide (gap:norm-element): the norm element of the group ring.

## How to read the build

Layer 1 sets up the complex; Layer 2 proves the vanishing theorem.

## Layer 1: the standard complex

### 1.1 Cochains

Define the cochains of `G` with values in `M`. *Needs:* GroupAlgebras §1.2.

**Checks.**

- For the trivial group the complex is `M` in degree zero.

### Dependencies

GroupAlgebras §1.2.

## Layer 2: vanishing

### 2.1 Vanishing

Prove that `Hⁿ(G, M) = 0` for `n ≥ 1` when `|G|` is invertible on `M`. *Needs:* Layer 1, gap:norm-element.

### Dependencies

Layer 1.

## Downstream consumers

- SpectralSequences uses the vanishing theorem.

## References

- [B] A book on group cohomology.
"""
NEW_ORDER = layout.reorder(OLD_ORDER)
KNOWN = {"ToyCohomology", "GroupAlgebras", "InducedModules", "SpectralSequences"}


def test_the_two_orders_are_the_same_readme_in_another_order():
    assert NEW_ORDER != OLD_ORDER and layout.section_permutation(OLD_ORDER, NEW_ORDER)
    titles = [p.title for p in layout.parts(NEW_ORDER)[1:]]
    assert titles[0] == "how to read the build" and titles[-3:] == ["scope and ownership", "exact supplier contracts",
                                                                     "references"]


def test_the_supplier_graph_reads_both_orders_alike():
    for name, readme in (("ToyCohomology", OLD_ORDER), ("ToyCohomology", NEW_ORDER)):
        edges = depgraph.supplier_edges({name: readme}, KNOWN)
        assert set(edges[name]) == {"GroupAlgebras"}
        assert depgraph.declared_consumers(readme, name) == {"SpectralSequences"}
    # (the edges carry the lines that name each supplier, which move with their sections)
    assert set(depgraph.supplier_edges({"T": OLD_ORDER}, KNOWN)["T"]) == set(
        depgraph.supplier_edges({"T": NEW_ORDER}, KNOWN)["T"])


def test_the_overlap_subject_leaves_the_supplier_section_aside_wherever_it_stands():
    assert overlaps._subject(OLD_ORDER) == overlaps._subject(NEW_ORDER)
    assert "groupCohomology" not in " ".join(overlaps._subject(NEW_ORDER)[0])


def test_targets_samples_gaps_and_the_gate_read_both_orders_alike(tmp_path):
    assert gates.readme_targets(OLD_ORDER) == gates.readme_targets(NEW_ORDER) == ["1.1 Cochains", "2.1 Vanishing"]
    assert sample_items(OLD_ORDER) == sample_items(NEW_ORDER)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"gaps": [{"slug": "norm-element"}]}), encoding="utf-8")
    assert gap_reference_problems(OLD_ORDER, plan) == gap_reference_problems(NEW_ORDER, plan) == []
    lean = "import Mathlib\n\ntheorem vanishing : True := sorry\n"
    opts = gates.GateOptions()
    old, new = gates.scan_folder("ToyCohomology", OLD_ORDER, lean, opts), gates.scan_folder("ToyCohomology", NEW_ORDER,
                                                                                              lean, opts)
    assert old.counts == new.counts and old.details == new.details
