"""The supplier graph between roadmaps, its check against the merge order, and the merge chain.

Ported from the maintainer's decycle/depcheck_units.py, depcheck.py and merge_order.py, as pure
functions over README texts (no /tmp lists, no hard-coded pull requests or bundles):

* `units(text)` yields the README lines and clauses that name suppliers: `*Needs:*` clauses (cut
  at their first sentence end), lines saying "Supplied by", "supplier", "prerequisites are", rows
  of supplier tables, supplier-contract bullets, `### From <Name>` headings and the sentences of a
  `### Dependencies` section, minus anything that describes consumers: a consumers section
  (`## Downstream consumers` and every heading nested in it; any heading that says "consum",
  "export", "outbound" or "downstream" up to the next heading) and, in a prose line, a sentence
  "X consumes ..." of another roadmap X.
* `supplier_edges(readmes, ...)` turns those into edges consumer -> supplier. A roadmap the README
  names as its consumer (`declared_consumers`: listed in a consumers section, "X consumes ...",
  "consumed by X") is a supplier only where an explicit supplier statement names it (a `*Needs:*`
  clause, a `### From` heading, a supplier table or contract bullet, a `### Dependencies`
  sentence), never by a mention in a prose line that happens to say "supplier".
* `check_order(edges, order, ...)` reports UPWARD edges (a supplier later in the merge order),
  CYCLEs and roadmaps missing from the order: every one is a design defect. A roadmap already on the
  content repository's main (`merged`) is a supplier available to every pull request: an edge to it
  is never UPWARD, it is not checked as a consumer, and a cycle among such roadmaps alone is not
  reported. The merge order is the order of the open pull requests; where it lists a roadmap on
  main means nothing.
* `move_after_suppliers(...)` and `repair_upward(...)` repair an UPWARD edge of a listed roadmap by
  moving it right after its last supplier, unless one of its consumers comes no later than that
  supplier (the move would put a supplier after a consumer).
* `merge_chain(...)` is the transitive closure of a roadmap's downward suppliers among the open
  pull requests, in merge-order rank: what must merge before it. The PR paragraph carries this
  chain only, never a position number, because a position in a moving list goes stale the moment
  another pull request opens or merges.
"""

from __future__ import annotations

import re
from collections import defaultdict

EXCL = re.compile(
    r"consumed by|consumer|(?i:downstream)|\bNo\b[^.]{0,80}\binput\b|not an input|not a prerequisite|supplies to|"
    r"exports? to|\b(?:It|it|This|this|this result|which|that|section|layer|roadmap) supplies\b|supplies [A-Z][a-z]+[A-Z]|"
    r"outbound|scope boundary|specializ(?:ation|es) of|is an outbound")
DEPCONS = re.compile(
    r"consum|consumes?\b|used by|asks? for|not a prerequisite|re-?export|export|downstream|outbound|belongs? to|owned by|"
    r"kept as a hypothesis|are [A-Z][A-Za-z0-9]+ [A-Z0-9.–-]+'s", re.I)
NAME = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:PartII|PartIII|PartIV)?)\b")
HEADING = re.compile(r"(#{1,6})(?:\s|$)")
CONSUMER_HEADING = re.compile(r"consum|export|outbound|downstream", re.I)
# The section that lists a roadmap's consumers (`## Downstream consumers`, by convention), whose
# nested headings belong to it too. Other headings that say "consume" or "export" ("What this roadmap
# consumes", "Layer 7: exports") are skipped, as before, only up to the next heading.
CONSUMERS_SECTION = re.compile(r"^#{1,6}\s+(?:Other\s+)?(?:Downstream(?:\s+consumers|\s+roadmaps|\s+branch\s+roadmaps)?|"
                               r"Consumers)\s*:?\s*$", re.I)
# "X consumes ...", "X R3 consumes ...", "Y and X likewise only consume ...": X names a consumer (unless X
# is the README's own roadmap); so do the names after "consumed by".
ROADMAP = r"(?!(?:It|Its|This|That|These|Those|Which|Each|Every|The|Layers?|Sections?|Stages?|Parts?)\b)[A-Z][A-Za-z0-9]+"
LABEL = r"(?:[:\s]+(?:Layers?\s+|§)?[A-Z]{0,3}\d[\w.–/-]*)?"           # R3, :L5a, G5.5, Layer 6, §3A
CONSUMES = re.compile(rf"\b({ROADMAP}){LABEL}`?\)?,?\s+(?:(?:also|only|likewise|directly|itself|then)\s+)*consumes?\b")
CONSUMED_BY = re.compile(rf"consumed (?:only |directly |also )?by (?:the )?(?:roadmaps? )?`?({ROADMAP}(?:{LABEL}`?(?:,? and |, )`?"
                         rf"{ROADMAP})*)")
LISTED = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+[*_`]{0,3}(?:TauCetiRoadmap\.)?([A-Z][A-Za-z0-9]+)|^\s*\|\s*[*_`]{0,3}"
                    r"(?:TauCetiRoadmap\.)?([A-Z][A-Za-z0-9]+)")


def _sentences(text: str) -> list[str]:
    out, pos = [], 0
    for m in re.finditer(r"\.(\s+|$)", text):
        out.append(text[pos:m.start() + 1])
        pos = m.end()
    if text[pos:].strip():
        out.append(text[pos:])
    return out


def heading_level(line: str) -> int:
    """The level of a Markdown heading; a line that only starts with `#` (`#279 ...`, wrapped text) is
    deeper than any heading."""
    m = HEADING.match(line)
    return len(m.group(1)) if m else 7


def declared_consumers(text: str, name: str = "") -> set[str]:
    """The roadmaps a README names as its consumers: the entries listed in a consumers section (the
    names that open its bullets and table rows, and its nested headings), the subject of "X
    consumes ..." and the names after "consumed by". `name` (the README's own roadmap) is never one."""
    found: set[str] = set()
    level = 0
    for line in text.split("\n"):
        if line.startswith("#"):
            depth = heading_level(line)
            if level and depth > level:
                found.update(NAME.findall(line))
                continue
            level = depth if CONSUMERS_SECTION.match(line) else 0
            continue
        if level:
            m = LISTED.match(line)
            if m:
                found.add(m.group(1) or m.group(2))
        found.update(CONSUMES.findall(line))
        for group in CONSUMED_BY.findall(line):
            found.update(NAME.findall(group))
    return found - {name}


def units(text: str, name: str = ""):
    """Yield (line_no, text) units of a README that name its suppliers (see the module docstring).
    `name` is the README's own roadmap, when known ("<name> consumes X" names a supplier)."""
    for line_no, unit, _prose in _units(text, name):
        yield line_no, unit


def _units(text: str, name: str = ""):
    """`units` as (line_no, text, prose): `prose` marks a line taken only because it says "supplier"
    (or "supplies", "Supplied by", "prerequisites are"), the one kind of unit that also mentions
    roadmaps in other roles."""
    lines = text.split("\n")
    sec_supplier = sec_consumer = sec_deplist = False
    consumer_level = 0            # the level of the consumers heading whose section this is (0: none)
    buf: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if line.startswith("#"):
            level = heading_level(line)
            if consumer_level and level > consumer_level:
                continue          # a heading nested in a consumers section: still consumers
            sec_consumer = bool(CONSUMER_HEADING.search(line))
            consumer_level = level if CONSUMERS_SECTION.match(line) else 0
            sec_supplier = bool(re.search(r"suppl|depend|input|needs|prerequisit|contract|builds? on|^#+\s*From\b",
                                          line, re.I)) and not sec_consumer
            sec_deplist = bool(re.match(r"#+\s*Dependencies\b", line)) and not sec_consumer
            if sec_supplier and re.match(r"#+\s*From\b", line):
                yield i + 1, line, False
            buf = []
            continue
        if sec_consumer:
            continue
        if sec_deplist:
            if line.strip():
                buf.append((i + 1, line.strip()))
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if buf and (not nxt.strip() or nxt.startswith("#")):
                joined, start = " ".join(t for _, t in buf), buf[0][0]
                for sent in _sentences(joined):
                    if sent.strip() and not DEPCONS.search(sent):
                        yield start, sent, False
                buf = []
            continue
        if re.search(r"\*Needs:\*|^\s*Needs:|Supplied by|\| *Supplied|supplies|supplier|prerequisites? (?:are|is|:)", line) \
                and "*Needs:*" not in line:
            if re.match(r"\s*\|", line) or EXCL.search(line):
                yield i + 1, line, True       # a table row, or a line EXCL refuses: judged whole
            else:
                # A sentence "X consumes ..." of another roadmap X names a consumer, not a supplier.
                keep = [s for s in _sentences(line) if s.strip() and not (set(CONSUMES.findall(s)) - {name})]
                if keep:
                    yield i + 1, " ".join(keep), True
        m = re.search(r"\*Needs:\*", line)
        if m:
            clause, j = line[m.end():], i + 1
            while (j < len(lines) and lines[j].strip() and not re.match(r"\s*(#|\*\*|- |\* |\d+\. |\|)", lines[j])
                   and not re.search(r"\.(\s|$)", clause)):
                clause += " " + lines[j].strip()
                j += 1
            cut = re.search(r"\.(\s|$)", clause)
            yield i + 1, "*Needs:*" + (clause[:cut.start()] if cut else clause), False
            continue
        if sec_supplier and re.match(r"\s*\|", line) and not re.match(r"\s*\|\s*-{2,}", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and not re.match(r"(?i)(supplier|roadmap|source|from)\b", cells[0]) and not DEPCONS.search(line):
                yield i + 1, line, False
            continue
        if sec_supplier and re.match(r"\s*- \*{1,2}(?:TauCetiRoadmap\.)?[A-Z][A-Za-z0-9]+", line):
            bullet, j = line, i + 1
            while j < len(lines) and lines[j].strip() and re.match(r"\s{2,}\S", lines[j]) and not re.match(r"\s*- ", lines[j]):
                bullet += " " + lines[j].strip()
                j += 1
            keep = [s for s in _sentences(bullet) if s.strip() and not DEPCONS.search(s)]
            if keep:
                yield i + 1, " ".join(keep), False


def supplier_edges(readmes: dict[str, str], known: set[str] | None = None,
                   main_extended: set[str] | frozenset = frozenset()) -> dict[str, dict[str, set[int]]]:
    """Edges consumer -> supplier -> README line numbers, among the roadmaps in `readmes` (and
    `known`, if given). Roadmaps already on main that are being extended (`main_extended`) are
    suppliers for everyone and never consumers. A roadmap the README declares as its consumer is
    not taken from a prose line (see the module docstring)."""
    ours = set(readmes) - set(main_extended)
    names = ours | set(known or ())
    graph: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for consumer, text in readmes.items():
        if consumer in main_extended:
            continue
        declared = declared_consumers(text, consumer)
        for line_no, unit, prose in _units(text, consumer):
            if EXCL.search(unit):
                continue
            for supplier in set(NAME.findall(unit)):
                if supplier in names and supplier != consumer and not (prose and supplier in declared):
                    graph[consumer][supplier].add(line_no)
    return graph


def read_order(text: str) -> list[str]:
    seen, out = set(), []
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def strongly_connected(adj: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iteratively (deep supplier chains must not hit the recursion limit).
    Returns the components with more than one member."""
    index, low, onstack, stack, comps, counter = {}, {}, set(), [], [], [0]
    for root in list(adj):
        if root in index:
            continue
        work = [(root, iter(adj.get(root, ())))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        onstack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    onstack.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                    advanced = True
                    break
                if w in onstack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    comps.append(sorted(comp))
    return comps


def check_order(edges: dict[str, dict[str, set[int]]], order: list[str], only: set[str] | None = None,
                merged: set[str] | frozenset = frozenset()) -> list[str]:
    """Problems with the supplier graph: NOT IN ORDER, UPWARD and CYCLE lines (empty = clean).
    `only` restricts the per-roadmap lines, and the cycles reported, to those involving those roadmaps.
    `merged`: the roadmaps on the content repository's main, never an UPWARD supplier, never checked
    as consumers, and a cycle among them alone not reported."""
    pos = {name: i for i, name in enumerate(order)}
    problems = []
    for consumer in sorted(edges, key=lambda n: pos.get(n, len(pos))):
        if (only and consumer not in only) or consumer in merged:
            continue
        if consumer not in pos:
            problems.append(f"NOT IN ORDER: {consumer} (insert it after its last supplier)")
            continue
        for supplier, lines in sorted(edges[consumer].items()):
            if supplier in pos and supplier not in merged and pos[supplier] > pos[consumer]:
                problems.append(f"UPWARD: {consumer} (#{pos[consumer] + 1}) -> {supplier} (#{pos[supplier] + 1}) "
                                f"at README lines {sorted(lines)[:6]}")
    adj = {a: set(b) for a, b in edges.items()}
    for comp in strongly_connected(adj):
        # With `only`, a cycle elsewhere in the graph is another roadmap's defect (2026-10-27: one
        # author's cycles failed every other roadmap's gate once their pull requests were managed).
        if (only and not (set(comp) & only)) or set(comp) <= set(merged):
            continue
        problems.append("CYCLE among: " + ", ".join(comp))
    return problems


def remove_from_order(text: str, names: set[str]) -> str:
    """The merge-order file without the lines that list `names`; comments and the other lines kept in
    their order."""
    lines = [line for line in text.splitlines() if line.split("#", 1)[0].strip() not in names]
    return "\n".join(lines) + "\n"


def merge_chain(name: str, edges: dict[str, dict[str, set[int]]], order: list[str], open_names: set[str]) -> list[str]:
    """The roadmaps (with open pull requests) that must merge before `name`: the transitive closure of
    its downward suppliers, sorted by merge-order rank. Upward edges are defects reported by
    check_order and never enter the chain."""
    pos = {n: i for i, n in enumerate(order)}
    if name not in pos:
        return []
    seen: set[str] = set()
    todo = [name]
    while todo:
        m = todo.pop()
        for s in edges.get(m, {}):
            if s in pos and pos[s] < pos[m] and s in open_names and s != name and s not in seen:
                seen.add(s)
                todo.append(s)
    return sorted(seen, key=lambda n: pos[n])


MARK_OPEN, MARK_CLOSE = "<!-- merge-order -->", "<!-- /merge-order -->"


def _cited(prs: list[tuple]) -> str:
    """`#248 (LFunctions, roed-math), #934 (QuaternionicModularForms)`: (pr, roadmap[, author])."""
    return ", ".join(f"#{p[0]} ({p[1]}, {p[2]})" if len(p) > 2 and p[2] else f"#{p[0]} ({p[1]})" for p in prs)


def merge_order_paragraph(pr: int, chain: list[tuple[int, str]], external: list[tuple] | None = None,
                          mutual: list[tuple] | None = None) -> str:
    """The idempotent 'Merge order' paragraph of a pull request description, in the wording of the
    maintainer's merge_order.py: the chain only, no position number. `chain` holds (pr, roadmap)
    pairs in merge order; `external` and `mutual` hold (pr, roadmap, author) for other authors' pull
    requests that add roadmaps this one cites (those that cite it back are `mutual`). `external` is
    None when the other authors' pull requests were not read: then the paragraph says nothing about
    them, rather than that there are none."""
    if chain:
        head = " → ".join(f"#{p} ({n})" for p, n in chain) + f" → **this pull request** (#{pr})."
    else:
        head = (f"No open pull request of ours must merge before this one; **this pull request** (#{pr}) can merge "
                "as soon as it is approved.")
    ext = ""
    if external:
        ext = (" It also depends on other authors' pull requests that add roadmaps it cites, which must merge first: "
               + _cited(external) + ".")
    elif external is not None:
        ext = " It depends on no other author's open pull request."
    if mutual:
        ext += (" It also cites " + _cited(mutual) + ", which in turn consume this one (directly or through one "
                "another); these specifications have no whole-pull-request order among themselves, and the "
                "stage-level graph between them is acyclic.")
    return f"{MARK_OPEN}\n**Merge order.** {head}{ext} Roadmaps already on main are not listed.\n{MARK_CLOSE}"


def apply_paragraph(body: str, para: str, footer: str = "\U0001f916 Generated with") -> str | None:
    """The description with the paragraph inserted or replaced, or None when the body read looks
    like an API error or is empty (never overwrite a description with an error)."""
    text = body or ""
    stripped = text.lstrip()
    if not stripped or len(stripped) < 40 or (stripped.startswith("{") and "documentation_url" in stripped[:400]):
        return None
    if MARK_OPEN in text and MARK_CLOSE in text:
        return re.sub(re.escape(MARK_OPEN) + r".*?" + re.escape(MARK_CLOSE), lambda _: para, text, flags=re.S)
    i = text.rfind(footer)
    if i >= 0:
        return text[:i].rstrip() + "\n\n" + para + "\n\n" + text[i:]
    return text.rstrip() + "\n\n" + para + "\n"


def dependency_paragraph(chain: list[tuple[int, str]] | None) -> str:
    """The 'Dependencies' paragraph of a port pull request, from the same suppliers as its 'Merge
    order' chain: the open pull requests of ours that must merge first, in merge order ("none" only
    when the chain is empty; not determined when the open pull requests could not be read)."""
    if chain is None:
        listed = "not determined (the open pull requests could not be read; `merge-order` fills it in)"
    else:
        listed = ", ".join(f"#{p} ({n})" for p, n in chain) or "none"
    return ("Dependencies (the roadmaps the README cites by name and section, and what they in turn need; the "
            f"Lean imports none of them). In open pull requests of ours: {listed}. Roadmaps on main are cited by "
            "name; everything else named in the README is a consumer, not a prerequisite.")


class OrderCycle(ValueError):
    """Placing a roadmap would put one of its suppliers after one of its consumers."""


LEAN_SUPPLIER = re.compile(r"\bTauCetiRoadmap\.([A-Z][A-Za-z0-9]*)\b")


def package_suppliers(name: str, readme: str, lean: str, known: set[str]) -> set[str]:
    """The roadmaps a package consumes: those its README names as suppliers (as the dependency
    check reads them) and every `TauCetiRoadmap.<X>` its Lean names, among `known`."""
    edges = supplier_edges({name: readme}, known)
    return (set(edges.get(name, {})) | set(LEAN_SUPPLIER.findall(lean or ""))) - {name}


def place_in_order(text: str, name: str, suppliers: set[str], consumers: set[str] = frozenset()) -> tuple[str, list[str]]:
    """The merge-order file with `name` placed right after its last listed supplier (at the end
    when none is listed), and notes (suppliers missing from the order). Raises OrderCycle when a
    listed consumer of `name` comes before one of its listed suppliers. A listed name stays."""
    order = read_order(text)
    if name in order:
        return text, []
    pos = {n: i for i, n in enumerate(order)}
    listed = sorted((s for s in suppliers if s in pos), key=pos.get)
    notes = [f"supplier {s} of {name} is not in the merge order; it must be inserted before {name}"
             for s in sorted(suppliers - set(pos))]
    early = [c for c in consumers if c in pos and listed and pos[c] <= pos[listed[-1]]]
    if early:
        c = min(early, key=pos.get)
        raise OrderCycle(f"{name} consumes {listed[-1]} (#{pos[listed[-1]] + 1}) and is consumed by {c} "
                         f"(#{pos[c] + 1}), which comes first: a supplier after a consumer, a cycle through {name}")
    return insert_into_order(text, name, set(listed)), notes


def insert_into_order(text: str, name: str, suppliers: set[str]) -> str:
    """The merge-order file with `name` inserted right after its last supplier (appended when none of
    its suppliers is listed). Comments and other lines are kept; an already listed name is left alone."""
    lines = text.splitlines()
    names = [line.split("#", 1)[0].strip() for line in lines]
    if name in names:
        return text
    last = max((i for i, n in enumerate(names) if n and n in suppliers), default=None)
    if last is None:
        lines.append(name)
    else:
        lines.insert(last + 1, name)
    return "\n".join(lines) + "\n"


def closure(adj: dict[str, set[str]]) -> dict[str, set[str]]:
    """Transitive closure of `adj` (name -> names reachable through it, the name itself excluded
    unless it lies on a cycle)."""
    out: dict[str, set[str]] = {}
    for n, direct in adj.items():
        seen: set[str] = set()
        stack = list(direct)
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(adj.get(x, ()))
        out[n] = seen
    return out


def move_after_suppliers(text: str, name: str, suppliers: set[str],
                         consumers: set[str] = frozenset(), *,
                         consumer_closure: dict[str, set[str]] | None = None,
                         supplier_closure: dict[str, set[str]] | None = None,
                         movable: set[str] | frozenset = frozenset()) -> tuple[str, str]:
    """The repair of an UPWARD edge of `name`: the merge-order file with `name`, listed above one of
    its listed `suppliers`, moved to right after the last of them (its line and comment kept, a
    duplicate line of it dropped), and a note saying so. Unchanged with no note when `name` is not
    listed or no listed supplier comes after it.

    A listed consumer of `name` that comes no later than that last supplier would end up before its
    supplier. Without the closures the move is refused (OrderCycle). With them (`consumer_closure`:
    name -> its transitive consumers, `supplier_closure`: name -> its transitive suppliers, `movable`:
    the roadmaps whose README the caller read, the only ones it may move) every such consumer of
    `name`, direct or transitive, rides along: the block keeps its relative order and lands right after
    the last supplier. The move is still refused when a block member is a transitive supplier of that
    last supplier (a real cycle) or when a rider is not movable (a roadmap whose README was not read)."""
    order = read_order(text)
    if name not in order:
        return text, ""
    pos = {n: i for i, n in enumerate(order)}
    below = [s for s in suppliers if s in pos and s != name and pos[s] > pos[name]]
    if not below:
        return text, ""
    last = max(below, key=pos.get)
    early = [c for c in consumers if c in pos and c != name and pos[c] <= pos[last]]
    block = [name]
    if early:
        c = min(early, key=pos.get)
        if consumer_closure is None or supplier_closure is None:
            raise OrderCycle(f"{name} consumes {last} (#{pos[last] + 1}) and is consumed by {c} (#{pos[c] + 1}), "
                             f"which comes first: moving {name} after {last} would put a supplier after a consumer, "
                             f"a cycle through {name}")
        loop = sorted((supplier_closure.get(name, set()) & consumer_closure.get(name, set())) - {name}, key=pos.get)
        if loop:
            raise OrderCycle(f"{name} consumes {last} (#{pos[last] + 1}) and is consumed by {c} (#{pos[c] + 1}), which "
                             f"comes first; {name} lies on a dependency cycle with {', '.join(loop)}, so no move "
                             f"repairs the order: a cycle through {name}")
        riders = sorted({r for r in consumer_closure.get(name, set()) if r in pos and r != name and pos[r] <= pos[last]},
                        key=pos.get)
        on_cycle = [r for r in [name] + riders if r in supplier_closure.get(last, set())]
        if on_cycle:
            raise OrderCycle(f"{name} consumes {last} (#{pos[last] + 1}), which consumes {on_cycle[0]} "
                             f"(#{pos[on_cycle[0]] + 1}), which consumes {name}: a cycle through {name}")
        stuck = [r for r in riders if r not in movable]
        if stuck:
            raise OrderCycle(f"{name} consumes {last} (#{pos[last] + 1}) and is consumed by {stuck[0]} "
                             f"(#{pos[stuck[0]] + 1}), which comes first and whose README was not read, so it "
                             f"cannot be moved along: a cycle through {name} unless {stuck[0]} moves too")
        block = [name] + riders
    lines = text.splitlines()
    names = [line.split("#", 1)[0].strip() for line in lines]
    first_line = {}
    for i, n in enumerate(names):
        if n in block and n not in first_line:
            first_line[n] = lines[i]
    kept = [(ln, n) for ln, n in zip(lines, names, strict=True) if n not in block]
    at = next(i for i, (_ln, n) in enumerate(kept) if n == last)
    for k, n in enumerate(block):
        kept.insert(at + 1 + k, (first_line[n], n))
    others = sorted(set(below) - {last}, key=pos.get)
    riders = block[1:]
    note = (f"{name} moved in the merge order from #{pos[name] + 1} to #{pos[last] + 1}, right after its last supplier "
            f"{last}" + (f" ({', '.join(others)} also came after it)" if others else "")
            + (f"; its consumer{'s' if len(riders) > 1 else ''} {', '.join(riders)} moved with it" if riders else ""))
    return "\n".join(ln for ln, _n in kept) + "\n", note


def repair_upward(text: str, readmes: dict[str, str], main_extended: set[str] | frozenset = frozenset(),
                  only: set[str] | None = None, merged: set[str] | frozenset = frozenset()) -> tuple[str, dict[str, str]]:
    """The merge-order file with every roadmap of `readmes` (or of `only`) that it lists above a
    supplier its README names moved right after its last supplier (`move_after_suppliers`), until no
    such move is left; and name -> what happened to it (moved, or why not: a move that would put one
    of its consumers among `readmes` before it is not made). A roadmap on the content repository's
    main (`merged`) is neither moved nor a supplier anything moves after. Idempotent: a clean order is
    returned as it is, with no notes."""
    order = read_order(text)
    edges = supplier_edges(readmes, set(order) | set(readmes), main_extended)
    edges = {c: {s: lines for s, lines in sups.items() if s not in merged}
             for c, sups in edges.items() if c not in merged}
    consumers: dict[str, set[str]] = defaultdict(set)
    for c, sups in edges.items():
        for s in sups:
            consumers[s].add(c)
    consumer_closure = closure(dict(consumers))
    supplier_closure = closure({c: set(sups) for c, sups in edges.items()})
    movable = set(edges)
    notes: dict[str, str] = {}
    for _ in range(len(order) + 1):
        moved = False
        for name in [n for n in read_order(text) if n in edges and (only is None or n in only)]:
            try:
                new, note = move_after_suppliers(text, name, set(edges[name]), consumers[name],
                                                 consumer_closure=consumer_closure,
                                                 supplier_closure=supplier_closure, movable=movable)
            except OrderCycle as exc:
                notes[name] = f"{name} not moved in the merge order: {exc}"
                continue
            if note:
                text, notes[name], moved = new, note, True
                break                   # positions changed: start again from the top
        if not moved:
            break
    return text, notes
