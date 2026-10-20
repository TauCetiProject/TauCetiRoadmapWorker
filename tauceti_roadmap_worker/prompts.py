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
# Every brief: one per lane, the red-team lane's section pass ([redteam] sharded), and the brief the
# board pins on a pull request under review for a browser model.
BRIEFS = LANES + ("redteam-section", "review-web")


class PromptError(Exception):
    pass


# A worker renders from the snapshot it took at startup (build.snapshot), never from an install
# that may be replaced under it while it runs.
_ROOT: Path | None = None


def use(root: Path | None) -> None:
    global _ROOT
    _ROOT = Path(root) if root else None


def root_dir(root: Path | None = None) -> Path:
    return Path(root) if root else (_ROOT or PROMPTS)


def expand(text: str, root: Path | None = None) -> str:
    root = root_dir(root)

    def include(m: re.Match) -> str:
        path = root / m.group(1)
        if not path.is_file():
            raise PromptError(f"missing section {m.group(1)}")
        return path.read_text(encoding="utf-8").rstrip("\n")
    return INCLUDE.sub(include, text)


def template(lane: str, root: Path | None = None) -> str:
    root = root_dir(root)
    path = root / f"{lane}.md"
    if not path.is_file():
        raise PromptError(f"no brief for lane {lane!r} at {path}")
    return expand(path.read_text(encoding="utf-8"), root)


def placeholders(lane: str, root: Path | None = None) -> set[str]:
    return set(PLACEHOLDER.findall(template(lane, root)))


def render(lane: str, values: dict[str, str], root: Path | None = None) -> str:
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
