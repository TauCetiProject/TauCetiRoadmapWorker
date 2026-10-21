import re

from tauceti_roadmap_worker.depgraph import (MARK_CLOSE, MARK_OPEN, apply_paragraph, dependency_paragraph, merge_chain,
                                             merge_order_paragraph, supplier_edges)

ORDER = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
# Echo needs Delta and Bravo; Delta needs Charlie; Charlie needs Alpha; Foxtrot needs Echo.
# Bravo cites Foxtrot upward (a defect, never in a chain).
READMES = {
    "Alpha": "",
    "Bravo": "*Needs:* Foxtrot §1.\n",
    "Charlie": "*Needs:* Alpha §1.\n",
    "Delta": "*Needs:* Charlie §2.\n",
    "Echo": "*Needs:* Delta §3 and Bravo §1.\n",
    "Foxtrot": "*Needs:* Echo §1.\n",
}
EDGES = supplier_edges(READMES, set(ORDER))


def test_transitive_closure_in_order_rank():
    assert merge_chain("Echo", EDGES, ORDER, set(ORDER)) == ["Alpha", "Bravo", "Charlie", "Delta"]
    assert merge_chain("Foxtrot", EDGES, ORDER, set(ORDER)) == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]


def test_chain_skips_merged_and_upward():
    open_now = {"Bravo", "Charlie", "Delta", "Echo", "Foxtrot"}          # Alpha has merged
    assert merge_chain("Echo", EDGES, ORDER, open_now) == ["Bravo", "Charlie", "Delta"]
    assert merge_chain("Bravo", EDGES, ORDER, set(ORDER)) == []      # Bravo -> Foxtrot is upward: not a prerequisite
    assert merge_chain("Zulu", EDGES, ORDER, set(ORDER)) == []      # not in the order


def test_paragraph_is_chain_only_without_position():
    prs = {"Alpha": 901, "Bravo": 902, "Charlie": 903, "Delta": 904, "Echo": 905}
    chain = [(prs[n], n) for n in merge_chain("Echo", EDGES, ORDER, set(ORDER))]
    para = merge_order_paragraph(905, chain)
    assert para.startswith(MARK_OPEN) and para.endswith(MARK_CLOSE)
    assert "#901 (Alpha) → #902 (Bravo) → #903 (Charlie) → #904 (Delta) → **this pull request** (#905)." in para
    assert "position" not in para.lower() and not re.search(r"\b\d+ of \d+\b|rank", para)
    alone = merge_order_paragraph(901, [])
    assert "can merge as soon as it is approved" in alone
    ext = merge_order_paragraph(905, chain, external=[(77, "Other")], mutual=[(78, "Peer")])
    assert "#77 (Other)" in ext and "#78 (Peer)" in ext


def test_paragraph_wording_is_the_maintainers():
    """The exact sentences decycle/merge_order.py writes on the maintainer's pull requests."""
    assert merge_order_paragraph(792, [(785, "ReductiveGroupsPartII")],
                                 external=[(248, "LFunctions", "roed-math"), (934, "QuaternionicModularForms", "WilliamCoram")]) == (
        f"{MARK_OPEN}\n**Merge order.** #785 (ReductiveGroupsPartII) → **this pull request** (#792). It also depends on "
        "other authors' pull requests that add roadmaps it cites, which must merge first: #248 (LFunctions, roed-math), "
        f"#934 (QuaternionicModularForms, WilliamCoram). Roadmaps already on main are not listed.\n{MARK_CLOSE}")
    assert merge_order_paragraph(1336, [], external=[]) == (
        f"{MARK_OPEN}\n**Merge order.** No open pull request of ours must merge before this one; **this pull request** "
        "(#1336) can merge as soon as it is approved. It depends on no other author's open pull request. Roadmaps "
        f"already on main are not listed.\n{MARK_CLOSE}")
    # Other authors' pull requests not read: nothing is said about them (never "none" unchecked).
    assert "other author" not in merge_order_paragraph(1336, [], external=None)
    assert merge_order_paragraph(5, [], external=[], mutual=[(78, "Peer", "someone")]).split("**this pull request** (#5) ")[1] == (
        "can merge as soon as it is approved. It depends on no other author's open pull request. It also cites "
        "#78 (Peer, someone), which in turn consume this one (directly or through one another); these specifications "
        "have no whole-pull-request order among themselves, and the stage-level graph between them is acyclic. "
        f"Roadmaps already on main are not listed.\n{MARK_CLOSE}")


def test_apply_paragraph_is_idempotent_and_safe():
    body = "Synopsis of the roadmap, long enough to be a real description.\n\n\U0001f916 Generated with [Claude Code](x)"
    para1 = merge_order_paragraph(905, [(901, "Alpha")])
    once = apply_paragraph(body, para1)
    assert once.index(MARK_OPEN) < once.index("\U0001f916 Generated with")
    assert apply_paragraph(once, para1) == once
    para2 = merge_order_paragraph(905, [])
    twice = apply_paragraph(once, para2)
    assert twice.count(MARK_OPEN) == 1 and "#901" not in twice
    assert apply_paragraph('{"message": "API rate limit exceeded", "documentation_url": "https://docs"}', para1) is None
    assert apply_paragraph("", para1) is None


def test_dependency_paragraph_is_the_chain():
    """Regression (TauCetiRoadmap #1351): the Dependencies sentence said "none" above a Merge order
    chain of eleven pull requests of ours; both now come from the same chain."""
    text = dependency_paragraph([(901, "Alpha"), (903, "Charlie")])
    assert "In open pull requests of ours: #901 (Alpha), #903 (Charlie)." in text      # what sync_prose.py rewrites
    assert "In open pull requests of ours: none." in dependency_paragraph([])
    assert "not determined" in dependency_paragraph(None)
