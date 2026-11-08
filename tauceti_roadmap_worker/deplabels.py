"""The dependency label (`roadmaps dependency-labels`), recomputed from the open pull requests as they are.

The label (`--label`, else `[prreview] dependency_label`; `awaiting-dependencies` on the content
repository) is on an open roadmap pull request exactly while it waits for another pull request. Every
run works it out again from the current state, and nothing is kept between runs:

* every open head, fetched into the host's mirror for this run (`openroadmaps.refresh(force=True)`);
  a refresh that fails stops the run before anything is written;
* every open pull request's current description, and the state of every pull request a description
  names, read in batches (`pull_states`);
* main as it is.

What a pull request waits for is `overlaps.dependency_record` of each roadmap folder it carries, at its
current head, with its current description:

* every supplier roadmap its README names or its Suggested.lean imports that is not on main (with the
  open pull requests that add it, or none: a roadmap that exists nowhere waits too);
* every open revision whose numbering it cites;
* every dependency its description declares (its `<!-- depends-on -->` block, then its prose): a roadmap
  not on main until it is on main, a pull request until it merges (one closed without merging never
  brings what is cited, so it still waits).

So a head whose AI acceptance was carried over a small repair, a description edited since the last
review, a supplier opened after it, or the first run on a backlog, are all read as they are now.

The label is added while a pull request waits for something and removed once it waits for nothing. A
pull request whose description, or the state of a pull request its description names, GitHub did not
give (anything but a 404, which means the number is no pull request) keeps its label as it is. A merged
pull request that still carries the label loses it.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from . import openroadmaps, overlaps
from .gh import GHError

BATCH = 40
NOT_FOUND = re.compile(r"\b404\b|Not Found")
QUERY = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { %s } }"
ALIAS = "p%d: issueOrPullRequest(number: %d) { __typename ... on PullRequest { state body } }"
STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def rest_state(gh, repo: str, number: int) -> dict | None:
    """{state, body} of one pull request by REST; {"state": "absent"} when GitHub says 404 (no such pull
    request: an issue's number, say); None when it gave no answer (a network error, a 5xx, a 403)."""
    try:
        data = gh.api(f"repos/{repo}/pulls/{number}") or {}
    except GHError as exc:
        return {"state": "absent"} if NOT_FOUND.search(str(exc)) else None
    merged = data.get("merged") or data.get("merged_at")
    state = "open" if data.get("state") == "open" else "merged" if merged else "closed"
    return {"state": state, "body": data.get("body") or ""}


def pull_states(gh, repo: str, numbers, batch: int = BATCH) -> dict[int, dict | None]:
    """number -> {"state": "open" | "merged" | "closed", "body": ...} for a pull request, {"state":
    "absent"} for an issue's number or one GitHub says does not exist, None when GitHub did not answer.
    One GraphQL query per `batch` numbers; a batch the query fails for, and a number it leaves empty,
    are read one by one by REST (`rest_state`)."""
    owner, name = repo.split("/", 1)
    wanted = sorted({int(n) for n in numbers})
    out: dict[int, dict | None] = {}
    for i in range(0, len(wanted), batch):
        chunk = wanted[i:i + batch]
        try:
            data = (gh.graphql(QUERY % " ".join(ALIAS % (n, n) for n in chunk), owner=owner, name=name) or {})
            nodes = data.get("repository") or {}
        except GHError:
            nodes = None
        for n in chunk:
            node = nodes.get(f"p{n}") if nodes is not None else None
            if not node:
                out[n] = rest_state(gh, repo, n)
            elif node.get("__typename") != "PullRequest":
                out[n] = {"state": "absent"}
            else:
                out[n] = {"state": STATES.get(node.get("state") or "", "closed"), "body": node.get("body") or ""}
    return out


def waiting_for(state, index: dict, pr: dict, body: str, changed, states: dict[int, dict | None],
                open_prs: set[int]) -> tuple[list[str], bool]:
    """(what the open pull request `pr` waits for, one phrase each; whether that is known): the
    dependency records of every roadmap folder it carries, read at its head in the mirror."""
    folders = sorted((pr.get("folders") or {}).items())
    texts = overlaps._batch(state, [f"{openroadmaps.pr_ref(pr['pr'])}:{folder}/{file}" for _n, folder in folders
                                    for file in ("README.md", "Suggested.lean")])
    waits: set[str] = set()
    known = True
    for i, (name, _folder) in enumerate(folders):
        record = overlaps.dependency_record(state, index, texts[2 * i] or "", texts[2 * i + 1] or "", name, pr=pr["pr"],
                                            body=body, changed=changed)
        for s in record["suppliers"]:
            waits.add(f"{s['roadmap']} ({', '.join(f'#{n}' for n in s['prs']) or 'in no open pull request'})")
        for r in record["revisions"]:
            waits.add(f"#{r['pr']} (revision of {r['roadmap']})")
        for n in record["declared"]:
            got = {"state": "open"} if n in open_prs else states.get(n)
            if got is None:
                known = False
            elif got["state"] == "open":
                waits.add(f"#{n} (declared)")
            elif got["state"] == "closed":
                waits.add(f"#{n} (declared, closed without merging)")
    return sorted(waits), known


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
    index = openroadmaps.refresh(ctx.gh, cfg, force=True)
    prs = index.get("prs", [])
    open_prs = {p["pr"] for p in prs}
    current = pull_states(ctx.gh, repo, open_prs)
    bodies = {n: (got or {}).get("body") for n, got in current.items()}
    named = {n for p in prs if bodies.get(p["pr"]) is not None
             for n in overlaps.declared_dependencies(bodies[p["pr"]], own=p["pr"])}
    states = pull_states(ctx.gh, repo, named - open_prs)
    changed = overlaps.revisions(state, index)
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
        body = bodies.get(number)
        if body is None:
            if labelled:
                out.append(f"#{number}: {label} left as it is (its description was not read)")
            continue
        waits, known = waiting_for(state, index, p, body, changed, states, open_prs)
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
