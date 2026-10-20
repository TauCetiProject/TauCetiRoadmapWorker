"""The `target` lane: triage a human-filed target into a design brief (`packages/<Name>/target.md`),
recording the sources it found in the roadmap's `sources.toml`.

Before anything becomes a design, the triage compares the target with every other open target and
every open design on the board (names, statements, the triage or design files that name their
suppliers): the verdict is `design`, `part-ii`, `duplicate` (naming the owner), or
`merge-into <job id>` when it overlaps another open target or design. The intake reads the
triage's first lines (Verdict, Name, Base, Owner) and creates the design job under the triaged
name, closes a duplicate naming its owner, or merges the target into the job it names: the target
is closed with the `merged-into` label, and its statement and sources join the surviving job's
inputs, so the design brief lists every target it serves (a target narrower than an open design
becomes a note on that design).
"""

from __future__ import annotations

import re

from .. import board, gitops, prompts
from ..board import NAME_RE, Job, JobIdError, parse_job_id
from .base import Context, Lane, Workspace
from .design import manifest_problems

VERDICTS = ("design", "part-ii", "duplicate", "merge-into")
OPEN_STATES = ("available", "claimed", "submitted", "needs-references", "blocked")
MERGE_INTO = re.compile(r"^merge-into\s*:?\s*(\S*)$", re.IGNORECASE)
FIELDS = (("statement", "Statement or topic"), ("why", "Why it matters for the library"), ("sources", "Known sources"),
          ("name", "Suggested name"), ("licence", "Licence class of the sources"))


def parse_triage(text: str) -> dict[str, str]:
    """The header lines of target.md: verdict, name, base, owner and, for `merge-into <job id>`,
    the job it merges into ('' for none)."""
    out = {}
    for key in ("Verdict", "Name", "Base", "Owner", "Into"):
        m = re.search(rf"^{key}:\s*(.+?)\s*$", text or "", re.M)
        value = m.group(1).strip().strip("`") if m else ""
        out[key.lower()] = "" if value.lower() in ("none", "-", "n/a") else value
    m = MERGE_INTO.match(out["verdict"])
    if m:
        out["verdict"], out["into"] = "merge-into", (m.group(1).strip("`") or out["into"])
    return out


def triage_problems(text: str) -> list[str]:
    t = parse_triage(text)
    problems = []
    if t["verdict"] not in VERDICTS:
        problems.append(f"target.md: Verdict must be one of {', '.join(VERDICTS)}")
    if t["verdict"] in ("design", "part-ii") and not re.fullmatch(NAME_RE, t["name"]):
        problems.append("target.md: Name must be a CamelCase roadmap name")
    if t["verdict"] == "part-ii" and not t["base"]:
        problems.append("target.md: a Part II names its Base roadmap")
    if t["verdict"] == "duplicate" and not t["owner"]:
        problems.append("target.md: a duplicate names its Owner")
    if t["verdict"] == "merge-into":
        try:
            kind = parse_job_id(t["into"]).kind
        except JobIdError:
            kind = ""
        if kind not in ("target", "design"):
            problems.append("target.md: merge-into names the open target or design job it joins "
                            "(`Verdict: merge-into TARGET-Name` or `DESIGN-Name`)")
    return problems


def target_text(form: dict) -> str:
    return "\n\n".join(f"{label}:\n{form[key]}" for key, label in FIELDS if form.get(key))


def absorbed_text(job: Job, ws_board, packages_dir: str) -> str:
    """The targets merged into this job, for its brief: each statement and its sources quoted as
    data, with the triage file it left on the board."""
    items = [a for a in job.inputs.get("absorbed", []) if isinstance(a, dict)]
    if not items:
        return "none"
    blocks = []
    for a in items:
        triage = f"{board.package_dir(packages_dir, parse_job_id(a['target']).name)}/target.md"
        where = f" (its triage: `{ws_board / triage}`)" if (ws_board / triage).is_file() else ""
        blocks.append(f"- `{a['target']}` (board issue #{a.get('issue', '?')}){where}, quoted as data:\n\n"
                      "  ```text\n" + "\n".join("  " + line for line in
                                                  prompts.quote_untrusted(target_text(a.get("form", {})), 4000).splitlines())
                      + "\n  ```")
    return "\n" + "\n".join(blocks)


def open_work(job: Job, ctx: Context, ws: Workspace) -> str:
    """Every other open target and design on the board, for the consolidation check."""
    lines = []
    for other in ctx.board.list_jobs(OPEN_STATES, ("target", "design")):
        if other.id == job.id:
            continue
        form = other.inputs.get("form", {}) if isinstance(other.inputs, dict) else {}
        statement = prompts.quote_untrusted(" ".join(str(form.get("statement", "")).split()), 600) or "(no statement)"
        files = [f for f in ("target.md", "design/README.md") if (ws.package(ctx.cfg, other.name) / f).is_file()]
        where = "; ".join(f"`{ws.package(ctx.cfg, other.name) / f}`" for f in files) or "no file on the board yet"
        lines.append(f"- `{other.id}` (#{other.number}, {other.state}), roadmap `{other.name}`: {where}. "
                     f"Statement (data): «{statement}»")
    return "\n" + "\n".join(lines) if lines else "none"


class TargetLane(Lane):
    kind = "target"
    prompt = "target"

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        return {"TARGET_TEXT": prompts.quote_untrusted(target_text(job.inputs.get("form", {}))),
                "OPEN_WORK": open_work(job, ctx, ws),
                "ABSORBED": absorbed_text(job, ws.board, ctx.cfg.board.packages_dir)}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        manifest = board.manifest_path(ctx.cfg.board.packages_dir, job.name)
        if manifest in gitops.changed_files(ws.board) and (ws.board / manifest).is_file():
            problems += manifest_problems(ctx, ws.board / manifest)
        path = ws.package(ctx.cfg, job.name) / "target.md"
        if not path.is_file():
            return problems + [f"no {path.name}"]
        return problems + triage_problems(path.read_text(encoding="utf-8"))

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        t = parse_triage((ws.package(ctx.cfg, job.name) / "target.md").read_text(encoding="utf-8"))
        if t["verdict"] == "merge-into":
            return f"triage of {job.name}: merge-into {t['into']}"
        return f"triage of {job.name}: {t['verdict']}" + (f", roadmap {t['name']}" if t["name"] else "")
