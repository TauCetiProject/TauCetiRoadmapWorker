"""Supplier cycles through other open pull requests (`depgraph.open_cycles`): a roadmap that exists
only in another author's open pull request is in neither the merge order nor the managed snapshots,
so `check_order` cannot see that it cites the roadmap under check back."""

from tauceti_roadmap_worker import cli, gates
from tauceti_roadmap_worker.depgraph import OPEN_CYCLE, open_cycles

NEEDS = "## Layer 1: x\n\nDefine `x`. *Needs:* {} §1.2.\n"
PLAIN = "## Layer 1: x\n\nDefine `x` from Mathlib alone.\n"
MAIN = [("main", "Base", PLAIN), ("main", "Tools", PLAIN)]


def needs(*names: str) -> str:
    return "".join(NEEDS.format(n) for n in names)


def test_two_pull_requests_that_cite_each_other():
    texts = MAIN + [("#10", "Alpha", needs("Base")), ("#20", "Beta", needs("Alpha"))]
    assert open_cycles("Alpha", needs("Base"), texts, {"Base", "Tools"}, 10) == []
    # the candidate text replaces the head's: the citation about to be pushed is what is checked
    lines = open_cycles("Alpha", needs("Base", "Beta"), texts, {"Base", "Tools"}, 10)
    assert lines == [(f"{OPEN_CYCLE}: Alpha (#10) cites Beta; Beta (#20) cites Alpha "
                     "(no merge order exists while all of these stand)")]
    # and the repair is seen at once, whatever the head still says
    stale = MAIN + [("#10", "Alpha", needs("Beta")), ("#20", "Beta", needs("Alpha"))]
    assert open_cycles("Alpha", needs("Base"), stale, {"Base", "Tools"}, 10) == []
    assert open_cycles("Alpha", needs("Base"), stale, {"Base", "Tools"}) == []       # the pull request found by name


def test_a_circle_through_three_pull_requests_is_reported_in_order():
    texts = MAIN + [("#10", "Alpha", PLAIN), ("#20", "Beta", needs("Gamma")), ("#30", "Gamma", needs("Alpha"))]
    lines = open_cycles("Alpha", needs("Beta"), texts, {"Base", "Tools"}, 10)
    assert lines == [(f"{OPEN_CYCLE}: Alpha (#10) cites Beta; Beta (#20) cites Gamma; Gamma (#30) cites Alpha "
                     "(no merge order exists while all of these stand)")]
    # a cycle elsewhere is another roadmap's defect
    other = MAIN + [("#10", "Alpha", PLAIN), ("#20", "Beta", needs("Gamma")), ("#30", "Gamma", needs("Beta"))]
    assert open_cycles("Alpha", needs("Beta"), other, {"Base", "Tools"}, 10) == []


def test_a_roadmap_on_main_constrains_nothing():
    # Beta cites Base, and a pull request that revises Base cites Alpha: Base is on main already, so Beta
    # waits for nothing but Alpha's own pull request.
    texts = MAIN + [("#10", "Alpha", PLAIN), ("#20", "Beta", needs("Base")), ("#30", "Base", needs("Alpha"))]
    assert open_cycles("Alpha", needs("Beta"), texts, {"Base", "Tools"}, 10) == []
    # the revision itself waits for the new roadmap it cites, and nothing can wait for the revision
    assert open_cycles("Base", needs("Alpha"), texts, {"Base", "Tools"}, 30) == []
    # the rows of main say what is on main when `merged` does not
    assert open_cycles("Alpha", needs("Beta"), texts, set(), 10) == []


def test_the_folders_of_one_pull_request_merge_together():
    # #20 adds Beta and Delta; Alpha cites Beta and Delta cites Alpha: no cycle between roadmaps, one
    # between the pull requests.
    texts = MAIN + [("#10", "Alpha", PLAIN), ("#20", "Beta", PLAIN), ("#20", "Delta", needs("Alpha"))]
    lines = open_cycles("Alpha", needs("Beta"), texts, {"Base", "Tools"}, 10)
    assert lines == [(f"{OPEN_CYCLE}: Alpha (#10) cites Beta; Delta (#20) cites Alpha "
                     "(no merge order exists while all of these stand)")]
    # a citation between two folders of the same pull request is no dependency at all
    own = MAIN + [("#10", "Alpha", PLAIN), ("#10", "Beta", needs("Alpha"))]
    assert open_cycles("Alpha", needs("Beta"), own, {"Base", "Tools"}, 10) == []


def test_a_stacked_branch_that_carries_its_parent_is_not_the_one_that_adds_it():
    # #25 is stacked on #20 against main: it carries Beta too, and its own Gamma cites Alpha. Alpha cites
    # Beta, which the older #20 adds, and #20 waits for nothing.
    texts = MAIN + [("#10", "Alpha", PLAIN), ("#20", "Beta", PLAIN), ("#25", "Beta", PLAIN),
                    ("#25", "Gamma", needs("Alpha", "Beta"))]
    assert open_cycles("Alpha", needs("Beta"), texts, {"Base", "Tools"}, 10) == []


def test_a_roadmap_with_no_pull_request_yet_stands_alone():
    texts = MAIN + [("#20", "Beta", needs("Alpha"))]            # a package on the board, cited ahead of its port
    lines = open_cycles("Alpha", needs("Beta"), texts, {"Base", "Tools"})
    assert lines == [(f"{OPEN_CYCLE}: Alpha cites Beta; Beta (#20) cites Alpha "
                     "(no merge order exists while all of these stand)")]


def test_the_gate_counts_it_as_a_dependency_problem():
    from .test_gate_scanners import CLEAN_LEAN, CLEAN_README, opts
    texts = MAIN + [("#10", "Toy", CLEAN_README), ("#20", "Beta", needs("Toy"))]
    cyclic = CLEAN_README + "\n*Needs:* Beta §2.\n"
    r = gates.scan_folder("Toy", cyclic, CLEAN_LEAN, opts(open_readmes=texts, pull_request=10, merged={"Base", "Tools"}))
    assert r.counts["depcheck"] == 1 and r.details["depcheck"][0].startswith(OPEN_CYCLE), r.details
    # with a merge order as well: both checks, one count each
    r = gates.scan_folder("Toy", cyclic, CLEAN_LEAN, opts(order=["Toy"], open_readmes=texts, pull_request=10))
    assert r.counts["depcheck"] == 1, r.details
    r = gates.scan_folder("Toy", CLEAN_README, CLEAN_LEAN, opts(order=["Toy"], open_readmes=texts, pull_request=10))
    assert r.counts["depcheck"] == 0, r.details
    assert "CYCLE through open pull requests" in gates.GATE_RULES["depcheck"]


def test_the_agents_check_prints_it():
    texts = MAIN + [("#10", "Alpha", PLAIN), ("#20", "Beta", needs("Alpha"))]
    lines, problems = cli.depcheck_lines("Alpha", needs("Beta"), {}, ["Alpha"], set(), open_readmes=texts, pull_request=10)
    assert len(problems) == 1 and problems[0].startswith(OPEN_CYCLE) and lines[-1].endswith("1 problem(s)")
    lines, problems = cli.depcheck_lines("Alpha", needs("Beta"), {}, ["Alpha"], set())
    assert problems == []
