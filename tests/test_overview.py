"""The overview pass (`overview`): the opening rewritten by an agent, the sections reordered, direction
words corrected in the sections that moved, the check that nothing else changed (`layout.rewrite_problems`),
the carry of the AI acceptance across such a push (`intake`), and the command."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import intake, layout, overview

from .fakes import FakeBoard, example_config
from .test_layout import FOO, pulls
from .test_prreview import H1, H2, REPO, ContentGH, created, node, review_job, sync, verdict_file

H3 = "3" * 40

OLD = """# Roadmap: toy groups

**Summary.** Develop orders, Lagrange's theorem (Layer 2) and `Toy.order_dvd` from `Subgroup.index`.

## Scope and ownership

The layers are labelled L1–L2 in the layer headings below. This roadmap owns the order of an element,
as the supplier contracts below state.

## Conventions

Groups are written multiplicatively.

## Exact supplier contracts

### From Mathlib

- `Subgroup.index`, the index of a subgroup, as fixed in the conventions above.

## How to read the build

Layer 1 defines the order; Layer 2 proves Lagrange's theorem, using the conventions above.

## Layer 1: the order of an element

### 1.1 Order

Define the order of `g` as the least positive `n` with `g ^ n = 1`, for `G` finite.

**Checks.**

- In the cyclic group of order 6 the generator has order 6.

## Layer 2: Lagrange's theorem

### 2.1 Lagrange

Prove that the order of a subgroup of a finite group divides the order of the group.

## References

- [L] A book on groups.
"""

OPENING = """**Summary.** This roadmap develops the order of an element of a finite group and proves
Lagrange's theorem: the order of a subgroup of a finite group divides the order of the group.

It builds on Mathlib's index of a subgroup. Layer 1 defines the order; Layer 2 proves Lagrange's theorem."""


def reordered_and_fixed(text=OLD):
    return overview.fix_directions(layout.reorder(text))


# --- the direction words ---------------------------------------------------------------------------------

def test_direction_words_the_move_made_wrong_are_corrected_in_the_sections_that_moved_only():
    text, changes = reordered_and_fixed()
    scope = next(p.body for p in layout.parts(text) if p.title == layout.SCOPE)
    guide = next(p.body for p in layout.parts(text) if p.title == layout.HOW_TO_READ)
    suppliers = next(p.body for p in layout.parts(text) if p.title == layout.SUPPLIERS)
    # Scope now follows the layers; the supplier contracts still follow Scope; the guide now comes first.
    assert "in the layer headings above" in scope and "as the supplier contracts below state" in scope
    assert "using the conventions below" in guide
    # The conventions now come before the supplier contracts: "above" is right there and stays.
    assert "as fixed in the conventions above" in suppliers
    assert changes == [("how to read the build", "the conventions above", "the conventions below"),
                       ("scope and ownership", "the layer headings below", "the layer headings above")]
    # A section that did not move keeps its words, and a README whose words point the right way is left alone.
    assert overview.fix_directions(text) == (text, [])
    assert not layout.rewrite_problems(OLD, overview.build(text, OPENING, fold=False))


def test_a_readme_an_earlier_pass_reordered_has_its_direction_words_corrected_too():
    """`layout` reorders without touching a word, so the sections it moved keep "below" for what is
    now above: the overview pass corrects them, and the check passes against the reordered head."""
    earlier = layout.reorder(OLD)                       # what the `layout` pass pushed
    assert layout.moved(earlier) == []
    text, changes = overview.fix_directions(earlier)
    assert (text, changes) == reordered_and_fixed()
    assert changes == [("how to read the build", "the conventions above", "the conventions below"),
                       ("scope and ownership", "the layer headings below", "the layer headings above")]
    assert not layout.rewrite_problems(earlier, overview.build(text, OPENING, fold=False))


def test_the_agent_may_propose_only_direction_swaps_in_the_sections_that_moved():
    text, _changes = reordered_and_fixed(OLD.replace("This roadmap owns", "As stated below, this roadmap owns"))
    new, applied, refused = overview.apply_proposals(text, [
        {"section": "Scope and ownership", "old": "As stated below", "new": "As stated above"},
        {"section": "Scope and ownership", "old": "the order of an element", "new": "the order of a group"},
        {"section": "Layer 1: the order of an element", "old": "least positive", "new": "least positive"},
        {"section": "Conventions", "old": "multiplicatively", "new": "additively"},
        {"section": "Scope and ownership", "old": "the", "new": "the"}])
    assert applied == ["Scope and ownership: 'As stated below' -> 'As stated above'"]
    assert len(refused) == 4 and "is not a swap of direction words" in refused[0]
    assert "not a section that moves" in refused[1] and "not in it exactly once" in refused[3]
    assert "As stated above, this roadmap owns" in new and not layout.rewrite_problems(
        OLD.replace("This roadmap owns", "As stated below, this roadmap owns"), overview.build(new, OPENING, False))


# --- the check ----------------------------------------------------------------------------------------------

def test_the_check_accepts_each_allowed_kind_of_change():
    text, _ = reordered_and_fixed()
    assert layout.rewrite_problems(OLD, layout.reorder(OLD)) == []                          # (b) alone
    assert layout.rewrite_problems(OLD, overview.build(layout.reorder(OLD), OPENING, False)) == []   # (a)
    assert layout.rewrite_problems(OLD, overview.build(text, OPENING, False)) == []         # (a), (b), (d)
    folded = overview.build(text, OPENING, True)                                           # (c)
    assert "## How to read the build" not in folded and layout.rewrite_problems(OLD, folded) == []


@pytest.mark.parametrize("edit, problem", [
    (("for `G` finite", "for every `G`"), "the section `## Layer 1: the order of an element` changed"),
    (("the generator has order 6", "the generator has order 3"), "the section `## Layer 1: the order of an element`"),
    (("divides the order", "equals the order"), "the section `## Layer 2: Lagrange's theorem` changed"),
    (("written multiplicatively", "written additively"), "the section `## Conventions` changed"),
    (("Groups are written", "Groups below are written"), "the section `## Conventions` changed"),
    (("# Roadmap: toy groups", "# Roadmap: groups"), "the title changed"),
    (("**Summary.** This roadmap", "This roadmap"), "the opening does not open with `**Summary.**`"),
    (("This roadmap owns the order", "This roadmap owns the index"), "the section `## Scope and ownership` changed"),
    (("Layer 2 proves Lagrange's", "Layer 2 states Lagrange's"), "the section `## How to read the build` changed"),
])
def test_the_check_refuses_any_other_edit(edit, problem):
    text, _ = reordered_and_fixed()
    new = overview.build(text, OPENING, False).replace(*edit)
    assert any(p.startswith(problem) for p in layout.rewrite_problems(OLD, new)), layout.rewrite_problems(OLD, new)
    # A section dropped or added is refused too.
    assert "is gone" in " ".join(layout.rewrite_problems(OLD, new.split("## References")[0]))
    assert "changed" in " ".join(layout.rewrite_problems(OLD, overview.build(text, OPENING, False) + "\n## Notes\n\nX.\n"))


def test_the_opening_is_held_to_the_form_the_brief_asks():
    old = overview.layout.split_opening(OLD)[1]
    assert overview.opening_problems(OPENING, old) == []
    assert overview.opening_problems("The roadmap.", old) == ["it does not open with `**Summary.**`"]
    assert "it names Lean declarations the old opening did not: Toy.order_one" in \
        overview.opening_problems("**Summary.** It proves `Toy.order_one`.", old)
    assert overview.opening_problems("**Summary.** It cites `Toy.order_dvd`.", old) == []      # the old one did
    # a subscript in code font is mathematics, not a declaration name
    assert overview.opening_problems("**Summary.** For a level `K_f`, the norm `N𝔡_u`, `ord_p` and `H_m` of `zeta_K`.", old) == []
    assert overview.lean_names("`K_f` `map_add` `le_of_lt` `Foo.bar` `x_i` `H_dR`") == {"map_add", "le_of_lt", "Foo.bar"}
    assert overview.opening_problems("**Summary.** See §2.1 and #1234.", old) == [
        "it has a pull-request number", "it has a section-number locator"]
    assert "it has 900 words (at most about 700)" in overview.opening_problems("**Summary.** " + "w " * 899, old)
    # TeX-only syntax in plain text is the push gate's `tex` check: refused here, so the opening is rewritten
    tex = overview.opening_problems("**Summary.** The subcomplexes Ω^{≥i} of H_{m} and \\mathbb Z.", old)
    assert len(tex) == 1 and tex[0].startswith("it has TeX-only syntax outside code font (H_{m}, \\mathbb, Ω^{≥i})")
    assert overview.opening_problems("**Summary.** The subcomplexes `Ω^{≥i}` of `H_{m}`, and K_n.", old) == []


# --- the agent's proposal -------------------------------------------------------------------------------------

def agent_writing(opening, plan, touch_readme=False):
    """A stand-in for the agent process: it writes the opening and the plan in its working directory."""
    def launch(argv, cwd=None, **kwargs):
        (cwd / overview.OUT_NAME).write_text(opening, encoding="utf-8")
        (cwd / overview.PLAN_NAME).write_text(json.dumps(plan), encoding="utf-8")
        if touch_readme:          # whatever it does to the README is not what the worker builds from
            (cwd / "README.md").write_text("# Roadmap: something else\n", encoding="utf-8")
        return SimpleNamespace(stdin=SimpleNamespace(write=lambda s: None, close=lambda: None), stdout=iter(()),
                               wait=lambda timeout=None: 0, pid=0)
    return launch


def context(tmp_path, gh=None, jobs=()):
    cfg = example_config()
    cfg.roadmap.pool = ["carol"]
    return SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(jobs=list(jobs)), operator="carol", dry_run=True,
                           state=tmp_path / "state", wdir=tmp_path / "worker")


def test_a_proposal_is_built_from_the_agents_opening_and_checked(tmp_path):
    ctx = context(tmp_path)
    plan = {"fold_guide": True, "directions": [], "headlines": [{"statement": "Lagrange", "where": "2.1"}]}
    got = overview.propose(ctx, tmp_path / "w", "Toy", "TauCetiRoadmap/Toy/README.md", OLD, "import Mathlib\n",
                           agent="claude", model="m", launch=agent_writing(OPENING, plan, touch_readme=True))
    assert got.new and got.why == "" and layout.rewrite_problems(OLD, got.new) == []
    assert overview.layout.split_opening(got.new)[1] == OPENING and "## How to read the build" not in got.new
    assert got.changes[0] == (f"opening {overview.words(overview.layout.split_opening(OLD)[1])} -> "
                              f"{overview.words(OPENING)} words") and "guide folded into the opening" in got.changes
    assert (tmp_path / "w" / "README.proposed.md").read_text(encoding="utf-8") == got.new
    # An opening the form refuses is asked for again, then refused; nothing is proposed.
    got = overview.propose(ctx, tmp_path / "w", "Toy", "TauCetiRoadmap/Toy/README.md", OLD, "",
                           agent="claude", model="m", launch=agent_writing("No summary here.", plan))
    assert got.new == "" and got.why == "refused: it does not open with `**Summary.**`"
    assert (tmp_path / "w" / "run-2.out").is_file()


def writer_and_reader(opening, plan, answers, exit_code=0):
    """The writer of `agent_writing`, and a reader that answers the k-th reading with `answers[k]` (None:
    it writes nothing) and exits with `exit_code`. The reader is the run whose working directory is a
    `check-<n>` folder; `seen` has its files and, as `argv`, its command."""
    write, seen = agent_writing(opening, plan), []

    def launch(argv, cwd=None, **kwargs):
        if not cwd.name.startswith("check-"):
            return write(argv, cwd=cwd, **kwargs)
        seen.append({p.name: p.read_text(encoding="utf-8") for p in cwd.iterdir() if p.is_file()} | {"argv": argv})
        answer = answers[len(seen) - 1]
        if answer is not None:
            (cwd / overview.CHECK_NAME).write_text(json.dumps(answer), encoding="utf-8")
        return SimpleNamespace(stdin=SimpleNamespace(write=lambda s: None, close=lambda: None), stdout=iter(()),
                               wait=lambda timeout=None: exit_code, pid=0)
    return launch, seen


def test_a_second_agent_reads_the_opening_against_the_rest_before_it_is_proposed(tmp_path):
    ctx, plan = context(tmp_path), {"fold_guide": False, "directions": []}
    args = (ctx, tmp_path / "w", "Toy", "TauCetiRoadmap/Toy/README.md", OLD, "import Mathlib\n")
    good = {"sentences": [{"sentence": "It proves Lagrange's theorem.", "where": "2.1", "supported": True}]}
    bad = {"sentences": [good["sentences"][0], {"sentence": "The order divides the order of every group.",
                                                "where": "2.1", "supported": False, "why": "2.1 is for finite groups"}]}
    # Supported throughout: proposed. The reader had the opening and the README without it, nothing else.
    launch, seen = writer_and_reader(OPENING, plan, [good])
    got = overview.propose(*args, agent="claude", model="m", launch=launch, check=True)
    assert got.new and len(seen) == 1 and seen[0][overview.OUT_NAME].strip() == OPENING.strip()
    assert "**Summary.**" not in seen[0]["README.md"] and "## Layer 2: Lagrange's theorem" in seen[0]["README.md"]
    assert seen[0]["README.md"].startswith("# Roadmap: toy groups\n")
    # A sentence the README does not support goes back to the writer once; unsupported again, nothing is proposed.
    launch, seen = writer_and_reader(OPENING, plan, [bad, good])
    assert overview.propose(*args, agent="claude", model="m", launch=launch, check=True).new and len(seen) == 2
    launch, seen = writer_and_reader(OPENING, plan, [bad, bad])
    got = overview.propose(*args, agent="claude", model="m", launch=launch, check=True)
    assert got.new == "" and got.why == ("refused: the README does not support “The order divides the order of every "
                                         "group.” (2.1 is for finite groups)")
    # A reader that leaves no answer, or lists nothing, or omits `supported`, never passes an opening.
    for answer, why in ((None, "the reader of the opening wrote no usable `check.json`"),
                        ({"sentences": []}, "the reader of the opening listed no sentence in `check.json`"),
                        ({"sentences": [{"sentence": "It proves Lagrange's theorem."}]},
                         "the README does not support “It proves Lagrange's theorem.”")):
        launch, seen = writer_and_reader(OPENING, plan, [answer, answer])
        assert overview.propose(*args, agent="claude", model="m", launch=launch, check=True).why == "refused: " + why
    # A reader that fails after writing its support for one sentence has not read the opening.
    launch, seen = writer_and_reader(OPENING, plan, [good, good], exit_code=1)
    got = overview.propose(*args, agent="claude", model="m", launch=launch, check=True)
    assert got.new == "" and got.why == "refused: the reader of the opening did not finish (exit 1)" and len(seen) == 2
    # A reader of the other family runs on that family's configured model, not on the writer's.
    launch, seen = writer_and_reader(OPENING, plan, [good])
    assert overview.propose(*args, agent="claude", model="m", launch=launch, check=True, check_agent="codex").new
    assert ctx.cfg.agents.codex_model in seen[0]["argv"] and "m" not in seen[0]["argv"]
    launch, seen = writer_and_reader(OPENING, plan, [good])
    assert overview.propose(*args, agent="claude", model="m", launch=launch, check=True, check_agent="claude").new
    assert "m" in seen[0]["argv"]
    # Without `check` no reader runs.
    launch, seen = writer_and_reader(OPENING, plan, [])
    assert overview.propose(*args, agent="claude", model="m", launch=launch).new and seen == []


# --- the intake: a chain of such pushes carries the acceptance ---------------------------------------------

def chain_gh(head_readme, lean_changed=False, comments=None, labels=("ai-found-no-errors",)):
    """#12: H1 accepted with the old README; H2 its reordering; H3 an overview pass on H2 (`head_readme`)."""
    job = review_job(40, 1, 12, H1, "done")
    readmes = {H1: OLD, H2: layout.reorder(OLD), H3: head_readme}
    entries = {(h, FOO): {"README.md": f"r{h[0]}", "Suggested.lean": "lean-2" if (lean_changed and h == H3) else "lean",
                          "metadata.toml": "meta"} for h in readmes}
    head = H3 if head_readme else H2
    gh = ContentGH([node(12, head, labels=labels)], files=verdict_file(job, "accepted", H1),
                   trees={(h, FOO): f"tree-{h[0]}" for h in readmes}, entries=entries,
                   blobs={(h, f"{FOO}/README.md"): t for h, t in readmes.items() if t}, comments=comments)
    return gh, job


def record(head=H3, login="carol", source=H2):
    return {12: [{"id": 9, "user": {"login": login},
                  "body": intake.layout_note(source, head, ["Foo"], kind="overview")}]}


def test_a_chain_of_reordering_then_overview_carries_the_acceptance_with_the_commands_record():
    text, _ = reordered_and_fixed()
    new = overview.build(text, OPENING, True)
    # The reordered head first: carried, as before.
    gh, job = chain_gh(None)
    out, fake = sync(gh, [job])
    assert created(fake) == [] and out[-1].startswith("#12: ai-found-no-errors kept")
    # The overview head, with the record the command posts: carried from the accepted head.
    gh, job = chain_gh(new, comments=record())
    out, fake = sync(gh, [job], pool=["carol"])
    assert created(fake) == [] and gh.writes == []
    assert out == [(f"#12: ai-found-no-errors kept (the roadmap folders at {H3[:7]} are those of {H1[:7]}, up to the opening "
                    "overview and the order of the README sections of Foo, whose acceptance stands: the head only "
                    "moved, no review filed)")]
    # Without the record, or with one by a login outside the pool, the rewritten opening is a change.
    for comments in (None, record(login="mallory"), record(head=H2)):
        gh, job = chain_gh(new, comments=comments)
        out, fake = sync(gh, [job], pool=["carol"])
        assert out[0] == "#12: ai-found-no-errors removed (head moved)" and created(fake) == [("PRREVIEW-Foo~2", "Foo")]
    # A line of Suggested.lean changed in the same push: not carried, whatever the record says.
    gh, job = chain_gh(new, lean_changed=True, comments=record())
    out, fake = sync(gh, [job], pool=["carol"])
    assert out[0] == "#12: ai-found-no-errors removed (head moved)"
    # A changed hypothesis in a layer: not carried.
    gh, job = chain_gh(new.replace("for `G` finite", "for every `G`"), comments=record())
    out, fake = sync(gh, [job], pool=["carol"])
    assert out[0] == "#12: ai-found-no-errors removed (head moved)"


def test_the_overview_note_reads_as_housekeeping_and_names_its_kind():
    note = intake.layout_note(H2, H3, ["Foo"], kind="overview")
    assert note.startswith(f"Housekeeping at `{H3[:7]}`: the opening overview of the README was rewritten in `Foo/`")
    assert "The mathematics of the layers is unchanged" in note and "no further change is needed" in note
    assert intake.layout_record(note) == {"from": H2, "head": H3, "folders": ["Foo"], "kind": "overview"}
    assert intake.layout_record(intake.layout_note(H1, H2, ["Foo"])) == {"from": H1, "head": H2, "folders": ["Foo"]}


# --- the command ------------------------------------------------------------------------------------------

def command(tmp_path, gh, jobs=(), write=False, opening=OPENING, **kw):
    ctx = context(tmp_path, gh, jobs)
    ctx.dry_run = not write
    slept = []
    lines = overview.run(ctx, write=write, report=lambda line: None, sleep=slept.append, agent="claude", model="m",
                         launch=agent_writing(opening, {"fold_guide": False, "directions": []}), **kw)
    return lines, slept, ctx


def test_the_dry_run_writes_the_proposed_readme_and_shows_the_opening_sizes(tmp_path):
    gh = pulls(1)
    gh.readmes[("carol/fork1", "TauCetiRoadmap/Foo1/README.md", "1" * 40)] = OLD
    lines, _slept, ctx = command(tmp_path, gh, numbers=[1])
    proposed = ctx.state / "overviews" / "pr-1" / "Foo1" / "README.proposed.md"
    assert gh.writes == [] and proposed.is_file() and layout.rewrite_problems(OLD, proposed.read_text()) == []
    sizes = f"{overview.words(overview.layout.split_opening(OLD)[1])} -> {overview.words(OPENING)} words"
    assert lines[0].startswith(f"[dry run] #1 (carol) Foo1: opening {sizes} (")
    assert f"proposed README: {proposed}" in lines[0]
    assert command(tmp_path, gh, numbers=[1], listing=True)[0] == [
        "#1 (carol): would write the opening overview of Foo1"]


def test_the_command_pushes_the_checked_readme_with_its_record_and_refuses_approved_queued_or_busy(tmp_path):
    gh = pulls(1, 2, 3, 4)
    for n in (1, 2, 3, 4):
        gh.readmes[(f"carol/fork{n}", f"TauCetiRoadmap/Foo{n}/README.md", f"{n}" * 40)] = OLD
    gh.status = {2: {"reviewDecision": "APPROVED"}, 3: {"isInMergeQueue": True}}
    busy = review_job(40, 1, 4, "4" * 40, "submitted", name="Foo4", folder="Foo4")
    lines, _slept, _ctx = command(tmp_path, gh, [busy], write=True, numbers=[1, 2, 3, 4])
    assert [(m, p) for m, p, _x in gh.writes] == [
        ("POST", "repos/carol/fork1/git/trees"), ("POST", "repos/carol/fork1/git/commits"),
        ("POST", f"repos/{REPO}/issues/1/comments"), ("PATCH", "repos/carol/fork1/git/refs/heads/roadmap/1")]
    note = gh.writes[2][2]["body"]
    assert intake.layout_record(note)["kind"] == "overview" and intake.layout_record(note)["head"] == "9" * 40
    tree = gh.writes[0][2]["tree"][0]
    assert tree["path"] == "TauCetiRoadmap/Foo1/README.md" and layout.rewrite_problems(OLD, tree["content"]) == []
    assert lines[2:5] == ["#2 (carol): refused: a human approval stands (a push would dismiss it): it merges as it is",
                          "#3 (carol): refused: it is in the merge queue (a push would take it out): it merges as it is",
                          "#4 (carol): skipped: PRREVIEW-Foo4~1 is submitted (run again once it is over)"]
    assert lines[0].startswith("#1 (carol) Foo1: opening") and lines[1].startswith("#1 (carol): pushed 9999999")
    # A pull request that carries an overview record of ours is done: not again, unless forced.
    gh = pulls(1)
    gh.readmes[("carol/fork1", "TauCetiRoadmap/Foo1/README.md", "1" * 40)] = OLD
    gh.comments = {1: [{"id": 5, "user": {"login": "carol"}, "body": note}]}
    lines, _slept, _ctx = command(tmp_path, gh, write=True, numbers=[1])
    assert lines[0] == "#1 (carol): done before (an overview record is on the pull request; --force to redo)"
