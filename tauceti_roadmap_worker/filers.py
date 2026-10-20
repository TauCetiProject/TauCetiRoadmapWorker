"""Who may file targets: `[board] target_filers`, logins or teams (`team:<org>/<slug>`).

`board sync` turns a target issue into a job only when its author may file targets; an issue from
anyone else is closed with one comment and the `not-a-filer` label, never a job. `targets file`
and `file-many` check the running login the same way. A team's members are read through the API
and kept in the state directory for an hour. A token that cannot read the organisation (the
Actions token of the board's workflow) falls back to the last list it read, then to
`board/filers.json` in the board repository, which every run that can read the team keeps up to
date. When the filers cannot be resolved at all, nothing is closed: the issue waits, with a warning.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .gh import GHError

CACHE_SECONDS = 3600
BOARD_FILE = "board/filers.json"
LABEL = "not-a-filer"


def cache_path(state: Path) -> Path:
    return Path(state) / "filers.json"


def refusal(entries: list[str]) -> str:
    teams = [e[5:] for e in entries if e.startswith("team:")]
    if teams:
        org, _, slug = teams[0].partition("/")
        return f"Targets are filed by members of the {org} {slug} team for now; ask a maintainer."
    return "Targets are filed only by the board's listed filers for now; ask a maintainer."


def _read_cache(state: Path) -> dict:
    try:
        data = json.loads(cache_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _board_file(ctx) -> tuple[dict, str | None]:
    try:
        current = ctx.gh.api(f"repos/{ctx.cfg.repositories.board_repo()}/contents/{BOARD_FILE}")
        return json.loads(base64.b64decode(current["content"]).decode("utf-8")), current.get("sha")
    except (GHError, KeyError, ValueError, TypeError):
        return {}, None


def team_members(ctx, team: str, now: float | None = None) -> tuple[set[str] | None, str]:
    """(logins, where they came from) for `org/slug`, or (None, why not)."""
    now = time.time() if now is None else now
    cache = _read_cache(ctx.state)
    entry = cache.get(team) or {}
    if entry and now - float(entry.get("at", 0)) < CACHE_SECONDS:
        return {m.lower() for m in entry.get("members", [])}, "cache"
    org, _, slug = team.partition("/")
    try:
        members = sorted(m["login"] for m in ctx.gh.api(f"orgs/{org}/teams/{slug}/members?per_page=100", paginate=True))
    except (GHError, KeyError, TypeError) as exc:
        if entry:
            return {m.lower() for m in entry.get("members", [])}, f"cache from before ({exc})"
        published, _sha = _board_file(ctx)
        if team in published:
            return {m.lower() for m in published[team]}, f"{BOARD_FILE} ({exc})"
        return None, f"team {team} unreadable and no cached list ({exc})"
    cache[team] = {"at": now, "members": members}
    cache_path(ctx.state).parent.mkdir(parents=True, exist_ok=True)
    cache_path(ctx.state).write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    publish(ctx, team, members)
    return {m.lower() for m in members}, "api"


def publish(ctx, team: str, members: list[str]) -> None:
    """Keep `board/filers.json` in the board repository current, for runs whose token cannot read
    the team (best effort)."""
    published, sha = _board_file(ctx)
    if published.get(team) == members:
        return
    published[team] = members
    payload = {"message": f"Board: target filers of {team}",
               "content": base64.b64encode((json.dumps(published, indent=1, sort_keys=True) + "\n").encode()).decode()}
    if sha:
        payload["sha"] = sha
    try:
        ctx.gh.write("PUT", f"repos/{ctx.cfg.repositories.board_repo()}/contents/{BOARD_FILE}", payload)
    except GHError:
        pass


def allowed(ctx, login: str) -> tuple[bool | None, str]:
    """(True | False | None, note): whether `login` may file targets; None when the filers cannot
    be resolved (then nothing is decided)."""
    entries = list(ctx.cfg.board.target_filers)
    if not entries:
        return True, "anyone may file targets"
    who, notes = (login or "").lower(), []
    for entry in entries:
        if not entry.startswith("team:"):
            if entry.lower() == who:
                return True, f"listed as {entry}"
            continue
        members, where = team_members(ctx, entry[5:])
        if members is None:
            notes.append(where)
        elif who in members:
            return True, f"member of {entry[5:]} ({where})"
    if notes:
        return None, "; ".join(notes)
    return False, refusal(entries)
