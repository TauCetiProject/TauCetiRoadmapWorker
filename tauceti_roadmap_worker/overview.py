"""The opening overview of a roadmap README, written at scale with the AI acceptance kept.

The README-shape brief opens a roadmap with an overview a mathematician can read cold: what the
roadmap builds, its headline theorems in ordinary language with their real hypotheses, the few
neighbours in Tau Ceti that place it, and a short guide to the layers. Older roadmaps open with a
list of topics or of carriers. `tauceti-roadmap overview` has an agent (`prompts/overview.md`)
rewrite ONLY the opening of each README of a pull request: everything between the `# Roadmap:` title
and the first `##` heading. In the same pass the sections are put in the new order (`layout.reorder`),
the direction words that the move made wrong inside the sections that moved are corrected, those a
small tested list of patterns settles mechanically (`fix_directions`) and those the agent proposes
when they are only such swaps (`apply_proposals`), and the agent may fold a short `## How to read the
build` into the overview.

`rewrite_problems` is the check, which the intake runs as well on the two heads' READMEs: the new
README equals the old one except (a) the opening, (b) the order of the top-level sections, (c) a
`## How to read the build` section folded into the opening (gone whole), and (d) direction-word swaps
(`SWAPS`) inside the sections that move (`MOVED`). A README the check refuses is never pushed; the
agent's output is discarded and the refusal reported.

That check says the layers kept their words. It does not say the opening is true to them, and the push
carries the acceptance with no review: so a second agent, which did not write the opening, reads each
of its sentences against the rest of the README before anything is proposed (`unsupported`,
`prompts/overview-check.md`); a sentence it cannot support goes back to the writer once.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import agents, gates, intake, layout, openroadmaps, prbatch, prompts, relayout
from .gh import GHError, GHHalt, GHRateLimited

OUT_NAME, PLAN_NAME, CHECK_NAME = "opening.md", "overview.json", "check.json"
WORD_LIMIT = 850          # words of an opening the worker refuses beyond (the brief asks for 250 to 700)
TIMEOUT = 30 * 60
LEAN_NAME = re.compile(r"`([A-Za-z_][\w']*(?:\.[A-Za-z_][\w']*)+|[A-Za-z][\w']*_[\w'.]+)`")


def lean_names(text: str) -> set[str]:
    """The backticked tokens of an opening that read as Lean declaration names: a dotted name, or a name with
    underscores that is not a subscript. `K_f`, `H_m`, `N𝔡_u`, `ord_p` and `zeta_K` are mathematics in code font
    (the brief asks for it there); a declaration name has two underscores or more (`le_of_lt`) or at least three
    characters on each side of its one underscore (`map_add`). (2026-11-19: two openings were refused twice
    each for `K_f` and `N𝔡_u`.)"""
    return {name for name in LEAN_NAME.findall(text)
            if "." in name or name.count("_") >= 2 or min(len(part) for part in name.split("_")) >= 3}


# --- the opening -------------------------------------------------------------------------------------

def words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def opening_problems(new: str, old: str) -> list[str]:
    """Why an opening the agent wrote is refused: the form the brief asks for, checked mechanically."""
    problems = []
    if not new.strip().startswith("**Summary.**"):
        problems.append("it does not open with `**Summary.**`")
    if words(new) > WORD_LIMIT:
        problems.append(f"it has {words(new)} words (at most about 700)")
    if re.search(r"^\s{0,3}#", new, re.MULTILINE):
        problems.append("it has a heading line")
    for pattern, what in ((r"<!--|-->", "an HTML comment"), (r"/home/|/tmp/|/Users/", "a machine path"),
                          (r"(?<![\w`/])@[A-Za-z][\w-]*", "an @-mention"), (r"(?<![\w&])#\d{2,}\b", "a pull-request number"),
                          (r"\\\(|\\\[|\$[^$\s]", "LaTeX"), (r"§", "a section-number locator")):
        if re.search(pattern, new) and not re.search(pattern, old):
            problems.append(f"it has {what}")
    names = lean_names(new) - lean_names(old)
    if names:
        problems.append("it names Lean declarations the old opening did not: " + ", ".join(sorted(names)[:5]))
    # the push gate's own check (`tex`), run here so that the opening is rewritten rather than pushed with it
    # (2026-11-19: 22 accepted READMEs carried `H_{m}` or `Ω^{≥i}` in plain text in their new openings)
    tex = gates.scan_tex_residue(new)
    if tex:
        found = sorted({m.group(0) for h in tex for m in re.finditer(r"\S*[_^]\{[^}\s]*\}?|\\[A-Za-z]+", h.text)})
        problems.append("it has TeX-only syntax outside code font (" + ", ".join(found[:5]) + "): write the formula in "
                        "Unicode or put it whole in a backtick code span")
    return problems


# --- direction words in the sections that moved --------------------------------------------------------

REFERENTS = {
    "layers": lambda s: s.title.startswith("layer "),
    "suppliers": lambda s: s.title == layout.SUPPLIERS,
    "scope": lambda s: s.title == layout.SCOPE,
    "conventions": lambda s: s.title == "conventions",
    "references": lambda s: s.title == layout.REFERENCES,
    "consumers": lambda s: s.title == "downstream consumers",
}
# Phrases whose referent is explicit: (pattern, referent). A direction word is corrected only when
# every section of the referent lies on one side of the section it stands in.
DIRECTIONS = [
    (re.compile(r"\b(?:the\s+)?(?:layer\s+headings|layer\s+sections|layers)\s+(?P<dir>below|above)\b", re.IGNORECASE), "layers"),
    (re.compile(r"\bLayers?\s+[A-Z]?\d+(?:\s*(?:[–-]|to|and)\s*[A-Z]?\d+)?\s+(?P<dir>below|above)\b"), "layers"),
    (re.compile(r"\bthe\s+(?P<dir>following|preceding)\s+layers\b", re.IGNORECASE), "layers"),
    (re.compile(r"\b(?:the\s+)?(?:exact\s+)?supplier\s+contracts\s+(?P<dir>below|above)\b", re.IGNORECASE), "suppliers"),
    (re.compile(r"\b(?:the\s+)?scope(?:\s+and\s+ownership)?(?:\s+section)?\s+(?P<dir>below|above)\b", re.IGNORECASE), "scope"),
    (re.compile(r"\b(?:the\s+)?conventions(?:\s+section)?\s+(?P<dir>below|above)\b", re.IGNORECASE), "conventions"),
    (re.compile(r"\b(?:the\s+)?references\s+(?P<dir>below|above)\b", re.IGNORECASE), "references"),
    (re.compile(r"\b(?:the\s+)?downstream\s+consumers\s+(?P<dir>below|above)\b", re.IGNORECASE), "consumers"),
]


def _cased(word: str, like: str) -> str:
    return word.capitalize() if like[:1].isupper() else word


def fix_directions(reordered: str) -> tuple[str, list[tuple[str, str, str]]]:
    """`reordered` (a README in the new order) with the direction words of the tested patterns
    (`DIRECTIONS`) put right inside the sections the reordering moves (`layout.MOVED`), whether this
    pass moved them or an earlier one did (`layout` reorders without touching a word): (the text,
    [(heading, old phrase, new phrase)]). A phrase is corrected only when every section it refers to
    lies on one side of the section it stands in, so a phrase that points the right way stays; every
    other byte stays."""
    moved = set(layout.MOVED)
    found = layout.parts(reordered)
    sections, changes, out = found[1:], [], []
    for i, s in enumerate(sections):
        body = s.body
        if s.title in moved:
            for pattern, ref in DIRECTIONS:
                where = [j for j, t in enumerate(sections) if REFERENTS[ref](t) and j != i]
                if not where or not (all(j > i for j in where) or all(j < i for j in where)):
                    continue
                after = all(j > i for j in where)
                pieces, last = [], 0
                for m in pattern.finditer(body):
                    word = m.group("dir").lower()
                    want = ("below" if after else "above") if word in ("below", "above") else \
                        ("following" if after else "preceding")
                    if want == word:
                        continue
                    start, end = m.span("dir")
                    pieces += [body[last:start], _cased(want, m.group("dir"))]
                    changes.append((s.title, m.group(0), body[m.start():start] + _cased(want, m.group("dir"))
                                    + body[end:m.end()]))
                    last = end
                body = "".join(pieces) + body[last:]
        out.append(layout.Part(s.title, body, s.gap))
    return layout.join([found[0], *out], [p.gap for p in found]), changes


def apply_proposals(text: str, proposals) -> tuple[str, list[str], list[str]]:
    """The agent's proposed direction fixes applied to `text` (a README in the new order): each a
    {section, old, new} where `section` names a section that moves (`MOVED`), `old` occurs exactly once
    in it, and `new` is `old` with direction words swapped only. (the text, applied, refused)."""
    found = layout.parts(text)
    sections = list(found[1:])
    applied, refused = [], []
    for item in proposals if isinstance(proposals, list) else []:
        if not isinstance(item, dict):
            continue
        heading, old, new = (str(item.get(k) or "") for k in ("section", "old", "new"))
        key = layout.title_key(heading.removeprefix("## "))
        k = next((n for n, s in enumerate(sections) if s.title == key), None)
        if key not in layout.MOVED or k is None:
            refused.append(f"{heading!r}: not a section that moves")
        elif not old or sections[k].body.count(old) != 1:
            refused.append(f"{heading!r}: {old[:60]!r} is not in it exactly once")
        elif not layout.swaps(old, new):
            refused.append(f"{heading!r}: {old[:60]!r} -> {new[:60]!r} is not a swap of direction words")
        else:
            s = sections[k]
            sections[k] = layout.Part(s.title, s.body.replace(old, new), s.gap)
            applied.append(f"{heading}: {old!r} -> {new!r}")
    return layout.join([found[0], *sections], [p.gap for p in found]), applied, refused


# --- the README, and the check -------------------------------------------------------------------------

def build(text: str, opening: str, fold: bool) -> str:
    """The README `text` (in the new order) with `opening` in place of its opening, and with its
    `## How to read the build` section removed when `fold` (the agent folded it into the opening)."""
    found = layout.parts(text)
    title, _old = layout.split_opening(text)
    head = layout.Part("", (title + "\n\n" if title else "") + opening.strip("\n") + "\n", found[0].gap or "\n")
    parts, gaps = [head, *found[1:]], [head.gap, *[p.gap for p in found[1:]]]
    if fold:
        k = next((n for n, p in enumerate(parts) if n and p.title == layout.HOW_TO_READ), None)
        if k is not None:
            del parts[k]
            del gaps[k]
    return layout.join(parts, gaps)


# --- the command ---------------------------------------------------------------------------------------

@dataclass
class Folder:
    folder: str
    path: str
    old: str                                     # the README at the head
    new: str = ""                                # the README proposed (checked)
    changes: list[str] = field(default_factory=list)
    why: str = ""                                # why it is not proposed


def agent_values(ctx, workdir: Path, folder: str, original: str, fixed: list[tuple[str, str, str]]) -> dict:
    opening = layout.split_opening(original)[1]
    moved = layout.moved(original)
    # A README an earlier pass reordered: the sections it moved stand where they were put, with the
    # direction words they had before, so the agent reads them all the same.
    standing = [p.body.split("\n", 1)[0].strip() for p in layout.parts(original)[1:] if p.title in layout.MOVED]
    guide = next((p for p in layout.parts(original)[1:] if p.title == layout.HOW_TO_READ), None)
    return {"ROADMAP": folder.rsplit("/", 1)[-1], "README": str(workdir / "README.md"),
            "LEAN": str(workdir / "Suggested.lean"), "OUT": str(workdir / OUT_NAME), "PLAN": str(workdir / PLAN_NAME),
            "OLD_WORDS": str(words(opening)),
            "MOVED": (", ".join(f"`{h}`" for h in moved)
                      or (", ".join(f"`{h}`" for h in standing) + " (an earlier pass put them where they are, "
                          "without touching a word: read them for direction words all the same)" if standing
                          else "none (the README was in the new order already)")),
            "GUIDE": (f"The README has `## How to read the build` ({words(guide.body)} words)." if guide
                      else "The README has no `## How to read the build` section."),
            "MECHANICAL": "\n".join(f"- in `## {h}`: “{a}” became “{b}”" for h, a, b in fixed) or "- none"}


def unsupported(ctx, workdir: Path, folder: str, new: str, lean: str, attempt: int, *, agent: str, model: str,
                launch=None) -> tuple[list[str], bool]:
    """(what a second agent, which did not write the opening, finds in it that the rest of the README
    does not support, whether that agent is at its usage limit). The mechanical checks hold the opening
    to its form and the layers to their words; whether a sentence of the opening says what a layer says
    is a reading, and the acceptance is carried across the push with no review, so this is the only one
    (2026-11-18: an opening said a notion did not depend "on the metric" where the layer proves it for
    any two continuous Riemannian metrics; a referee found it a day later). The reader gets the opening
    and the README without it in a folder of its own (`check-<attempt>`), and writes `check.json`: one
    entry for each asserting sentence. No answer, or an answer that lists no sentence, is a problem too,
    and so is a reader that did not finish: what it wrote before it failed covers part of the opening."""
    cfg, where = ctx.cfg, workdir / f"check-{attempt}"
    if where.exists():
        shutil.rmtree(where)
    where.mkdir(parents=True)
    title, opening = layout.split_opening(new)
    found = layout.parts(new)
    (where / OUT_NAME).write_text(opening + "\n", encoding="utf-8")
    (where / "README.md").write_text(
        layout.join([layout.Part("", title + "\n", "\n"), *found[1:]], ["\n", *[p.gap for p in found[1:]]]), encoding="utf-8")
    (where / "Suggested.lean").write_text(lean, encoding="utf-8")
    brief = prompts.render("overview-check", {
        "ROADMAP": folder.rsplit("/", 1)[-1], "OPENING": str(where / OUT_NAME), "README": str(where / "README.md"),
        "LEAN": str(where / "Suggested.lean"), "OUT": str(where / CHECK_NAME)})
    result = agents.run_agent(agents.AgentRun(
        agent=agent, model=model, workdir=where, brief=brief, log_out=where / "run.out", log_err=where / "run.err",
        timeout=TIMEOUT, sandbox_profile=cfg.sandbox.profile, proxy_url=cfg.sandbox.proxy_url,
        netjail=cfg.sandbox.netjail, claude_hook=cfg.sandbox.claude_hook, settings_dir=ctx.wdir / "settings"), launch)
    if getattr(result, "limited", False):
        return [], True
    if not result.ok:
        return ["the reader of the opening did not finish ("
                + ("timed out" if result.timed_out else f"exit {result.exit_code}") + ")"], False
    try:
        answer = json.loads((where / CHECK_NAME).read_text(encoding="utf-8"))
        sentences = [x for x in answer["sentences"] if isinstance(x, dict) and str(x.get("sentence") or "").strip()]
    except (OSError, ValueError, KeyError, TypeError):
        return [f"the reader of the opening wrote no usable `{CHECK_NAME}`"], False
    if not sentences:
        return [f"the reader of the opening listed no sentence in `{CHECK_NAME}`"], False
    return [f"the README does not support “{' '.join(str(x['sentence']).split())[:300]}”"
            + (f" ({' '.join(str(x['why']).split())[:300]})" if x.get("why") else "")
            for x in sentences if x.get("supported") is not True], False


def propose(ctx, workdir: Path, folder: str, path: str, readme: str, lean: str, *, agent: str, model: str,
            launch=None, attempts: int = 2, check: bool = False, check_agent: str = "", check_model: str = "") -> Folder:
    """The agent's overview of one README, checked: reordered, direction words fixed, the agent's opening
    and proposals applied, then `rewrite_problems` against the README at the head and, with `check`, a
    second agent's reading of the opening against the rest (`unsupported`; `check_agent`, else the
    writer's, on `check_model`, else the writer's model for the writer's agent and the configured model
    of its family for the other). Its files stay in `workdir` (the proposed README as
    `README.proposed.md`)."""
    out = Folder(folder, path, readme)
    try:
        reordered = layout.reorder(readme)
    except layout.LayoutError as exc:
        out.why = f"not reordered ({exc})"
        return out
    fixed_text, fixed = fix_directions(reordered)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    (workdir / "README.md").write_text(fixed_text, encoding="utf-8")
    (workdir / "Suggested.lean").write_text(lean, encoding="utf-8")
    (workdir / "README.old.md").write_text(readme, encoding="utf-8")
    values = agent_values(ctx, workdir, folder, readme, fixed)
    problems: list[str] = []
    cfg = ctx.cfg
    for attempt in range(1, attempts + 1):
        brief = prompts.render("overview", values)
        if problems:
            brief += ("\n## Your previous attempt was refused\n\n" + "\n".join(f"- {p}" for p in problems)
                      + "\n\nWrite both files again.\n")
        for name in (OUT_NAME, PLAN_NAME):
            (workdir / name).unlink(missing_ok=True)
        (workdir / "README.md").write_text(fixed_text, encoding="utf-8")
        result = agents.run_agent(agents.AgentRun(
            agent=agent, model=model, workdir=workdir, brief=brief, log_out=workdir / f"run-{attempt}.out",
            log_err=workdir / f"run-{attempt}.err", timeout=TIMEOUT, sandbox_profile=cfg.sandbox.profile,
            proxy_url=cfg.sandbox.proxy_url, netjail=cfg.sandbox.netjail, claude_hook=cfg.sandbox.claude_hook,
            settings_dir=ctx.wdir / "settings"), launch)
        if getattr(result, "limited", False):
            out.why = "the agent is at its usage limit"
            return out
        opening = (workdir / OUT_NAME).read_text(encoding="utf-8") if (workdir / OUT_NAME).is_file() else ""
        try:
            plan = json.loads((workdir / PLAN_NAME).read_text(encoding="utf-8")) if (workdir / PLAN_NAME).is_file() else {}
        except ValueError:
            plan = None
        problems = []
        if not opening.strip():
            problems.append(f"it wrote no `{OUT_NAME}`")
        if not isinstance(plan, dict):
            problems.append(f"`{PLAN_NAME}` is not a JSON object")
            plan = {}
        problems += opening_problems(opening, layout.split_opening(readme)[1]) if opening.strip() else []
        if problems:
            continue
        text, applied, refused = apply_proposals(fixed_text, plan.get("directions") or [])
        new = build(text, opening, bool(plan.get("fold_guide")))
        problems = layout.rewrite_problems(readme, new)
        if problems:
            continue
        if check:
            reader = check_agent or agent
            problems, limited = unsupported(
                ctx, workdir, folder, new, lean, attempt, agent=reader, launch=launch,
                model=check_model or (model if reader == agent else ctx.cfg.model_for(reader)))
            if limited:
                out.why = "the agent that reads the opening is at its usage limit"
                return out
            if problems:
                continue
        out.new = new
        out.changes = ([f"opening {words(layout.split_opening(readme)[1])} -> {words(opening)} words"]
                       + ([f"moves {', '.join(layout.moved(readme))}"] if layout.moved(readme) else [])
                       + (["guide folded into the opening"] if plan.get("fold_guide") and
                          any(p.title == layout.HOW_TO_READ for p in layout.parts(readme)[1:]) else [])
                       + [f"direction: {h}: {a!r} -> {b!r}" for h, a, b in fixed] + applied
                       + [f"direction proposal refused: {r}" for r in refused])
        (workdir / "README.proposed.md").write_text(new, encoding="utf-8")
        (workdir / "record.json").write_text(json.dumps({"folder": folder, "changes": out.changes,
                                                         "plan": plan}, indent=1, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
        return out
    out.why = "refused: " + "; ".join(problems[:4])
    (workdir / "refused.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
    return out


def commit_message(folders: list[str]) -> str:
    return (f"{', '.join(folders)}: a readable opening overview, and the sections in the new order\n\n"
            "The opening of the README is rewritten as an overview (what the roadmap builds, its headline "
            "theorems with their hypotheses, its nearest neighbours, a guide to the layers); the scope and "
            "the supplier contracts move to the end. No section of the layers changes; inside the sections "
            "that moved, only direction words such as \"below\" are corrected.\n")


def done_before(comments: list[dict], trusted: set[str]) -> bool:
    """Whether a pull request already carries an overview record of ours (`intake.layout_record`)."""
    return any((r := intake.layout_record(c.get("body") or "")) and r.get("kind") == "overview"
               and str(((c.get("user") or {}).get("login")) or "").lower() in trusted for c in comments)


def run(ctx, *, numbers=(), authors=(), pool: bool = False, write: bool = False, pace: float = 2.0, limit: int = 0,
        accepted_only: bool = False, listing: bool = False, force: bool = False, agent: str = "claude",
        model: str = "", launch=None, report=print, sleep=time.sleep, check: bool = False, check_agent: str = "",
        check_model: str = "") -> list[str]:
    """The command: for each pull request asked for (as `layout` selects them), refuse an approved or
    queued one, skip one with a round under way, and propose an overview for each README it adds or
    changes (`propose`); print the old and new opening sizes and where the proposed README is; with
    `write`, push the checked READMEs of a pull request in one commit with the note (`push_files`), the
    head read again first. `listing` runs no agent. With `check`, a second agent reads each opening
    against the rest of its README before it is proposed (`unsupported`)."""
    cfg, repo, out = ctx.cfg, ctx.cfg.repositories.content, []

    def say(line: str) -> None:
        out.append(line)
        report(line)
    listed = openroadmaps.inventory(ctx.gh, repo, (cfg.roadmap.active_dir,), any_file=True,
                                    skip=cfg.prreview.aggregate_prs)
    entries = relayout.selected(ctx, listed, list(numbers), list(authors), pool)
    if accepted_only:
        entries = [e for e in entries if cfg.prreview.label in (e.get("labels") or [])]
    if not entries:
        say("no pull request selected")
        return out
    on_main = openroadmaps.readme_folders(ctx.gh, repo, cfg.project.branch, cfg.roadmap.active_dir)
    jobs = [*ctx.board.kind_jobs("prreview", "open"), *ctx.board.kind_jobs("gate", "open")]
    found = relayout.statuses(ctx.gh, repo, [int(e["pr"]) for e in entries if not e.get("missing")])
    trusted = intake.layout_trusted(cfg)
    reader = None
    pushed = 0
    for entry in entries:
        number = int(entry["pr"])
        try:
            p = relayout.plan(ctx, entry, found.get(number), jobs, on_main)
            who = f"#{number}" + (f" ({p.author})" if p.author else "")
            if p.refused or p.skipped:
                say(f"{who}: " + (f"refused: {p.refused}" if p.refused else f"skipped: {p.skipped}"))
                continue
            if not force:
                if reader is None:
                    reader = prbatch.PullReader(ctx.gh, repo, [int(e["pr"]) for e in entries if not e.get("missing")],
                                                cfg.board.graphql_batch)
                if done_before(reader.comments(number), trusted):
                    say(f"{who}: done before (an overview record is on the pull request; --force to redo)")
                    continue
            folders, _why = intake.review_plan(entry, cfg, on_main)
            active = cfg.roadmap.active_dir.strip("/")
            todo = [f for f, change in folders.items() if change]
            if not todo:
                say(f"{who}: nothing to do: no README this pull request adds or changes")
                continue
            if listing:
                say(f"{who}: would write the opening overview of " + ", ".join(todo))
                continue
            proposals = []
            for folder in todo:
                path = f"{active}/{folder}/README.md"
                try:
                    readme = ctx.gh.raw_file(p.head_repo, path, ref=p.head)
                except GHError as exc:
                    say(f"{who} {folder}: README not read ({str(exc).splitlines()[0][:120]})")
                    continue
                try:
                    lean = ctx.gh.raw_file(p.head_repo, f"{active}/{folder}/Suggested.lean", ref=p.head)
                except GHError:
                    lean = ""
                workdir = ctx.state / "overviews" / f"pr-{number}" / folder.replace("/", "__")
                got = propose(ctx, workdir, folder, path, readme, lean, agent=agent, model=model, launch=launch,
                              check=check, check_agent=check_agent, check_model=check_model)
                if not got.new:
                    say(f"{who} {folder}: not proposed: {got.why} (scratch: {workdir})")
                    continue
                proposals.append(got)
                old_open, new_open = layout.split_opening(readme)[1], layout.split_opening(got.new)[1]
                say(("[dry run] " if not write else "") + f"{who} {folder}: opening {words(old_open)} -> "
                    f"{words(new_open)} words ({len(old_open):,} -> {len(new_open):,} characters); "
                    + "; ".join(got.changes[1:] or ["no other change"])
                    + f"; proposed README: {workdir / 'README.proposed.md'}")
            if not write or not proposals:
                continue
            if limit and pushed >= limit:
                say(f"{who}: not pushed this run (--limit {limit})")
                continue
            if pushed:
                sleep(pace)
            names = [g.folder for g in proposals]
            try:
                sha = relayout.push_files(ctx, number, p.head, p.head_repo, p.branch,
                                          {g.path: g.new for g in proposals}, commit_message(names),
                                          lambda sha, head=p.head, names=names: intake.layout_note(
                                              head, sha, names, kind="overview"))
            except GHError as exc:
                say(f"{who}: not pushed: {str(exc).splitlines()[0][:200]}")
                continue
            pushed += 1
            say(f"{who}: pushed {sha[:7]} on {p.head_repo}:{p.branch} ({', '.join(names)}); note posted")
        except (GHRateLimited, GHHalt) as exc:
            say(f"stopped at #{number}: {str(exc).splitlines()[0][:200]}; {pushed} pushed, run again later")
            break
    if write:
        say(f"{pushed} pull request{'s' if pushed != 1 else ''} pushed")
    return out
