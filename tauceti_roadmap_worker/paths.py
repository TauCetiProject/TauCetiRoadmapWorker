"""Install-location detection and the per-project state directory.

`HERE` is where the bundled assets live: the repository root in a source checkout (prompts/ sits
beside the package) and the package directory in an installed wheel (pyproject force-includes
prompts/ into the package).

Everything a worker writes at run time lives under the project's state directory, the same
per-project layout TauCetiTools uses:

    $TAUCETI_PROJECT_STATE                       (explicit override), else
    $XDG_STATE_HOME/tauceti/projects/<project>/  (if XDG_STATE_HOME is set), else
    ~/.local/state/tauceti/projects/<project>/

Inside it (see docs/OPERATORS.md for the full list):

    workers/<worker-id>/        checkouts, briefs, logs of one worker
    claims/<repo>.git           scratch object store for lease refs
    sources/                    fetched sources and their text extractions
    rev/<pr>/                   read-only clones of open roadmap pull requests at their heads
    hold-draft.txt              pull requests that must stay drafts (one number per line)
    label-ignore.txt            "<pr> <timestamp-prefix>" comments that never flip labels
    stacks.toml                 stacked pull requests: child = parent
    external-deps.json          other authors' roadmap pull requests a pull request cites (beyond the scan)
    other-prs.json              roadmap folders other authors' open pull requests add, by head
    dropped-external.txt        other authors' pull requests that never count as dependencies
    mutual.txt                  "<pr> <other-pr>" pairs that cite each other (no whole-PR order)
"""

from __future__ import annotations

import os
from pathlib import Path

_pkg = Path(__file__).resolve().parent
HERE = _pkg if (_pkg / "prompts").is_dir() else _pkg.parent
PROMPTS = HERE / "prompts"
ISSUE_TEMPLATES = (_pkg / "issue_templates") if (_pkg / "issue_templates").is_dir() else HERE / ".github" / "ISSUE_TEMPLATE"
SANDBOX = _pkg / "sandbox"


def state_root(project: str, env: dict | None = None) -> Path:
    """The per-project state directory (not created here)."""
    env = os.environ if env is None else env
    explicit = env.get("TAUCETI_PROJECT_STATE")
    if explicit:
        return Path(explicit).expanduser()
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path(env.get("HOME") or Path.home()) / ".local" / "state"
    return base / "tauceti" / "projects" / project


def worker_dir(project: str, worker_id: str, env: dict | None = None) -> Path:
    return state_root(project, env) / "workers" / sanitize_worker_id(worker_id)


def sanitize_worker_id(raw: str) -> str:
    """Worker ids become directory names, branch prefixes and lease owners: lower-case, [a-z0-9-]."""
    out = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw.lower())
    return out.strip("-") or "default"
