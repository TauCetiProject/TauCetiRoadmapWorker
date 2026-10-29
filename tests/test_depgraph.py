import textwrap

import pytest

from tauceti_roadmap_worker.depgraph import (check_order, declared_consumers, read_order, strongly_connected,
                                            supplier_edges, units)

ORDER = read_order("# suppliers first\nAlpha\nBeta\nGamma  # a trailing comment\n\nBeta\n")


def readme(body: str) -> str:
    return textwrap.dedent(body)


def test_read_order():
    assert ORDER == ["Alpha", "Beta", "Gamma"]


def test_downward_edge_is_fine():
    readmes = {"Beta": readme("""\
        ## Layer 1: x
        Define `x`. *Needs:* Alpha §1.2, Mathlib `Nat.card`.
        """), "Alpha": "# Roadmap: alpha\n"}
    edges = supplier_edges(readmes, set(ORDER))
    assert dict(edges["Beta"]) == {"Alpha": {2}}
    assert check_order(edges, ORDER) == []


def test_upward_edge_is_flagged():
    readmes = {"Alpha": readme("""\
        ### 1.1 y
        Prove `y`. *Needs:* Gamma layer 3.
        """), "Gamma": "# Roadmap: gamma\n"}
    problems = check_order(supplier_edges(readmes, set(ORDER)), ORDER)
    assert problems == ["UPWARD: Alpha (#1) -> Gamma (#3) at README lines [2]"]


def test_cycle_is_flagged():
    readmes = {"Alpha": "*Needs:* Beta §1.\n", "Beta": "*Needs:* Alpha §2.\n"}
    problems = check_order(supplier_edges(readmes, set(ORDER)), ORDER)
    assert "CYCLE among: Alpha, Beta" in problems
    assert any(p.startswith("UPWARD: Alpha") for p in problems)


def test_consumers_are_not_suppliers():
    text = readme("""\
        ## Layer 1: z
        Prove `z`. *Needs:* Alpha §1. Consumed by Gamma §4, which builds on it.

        ## Downstream consumers
        - *Gamma.* uses `z` in layer 4; it supplies nothing here.

        ### Dependencies
        Alpha layer 1 supplies `w`. Gamma consumes the result of 1.1.
        """)
    edges = supplier_edges({"Beta": text}, set(ORDER))
    assert set(edges["Beta"]) == {"Alpha"}


def test_supplier_table_and_contract_bullets():
    text = readme("""\
        ## Exact supplier contracts
        | Supplier | What |
        | --- | --- |
        | Alpha | the carrier `A` |
        - *Gamma.* the map `g`, used in 2.1.
        ### From Beta
        """)
    found = {name for _ln, unit in units(text) for name in ("Alpha", "Beta", "Gamma") if name in unit}
    assert found == {"Alpha", "Beta", "Gamma"}


def test_not_in_order_and_only_filter():
    readmes = {"Delta": "*Needs:* Alpha §1.\n", "Alpha": "", "Beta": "*Needs:* Gamma §1.\n", "Gamma": ""}
    edges = supplier_edges(readmes, set(ORDER))
    problems = check_order(edges, ORDER)
    assert "NOT IN ORDER: Delta (insert it after its last supplier)" in problems
    assert check_order(edges, ORDER, only={"Delta"}) == ["NOT IN ORDER: Delta (insert it after its last supplier)"]


def test_main_extended_roadmaps_are_never_consumers():
    readmes = {"Alpha": "*Needs:* Gamma §1.\n", "Gamma": ""}
    assert check_order(supplier_edges(readmes, set(ORDER), main_extended={"Alpha"}), ORDER) == []


def test_scc_is_iterative():
    chain = {f"N{i}": {f"N{i + 1}"} for i in range(5000)}
    chain["N5000"] = {"N0"}
    [comp] = strongly_connected(chain)
    assert len(comp) == 5001


def test_insert_into_order_after_last_supplier():
    from tauceti_roadmap_worker.depgraph import insert_into_order
    text = "# order\nAlpha\nBeta  # note\nGamma\n"
    assert insert_into_order(text, "Delta", {"Alpha", "Beta"}) == "# order\nAlpha\nBeta  # note\nDelta\nGamma\n"
    assert insert_into_order(text, "Delta", set()) == text + "Delta\n"
    assert insert_into_order(text, "Beta", {"Gamma"}) == text
    assert read_order(insert_into_order(text, "Delta", {"Gamma", "Alpha"})) == ["Alpha", "Beta", "Gamma", "Delta"]


# --- consumer-only mentions (2026-10-22: a false `CYCLE among: HabiroCohomologyFoundations, QWittVectors`) ---

QWITT = readme("""\
    # Roadmap: arithmetic Λ-rings and q-Witt vectors

    ## Scope and ownership

    HabiroCyclotomicCompletions owns integral cyclotomic ideal theory. HabiroRings and HabiroCohomologyFoundations own Habiro rings, gluing, q-Hodge filtrations and descent; RefinedTraceMethods owns refined traces. The completion extension of DerivedModuleAlgebra requires Part II suppliers with the interfaces below.

    ## Exact supplier contracts

    ### From TauCetiRoadmap.HabiroCyclotomicCompletions

    The integral cyclotomic contract.

    ## Downstream consumers

    - **HabiroCohomologyFoundations**: Layers 2–6 supply relative arithmetic Λ and degree-zero q-Witt theory.
    - **HabiroRings**: Layers 1–6: arithmetic Λ-perfection.
    - **RefinedTraceMethods**: Layers 7–10 provide the q-Witt operations.

    Consumers use the exported algebraic interfaces. Their constructions supply no prerequisites to QWittVectors.
    """)
HABIRO = readme("""\
    # Roadmap: Habiro cohomology

    ## Layer 1: the completed overlap

    Construct the overlap. *Needs:* QWittVectors Layer 6 (the completed overlap).
    """)
QWITT_ORDER = ["DerivedModuleAlgebra", "HabiroCyclotomicCompletions", "HabiroRings", "QWittVectors",
               "RefinedTraceMethods", "HabiroCohomologyFoundations"]


def test_a_roadmap_named_only_as_a_consumer_is_no_supplier():
    """QWittVectors' scope paragraph says "suppliers" and names HabiroCohomologyFoundations as an owner;
    its consumers section lists it. The old parser read the paragraph whole: QWittVectors ->
    HabiroCohomologyFoundations, and with HabiroCohomologyFoundations' *Needs:* a cycle."""
    readmes = {"QWittVectors": QWITT, "HabiroCohomologyFoundations": HABIRO}
    scope = next(unit for _ln, unit in units(QWITT, "QWittVectors") if "own Habiro rings" in unit)
    assert "HabiroCohomologyFoundations" in scope          # the prose line is still read ...
    edges = supplier_edges(readmes, set(QWITT_ORDER))
    # ... but a declared consumer is not taken from it; the explicit suppliers stay.
    assert set(edges["QWittVectors"]) == {"HabiroCyclotomicCompletions", "DerivedModuleAlgebra"}
    assert set(edges["HabiroCohomologyFoundations"]) == {"QWittVectors"}
    assert check_order(edges, QWITT_ORDER) == []
    assert declared_consumers(QWITT, "QWittVectors") >= {"HabiroCohomologyFoundations", "HabiroRings",
                                                         "RefinedTraceMethods"}


def test_headings_nested_in_a_consumers_section_are_consumers_too():
    text = readme("""\
        *Needs:* Alpha §1.

        ## Downstream consumers

        ### Gamma

        Gamma reads the supplier interface of layer 2 through Delta's carrier.

        ## References
        """)
    edges = supplier_edges({"Beta": text}, {"Alpha", "Gamma", "Delta"})
    assert set(edges["Beta"]) == {"Alpha"}
    # "What this roadmap consumes" is not a consumers section: its nested `### From` headings still count.
    text = "## What this roadmap consumes\n\n### From Alpha\n\nThe carrier.\n"
    assert set(supplier_edges({"Beta": text}, {"Alpha"})["Beta"]) == {"Alpha"}


def test_x_consumes_and_consumed_by_x_name_consumers():
    text = readme("""\
        Our supplier is Alpha. Gamma R3 consumes Delta's carrier together with ours.

        Layer 2 is consumed by Epsilon and Zeta.

        The supplier contract of layer 3 is the one Epsilon reads with Alpha.
        """)
    edges = supplier_edges({"Beta": text}, {"Alpha", "Gamma", "Delta", "Epsilon", "Zeta"})
    assert set(edges["Beta"]) == {"Alpha"}
    assert declared_consumers(text, "Beta") >= {"Gamma", "Epsilon", "Zeta"}
    # The README's own roadmap consuming a supplier names that supplier.
    own = "Beta consumes Alpha's carrier; the supplier contract is below.\n"
    assert set(supplier_edges({"Beta": own}, {"Alpha"})["Beta"]) == {"Alpha"}


def test_an_explicit_supplier_statement_counts_for_a_declared_consumer():
    """A roadmap both consumes and supplies (stage-level mutual citation): an explicit *Needs:* keeps
    the edge, and check_order reports the cycle as before."""
    text = "*Needs:* Gamma §2.\n\n## Downstream consumers\n\n- **Gamma**: uses layer 3.\n"
    assert set(supplier_edges({"Beta": text}, {"Gamma"})["Beta"]) == {"Gamma"}


# --- roadmaps on the content repository's main ----------------------------------------------------

def test_a_roadmap_on_main_is_never_an_upward_supplier_nor_a_checked_consumer():
    from tauceti_roadmap_worker.depgraph import remove_from_order, repair_upward
    readmes = {"Alpha": "*Needs:* Gamma §3, Beta §1.\n", "Gamma": "*Needs:* Delta §2.\n", "Beta": "# Roadmap: beta\n",
               "Epsilon": "*Needs:* Beta §1.\n"}
    order = ORDER + ["Delta"]
    edges = supplier_edges(readmes, set(order))
    assert check_order(edges, order) == ["UPWARD: Alpha (#1) -> Beta (#2) at README lines [1]",
                                         "UPWARD: Alpha (#1) -> Gamma (#3) at README lines [1]",
                                         "UPWARD: Gamma (#3) -> Delta (#4) at README lines [1]",
                                         "NOT IN ORDER: Epsilon (insert it after its last supplier)"]
    # Gamma is on main: no edge to it is upward, and it is not checked as a consumer; nor is Epsilon.
    assert check_order(edges, order, merged={"Gamma", "Epsilon"}) == [
        "UPWARD: Alpha (#1) -> Beta (#2) at README lines [1]"]
    assert check_order(edges, order, only={"Alpha"}, merged={"Gamma", "Beta"}) == []
    # A cycle among roadmaps on main alone is not reported. Beta is on main and its open revision
    # consumes Alpha: no cycle for Alpha's gate (the revision is another pull request's business), but
    # the revision's own gate sees its open supplier as upward of main, and the cycle.
    loop = supplier_edges({"Alpha": "*Needs:* Beta §1.\n", "Beta": "*Needs:* Alpha §2.\n"}, set(ORDER))
    assert check_order(loop, ORDER, merged={"Alpha", "Beta"}) == []
    assert check_order(loop, ORDER, merged={"Beta"}) == []
    assert check_order(loop, ORDER, only={"Alpha"}, merged={"Beta"}) == []
    assert check_order(loop, ORDER, only={"Beta"}, merged={"Beta"}) == [
        "UPWARD: Beta (on main) -> Alpha (#1) at README lines [1]", "CYCLE among: Alpha, Beta"]
    # The order's repair moves nothing after a roadmap on main, and never moves one.
    text = "# suppliers first\nAlpha\nBeta\nGamma\n"
    assert repair_upward(text, readmes, merged={"Gamma"})[0] == "# suppliers first\nBeta\nAlpha\nGamma\n"
    assert repair_upward(text, readmes, merged={"Gamma", "Beta"}) == (text, {})
    assert remove_from_order("# suppliers first\nAlpha  # x\nBeta\nGamma\n", {"Alpha", "Gamma"}) == "# suppliers first\nBeta\n"


def test_move_with_riders_carries_the_consumer_and_refuses_a_cycle():
    from tauceti_roadmap_worker.depgraph import OrderCycle, move_with_riders
    text = "Alpha\nBeta\nGamma\nToy\n"
    edges = supplier_edges({"Alpha": "*Needs:* Toy §1.\n", "Beta": "*Needs:* Alpha §2.\n", "Gamma": "*Needs:* Beta §1.\n",
                            "Toy": "x\n"}, {"Alpha", "Beta", "Gamma", "Toy"})
    new, note = move_with_riders(text, "Alpha", set(edges["Alpha"]), edges)
    assert new == "Toy\nAlpha\nBeta\nGamma\n" and "its consumers Beta, Gamma moved with it" in note
    # Toy on main: nothing moves after it.
    assert move_with_riders(text, "Alpha", set(edges["Alpha"]), edges, merged={"Toy"}) == (text, "")
    loop = supplier_edges({"Alpha": "*Needs:* Toy §1.\n", "Toy": "*Needs:* Alpha §2.\n"}, {"Alpha", "Toy"})
    with pytest.raises(OrderCycle):
        move_with_riders("Alpha\nToy\n", "Alpha", set(loop["Alpha"]), loop)


def test_a_late_supplier_is_pulled_up_with_its_chain_when_that_moves_fewer():
    from tauceti_roadmap_worker.depgraph import OrderCycle, pull_suppliers_up, repair_edge
    # Alpha (#1) consumes Late (#5); Late consumes Chain (#4); Beta and Gamma consume Alpha.
    text = "Alpha\nBeta\nGamma\nChain\nLate\n"
    edges = supplier_edges({"Alpha": "*Needs:* Late §1.\n", "Beta": "*Needs:* Alpha §1.\n", "Gamma": "*Needs:* Beta §1.\n",
                            "Late": "*Needs:* Chain §2.\n", "Chain": "x\n"}, {"Alpha", "Beta", "Gamma", "Chain", "Late"})
    new, note = pull_suppliers_up(text, "Alpha", set(edges["Alpha"]), edges)
    assert new == "Chain\nLate\nAlpha\nBeta\nGamma\n"
    assert note == "Late moved in the merge order up before Alpha (supplier of it listed after it); Chain moved with it"
    # The pull moves 2 roadmaps, the ride-along 3: the pull wins (a tie goes to the ride).
    assert repair_edge(text, "Alpha", set(edges["Alpha"]), edges)[0] == new
    # A leaf consumer with a long supplier chain moves down instead.
    text2 = "Leaf\nOne\nTwo\nThree\nFour\n"
    edges2 = supplier_edges({"Leaf": "*Needs:* Four §1.\n", "Four": "*Needs:* Three §1.\n", "Three": "*Needs:* Two §1.\n",
                             "Two": "*Needs:* One §1.\n", "One": "x\n"}, {"Leaf", "One", "Two", "Three", "Four"})
    assert set(edges2["Leaf"]) == {"Four"}
    assert repair_edge(text2, "Leaf", set(edges2["Leaf"]), edges2)[0] == "One\nTwo\nThree\nFour\nLeaf\n"
    # Late on main: nothing to pull.
    assert pull_suppliers_up(text, "Alpha", set(edges["Alpha"]), edges, merged={"Late"}) == (text, "")
    loop = supplier_edges({"Alpha": "*Needs:* Toy §1.\n", "Toy": "*Needs:* Alpha §2.\n"}, {"Alpha", "Toy"})
    with pytest.raises(OrderCycle):
        repair_edge("Alpha\nToy\n", "Alpha", set(loop["Alpha"]), loop)
