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
    assert "can merge once approved" in alone
    ext = merge_order_paragraph(905, chain, external=[(77, "Other")], mutual=[(78, "Peer")])
    assert "#77 (Other)" in ext and "#78 (Peer)" in ext


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


def test_dependency_paragraph_for_new_port():
    readme = "*Needs:* Charlie §2 and Alpha §1, Mathlib `Nat`. Consumed by Foxtrot.\n"
    text = dependency_paragraph("Delta", readme, {"Alpha": 901, "Charlie": 903, "Foxtrot": 906}, ORDER)
    assert "#901 (Alpha), #903 (Charlie)" in text and "Foxtrot" not in text.split("In open")[1].split(".")[0]
    assert "none" in dependency_paragraph("Alpha", "", {"Bravo": 902}, ORDER)
