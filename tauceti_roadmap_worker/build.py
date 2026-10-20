"""The build a worker runs, and the snapshot of its files.

A tool reinstall (`uv tool install --force`) replaces the package under a running worker. Python
code is loaded at startup, but briefs and templates are files read at lane time, so a worker could
render a new brief with old code (a placeholder its lanes do not fill). At startup the worker
therefore copies the prompts and issue templates into its state directory and renders only from
that copy; and at every round boundary it compares the installed build with the one it started
on, and exits for a restart when they differ.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from . import __version__, paths, prompts, references

PACKAGE = Path(__file__).resolve().parent


def build_id() -> str:
    """`<version>+<hash>` of the installed code, briefs and templates."""
    digest = hashlib.sha256()
    for root in (PACKAGE, paths.PROMPTS, paths.ISSUE_TEMPLATES):
        files = sorted(p for p in Path(root).rglob("*") if p.is_file() and "__pycache__" not in p.parts
                       and p.suffix in (".py", ".md", ".yml", ".yaml", ".json", ".toml", ".txt", ".sh", ".c"))
        for p in files:
            digest.update(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes() + b"\0")
    return f"{__version__}+{digest.hexdigest()[:10]}"


def snapshot(wdir: Path, build: str | None = None) -> Path:
    """Copy the briefs and templates of this build into `<wdir>/build/<build>/` (once), make the
    renderers use the copy, and drop the copies of earlier builds."""
    build = build or build_id()
    root = Path(wdir) / "build"
    dest = root / build
    if not (dest / "prompts").is_dir():
        tmp = root / (build + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(paths.PROMPTS, tmp / "prompts")
        shutil.copytree(paths.ISSUE_TEMPLATES, tmp / "issue_templates")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
    for old in root.iterdir():
        if old.is_dir() and old.name != build:
            shutil.rmtree(old, ignore_errors=True)
    prompts.use(dest / "prompts")
    references.use_templates(dest / "issue_templates")
    return dest
