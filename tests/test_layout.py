"""The README section order (`layout`), its carry across a layout-only push (`intake`), and the command
that applies it to pull requests (`relayout`)."""

from __future__ import annotations

import pytest

from tauceti_roadmap_worker import intake, layout, replies
from tauceti_roadmap_worker.gh import GHError

from .fakes import example_config
from .test_prreview import H1, H2, REPO, ContentGH, created, node, review_job, sync, verdict_file

OLD = """# Roadmap: toy groups

**Summary.** Builds the order of an element of a finite group and proves Lagrange's theorem.

## Scope and ownership

This roadmap owns the order of an element.

## Conventions

Groups are written multiplicatively.

```text
## not a heading: inside a fence
```

## Exact supplier contracts

### From Mathlib

- `Subgroup.index`, the index of a subgroup.

## How to read the build

Layer 1 defines the order; Layer 2 proves Lagrange's theorem.

## Layer 1: the order of an element

### 1.1 Order

Define the order of `g` as the least positive `n` with `g ^ n = 1`.

## Layer 2: Lagrange's theorem

### 2.1 Lagrange

Prove that the order of a subgroup divides the order of the group.

## Downstream consumers

- Sylow theory.

## References

- [Lang] S. Lang, Algebra.
"""


def bodies(text: str) -> list[str]:
    return [p.body for p in layout.parts(text)]


def headings(text: str) -> list[str]:
    return [p.title for p in layout.parts(text)[1:]]


# --- the reordering ------------------------------------------------------------------------------------

def test_an_old_readme_is_reordered_with_scope_and_suppliers_at_the_end_and_the_guide_after_the_summary():
    new = layout.reorder(OLD)
    assert headings(new) == ["how to read the build", "conventions", "layer 1: the order of an element",
                             "layer 2: lagrange's theorem", "downstream consumers", "scope and ownership",
                             "exact supplier contracts", "references"]
    # A permutation of the sections, every byte of each kept, the blank lines where they were.
    assert layout.section_permutation(OLD, new) and sorted(bodies(OLD)) == sorted(bodies(new))
    assert len(new) == len(OLD) and sorted(new) == sorted(OLD)
    assert [p.gap for p in layout.parts(new)] == [p.gap for p in layout.parts(OLD)]
    assert "```text\n## not a heading: inside a fence\n```" in new
    # Idempotent; a README in order comes back byte for byte.
    assert layout.reorder(new) == new and layout.in_order(new) and not layout.in_order(OLD)
    assert layout.moved(OLD) == ["## Scope and ownership", "## Exact supplier contracts", "## How to read the build"]
    assert layout.moved(new) == []


def test_without_references_the_two_sections_go_last_and_a_missing_final_newline_stays_missing():
    text = OLD.split("## References")[0].rstrip("\n")             # ends without a newline
    new = layout.reorder(text)
    assert headings(new)[-2:] == ["scope and ownership", "exact supplier contracts"]
    assert not new.endswith("\n") and len(new) == len(text) and layout.section_permutation(text, new)
    assert layout.reorder(new) == new


def test_only_the_named_headings_move_and_other_shapes_are_left_alone_or_refused():
    # A heading that mixes scope with other matter stays where its author put it.
    mixed = "# Roadmap: x\n\n**Summary.** S.\n\n## Scope and conventions\n\nA.\n\n## Layer 1: y\n\nB.\n"
    assert layout.reorder(mixed) == mixed
    # Case and a trailing colon do not matter; a `## Summary` section keeps the guide after it.
    summary = ("# Roadmap: x\n\n## Summary\n\nS.\n\n## SCOPE AND OWNERSHIP:\n\nA.\n\n## How to read the build\n\nG.\n\n"
               "## Layer 1: y\n\nB.\n")
    assert headings(layout.reorder(summary)) == ["summary", "how to read the build", "layer 1: y", "scope and ownership"]
    # No sections: nothing to do. A level-one heading after the first section: refused.
    assert layout.reorder("# Roadmap: x\n\nOnly prose.\n") == "# Roadmap: x\n\nOnly prose.\n"
    with pytest.raises(layout.LayoutError, match="level-one heading"):
        layout.reorder("# Roadmap: x\n\n## Scope and ownership\n\nA.\n\n# Another title\n\n## Layer 1: y\n")


def test_a_section_permutation_is_exactly_a_reordering():
    new = layout.reorder(OLD)
    assert layout.section_permutation(OLD, OLD) and layout.section_permutation(new, OLD)
    assert not layout.section_permutation(OLD, new.replace("least positive", "smallest positive"))
    assert not layout.section_permutation(OLD, new.replace("**Summary.** Builds", "**Summary.** Constructs"))
    assert not layout.section_permutation(OLD, new.replace("## References", "## Bibliography"))
    assert not layout.section_permutation(OLD, new + "\n## Notes\n\nMore.\n")
    # Blank lines between sections are not text.
    assert layout.section_permutation(OLD, new.replace("\n\n## Conventions", "\n\n\n## Conventions"))


# --- the intake: a layout-only push carries the acceptance ------------------------------------------------

FOO = "TauCetiRoadmap/Foo"


def layout_gh(new_readme: str, lean_changed: bool = False, labels=("ai-approved",), comments=None):
    """Pull request #12 moved from H1, accepted, to H2, whose README is `new_readme` (H1's is OLD)."""
    job = review_job(40, 1, 12, H1, "done")
    entries = {(H1, FOO): {"README.md": "readme-1", "Suggested.lean": "lean-1", "metadata.toml": "meta"},
               (H2, FOO): {"README.md": "readme-2", "Suggested.lean": "lean-2" if lean_changed else "lean-1",
                           "metadata.toml": "meta"}}
    gh = ContentGH([node(12, H2, labels=labels)], files=verdict_file(job, "accepted", H1),
                   trees={(H1, FOO): "tree-1", (H2, FOO): "tree-2"}, entries=entries,
                   blobs={(H1, f"{FOO}/README.md"): OLD, (H2, f"{FOO}/README.md"): new_readme}, comments=comments)
    return gh, job


def test_a_push_that_only_reorders_the_readme_sections_keeps_the_label_and_files_no_review():
    gh, job = layout_gh(layout.reorder(OLD))
    out, fake = sync(gh, [job])
    assert created(fake) == [] and ("DELETE", f"repos/{REPO}/issues/12/labels/ai-approved", {}) not in gh.writes
    assert out == [f"#12: layout note posted ({H2[:7]} only reorders the README sections of {H1[:7]})",
                   (f"#12: ai-approved kept (the roadmap folders at {H2[:7]} are those of {H1[:7]}, up to the order of "
                    "the README sections of Foo, whose acceptance stands: the head only moved, no review filed)")]
    [(method, path, payload)] = gh.writes
    assert (method, path) == ("POST", f"repos/{REPO}/issues/12/comments")
    note = payload["body"]
    assert note.startswith(f"Housekeeping at `{H2[:7]}`: only the order of the README's sections changed in `Foo/`")
    assert intake.layout_record(note) == {"from": H1, "head": H2, "folders": ["Foo"]}
    # The note is never read as a review, whoever posts it.
    for user in ("carol", "someone-else", "github-actions[bot]"):
        event = replies.Event("2026-11-18T10:00:00Z", user, note)
        assert not replies.is_review(event, {"carol"}, example_config().replies.review_markers,
                                     housekeeping=example_config().replies.housekeeping_prefixes,
                                     ignore={"github-actions[bot]"})
    # A note already on the pull request (the command posted it) is not posted again.
    gh, job = layout_gh(layout.reorder(OLD), comments={12: [{"id": 7, "body": note, "user": {"login": "carol"}}]})
    out, fake = sync(gh, [job])
    assert gh.writes == [] and created(fake) == [] and out[0].startswith("#12: ai-approved kept")
    # An unlabelled pull request gets its label back on the same ground.
    gh, job = layout_gh(layout.reorder(OLD), labels=())
    out, fake = sync(gh, [job])
    assert ("POST", f"repos/{REPO}/issues/12/labels", {"labels": ["ai-approved"]}) in gh.writes and created(fake) == []


@pytest.mark.parametrize("edit", ["word", "lean", "section"])
def test_a_reordering_with_any_other_edit_in_the_same_push_is_not_carried(edit):
    readme = layout.reorder(OLD)
    if edit == "word":
        readme = readme.replace("least positive", "smallest positive")
    elif edit == "section":
        readme += "\n## Notes\n\nA new section.\n"
    gh, job = layout_gh(readme, lean_changed=edit == "lean")
    out, fake = sync(gh, [job])
    assert out[0] == "#12: ai-approved removed (head moved)" and created(fake) == [("PRREVIEW-Foo~2", "Foo")]
    assert not any("/comments" in path for _m, path, _p in gh.writes)


def test_a_reordering_of_a_head_whose_review_asked_for_changes_carries_nothing():
    job = review_job(40, 1, 12, H1, "done")
    gh, _job = layout_gh(layout.reorder(OLD), labels=())
    gh.files = verdict_file(job, "needs_changes", H1)
    out, fake = sync(gh, [job])
    assert created(fake) == [("PRREVIEW-Foo~2", "Foo")] and gh.writes == [] and gh.layout_queries == 0
    # Switched off: the ordinary rule applies, and nothing is read for it.
    gh, job = layout_gh(layout.reorder(OLD))
    out, fake = sync(gh, [job], carry_layout=False)
    assert out[0] == "#12: ai-approved removed (head moved)" and gh.layout_queries == 0


def test_files_github_does_not_give_leave_the_pull_request_as_it_is():
    gh, job = layout_gh(layout.reorder(OLD))
    gh.blobs = "unread"
    verdicts = gh.raw_file

    def missing(repo, path, ref=""):         # (the verdicts on the board are read as ever)
        if path.endswith("README.md"):
            raise GHError(f"{path}: 502")
        return verdicts(repo, path, ref)
    gh.raw_file = missing
    out, fake = sync(gh, [job])
    assert gh.writes == [] and created(fake) == []
    assert out == [(f"#12: ai-approved not checked (the roadmap folders at {H2[:7]} could not be compared with those "
                    "of an earlier head)")]


def test_a_folder_settled_at_the_head_before_a_reordering_is_not_reviewed_again():
    """`carry_unchanged_folders`: of a two-folder pull request, the folder that was accepted and only had
    its README reordered keeps its acceptance; the folder whose review asked for changes is reviewed."""
    foo = review_job(41, 1, 1164, H1, "done", folder="Foo")
    bar = review_job(42, 1, 1164, H1, "done", name="Bar", folder="Bar")
    bar_path = "TauCetiRoadmap/Bar"
    entries = {(H1, FOO): {"README.md": "r1", "Suggested.lean": "l"}, (H2, FOO): {"README.md": "r2", "Suggested.lean": "l"}}
    gh = ContentGH([node(1164, H2, paths=(f"{FOO}/README.md", f"{bar_path}/README.md"))],
                   files={**verdict_file(foo, "accepted", H1), **verdict_file(bar, "needs_changes", H1)},
                   trees={(H1, FOO): "f1", (H2, FOO): "f2", (H1, bar_path): "b1", (H2, bar_path): "b2"},
                   entries=entries, blobs={(H1, f"{FOO}/README.md"): OLD, (H2, f"{FOO}/README.md"): layout.reorder(OLD)})
    _out, fake = sync(gh, [foo, bar])
    assert created(fake) == [("PRREVIEW-Bar~2", "Bar")] and gh.writes == []
    # Foo with a word changed as well: both are reviewed.
    gh.blobs[(H2, f"{FOO}/README.md")] = layout.reorder(OLD).replace("least positive", "smallest positive")
    _out, fake = sync(gh, [foo, bar])
    assert sorted(created(fake)) == [("PRREVIEW-Bar~2", "Bar"), ("PRREVIEW-Foo~2", "Foo")]
