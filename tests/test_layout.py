"""The README section order (`layout`), its carry across a layout-only push (`intake`), and the command
that applies it to pull requests (`relayout`)."""

from __future__ import annotations

import pytest

from tauceti_roadmap_worker import layout

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
