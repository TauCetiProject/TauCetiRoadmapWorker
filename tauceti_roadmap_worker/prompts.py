"""Render a lane brief from `prompts/<lane>.md`.

A brief may include shared sections with a line `<!-- include: sections/<name>.md -->` (one level:
sections do not include others) and uses placeholders `__NAME__`. Rendering substitutes every
placeholder in one pass over the template, so text inserted as a value (a target's wording, a
reviewer's comment) is never scanned for placeholders or includes, and refuses to render when a
placeholder of the template has no value.
"""

from __future__ import annotations

import re
from pathlib import Path

from .paths import PROMPTS

INCLUDE = re.compile(r"^<!-- include: (sections/[A-Za-z0-9_.-]+\.md) -->$", re.M)
PLACEHOLDER = re.compile(r"__([A-Z][A-Z0-9_]*)__")
LANES = ("target", "design", "package", "review", "fix", "port", "redteam", "gate")
# Every brief: one per lane, plus the red-team lane's section pass ([redteam] sharded).
BRIEFS = LANES + ("redteam-section",)


class PromptError(Exception):
    pass


def expand(text: str, root: Path = PROMPTS) -> str:
    def include(m: re.Match) -> str:
        path = root / m.group(1)
        if not path.is_file():
            raise PromptError(f"missing section {m.group(1)}")
        return path.read_text(encoding="utf-8").rstrip("\n")
    return INCLUDE.sub(include, text)


def template(lane: str, root: Path = PROMPTS) -> str:
    path = root / f"{lane}.md"
    if not path.is_file():
        raise PromptError(f"no brief for lane {lane!r} at {path}")
    return expand(path.read_text(encoding="utf-8"), root)


def placeholders(lane: str, root: Path = PROMPTS) -> set[str]:
    return set(PLACEHOLDER.findall(template(lane, root)))


def render(lane: str, values: dict[str, str], root: Path = PROMPTS) -> str:
    text = template(lane, root)
    missing = sorted(set(PLACEHOLDER.findall(text)) - set(values))
    if missing:
        raise PromptError(f"brief {lane}: no value for {', '.join('__' + m + '__' for m in missing)}")
    rendered = PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)
    return re.sub(r"\n{3,}", "\n\n", rendered).rstrip() + "\n"


def quote_untrusted(text: str, limit: int = 20000) -> str:
    """Untrusted text for a fenced ```text block: it cannot close the fence, and it is bounded."""
    text = (text or "").replace("```", "ˋˋˋ").replace("~~~", "∼∼∼")
    return text if len(text) <= limit else text[:limit] + "\n[... truncated]"


def bullet_list(items: list[str], empty: str = "none") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty
