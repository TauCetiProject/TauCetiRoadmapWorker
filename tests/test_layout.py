"""The README section order (`layout`), its carry across a layout-only push (`intake`), and the command
that applies it to pull requests (`relayout`)."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import cli, intake, layout, relayout, replies
from tauceti_roadmap_worker.gh import GHError, GHRateLimited

from .fakes import FakeBoard, example_config
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


def test_the_command_reorders_a_local_readme(tmp_path, capsys):
    path = tmp_path / "README.md"
    path.write_text(OLD, encoding="utf-8")
    assert cli.main(["layout", str(path)]) == 0 and path.read_text(encoding="utf-8") == OLD
    assert "would reorder (## Scope and ownership, ## Exact supplier contracts, ## How to read the build)" in \
        capsys.readouterr().out
    assert cli.main(["layout", str(path), "--write"]) == 0 and path.read_text(encoding="utf-8") == layout.reorder(OLD)
    assert cli.main(["layout", str(path), "--write"]) == 0 and "already in order" in capsys.readouterr().out.split("\n")[-2]


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


# --- the command on pull requests ------------------------------------------------------------------------

H9 = "9" * 40


class PullsGH(ContentGH):
    """ContentGH with what the command reads and writes: the pull requests' states (GraphQL), the
    READMEs at their heads, the head commit's tree, and the git data writes."""

    def __init__(self, nodes, readmes, status=None, **kw):
        super().__init__(nodes, **kw)
        self.readmes, self.status, self.moved = dict(readmes), dict(status or {}), {}

    def graphql(self, query, **variables):
        if "isInMergeQueue" in query:
            asked = [int(n) for n in re.findall(r"p(\d+): pullRequest", query)]
            return {"repository": {f"p{n}": {"number": n, "state": "OPEN", "headRefOid": self.head_of(n),
                                             "headRefName": f"roadmap/{n}",
                                             "headRepository": {"nameWithOwner": f"carol/fork{n}"},
                                             "isInMergeQueue": False, "reviewDecision": None,
                                             "latestReviews": {"nodes": [{"state": "COMMENTED"}]},
                                             **self.status.get(n, {})} for n in asked}}
        return super().graphql(query, **variables)

    def head_of(self, n):
        return next(x["headRefOid"] for x in self.nodes if x["number"] == n)

    def api(self, path, paginate=False):
        if m := re.fullmatch(r"repos/[^/]+/[^/]+/git/commits/([0-9a-f]+)", path):
            return {"sha": m.group(1), "tree": {"sha": "tree-" + m.group(1)[:4]}}
        if m := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path):
            n = int(m.group(1))
            return {"head": {"sha": self.moved.get(n, self.head_of(n))}}
        return super().api(path, paginate)

    def raw_file(self, repo, path, ref=""):
        if (repo, path, ref) in self.readmes:
            return self.readmes[(repo, path, ref)]
        raise GHError(f"{path}: 404")

    def write(self, method, path, payload=None):
        super().write(method, path, payload)
        if path.endswith("/git/trees"):
            return {"sha": "new-tree"}
        if path.endswith("/git/commits"):
            return {"sha": H9}
        return {}


def command(gh, jobs=(), write=False, **kw):
    cfg = example_config()
    cfg.roadmap.pool = ["carol"]
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=FakeBoard(jobs=list(jobs)), operator="carol", dry_run=not write)
    slept = []
    lines = relayout.run(ctx, write=write, report=lambda line: None, sleep=slept.append, **kw)
    return lines, slept


def pulls(*numbers, added=True, **status):
    nodes = [node(n, f"{n}" * 40 if n < 10 else H1, paths=((f"TauCetiRoadmap/Foo{n}/README.md", "ADDED" if added else
                                                            "MODIFIED"),)) for n in numbers]
    readmes = {(f"carol/fork{n}", f"TauCetiRoadmap/Foo{n}/README.md", x["headRefOid"]): OLD for n, x in zip(numbers, nodes)}
    return PullsGH(nodes, readmes, status=status)


def test_the_dry_run_says_what_each_pull_request_would_get_and_writes_nothing():
    gh = pulls(1, 2, 3)
    gh.readmes[("carol/fork3", "TauCetiRoadmap/Foo3/README.md", "3" * 40)] = layout.reorder(OLD)
    lines, slept = command(gh, numbers=[2, 1, 3])
    assert gh.writes == [] and slept == []
    assert lines[0].startswith("[dry run] #2 (carol): reorders TauCetiRoadmap/Foo2/README.md (moves ## Scope and "
                               "ownership, ## Exact supplier contracts, ## How to read the build; ")
    assert lines[0].endswith("[not accepted at this head: the next review reads the new head]")
    assert lines[1].startswith("[dry run] #1 (carol): reorders") and lines[2] == "#3 (carol): nothing to do: Foo3: already in order"


def test_the_command_pushes_one_commit_of_the_readmes_alone_and_posts_the_note():
    gh = pulls(1, 2)
    lines, slept = command(gh, numbers=[1, 2], write=True, pace=2.5)
    assert slept == [2.5]                       # one pause, between the two pushes
    writes = [(m, p) for m, p, _x in gh.writes]
    # The note goes up just before the branch moves, so that the intake finds its record with the new head.
    assert writes == [("POST", "repos/carol/fork1/git/trees"), ("POST", "repos/carol/fork1/git/commits"),
                      ("POST", f"repos/{REPO}/issues/1/comments"), ("PATCH", "repos/carol/fork1/git/refs/heads/roadmap/1"),
                      ("POST", "repos/carol/fork2/git/trees"), ("POST", "repos/carol/fork2/git/commits"),
                      ("POST", f"repos/{REPO}/issues/2/comments"), ("PATCH", "repos/carol/fork2/git/refs/heads/roadmap/2")]
    tree, commit, note, ref = (x for _m, _p, x in gh.writes[:4])
    assert tree == {"base_tree": "tree-1111", "tree": [{"path": "TauCetiRoadmap/Foo1/README.md", "mode": "100644",
                                                        "type": "blob", "content": layout.reorder(OLD)}]}
    assert commit["parents"] == ["1" * 40] and commit["tree"] == "new-tree" and "Layout only" in commit["message"]
    assert ref == {"sha": H9, "force": False}
    assert intake.layout_record(note["body"]) == {"from": "1" * 40, "head": H9, "folders": ["Foo1"]}
    assert lines == [f"#1: pushed {H9[:7]} on carol/fork1:roadmap/1 (Foo1); note posted",
                     f"#2: pushed {H9[:7]} on carol/fork2:roadmap/2 (Foo2); note posted", "2 pull requests pushed"]


def test_approved_queued_and_busy_pull_requests_are_never_pushed_to():
    gh = pulls(1, 2, 3, 4, 5)
    gh.status = {1: {"reviewDecision": "APPROVED"}, 2: {"latestReviews": {"nodes": [{"state": "APPROVED"}]}},
                 3: {"isInMergeQueue": True}, 5: {"state": "MERGED"}}
    busy = review_job(40, 1, 4, "4" * 40, "claimed", name="Foo4", folder="Foo4")
    lines, _slept = command(gh, [busy], numbers=[1, 2, 3, 4, 5], write=True)
    assert gh.writes == []
    assert lines == ["#1 (carol): refused: a human approval stands (a push would dismiss it): it merges as it is",
                     "#2 (carol): refused: a human approval stands (a push would dismiss it): it merges as it is",
                     "#3 (carol): refused: it is in the merge queue (a push would take it out): it merges as it is",
                     "#4 (carol): skipped: PRREVIEW-Foo4~1 is claimed (run again once it is over)",
                     "#5 (carol): refused: it is merged", "0 pull requests pushed"]


def test_a_branch_that_moved_is_not_pushed_and_a_rate_limit_stops_the_run():
    gh = pulls(1, 2)
    gh.moved = {1: H2}
    lines, _slept = command(gh, numbers=[1, 2], write=True)
    assert lines[0] == f"#1: not pushed: the pull request moved to {H2[:7]} before the push; nothing pushed"
    assert not any(p.endswith(("refs/heads/roadmap/1", "issues/1/comments")) for _m, p, _x in gh.writes)
    assert lines[1].startswith("#2: pushed")

    # A branch update GitHub refuses (the branch moved after the last read) takes the note down again.
    class Refusing(PullsGH):
        def write(self, method, path, payload=None):
            if method == "PATCH":
                raise GHError("HTTP 422: Update is not a fast forward")
            out = super().write(method, path, payload)
            return {"id": 77} if path.endswith("/comments") else out
    gh = Refusing(pulls(1).nodes, pulls(1).readmes)
    lines, _slept = command(gh, numbers=[1], write=True)
    assert lines[0] == "#1: not pushed: HTTP 422: Update is not a fast forward"
    assert [(m, p) for m, p, _x in gh.writes][-2:] == [("POST", f"repos/{REPO}/issues/1/comments"),
                                                       ("DELETE", f"repos/{REPO}/issues/comments/77")]

    class Limited(PullsGH):
        def write(self, method, path, payload=None):
            raise GHRateLimited("API rate limit exceeded", 0)
    gh = Limited(pulls(1, 2).nodes, pulls(1, 2).readmes)
    lines, _slept = command(gh, numbers=[1, 2], write=True)
    assert lines == ["stopped at #1: API rate limit exceeded; 0 pushed, run again later", "0 pull requests pushed"]


def test_the_pool_and_authors_select_their_pull_requests_and_a_readme_left_alone_is_not_touched():
    gh = pulls(1, 2, added=False)
    gh.nodes.append(node(3, "3" * 40, author="dave", paths=("TauCetiRoadmap/Foo3/Suggested.lean",)))
    gh.nodes.append(node(4, "4" * 40, author="erin", paths=("TauCetiRoadmap/Foo4/README.md",)))
    gh.readmes[("carol/fork4", "TauCetiRoadmap/Foo4/README.md", "4" * 40)] = OLD
    gh.tree = ["TauCetiRoadmap/Foo3/README.md"]
    gh.pulls = {n: {"title": "", "body": ""} for n in (1, 2, 3, 4)}
    lines, _slept = command(gh, pool=True)
    assert [line.split(" ")[0 if not line.startswith("[") else 2] for line in lines] == ["#1", "#2"]
    lines, _slept = command(gh, authors=["dave", "erin"])
    assert lines[0] == "#3 (dave): nothing to do: Foo3: its README is as on main (not changed by this pull request): left alone"
    assert lines[1].startswith("[dry run] #4 (erin): reorders")
    lines, _slept = command(gh, authors=["erin"], accepted_only=True)
    assert lines == ["no pull request selected"]


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
