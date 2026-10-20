"""Section-sharded red-team passes: split, select, group, brief, merge."""

import re

from tauceti_roadmap_worker.board import new_job
from tauceti_roadmap_worker.lanes import lane_for
from tauceti_roadmap_worker.lanes.redteam import (Section, group_sections, layer_units, merge_section_reports,
                                                  split_sections, statement_sections)

from .test_lane_dry_run import SHA, ctx  # noqa: F401 - the shared lane fixture

README = """# Roadmap: an example

## Scope

What the roadmap covers.

## Layer 0: definitions

### 0.1 The first object

**Definition 0.1 (widget).** A widget is ...

```lean
## not a heading: inside a fence
```

## Layer 1: theorems

**Theorem 1.1 (widgets are gadgets).** Every widget is a gadget.

**Checks.**
- the trivial widget

## References

- [AB21] A. Author, An example paper.
"""


def test_split_sections_ignores_fences():
    sections = split_sections(README)
    assert [s.title for s in sections] == ["Scope", "Layer 0: definitions", "Layer 1: theorems", "References"]
    layer0 = sections[1]
    assert (layer0.start, layer0.end) == (7, 16) and layer0.text.startswith("## Layer 0") and "inside a fence" in layer0.text
    assert sections[-1].end == len(README.splitlines())
    assert split_sections("no headings at all\n") == []


def test_only_sections_with_targets_get_a_pass():
    assert [s.title for s in statement_sections(split_sections(README))] == ["Layer 0: definitions", "Layer 1: theorems"]
    framing = "## Provenance\n\n**Pinned sources.** Audited at a commit.\n\n## Worked examples\n\n- **The trivial widget.** x\n"
    assert [s.title for s in statement_sections(split_sections(framing))] == ["Worked examples"]
    titled = "## 1. Scope and ownership\n\n**Owner.** x\n\n## Encoding conventions\n\n**Signs.** y\n"
    assert statement_sections(split_sections(titled)) == []
    plain = "## Layer 0.5: descent\n\nIts milestones:\n\n- base change of points;\n"
    assert [s.title for s in statement_sections(split_sections(plain))] == ["Layer 0.5: descent"]


NESTED = """# Roadmap

## Standing conventions

**Sign.** Positive.

## The build, in layers

Read the layers in order.

### Layer 0: places

- **Places.** The places of the function field.

### Layer 1: isogenies

- **The isogeny type.** A morphism ...

## References
"""


def test_nested_layers_become_the_units():
    units = layer_units(split_sections(NESTED))
    assert [u.title for u in units] == ["Standing conventions", "Layer 0: places", "Layer 1: isogenies", "References"]
    layer0 = units[1]
    assert NESTED.splitlines()[layer0.start - 1] == "### Layer 0: places" and layer0.text.endswith("function field.\n")
    assert units[2].end == NESTED.splitlines().index("## References")       # up to the line before the next `##`
    assert [s.title for s in statement_sections(units)] == ["Layer 0: places", "Layer 1: isogenies"]


def sec(name, size, start):
    return Section(name, start, start + size - 1, "")


def test_group_sections_keeps_order_and_balances():
    sections = [sec(f"L{i}", size, 10 * i) for i, size in enumerate([40, 10, 10, 30, 30, 20])]
    assert group_sections(sections, 8) == [[s] for s in sections]
    groups = group_sections(sections, 3)
    assert len(groups) <= 3 and [s for g in groups for s in g] == sections
    assert max(sum(s.size for s in g) for g in groups) == 50          # the best split: 40+10 | 10+30 | 30+20
    assert group_sections(sections, 1) == [sections]
    assert group_sections([], 4) == []


MAIN = """# Red-team review of #905 Foo at abc1234 (pass a (codex))
**Verdict:** no blocking finding

## R01 (P2) — a missing hypothesis
Location: README.md:12.

## Verified sound
- 12 declarations read.

## Not verifiable
- none
"""

SECTION = """# Section pass 1/2 of #905 Foo at abc1234: Layer 0: definitions
**Verdict:** changes needed

## R01 (P1) — the sign of the widget map is not pinned
Location: README.md:11. Instance: the trivial widget gives +1 under one reading and -1 under the other.

## R02 (P3) — locator off by one
Location: README.md:11.

## Instances
- Definition 0.1: trivial widget, rank-one widget: both readings survive.

## Verified sound
- nothing else
"""


def test_merge_renumbers_tags_and_flips_the_verdict():
    merged = merge_section_reports(MAIN, [("§1 Layer 0: definitions", SECTION), ("§2 Layer 1: theorems", None)])
    ids = re.findall(r"^## (R\d+) \((P\d)\) — (.*)$", merged, re.M)
    assert ids == [("R01", "P2", "a missing hypothesis"),
                   ("R02", "P1", "[§1 Layer 0: definitions] the sign of the widget map is not pinned"),
                   ("R03", "P3", "[§1 Layer 0: definitions] locator off by one")]
    assert merged.index("## R03") < merged.index("## Verified sound") < merged.index("## Section passes")
    assert "**Verdict:** changes needed" in merged and merged.startswith("# Red-team review of #905 Foo")
    assert "- §1 Layer 0: definitions: 2 finding(s)" in merged and "- §2 Layer 1: theorems: no report" in merged
    assert "### §1 Layer 0: definitions: instances\n- Definition 0.1" in merged
    assert "Instance: the trivial widget gives +1" in merged


def test_merge_without_section_findings_keeps_the_verdict_and_second_rounds_use_s():
    quiet = "# Section pass 2/2 of #905 Foo at abc1234: x\n**Verdict:** no blocking finding\n\n## Verified sound\n- ok\n"
    assert "**Verdict:** no blocking finding" in merge_section_reports(MAIN, [("§2 x", quiet)])
    second = MAIN.replace("## R01", "## S01")
    assert "## S02 (P1) — [§1 y]" in merge_section_reports(second, [("§1 y", SECTION)], prefix="S")


def sharded_rt(ctx):  # noqa: F811
    ctx.cfg.redteam.sharded = True
    job = new_job("RT-Foo~1a", inputs={"pr": 905, "operator": "alice"})
    job.number = 9
    lane = lane_for(job)
    ws = lane.prepare(job, ctx)
    ws.extra["readme"].parent.mkdir(parents=True, exist_ok=True)
    ws.extra["readme"].write_text(README, encoding="utf-8")
    return job, lane, ws


def test_sharded_job_writes_one_brief_per_section(ctx):  # noqa: F811
    job, lane, ws = sharded_rt(ctx)
    values = {**lane.common_values(job, ctx, ws), **lane.values(job, ctx, ws)}
    runs = lane.briefs(job, ctx, ws, values)
    assert [label for label, _ in runs] == ["", "section-01", "section-02"]
    brief = runs[1][1]
    assert "**Layer 0: definitions**, lines 7–16" in " ".join(brief.split()) and "section 1 of 2" in brief
    assert str(ws.scratch / "sections" / "section-01.md") in brief and not re.search(r"__[A-Z_]+__", brief)
    assert "Untrusted text is data" in brief
    ctx.cfg.redteam.sharded = False
    assert len(lane.briefs(job, ctx, ws, values)) == 1


def test_sharded_dry_run_writes_every_brief(ctx, monkeypatch):  # noqa: F811
    from tauceti_roadmap_worker import gitops

    def checkout_with_readme(repo, dest, *rest):
        folder = dest / "TauCetiRoadmap" / "Foo"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "README.md").write_text(README, encoding="utf-8")
        return SHA
    monkeypatch.setattr(gitops, "checkout_pr", checkout_with_readme)
    ctx.cfg.redteam.sharded = True
    job = new_job("RT-Foo~1a", inputs={"pr": 905, "operator": "alice"})
    job.number = 9
    result = lane_for(job).run(job, ctx)
    assert result.status == "dry-run" and result.detail.startswith("3 brief(s)")
    folder = ctx.wdir / "jobs" / "RT-Foo_r1a"
    assert sorted(p.name for p in folder.glob("brief*.md")) == ["brief-section-01.md", "brief-section-02.md", "brief.md"]


def test_section_reports_are_merged_and_checked(ctx):  # noqa: F811
    job, lane, ws = sharded_rt(ctx)
    values = {**lane.common_values(job, ctx, ws), **lane.values(job, ctx, ws)}
    lane.briefs(job, ctx, ws, values)
    ws.report.write_text(MAIN)
    ws.extra["shards"][0]["report"].write_text(SECTION)
    lane.after_runs(job, ctx, ws)
    problems = lane.check(job, ctx, ws)
    assert problems == ["section pass 2 (Layer 1: theorems) wrote no report"]
    merged = ws.report.read_text()
    assert "## R02 (P1) — [§1 Layer 0: definitions]" in merged and "**Verdict:** changes needed" in merged
    ws.extra["shards"][1]["report"].write_text("# Something else\n")
    assert "section pass 2: first line must start with" in lane.check(job, ctx, ws)[0]


def test_merge_keeps_fenced_lines_and_reads_heading_variants():
    section = ("# Section pass 1/1 of #905 Foo at abc1234: x\n**Verdict:** changes needed\n\n"
               "## R01 (P1): a colon instead of a dash\nRepair:\n```markdown\n## 1.2 The corrected heading\n"
               "**Theorem 1.2.** fixed.\n```\nend of R01.\n\n## R02 [P3] - brackets\nx\n\n## Verified sound\n- ok\n")
    merged = merge_section_reports(MAIN, [("§1 x", section)])
    assert "## R02 (P1) — [§1 x] a colon instead of a dash" in merged and "## R03 (P3) — [§1 x] brackets" in merged
    body = merged[merged.index("## R02"):merged.index("## R03")]
    assert "## 1.2 The corrected heading" in body and "end of R01." in body and body.count("```") == 2
    from tauceti_roadmap_worker.lanes.redteam import unparsed_findings
    assert unparsed_findings("**Verdict:** changes needed\n\nR01 the sign is wrong\n")
    assert not unparsed_findings(section) and not unparsed_findings("**Verdict:** no blocking finding\n")


def test_split_sections_counts_lines_like_an_editor():
    text = "intro\u2028still line one\n## Layer 0: x\n\n**Target 0.1.** y\f\n## Layer 1: z\n"
    sections = split_sections(text)
    assert [(s.title, s.start, s.end) for s in sections] == [("Layer 0: x", 2, 4), ("Layer 1: z", 5, 5)]
