"""The supplier graph between roadmaps, its check against the merge order, and the merge chain.

Ported from the maintainer's decycle/depcheck_units.py, depcheck.py and merge_order.py, as pure
functions over README texts (no /tmp lists, no hard-coded pull requests or bundles):

* `units(text)` yields the README lines and clauses that name suppliers: `*Needs:*` clauses (cut
  at their first sentence end), lines saying "Supplied by", "supplier", "prerequisites are", rows
  of supplier tables, supplier-contract bullets, `### From <Name>` headings and the sentences of a
  `### Dependencies` section, minus anything that describes consumers.
* `supplier_edges(readmes, ...)` turns those into edges consumer -> supplier.
* `check_order(edges, order, ...)` reports UPWARD edges (a supplier later in the merge order),
  CYCLEs and roadmaps missing from the order: every one is a design defect.
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


def _sentences(text: str) -> list[str]:
    out, pos = [], 0
    for m in re.finditer(r"\.(\s+|$)", text):
        out.append(text[pos:m.start() + 1])
        pos = m.end()
    if text[pos:].strip():
        out.append(text[pos:])
    return out


def units(text: str):
    """Yield (line_no, text) units of a README that name its suppliers (see the module docstring)."""
    lines = text.split("\n")
    sec_supplier = sec_consumer = sec_deplist = False
    buf: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if line.startswith("#"):
            sec_consumer = bool(re.search(r"consum|export|outbound|downstream", line, re.I))
            sec_supplier = bool(re.search(r"suppl|depend|input|needs|prerequisit|contract|builds? on|^#+\s*From\b",
                                          line, re.I)) and not sec_consumer
            sec_deplist = bool(re.match(r"#+\s*Dependencies\b", line)) and not sec_consumer
            if sec_supplier and re.match(r"#+\s*From\b", line):
                yield i + 1, line
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
                        yield start, sent
                buf = []
            continue
        if re.search(r"\*Needs:\*|^\s*Needs:|Supplied by|\| *Supplied|supplies|supplier|prerequisites? (?:are|is|:)", line) \
                and "*Needs:*" not in line:
            yield i + 1, line
        m = re.search(r"\*Needs:\*", line)
        if m:
            clause, j = line[m.end():], i + 1
            while (j < len(lines) and lines[j].strip() and not re.match(r"\s*(#|\*\*|- |\* |\d+\. |\|)", lines[j])
                   and not re.search(r"\.(\s|$)", clause)):
                clause += " " + lines[j].strip()
                j += 1
            cut = re.search(r"\.(\s|$)", clause)
            yield i + 1, "*Needs:*" + (clause[:cut.start()] if cut else clause)
            continue
        if sec_supplier and re.match(r"\s*\|", line) and not re.match(r"\s*\|\s*-{2,}", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and not re.match(r"(?i)(supplier|roadmap|source|from)\b", cells[0]) and not DEPCONS.search(line):
                yield i + 1, line
            continue
        if sec_supplier and re.match(r"\s*- \*{1,2}(?:TauCetiRoadmap\.)?[A-Z][A-Za-z0-9]+", line):
            bullet, j = line, i + 1
            while j < len(lines) and lines[j].strip() and re.match(r"\s{2,}\S", lines[j]) and not re.match(r"\s*- ", lines[j]):
                bullet += " " + lines[j].strip()
                j += 1
            keep = [s for s in _sentences(bullet) if s.strip() and not DEPCONS.search(s)]
            if keep:
                yield i + 1, " ".join(keep)


def supplier_edges(readmes: dict[str, str], known: set[str] | None = None,
                   main_extended: set[str] | frozenset = frozenset()) -> dict[str, dict[str, set[int]]]:
    """Edges consumer -> supplier -> README line numbers, among the roadmaps in `readmes` (and
    `known`, if given). Roadmaps already on main that are being extended (`main_extended`) are
    suppliers for everyone and never consumers."""
    ours = set(readmes) - set(main_extended)
    names = ours | set(known or ())
    graph: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for consumer, text in readmes.items():
        if consumer in main_extended:
            continue
        for line_no, unit in units(text):
            if EXCL.search(unit):
                continue
            for supplier in set(NAME.findall(unit)):
                if supplier in names and supplier != consumer:
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


def check_order(edges: dict[str, dict[str, set[int]]], order: list[str], only: set[str] | None = None) -> list[str]:
    """Problems with the supplier graph: NOT IN ORDER, UPWARD and CYCLE lines (empty = clean).
    `only` restricts the per-roadmap lines to those roadmaps; cycles are always reported."""
    pos = {name: i for i, name in enumerate(order)}
    problems = []
    for consumer in sorted(edges, key=lambda n: pos.get(n, len(pos))):
        if only and consumer not in only:
            continue
        if consumer not in pos:
            problems.append(f"NOT IN ORDER: {consumer} (insert it after its last supplier)")
            continue
        for supplier, lines in sorted(edges[consumer].items()):
            if supplier in pos and pos[supplier] > pos[consumer]:
                problems.append(f"UPWARD: {consumer} (#{pos[consumer] + 1}) -> {supplier} (#{pos[supplier] + 1}) "
                                f"at README lines {sorted(lines)[:6]}")
    adj = {a: set(b) for a, b in edges.items()}
    for comp in strongly_connected(adj):
        problems.append("CYCLE among: " + ", ".join(comp))
    return problems


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


def dependency_paragraph(name: str, readme: str, open_prs: dict[str, int], order: list[str]) -> str:
    """The 'Dependencies' paragraph of a new port pull request: the suppliers named in the README
    that are still open pull requests, in merge order (port of open-port-pr.sh)."""
    suppliers = set()
    for _ln, unit in units(readme):
        if EXCL.search(unit):
            continue
        suppliers |= {s for s in NAME.findall(unit) if s in open_prs and s != name}
    ranked = [s for s in order if s in suppliers] + sorted(suppliers - set(order))
    listed = ", ".join(f"#{open_prs[s]} ({s})" for s in ranked) if ranked else "none"
    return ("Dependencies (cited in the README by roadmap name and section; the Lean imports none of them). "
            f"In open pull requests of ours: {listed}. Roadmaps on main are cited by name; "
            "everything else named in the README is a consumer, not a prerequisite.")


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
