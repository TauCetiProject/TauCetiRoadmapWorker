"""Filing targets: one (`targets file`) or many (`targets file-many --from <toml | jsonl>`).

A target is filed as the board's issue form would file it (`### <label>` sections, labels
`<namespace>` and `kind:target`); `board sync` then turns it into a TARGET job. A list is a TOML
file of `[[target]]` tables or a JSON Lines file of objects, with the fields `statement` and `why`
(required) and `sources`, `name`, `licence`, `title`:

    [[target]]
    name = "QuadraticFormsOverLocalFields"
    statement = "Classification of quadratic forms over non-archimedean local fields ..."
    why = "Supplier of the local-global principle"
    sources = "Serre, A Course in Arithmetic, Ch. IV"

`file-many` is a dry run unless `--yes`, and files each target once: an entry whose roadmap name
(the suggested name, else one built from the title or the statement) matches a target issue of
the board (open or closed) or a job of the board is skipped.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from . import board

FIELDS = ("statement", "why", "sources", "name", "licence", "title")


def issue_payload(cfg, fields: dict) -> dict:
    body = "\n\n".join([f"### Statement or topic\n\n{fields['statement']}",
                        f"### Why it matters for the library\n\n{fields['why']}",
                        f"### Known sources\n\n{fields.get('sources') or '_No response_'}",
                        f"### Suggested name\n\n{fields.get('name') or '_No response_'}",
                        f"### Licence class of the sources\n\n{fields.get('licence') or 'Unknown'}"])
    title = f"[Target] {fields.get('title') or fields.get('name') or fields['statement'][:60]}"
    return {"title": title, "body": body, "labels": [cfg.board.namespace_label, "kind:target"]}


def read_list(path: Path) -> list[dict]:
    """The entries of a target list (TOML `[[target]]` tables, or JSON Lines)."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".toml":
        entries = tomllib.loads(text).get("target", [])
    else:
        entries = [json.loads(line) for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return [{k: str(v).strip() for k, v in e.items() if k in FIELDS} if isinstance(e, dict) else {} for e in entries]


def target_name(fields: dict) -> str:
    return board.camel_name(fields.get("name") or fields.get("title") or fields.get("statement", "")[:80])


def known_names(ctx) -> dict[str, str]:
    """Roadmap name -> where the board has it already (a target issue or a job)."""
    cfg = ctx.cfg
    out = {j.name: j.id for j in ctx.board.all_jobs()}
    issues = ctx.gh.api(f"repos/{cfg.repositories.board_repo()}/issues?labels={cfg.board.namespace_label},kind:target"
                        "&state=all&per_page=100", paginate=True)
    for issue in issues or []:
        form = board.parse_target_form(issue.get("body") or "")
        name = board.camel_name(form.get("name") or issue.get("title", ""))
        if name:
            out.setdefault(name, f"#{issue['number']}")
    return out


def file_many(ctx, entries: list[dict], yes: bool = False) -> list[str]:
    known, out = known_names(ctx), []
    for k, fields in enumerate(entries, 1):
        name = target_name(fields)
        if not fields.get("statement") or not fields.get("why") or not name:
            out.append(f"entry {k}: needs a statement, a why and a usable name; skipped")
            continue
        if name in known:
            out.append(f"{name}: already on the board ({known[name]}); skipped")
            continue
        known[name] = "this list"
        payload = issue_payload(ctx.cfg, fields)
        if not yes:
            out.append(f"{name}: would file {payload['title']!r} (dry run; pass --yes)")
            continue
        created = ctx.gh.write("POST", f"repos/{ctx.cfg.repositories.board_repo()}/issues", payload)
        out.append(f"{name}: filed #{(created or {}).get('number', '?')} {payload['title']!r}")
    return out
