"""The dependency label (`roadmaps dependency-labels`), recomputed from the open pull requests as they are.

The label (`--label`, else `[prreview] dependency_label`; `awaiting-dependencies` on the content
repository) is on an open roadmap pull request exactly while it waits for another pull request. Every
run works it out again from the current state, and nothing is kept between runs:

* every open pull request that changes a file under the roadmap directories, its head fetched into the
  host's mirror for this run (`openroadmaps.refresh(force=True, any_file=True)`); a refresh that fails
  stops the run before anything is written;
* every open pull request's current description, and the state (and, once closed, the added files) of
  every other pull request a description names, read in batches (`pull_states`);
* main as it is.

What a pull request waits for is `overlaps.dependency_record` of each roadmap folder it touches
(`roadmap_folders`, by full path: the folder of every README.md, Suggested.lean or metadata.toml it
changes, and the deepest folder holding a README.md above any other Lean file it changes; a progress
report touches none), read at its head: the README and every
Lean file of the folder, a nested roadmap's aside:

* every supplier roadmap its README names or its Lean files import that is not on main (with the open
  pull requests that add it, or none: a roadmap that exists nowhere waits too), and every open revision
  whose numbering it cites, less what the folder's own README already cites on main or at the merge
  base, supplier by supplier and id by id (`record_of`: a pin bump does not wait for a supplier its
  roadmap has long named), and except a supplier that a roadmap on main newly names: the dependency
  check rejects that as an UPWARD edge, which the author removes rather than waits on (the run logs
  it);
* every dependency its description declares (its `<!-- depends-on -->` block, then its prose), until
  `met`: a pull request that merged, or the one that added a roadmap another pull request has since
  brought to main (`added_at_base`: no folder of that name at its merge base); one closed without
  merging that did not add its roadmap never brings what is cited, so it still waits.

So a head whose AI acceptance was carried over a small repair, a description edited since the last
review, a supplier opened after it, or the first run on a backlog, are all read as they are now.

The label is added while a pull request waits for something and removed once it waits for nothing. A
pull request whose description, or the state of a pull request its description names, GitHub did not
give keeps its label as it is; only GitHub's `HTTP 404` says that a number is no pull request. A merged
pull request that still carries the label loses it.
"""

from __future__ import annotations

import re
import subprocess
from urllib.parse import quote

from . import openroadmaps, overlaps
from .gh import GHError

BATCH = 40
NOT_FOUND = re.compile(r"\(HTTP 404\)|\bHTTP 404\b")          # the status, never a pull request numbered 404
QUERY = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { %s } }"
BODY = "p%d: issueOrPullRequest(number: %d) { __typename ... on PullRequest { state body } }"
FILES = ("p%d: issueOrPullRequest(number: %d) { __typename ... on PullRequest { state "
         "files(first: 100) { totalCount nodes { path changeType } } } }")
STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def rest_state(gh, repo: str, number: int, files: bool = False) -> dict | None:
    """{state, body} (with `files`: {state, added}, the paths it adds) of one pull request by REST;
    {"state": "absent"} when GitHub answers HTTP 404 (no such pull request: an issue's number, say);
    None when it gave no answer (a network error, a 5xx, a 403)."""
    try:
        data = gh.api(f"repos/{repo}/pulls/{number}") or {}
        listing = gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True) or [] if files else []
        changes = {status: [f.get("filename", "") for f in listing if f.get("status") == status]
                   for status in ("added", "removed")}
    except GHError as exc:
        return {"state": "absent"} if NOT_FOUND.search(str(exc)) else None
    merged = data.get("merged") or data.get("merged_at")
    state = "open" if data.get("state") == "open" else "merged" if merged else "closed"
    return {"state": state, **changes} if files else {"state": state, "body": data.get("body") or ""}


def pull_states(gh, repo: str, numbers, *, files: bool = False, batch: int = BATCH) -> dict[int, dict | None]:
    """number -> {"state": "open" | "merged" | "closed", "body": ...} for a pull request (with `files`:
    "added", the paths it adds, instead of "body"), {"state": "absent"} for an issue's number or one
    GitHub says does not exist, None when GitHub did not answer. One GraphQL query per `batch`
    numbers; a batch the query fails for, a number it leaves empty, and a pull request with more than
    100 files are read one by one by REST (`rest_state`)."""
    owner, name = repo.split("/", 1)
    wanted = sorted({int(n) for n in numbers})
    out: dict[int, dict | None] = {}
    for i in range(0, len(wanted), batch):
        chunk = wanted[i:i + batch]
        try:
            data = gh.graphql(QUERY % " ".join((FILES if files else BODY) % (n, n) for n in chunk),
                              owner=owner, name=name) or {}
            nodes = data.get("repository") or {}
        except GHError:
            nodes = None
        for n in chunk:
            node = nodes.get(f"p{n}") if nodes is not None else None
            listing = (node or {}).get("files") or {}
            if not node or (files and int(listing.get("totalCount") or 0) > len(listing.get("nodes") or [])):
                out[n] = rest_state(gh, repo, n, files)
            elif node.get("__typename") != "PullRequest":
                out[n] = {"state": "absent"}
            elif files:
                out[n] = {"state": STATES.get(node.get("state") or "", "closed"),
                          **{key: [f.get("path", "") for f in listing.get("nodes") or [] if f.get("changeType") == kind]
                             for key, kind in (("added", "ADDED"), ("removed", "DELETED"))}}
            else:
                out[n] = {"state": STATES.get(node.get("state") or "", "closed"), "body": node.get("body") or ""}
    return out


def _tree(state, ref: str, paths) -> list[str]:
    result = subprocess.run(["git", "-C", str(openroadmaps.mirror(state)), "ls-tree", "-r", "--name-only", ref, "--",
                             *paths], capture_output=True, text=True, check=False)
    return result.stdout.splitlines() if result.returncode == 0 else []


def roadmap_folders(state, index: dict, pr: dict) -> dict[str, list[str]]:
    """Roadmap folder -> the paths at the pull request's head of its README.md and of every Lean file in
    it, a nested roadmap's (a subfolder holding its own README.md) aside, for each roadmap folder
    the pull request touches: the folder of every README.md, Suggested.lean or metadata.toml it changes
    (`overlaps.folders_of`, by full path, so two folders of the same name are both here), and for every
    other Lean file it changes under the roadmap directories, the deepest folder above it that holds a
    README.md at the head (a progress report touches none). A folder without a README.md at the head (the pull request
    deletes the roadmap) waits for nothing."""
    ref, dirs = openroadmaps.pr_ref(pr["pr"]), tuple(index.get("dirs", []))
    folders = set(overlaps.folders_of(pr, dirs))
    others = [f["path"] for f in pr.get("files") or [] if f.get("path", "").split("/")[0] in dirs
              and f.get("path", "").endswith(".lean") and f.get("path", "").rsplit("/", 1)[0] not in folders]
    if others:
        listing = set(_tree(state, ref, sorted({p.split("/")[0] for p in others})))
        for path in others:
            parts = path.split("/")[:-1]
            for depth in range(len(parts), 1, -1):
                if "/".join(parts[:depth]) + "/README.md" in listing:
                    folders.add("/".join(parts[:depth]))
                    break
    out = {}
    for folder in sorted(folders):
        files = _tree(state, ref, [folder + "/"])
        if folder + "/README.md" not in files:
            continue
        nested = {f.rsplit("/", 1)[0] for f in files if f.endswith("/README.md") and f.rsplit("/", 1)[0] != folder}
        leans = [f for f in files if f.endswith(".lean") and not any(f.startswith(n + "/") for n in nested)]
        out[folder] = [folder + "/README.md", *leans]
    return out


def _texts(state, ref: str, paths: list[str]) -> tuple[str, str]:
    """The README and the Lean files (joined) of a roadmap folder at `ref`."""
    texts = overlaps._batch(state, [f"{ref}:{path}" for path in paths])
    return texts[0] or "", "\n".join(t or "" for t in texts[1:])


def record_of(state, index: dict, pr: dict, body: str, changed) -> dict:
    """What the pull request brings that waits: the union of `overlaps.dependency_record` over the roadmap
    folders it touches, read at its head (`roadmap_folders`: every Lean file of a folder with its
    README). A supplier the same README already names on main or at the merge base, or an id of an
    open revision it already cites there, is not this pull request's to wait for (a pin bump or a
    progress edit of a roadmap whose README names an open supplier does not wait for it), unless its
    Lean files import the supplier or its description declares it. The same README is the folder's own,
    at its path; a folder new at its path has none, unless the pull request moves a roadmap there
    (`overlaps.rename_source`).

    A roadmap on main never waits for one that is not: the dependency check rejects such an edge as
    UPWARD (`depgraph.check_order`), and the remedy is to drop it, the newer roadmap consuming the one
    on main if anything, never to hold the roadmap on main back. So a supplier not on main that the
    README or Lean of a folder on main newly names is `upward` (roadmap -> {prs, on}), not a wait; one
    its description alone declares still waits."""
    ref = openroadmaps.pr_ref(pr["pr"])
    main_ref = f"refs/heads/{index.get('branch', 'main')}"
    base = overlaps._merge_base(state, main_ref, ref)
    kept = {r for _n, r in overlaps.declared_items(body, own=pr["pr"]) if r}
    suppliers: dict[str, set[int]] = {}
    upward: dict[str, dict[str, set]] = {}
    revisions: set[tuple[int, str]] = set()
    declared: set[tuple[int, str | None]] = set()
    main = set(index.get("main_roadmaps", []))
    for folder, paths in roadmap_folders(state, index, pr).items():
        name = folder.rsplit("/", 1)[-1]
        readme, lean = _texts(state, ref, paths)
        record = overlaps.dependency_record(state, index, readme, lean, name, pr=pr["pr"], body=body, changed=changed)
        earlier = [main_ref] + ([base] if base else [])
        texts = overlaps._batch(state, [f"{at}:{paths[0]}" for at in earlier])
        source = (overlaps.rename_source(state, base, ref, folder, pr.get("files") or [],
                                         index.get("main_folders") or {}, tuple(index.get("dirs", [])))
                  if not any(texts) else None)
        if source:
            texts = overlaps._batch(state, [f"{at}:{source}/README.md" for at in earlier])
        had: set[str] = set()
        cited: set[tuple[int, str]] = set()
        for text in texts:
            if text:
                before = overlaps.dependency_record(state, index, text, "", name, pr=pr["pr"], changed=changed)
                had |= {s["roadmap"] for s in before["suppliers"]}
                cited |= {(r["pr"], i) for r in before["revisions"] for i in r["ids"]}
        on_main = name in main and any(texts)
        had -= kept | set(overlaps.LEAN_IMPORT.findall(lean))
        for s in record["suppliers"]:
            if s["roadmap"] in had:
                continue
            if on_main and s["cited"]:
                got = upward.setdefault(s["roadmap"], {"prs": set(), "on": set()})
                got["prs"].update(s["prs"])
                got["on"].add(name)
            else:
                suppliers.setdefault(s["roadmap"], set()).update(s["prs"])
        revisions |= {(r["pr"], r["roadmap"]) for r in record["revisions"]
                      if any((r["pr"], i) not in cited for i in r["ids"])}
        declared |= {(d["pr"], d["roadmap"]) for d in record["declared"]}
    return {"suppliers": suppliers, "revisions": revisions, "declared": declared, "upward": upward}


def added_at_base(state, index: dict, number: int, roadmap: str) -> bool:
    """Whether the open pull request `number` was opened to add `roadmap`, which is on main now: no
    folder of that name holds a README.md anywhere under the roadmap directories at the merge base of
    main and its head (wherever main keeps it now: a roadmap main has since moved to Completed/ was
    there all along)."""
    main_ref = f"refs/heads/{index.get('branch', 'main')}"
    base = overlaps._merge_base(state, main_ref, openroadmaps.pr_ref(number))
    if not base:
        return False
    listing = _tree(state, base, index.get("dirs", []))
    return bool(listing) and not any(p.endswith(f"/{roadmap}/README.md") for p in listing)


def met(number: int, roadmap: str | None, state, index: dict, open_prs: set[int],
        states: dict[int, dict | None]) -> bool | None:
    """Whether a declared dependency (a pull request, with the roadmap a block item names when that is
    on main) is met: True, False, or None when GitHub did not say. A merged pull request is met, and so
    is a number that is no pull request. The pull request that added a roadmap (`added_at_base` while
    open, its added files once closed) is met once that roadmap is on main, whichever pull request
    brought it (a closed one that also removed a README of that name moved the roadmap, and added
    nothing); any other open one, or one closed without merging, is not."""
    if number in open_prs:
        return bool(roadmap) and added_at_base(state, index, number, roadmap)
    got = states.get(number)
    if got is None:
        return None
    if got["state"] in ("merged", "absent"):
        return True
    readme = f"/{roadmap}/README.md"
    return (bool(roadmap) and any(p.endswith(readme) for p in got.get("added") or [])
            and not any(p.endswith(readme) for p in got.get("removed") or []))


def merged_with(gh, repo: str, label: str) -> list[int] | None:
    """The merged pull requests that still carry `label` (one search, at most 100), or None when the
    search was not answered."""
    try:
        found = gh.api(f"search/issues?q={quote(f'repo:{repo} is:pr is:merged label:{label}')}&per_page=100") or {}
    except GHError:
        return None
    return sorted(int(i["number"]) for i in found.get("items") or [])


def run(ctx, label: str) -> list[str]:
    """Bring `label` in line with what every open roadmap pull request waits for now (module docstring),
    and take it off merged pull requests. The log: one line per label written or left alone for want of
    an answer."""
    cfg = ctx.cfg
    repo, state = cfg.repositories.content, cfg.state_dir()
    index = openroadmaps.refresh(ctx.gh, cfg, force=True, any_file=True)
    prs = index.get("prs", [])
    open_prs = {p["pr"] for p in prs}
    bodies = {n: (got or {}).get("body") for n, got in pull_states(ctx.gh, repo, open_prs).items()}
    changed = overlaps.revisions(state, index)
    records = {p["pr"]: record_of(state, index, p, bodies[p["pr"]], changed) for p in prs
               if bodies.get(p["pr"]) is not None}
    named = {n for r in records.values() for n, _roadmap in r["declared"]} - open_prs
    states = pull_states(ctx.gh, repo, named, files=True)
    out = []

    def write(method: str, number: int, why: str) -> None:
        path = f"repos/{repo}/issues/{number}/labels"
        try:
            if method == "POST":
                ctx.gh.write("POST", path, {"labels": [label]})
            else:
                ctx.gh.write("DELETE", f"{path}/{quote(label, safe='')}")
            out.append(f"#{number}: {label} {'added' if method == 'POST' else 'removed'} ({why})")
        except GHError as exc:
            out.append(f"#{number}: {label} not {'added' if method == 'POST' else 'removed'} "
                       f"({str(exc).splitlines()[0][:160]})")

    for p in sorted(prs, key=lambda x: x["pr"]):
        number, labelled = p["pr"], label in p.get("labels", [])
        record = records.get(number)
        if record is None:
            if labelled:
                out.append(f"#{number}: {label} left as it is (its description was not read)")
            continue
        waits = {f"{s} ({', '.join(f'#{n}' for n in sorted(adders)) or 'in no open pull request'})"
                 for s, adders in record["suppliers"].items()}
        waits |= {f"#{n} (revision of {r})" for n, r in record["revisions"]}
        known = True
        for n, roadmap in record["declared"]:
            ok = met(n, roadmap, state, index, open_prs, states)
            if ok is None:
                known = False
            elif not ok:
                waits.add(f"#{n} (declared{'' if n in open_prs else ', closed without merging'})")
        waits = sorted(waits)
        for s, got in sorted(record.get("upward", {}).items()):
            out.append(f"#{number}: UPWARD, not waited for: {', '.join(sorted(got['on']))} (on main) -> {s} "
                       f"({', '.join(f'#{n}' for n in sorted(got['prs'])) or 'in no open pull request'}); "
                       f"a roadmap on main does not wait for one that is not, so the edge goes")
        if waits and not labelled:
            write("POST", number, "waits for " + ", ".join(waits))
        elif labelled and not waits and known:
            write("DELETE", number, "it waits for no open pull request")
        elif labelled and not waits:
            out.append(f"#{number}: {label} left as it is (a pull request its description names was not read)")
    merged = merged_with(ctx.gh, repo, label)
    if merged is None:
        out.append(f"merged pull requests carrying {label}: not searched (GitHub did not answer)")
    for number in merged or []:
        write("DELETE", number, "merged")
    return out
