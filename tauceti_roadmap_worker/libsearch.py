"""Library near-matches for a roadmap's `Suggested.lean`: what Loogle and LeanSearch return for each
declaration's constants and docstring, written for the reviewer as leads (never verdicts).

A roadmap target that Mathlib or Tau Ceti already proves under another name is invisible to the
citation tools, which only resolve names the README mentions. This pass asks the two public search
services about every declaration of the roadmap's Lean file and lists what comes back, so the reviewer
opens the candidates at the pin instead of relying on memory. Best effort: every failure is a line
in the report, never a failed round. Network: `loogle.lean-lang.org` and `leansearch.net` only."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

LOOGLE = "https://loogle.lean-lang.org/json?q="
LEANSEARCH = "https://leansearch.net/search"
TIMEOUT = 12          # seconds per request
BUDGET = 240          # seconds for the whole pass
MAX_DECLS = 80
HITS = 5

DECL_RE = re.compile(r"^(?:@\[[^\]]*\]\s*)?(?:noncomputable\s+|private\s+|protected\s+)*"
                     r"(theorem|lemma|def|abbrev|structure|class|instance)\s+([^\s:({\[]+)", re.M)
# binder classes and sorts that say nothing about the statement
GENERIC = {"Type", "Prop", "Sort", "Type*", "Fact", "DecidableEq", "Fintype", "Finite", "Nonempty", "Inhabited",
           "CommRing", "CommSemiring", "Ring", "Semiring", "Field", "Group", "CommGroup", "AddCommGroup",
           "AddCommMonoid", "Monoid", "CommMonoid", "Module", "Algebra", "TopologicalSpace", "MetricSpace",
           "Nat", "Int", "Rat", "Real", "Complex", "Set", "Finset", "List", "Function", "Nat.Prime", "True",
           "False", "Prime", "Function.Injective", "Function.Surjective", "Function.Bijective", "Small"}
IDENT = re.compile(r"(?<![\w.'])([A-Z][A-Za-z0-9_']*(?:\.[A-Za-z0-9_'][A-Za-z0-9_']*)*)")


@dataclass
class Decl:
    kind: str
    name: str
    statement: str      # text after the name up to `:=` (or end), binders included
    docstring: str = ""


@dataclass
class Hit:
    name: str
    module: str
    detail: str = ""


@dataclass
class Found:
    decl: Decl
    loogle_query: str = ""
    loogle: list[Hit] = field(default_factory=list)
    leansearch_query: str = ""
    leansearch: list[Hit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def same_name(self) -> list[Hit]:
        last = self.decl.name.rsplit(".", 1)[-1]
        return [h for h in self.loogle + self.leansearch
                if h.name.rsplit(".", 1)[-1] == last]


def declarations(lean: str) -> list[Decl]:
    """Every top-level declaration of a Lean file with the docstring written just before it."""
    out: list[Decl] = []
    for m in DECL_RE.finditer(lean):
        rest = lean[m.end():]
        cut = re.search(r"\s:=\s|\s:=$|\swhere\b|^\s*\|", rest, re.M)
        statement = rest[: cut.start()] if cut else rest[:800]
        statement = re.sub(r"\s+", " ", statement).strip()[:800]
        before = lean[: m.start()].rstrip()
        doc = ""
        if before.endswith("-/"):
            start = before.rfind("/--")
            if start >= 0:
                doc = re.sub(r"\s+", " ", before[start + 3: -2]).strip()
        out.append(Decl(m.group(1), m.group(2), statement, doc))
    return out


def conclusion(statement: str) -> str:
    """The part of a statement after its binders: the text after the last top-level `) :`, `] :` or `} :`."""
    best = -1
    depth = 0
    for i, ch in enumerate(statement):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0 and i > 0 and statement[i - 1] in ")]} " and statement[i + 1: i + 2] != "=":
            best = i
    return statement[best + 1:].strip() if best >= 0 else statement


def constants(text: str) -> list[str]:
    """Dotted or capitalised constants of a Lean text, most specific (longest, dotted) first, without the
    generic binder classes."""
    seen: list[str] = []
    for c in IDENT.findall(text):
        # single capital letters are type variables (and Polynomial's `X`, `C`, too generic to search)
        if c in GENERIC or c in seen or c.endswith(".Type") or (len(c) == 1):
            continue
        seen.append(c)
    return sorted(seen, key=lambda c: (-c.count("."), -len(c)))


STOPWORDS = {"forall", "exists", "iff", "eq", "ne", "zero", "one", "two", "lt", "le", "ge", "gt", "of", "and", "or",
             "not", "mem", "sub", "add", "mul", "div", "pow", "zpow", "inv", "neg", "smul", "apply", "comp", "imp",
             "left", "right", "self", "succ", "pred", "mod", "dvd", "prime", "coe", "cast", "finite", "fintype",
             "card", "sum", "prod", "range", "map", "image", "preimage", "union", "inter", "bijective",
             "injective", "surjective", "continuous", "open", "closed", "isOpen", "isClosed", "mono", "anti",
             "top", "bot", "sup", "inf", "max", "min", "nonempty", "empty", "univ", "id", "fun", "congr", "ext",
             "unique", "nat", "int", "rat", "real", "odd", "even", "pos", "nonneg", "iff'", "neg", "abs", "norm",
             "nonempty", "exists", "quotient", "span", "ker", "range", "eval", "aeval", "val", "mk", "some"}


def name_constants(decl: Decl) -> list[str]:
    """Constants a declaration name points at: its tokens of four letters or more that are not lemma-name
    stop words, in the declaration's namespace when it has one (`Polynomial.hasseDeriv`, `Polynomial.expand`)."""
    ns, _, last = decl.name.rpartition(".")
    out = []
    for tok in last.split("_"):
        if len(tok) < 4 or tok in STOPWORDS or not tok[0].isalpha():
            continue
        c = f"{ns}.{tok}" if ns and tok[0].islower() else tok
        if c not in out:
            out.append(c)
    return out[:3]


def loogle_queries(decl: Decl) -> list[str]:
    """Queries to try in turn: all the constants the declaration name points at (declarations mentioning each
    of them: the closest thing to the same statement); each of them alone; the first with the most specific
    constant of the conclusion; the conclusion's three, then two, most specific constants; a single conclusion
    constant only when it is namespaced (a bare class name such as `CharP` returns noise)."""
    cs = constants(conclusion(decl.statement)) or constants(decl.statement)
    out: list[str] = []

    def add(q: str) -> None:
        if q and q not in out:
            out.append(q)
    named = name_constants(decl)
    if named:
        add(", ".join(named))
        for c in named:
            add(c)
        if cs and cs[0] not in named:
            add(", ".join([named[0], cs[0]]))
    for n in (3, 2):
        if len(cs) >= n:
            add(", ".join(cs[:n]))
    if cs and "." in cs[0]:
        add(cs[0])
    return out


def leansearch_query(decl: Decl) -> str:
    """The first sentence of the docstring, else the declaration name read as words plus its conclusion."""
    if decl.docstring:
        return re.split(r"(?<=[.!?])\s", decl.docstring, maxsplit=1)[0][:300]
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", decl.name.replace(".", " ").replace("_", " "))
    return f"{words}: {conclusion(decl.statement)}"[:300]


def http_fetch(url: str, data: bytes | None = None) -> str:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "tauceti-roadmap-worker libsearch",
                                                           "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - two fixed public hosts
        return resp.read().decode("utf-8", errors="replace")


def ask_loogle(query: str, fetch: Callable[..., str] = http_fetch) -> list[Hit]:
    text = fetch(LOOGLE + urllib.parse.quote(query, safe=""))
    data = json.loads(text)
    return [Hit(h.get("name", ""), h.get("module", ""), (h.get("type") or "").replace("\n", " ")[:160])
            for h in data.get("hits", [])[:HITS]]


def ask_leansearch(query: str, fetch: Callable[..., str] = http_fetch) -> list[Hit]:
    body = json.dumps({"query": [query], "num_results": HITS}).encode()
    data = json.loads(fetch(LEANSEARCH, body))
    rows = data[0] if data and isinstance(data[0], list) else data
    out = []
    for r in rows[:HITS]:
        res = r.get("result", r) if isinstance(r, dict) else {}
        name = res.get("name", "")
        name = ".".join(name) if isinstance(name, list) else str(name)
        mod = res.get("module_name", "")
        mod = ".".join(mod) if isinstance(mod, list) else str(mod)
        out.append(Hit(name, mod, str(res.get("signature") or res.get("type") or "").replace("\n", " ")[:160]))
    return out


def search(lean: str, fetch: Callable[..., str] = http_fetch, clock: Callable[[], float] = time.monotonic,
           budget: float = BUDGET) -> tuple[list[Found], str]:
    """One `Found` per declaration (at most `MAX_DECLS`), within the time budget; the second value notes
    what was skipped."""
    decls = declarations(lean)
    start = clock()
    found: list[Found] = []
    note = ""
    for i, d in enumerate(decls[:MAX_DECLS]):
        if clock() - start > budget:
            note = f"time budget spent after {i} of {len(decls)} declarations; the rest were not searched"
            break
        f = Found(d)
        for q in loogle_queries(d):
            try:
                hits = ask_loogle(q, fetch)
            except Exception as exc:  # noqa: BLE001 - a lead-finder: record and go on
                f.errors.append(f"Loogle `{q}`: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            f.loogle_query = q
            if hits:
                f.loogle = hits
                break
        f.leansearch_query = leansearch_query(d)
        try:
            f.leansearch = ask_leansearch(f.leansearch_query, fetch)
        except Exception as exc:  # noqa: BLE001
            f.errors.append(f"LeanSearch: {type(exc).__name__}: {str(exc)[:80]}")
        found.append(f)
    if len(decls) > MAX_DECLS and not note:
        note = f"only the first {MAX_DECLS} of {len(decls)} declarations were searched"
    return found, note


def report(found: list[Found], note: str, rel: str) -> tuple[str, int]:
    """The Markdown report and the number of declarations with a same-name hit (the strongest lead)."""
    lines = [f"# Library near-matches for `{rel}`", "",
             "What Loogle (by the constants of each conclusion) and LeanSearch (by each docstring) return for every "
             "declaration. Leads, not verdicts: open a candidate at the pinned Mathlib or Tau Ceti commit and compare "
             "the statement before reporting that a target is already proved. A target the pin already states is a "
             "finding (cite it; do not restate it); a weaker or more special library statement is not.", ""]
    if note:
        lines += [f"_{note}._", ""]
    strong = 0
    for f in found:
        same = f.same_name
        strong += bool(same)
        lines.append(f"## `{f.decl.name}`" + (" — **same-name hit**" if same else ""))
        if f.loogle:
            lines.append(f"- Loogle `{f.loogle_query}`:")
            lines += [f"  - `{h.name}` ({h.module})" + (f": `{h.detail}`" if h.detail else "") for h in f.loogle]
        elif f.loogle_query:
            lines.append(f"- Loogle `{f.loogle_query}`: no hits")
        if f.leansearch:
            lines.append(f"- LeanSearch “{f.leansearch_query}”:")
            lines += [f"  - `{h.name}` ({h.module})" + (f": `{h.detail}`" if h.detail else "") for h in f.leansearch]
        for e in f.errors:
            lines.append(f"- not searched: {e}")
        lines.append("")
    return "\n".join(lines), strong
