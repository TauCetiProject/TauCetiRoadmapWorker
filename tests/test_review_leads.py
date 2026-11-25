"""Two leads a review round is handed (`needs`, `librarymain`): the sections a target's text cites and its
`*Needs:*` line does not list, and what the library's main branch holds past the roadmap's pin."""

from __future__ import annotations

import json

from tauceti_roadmap_worker import librarymain, needs

from .test_overlaps import lane_workspace, world  # noqa: F401 - `world` is a fixture

README = """# Roadmap: toy maps

**Summary.** Toy maps, as in §9.9 of nothing.

## Layer 1: linear maps

### 1.1 Stretch

Define the stretch.

*Needs:* Mathlib `LinearMap.det`.

### 1.2 Distortion

Define the distortion from the stretch of §1.1. It is multiplicative under the compositions of
§2.2, and Other §1.1 does not enter.

**Checks.**

- The identity has distortion 1, by §1.1; a Cantor-type map fails, by §2.1.

*Needs:* §1.1; OtherRoadmap §4.2, §4.3, §5.1–§5.3 (gap `x`); PDE §1.1.

### Dependencies

Layer 1 needs nothing. See §1.2.

## Layer 2: maps

### 2.1 The class

Define the class, using §1.1–§1.2 and `Other.lean` §3.1.

*Needs:* §1.2, §2.2.

### 2.2 Composition

Prove that a composition is in the class (§2.1, §1.2), as in §7.3.
"""


def test_the_sections_a_target_cites_are_compared_with_its_needs_line():
    found = {t.id: t for t in needs.targets(README)}
    assert list(found) == ["1.1", "1.2", "2.1", "2.2"]                 # numbered subsections only
    # another roadmap's sections are not this README's, although the numbers exist here too
    assert (found["1.2"].unlisted, found["1.2"].check_only, found["1.2"].later_listed) == (["2.2"], ["2.1"], [])
    # a range is filled in; a listed later section is reported; a backticked or foreign name hides its reference
    assert (found["2.1"].unlisted, found["2.1"].later_listed) == (["1.1"], ["2.2"])
    assert (found["2.2"].has_needs, found["2.2"].unlisted, found["2.2"].unknown) == (False, ["1.2", "2.1"], ["7.3"])
    assert found["1.1"].unlisted == found["1.1"].check_only == []
    text, count = needs.report(README, "TauCetiRoadmap/Toy/README.md")
    assert count == 3 and text.splitlines()[4:] == [
        "- **§1.2** (1.2 Distortion): cited in its text, later in the README, not in its `*Needs:*` line: §2.2; "
        "cited in a Check only: §2.1 (later in the README).",
        "- **§2.1** (2.1 The class): cited in its text, not in its `*Needs:*` line: §1.1; its `*Needs:*` line lists "
        "a later section: §2.2.",
        "- **§2.2** (2.2 Composition): no `*Needs:*` line; its text cites §1.2, §2.1; cites a section this README "
        "does not have: §7.3."]
    assert needs.report("# Roadmap: x\n\n## Layer 1\n\nProse.\n")[1] == 0
    clean = (README.replace("§2.2, and", "it, and").replace("by §2.1.", "clearly.").replace("§1.1–§1.2", "§1.2")
             .replace("§1.2, §2.2.", "§1.2.").replace("(§2.1, §1.2), as in §7.3", ""))
    text, count = needs.report(clean)
    assert count == 0 and text.endswith("\n\nNothing found in 4 numbered subsections.\n")


def test_an_inline_needs_annotation_and_a_plural_supplier_citation_are_read_as_such():
    head = "# Roadmap: x\n\n## Layer 1\n\n### 1.1 A\n\nDefine A.\n\n### 1.2 B\n\n"
    # the annotation after the statement, in its paragraph: what it lists is listed, not cited text
    assert needs.report(head + "Use §1.1. *Needs:* §1.1.\n")[1] == 0
    found = needs.targets(head + "Use §1.1 for B.\n*Needs:* Mathlib.\n")[1]
    assert (found.has_needs, found.unlisted) == (True, ["1.1"])
    # prose that names the annotation in backticks is not one
    assert needs.targets(head + "Use §1.1; the `*Needs:*` line of §1.1 is empty.\n")[1].has_needs is False
    # `Other §§1.1–1.2` is the supplier's range: it neither lists this README's §1.1 nor points forward
    found = needs.targets(head + "Use §1.1.\n\n*Needs:* OtherRoadmap §§1.1–1.2.\n")[1]
    assert (found.unlisted, found.later_listed) == (["1.1"], [])
    later = head + "Define B.\n\n*Needs:* §1.1; ShimuraVarieties §§4.1–4.3, 4.5 and §4.2a.\n\n## Layer 4\n\n" + "".join(
        f"### 4.{k} T\n\nProse.\n\n" for k in (1, 2, 3, 5))
    found = needs.targets(later)[1]
    assert (found.later_listed, found.unknown) == ([], [])
    assert needs.targets(later.replace("ShimuraVarieties ", ""))[1].later_listed == ["4.1", "4.2", "4.3", "4.5"]


def test_a_local_list_with_one_section_sign_is_read_whole():
    head = "# Roadmap: x\n\n## Layer 1\n\n" + "".join(f"### 1.{k} T\n\nDefine it.\n\n" for k in (1, 2, 3, 4))
    body = "### 1.5 U\n\nUse §1.1, §1.2 and §1.4.\n\n*Needs:* §§1.1–1.2, 1.4.\n"
    assert needs.report(head + body)[1] == 0
    assert needs.report(head + body.replace("§§1.1–1.2, 1.4", "§§1.1, 1.2 and 1.4"))[1] == 0
    # with a single sign a bare number is a number: "§1.1 and 1.4 times" lists §1.1 alone
    found = needs.targets(head + body.replace("§§1.1–1.2, 1.4.", "§1.1–1.2 and 1.4 times."))[4]
    assert found.unlisted == ["1.4"]
    # a section the annotation alone names, and this README has not
    found = needs.targets(head + "### 1.5 U\n\nDefine U.\n\n*Needs:* §9.9; §§1.1, 8.8.\n")[4]
    assert (found.unlisted, found.unknown) == ([], ["8.8", "9.9"])


PIN, HEAD = "a" * 40, "b" * 40
AT_PIN = ["TauCeti/Analysis/Sobolev/Basic.lean", "TauCeti/Analysis/Complex/Basic.lean", "README.md"]
AT_MAIN = AT_PIN + ["TauCeti/Analysis/Complex/Beltrami/Linear.lean", "TauCeti/Analysis/Complex/Beltrami/Basic.lean",
                    "TauCeti/Analysis/Sobolev/Trace.lean", "TauCeti/Algebra/Group/Maps.lean", "docs/Beltrami.md"]


class TreeGH:
    def __init__(self, head=HEAD):
        self.head, self.calls = head, []

    def api(self, path, jq=None):
        self.calls.append(path)
        if jq:
            return self.head + "\n"
        sha = path.split("/trees/")[1].split("?")[0]
        return {"tree": [{"type": "blob", "path": p} for p in (AT_PIN if sha == PIN else AT_MAIN)], "truncated": False}


def test_the_library_past_the_pin_is_listed_by_the_proper_names_of_the_readme(tmp_path):
    text = "The planar case is the Beltrami equation; the class is a Sobolev class. Maps are continuous.\n"
    gh = TreeGH()
    report, count = librarymain.report(gh, "o/Lib", PIN, "main", text, tmp_path / "trees")
    # the new directory under a name of the README first; an ordinary word (`Maps`) is no name; only Lean files
    assert count == 2 and report.splitlines()[4:] == [
        "- `TauCeti/Analysis/Complex/Beltrami/` (2 new files; beltrami)",
        "- `TauCeti/Analysis/Sobolev/` (1 new file; sobolev)"]
    assert "4 Lean files are on `main` and not in the pinned tree" in report and f"`{HEAD[:10]}`" in report
    # the trees are kept by commit: a second round reads the branch head only
    gh.calls.clear()
    assert librarymain.report(gh, "o/Lib", PIN, "main", text, tmp_path / "trees")[1] == 2
    assert gh.calls == ["repos/o/Lib/commits/main"]
    assert librarymain.report(TreeGH(PIN), "o/Lib", PIN, "main", text)[1] == 0          # the pin is the head
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": "m"},
                                                                         {"name": "Lib", "rev": PIN}]}))
    assert librarymain.pinned_rev(tmp_path, "lib") == PIN and librarymain.pinned_rev(tmp_path / "none", "Lib") == ""


def test_a_review_round_is_handed_both_leads(world, tmp_path):  # noqa: F811
    cfg, _index = world
    lane, job, ws, ctx = lane_workspace(cfg, tmp_path, "REV-PKG-Mine~2", README)
    cfg.repositories.library = "o/Lib"
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "Lib", "rev": PIN}]}))
    ctx.dry_run, ctx.gh = False, TreeGH()
    lane.prepare_library_main(ctx, ws, ws.board / "packages" / job.name / "README.md", ws.scratch / "none.lean")
    lane.prepare_needs(ws, ws.board / "packages" / job.name / "README.md")
    assert ws.extra["needs"][1] == 3 and ws.extra["library_main"][1] == 0     # the toy README names nobody
    (ws.board / "packages" / job.name / "README.md").write_text(README + "\nThe Beltrami equation.\n")
    lane.prepare_library_main(ctx, ws, ws.board / "packages" / job.name / "README.md", ws.scratch / "none.lean")
    ws.extra["open_index"] = {"prs": [], "at": "now"}
    text = lane.tools_text(ctx, ws)
    assert "- **The library past the pin**: `" in text and "lists 1 directories" in text
    assert "- **`*Needs:*` lines**: `" in text and "for 3 subsections" in text
    # the pin is the reviewed tree's: a pull request that pins behind the content repository's main is
    # compared from its own pin, although the content checkout is at the library's head already
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "Lib", "rev": HEAD}]}))
    lane.prepare_library_main(ctx, ws, ws.board / "packages" / job.name / "README.md", ws.scratch / "none.lean")
    assert ws.extra["library_main"][1] == 0
    board, ws.workdir = ws.workdir, tmp_path / "pr" / "12"
    ws.workdir.mkdir(parents=True)
    (ws.workdir / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "Lib", "rev": PIN}]}))
    lane.prepare_library_main(ctx, ws, ws.board / "packages" / job.name / "README.md", ws.scratch / "none.lean")
    assert ws.extra["library_main"][1] == 1
    ws.workdir = board
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "Lib", "rev": PIN}]}))
    # a manifest of the wrong shape is no pin, and never fails the round
    for bad in ("[]", '{"packages": [null]}', '{"packages": {"Lib": 1}}', "{"):
        (ws.workdir / "lake-manifest.json").write_text(bad)
        (tmp_path / "lake-manifest.json").write_text(bad)
        ws.extra.pop("library_main", None)
        lane.prepare_library_main(ctx, ws, ws.board / "packages" / job.name / "README.md", ws.scratch / "none.lean")
        assert "library_main" not in ws.extra
    (ws.workdir / "lake-manifest.json").unlink()
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "Lib", "rev": PIN}]}))
    lane.prepare_library_main(ctx, ws, ws.board / "packages" / job.name / "README.md", ws.scratch / "none.lean")
    cfg.prreview.library_main = False
    ws.extra.pop("library_main")
    lane.prepare_library_main(ctx, ws, ws.board / "packages" / job.name / "README.md", ws.scratch / "none.lean")
    assert "library_main" not in ws.extra
