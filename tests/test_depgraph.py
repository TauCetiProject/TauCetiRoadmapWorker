import textwrap

from tauceti_roadmap_worker.depgraph import check_order, read_order, strongly_connected, supplier_edges, units

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
