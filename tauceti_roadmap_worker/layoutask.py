"""`tauceti-roadmap layout --ask`: suggest to the author of a pull request to bring its READMEs into the
README layout, with the AI review's label off the pull request meanwhile.

The pool reorders its own pull requests (`relayout`) and rewrites their openings (`overview`). A pull
request of another author is theirs to change, so the command suggests: one comment on the pull
request that lists, for each roadmap folder it adds or changes, what the README lacks (`layout.lacks`:
the overview, sections, the order), with a record (`REQUEST_MARK`). The suggestions are kept in
`<state>/layout-requests.json`.

While a suggestion stands, `intake.sync_pr_reviews` takes `[prreview] label` off the pull request and
does not put it back (`holds`): the reviews go on as usual, and the label follows them again once the
READMEs at the head lack nothing the suggestion named, of the kinds `[prreview] layout_hold` names. The
suggestion is then settled and leaves the file. A push that only reorders the sections keeps an earlier
acceptance (`intake.LayoutCheck`), so an author who took up a suggestion about the order alone gets the
label back with no new review.

It is a suggestion: an author who replies that the README stays as it is gets the label back. That is
the operator's reading of the reply, never the worker's: `layout --ask --withdraw --pr N` marks the
suggestion withdrawn in the file (`withdraw`), and the label follows the reviews again. The record stays
while the pull request is open (`declined`), so that a later `layout --ask --others` does not make the
suggestion a second time to an author who said no.

Nothing is suggested on a pull request of the pool, on one the pull-request reviews do not cover
(`[prreview] authors`, `skip_authors`), on one this host reviews no pull requests of
(`[worker] no_review_authors`), nor twice on the same pull request, nor on one whose author declined.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import intake, layout, openroadmaps, prbatch
from .gh import GHError, GHHalt, GHRateLimited

REQUEST_MARK = "tauceti-roadmap-layout-request"
FILE = "layout-requests.json"
RULES = "https://github.com/{repo}/blob/{branch}/README.md#laying-out-the-readme"


def load(state: Path) -> dict[str, dict]:
    try:
        data = json.loads((Path(state) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save(state: Path, requests: dict[str, dict]) -> None:
    path = Path(state) / FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(requests, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def declined(request: dict) -> bool:
    """The suggestion was withdrawn (`withdraw`): its author replied that the README stays as it is."""
    return bool(request.get("withdrawn"))


def readme_paths(cfg, folders) -> dict[str, str]:
    active = cfg.roadmap.active_dir.strip("/")
    return {folder: f"{active}/{folder}/README.md" for folder in folders}


def lacking(ctx, repo: str, head: str, folders, kinds, batch: int) -> dict[str, dict[str, str]] | None:
    """folder -> what its README at `head` lacks of `kinds`; None when GitHub did not give a README the
    answer needs. A folder whose README is not there (any more) lacks nothing that can be asked for."""
    paths = readme_paths(ctx.cfg, folders)
    texts = prbatch.blob_texts(ctx.gh, repo, [(head, path) for path in paths.values()], batch)
    out = {}
    for folder, path in paths.items():
        if (head, path) not in texts:
            return None
        text = texts[(head, path)]
        found = {k: v for k, v in layout.lacks(text).items() if k in kinds} if text else {}
        if found:
            out[folder] = found
    return out


def note(cfg, found: dict[str, dict[str, str]], head: str) -> str:
    """The comment: what is asked, of which README, and what happens to the label."""
    active = cfg.roadmap.active_dir.strip("/")
    label = cfg.prreview.label
    rules = RULES.format(repo=cfg.repositories.content, branch=cfg.project.branch)
    ask = (f"**A suggestion on the README layout.** The roadmaps of this project are moving to one layout "
           f"([Laying out the README]({rules})): under the title an overview that begins `**Summary.**`, then "
           "`## Conventions` and the `## Layer k: …` sections in build order, then `## Scope and ownership`, "
           "`## Exact supplier contracts` and `## References` last. We suggest bringing the "
           f"README{'s' if len(found) != 1 else ''} of this pull request into it. What that would take today:")
    lines = [ask, ""]
    for folder, kinds in found.items():
        lines.append(f"- `{active}/{folder}/README.md`: " + "; ".join(kinds[k] for k in layout.NEEDS if k in kinds) + ".")
    record = {"head": head, "needs": {folder: sorted(kinds) for folder, kinds in found.items()}}
    held = (f"The `{label}` label is off this pull request for now: it comes back by itself once the README is in "
            "the layout and the AI review accepts that head (a push that only reorders the sections keeps an "
            "earlier acceptance). " if label else "")
    form = ("This is about form only: nothing here questions the mathematics. " + held
            + "If you would rather leave the README as it is, reply here saying so"
            + (", and the label is put back as it was." if label else "."))
    lines += ["", form, "", f"<!-- {REQUEST_MARK} {json.dumps(record, sort_keys=True)} -->", ""]
    return "\n".join(lines)


def holds(ctx, repo: str, listed: list[dict], batch: int) -> tuple[dict[int, str], list[str]]:
    """(pull request -> why the label is held, log lines): the requests that stand. One is dropped when
    its pull request is no longer open, and settled when the READMEs at the head lack nothing it asked
    for of the kinds `[prreview] layout_hold` names. A README GitHub did not give leaves the request
    standing. A suggestion its author declined holds nothing; its record goes when the pull request does."""
    state = getattr(ctx, "state", None)          # a board run with no state directory holds nothing
    requests = load(state) if state else {}
    if not requests:
        return {}, []
    kinds = set(ctx.cfg.prreview.layout_hold)
    by_number = {int(e["pr"]): e for e in listed}
    held, lines, changed = {}, [], False
    for key in sorted(requests, key=int):
        number, request = int(key), requests[key]
        entry = by_number.get(number)
        if entry is None:
            del requests[key]
            changed = True
            if not declined(request):
                lines.append(f"#{number}: layout request dropped (the pull request is no longer open)")
            continue
        if declined(request):
            continue
        asked = request.get("needs") or {}
        want = {k for ks in asked.values() for k in ks} & kinds
        found = lacking(ctx, repo, entry.get("head", ""), list(asked), want, batch) if want else {}
        if found is None:
            held[number] = "a layout request stands (its READMEs could not be read this time)"
        elif found:
            held[number] = ("a layout request stands: " + "; ".join(
                f"{folder} lacks " + ", ".join(k for k in layout.NEEDS if k in ks) for folder, ks in found.items()))
        else:
            del requests[key]
            changed = True
            lines.append(f"#{number}: layout request settled (the README is in the layout at {entry.get('head', '')[:7]})")
    if changed:
        save(state, requests)
    return held, lines


def withdraw(ctx, numbers, *, write: bool = False, report=print, now=None) -> list[str]:
    """Take back the suggestions made on these pull requests (an author replied that the README stays as
    it is): each is marked withdrawn in the requests file, so that the label follows the reviews again at
    the next `board sync` and the suggestion is not made again, and with `write` a short note says so on
    the pull request."""
    cfg, repo, out = ctx.cfg, ctx.cfg.repositories.content, []
    requests = load(ctx.state)
    for number in dict.fromkeys(int(n) for n in numbers):
        if str(number) not in requests or declined(requests[str(number)]):
            out.append(f"#{number}: no suggestion stands")
        elif not write:
            out.append(f"[dry run] #{number}: would withdraw the suggestion of {str(requests[str(number)].get('head', ''))[:7]}")
        else:
            label = cfg.prreview.label
            text = ("Understood: the README stays as it is. The layout suggestion above is withdrawn"
                    + (f", and the `{label}` label follows the AI review again." if label else "."))
            try:
                ctx.gh.write("POST", f"repos/{repo}/issues/{number}/comments", {"body": text + "\n"})
            except GHError as exc:
                out.append(f"#{number}: not withdrawn: {str(exc).splitlines()[0][:200]}")
                continue
            requests[str(number)] = {"head": requests[str(number)].get("head", ""),
                                     "withdrawn": now() if now else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            save(ctx.state, requests)
            out.append(f"#{number}: suggestion withdrawn; the label follows the reviews again")
    for line in out:
        report(line)
    return out


def selected(ctx, listed: list[dict], numbers: list[int], authors: list[str], others: bool) -> list[dict]:
    """The listing entries asked for: the pull requests named, those of the authors named, and with
    `others` every pull request of an author outside the pool that the pull-request reviews cover."""
    cfg = ctx.cfg
    pool = {str(a).lower() for a in cfg.roadmap.pool}
    quiet = {str(a).lower() for a in getattr(cfg.worker, "no_review_authors", None) or []}
    excepted = {int(n) for n in getattr(cfg.worker, "no_review_except", None) or []}
    by_number = {int(e["pr"]): e for e in listed}
    out = [by_number.get(int(n)) or {"pr": int(n), "missing": True} for n in dict.fromkeys(numbers)]
    who = {a.lower() for a in authors}
    for e in listed:
        author, number = str(e.get("author") or "").lower(), int(e["pr"])
        if number in numbers or author in pool:
            continue
        if author in who or (others and intake.author_reviewed(cfg.prreview, author)
                             and (author not in quiet or number in excepted)):
            out.append(e)
    return out


def run(ctx, *, numbers=(), authors=(), others: bool = False, kinds=layout.NEEDS, write: bool = False,
        pace: float = 2.0, limit: int = 0, again: bool = False, report=print, sleep=time.sleep, now=None) -> list[str]:
    """The command: for every pull request asked for, say what its READMEs lack of `kinds`, and with
    `write` post the request and record it, `pace` seconds apart, at most `limit` (0: all). A pull
    request already asked, or whose author declined the suggestion, is left alone unless `again`. A
    rate-limit answer stops the run."""
    cfg, repo, out = ctx.cfg, ctx.cfg.repositories.content, []
    batch = cfg.board.graphql_batch

    def say(line: str) -> None:
        out.append(line)
        report(line)
    listed = openroadmaps.inventory(ctx.gh, repo, (cfg.roadmap.active_dir,), any_file=True,
                                    skip=cfg.prreview.aggregate_prs)
    entries = selected(ctx, listed, list(numbers), list(authors), others)
    if not entries:
        say("no pull request selected")
        return out
    on_main = openroadmaps.readme_folders(ctx.gh, repo, cfg.project.branch, cfg.roadmap.active_dir)
    pool = {str(a).lower() for a in cfg.roadmap.pool}
    requests = load(ctx.state)
    asked = 0
    for entry in entries:
        number = int(entry["pr"])
        who = f"#{number}" + (f" ({entry.get('author')})" if entry.get("author") else "")
        try:
            if entry.get("missing"):
                say(f"{who}: refused: not an open roadmap pull request")
                continue
            if str(entry.get("author") or "").lower() in pool:
                say(f"{who}: refused: a pull request of the pool is reordered, not asked (`layout`, `overview`)")
                continue
            if str(number) in requests and not again:
                if declined(requests[str(number)]):
                    say(f"{who}: not asked: the author declined the suggestion ({requests[str(number)]['withdrawn']})")
                else:
                    say(f"{who}: already asked at {str(requests[str(number)].get('head', ''))[:7]}")
                continue
            folders, why = intake.review_plan(entry, cfg, on_main)
            changed = [f for f, change in folders.items() if change]
            if not changed:
                say(f"{who}: nothing to ask: " + (why or "no roadmap README it adds or changes"))
                continue
            head = str(entry.get("head") or "")
            found = lacking(ctx, repo, head, changed, set(kinds), batch)
            if found is None:
                say(f"{who}: not asked: its READMEs could not be read")
                continue
            if not found:
                say(f"{who}: nothing to ask: in the layout")
                continue
            what = "; ".join(f"{folder} lacks " + ", ".join(k for k in layout.NEEDS if k in ks) for folder, ks in found.items())
            if not write:
                say(f"[dry run] {who}: would ask: {what}")
                continue
            if limit and asked >= limit:
                say(f"{who}: not asked this run (--limit {limit})")
                continue
            if asked:
                sleep(pace)
            try:
                got = ctx.gh.write("POST", f"repos/{repo}/issues/{number}/comments", {"body": note(cfg, found, head)})
            except GHError as exc:
                say(f"{who}: not asked: {str(exc).splitlines()[0][:200]}")
                continue
            requests[str(number)] = {"head": head, "needs": {f: sorted(ks) for f, ks in found.items()},
                                     "comment": (got or {}).get("id") if isinstance(got, dict) else None,
                                     "asked": (now() if now else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))}
            save(ctx.state, requests)
            asked += 1
            say(f"{who}: asked: {what}")
        except (GHRateLimited, GHHalt) as exc:
            say(f"stopped at #{number}: {str(exc).splitlines()[0][:200]}; {asked} asked, run again later")
            break
    if write:
        say(f"{asked} pull request{'s' if asked != 1 else ''} asked")
    return out
