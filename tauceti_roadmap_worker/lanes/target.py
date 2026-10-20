"""The `target` lane: triage a human-filed target into a design brief (`packages/<Name>/target.md`).

The intake reads the triage's first lines (Verdict, Name, Base, Owner) and creates the design job
under the triaged name, or closes the target as a duplicate naming its owner.
"""

from __future__ import annotations

import re

from .. import prompts
from ..board import NAME_RE, Job
from .base import Context, Lane, Workspace

VERDICTS = ("design", "part-ii", "duplicate")
FIELDS = (("statement", "Statement or topic"), ("why", "Why it matters for the library"), ("sources", "Known sources"),
          ("name", "Suggested name"), ("licence", "Licence class of the sources"))


def parse_triage(text: str) -> dict[str, str]:
    """The header lines of target.md: verdict, name, base, owner ('' for none)."""
    out = {}
    for key in ("Verdict", "Name", "Base", "Owner"):
        m = re.search(rf"^{key}:\s*(.+?)\s*$", text or "", re.M)
        value = m.group(1).strip() if m else ""
        out[key.lower()] = "" if value.lower() in ("none", "-", "n/a") else value
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
    return problems


def target_text(form: dict) -> str:
    return "\n\n".join(f"{label}:\n{form[key]}" for key, label in FIELDS if form.get(key))


class TargetLane(Lane):
    kind = "target"
    prompt = "target"

    def values(self, job: Job, ctx: Context, ws: Workspace) -> dict:
        return {"TARGET_TEXT": prompts.quote_untrusted(target_text(job.inputs.get("form", {})))}

    def check(self, job: Job, ctx: Context, ws: Workspace) -> list[str]:
        problems = super().check(job, ctx, ws)
        path = ws.package(ctx.cfg, job.name) / "target.md"
        if not path.is_file():
            return problems + [f"no {path.name}"]
        return problems + triage_problems(path.read_text(encoding="utf-8"))

    def finish(self, job: Job, ctx: Context, ws: Workspace) -> str:
        t = parse_triage((ws.package(ctx.cfg, job.name) / "target.md").read_text(encoding="utf-8"))
        return f"triage of {job.name}: {t['verdict']}" + (f", roadmap {t['name']}" if t["name"] else "")
