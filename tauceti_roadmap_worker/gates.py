"""Push gates for one roadmap folder, as functions returning a structured result.

Port of the maintainer's gate.sh (with the mechanical checks of mech_check.py and readthrough.py),
plus the dependency check and merge chain from depgraph.py. `run_gate` reads
`<worktree>/<active_dir>/<Name>/{README.md,Suggested.lean}` (or a nested roadmap's folder,
`GateOptions.folder`), runs every scanner, and returns a
`GateResult` whose `ok` is False on any non-zero count. The CLI (`tauceti-roadmap gate`) exits 1
in that case: run it directly, never through a pipe that hides its exit status.

Every scanner is a pure function of the file text and returns a list of `Hit(line, text)`.
Differences from gate.sh, deliberately:
- `set_option` is counted in code only (a comment or docstring mentioning it is not an override).
- LaTeX delimiters and TeX syntax are counted outside code spans and code blocks, where the house
  style rule applies (Lean code in a backtick span may contain `$`).
- Sorry'd data is found per declaration, including `def f : T := sorry` and multi-line bodies; the
  gate.sh regex `[^:]*:=\\s*sorry` missed every definition with a type ascription.
- The campaign-specific checks (one forbidden theorem family, a dropped-names list in /tmp) became
  configuration: `gates.forbidden_patterns` and `gates.dropped_names_file`.
- `Suggested.lean` is the roadmap's acceptance test (its headline theorems), so no scanner counts its
  declarations or examples, sets a minimum size, or asks for a Lean counterpart of a README Check;
  an admitted `example` is still refused (`sorry-examples`).
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass, field, replace as _replace
from pathlib import Path
from urllib.parse import unquote

from . import depgraph
from .board import NAME_RE


@dataclass(frozen=True)
class Hit:
    line: int
    text: str


# --- text preparation -----------------------------------------------------------------------------

def strip_lean_comments(text: str) -> str:
    """Blank out Lean comments (nested `/- -/`, docstrings, `--` line comments), keeping newlines and
    string literals, so that line numbers stay valid."""
    out: list[str] = []
    i, n, depth, in_str = 0, len(text), 0, False
    while i < n:
        c, two = text[i], text[i:i + 2]
        if depth:
            if two == "/-":
                depth += 1
                out.append("  ")
                i += 2
            elif two == "-/":
                depth -= 1
                out.append("  ")
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            in_str = c != '"'
            i += 1
            continue
        if two == "/-":
            depth = 1
            out.append("  ")
            i += 2
            continue
        if two == "--":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        in_str = c == '"'
        out.append(c)
        i += 1
    return "".join(out)


def strip_markdown_code(text: str) -> str:
    """Blank out fenced code blocks and inline code spans, keeping newlines."""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"(?ms)^(```|~~~).*?^\1[^\n]*$", blank, text)
    # Indented code blocks: 4-space lines after a blank line (or after code), not list items.
    lines, in_code = text.split("\n"), False
    for k, line in enumerate(lines):
        indented = line.startswith(("    ", "\t")) and not re.match(r"\s*(?:[-*+]|\d+\.)\s", line)
        prev_blank = k == 0 or not lines[k - 1].strip()
        in_code = indented and (in_code or prev_blank)
        if in_code:
            lines[k] = " " * len(line)
    return re.sub(r"`[^`\n]*`", blank, "\n".join(lines))


def _hits(pattern: str, text: str, flags: int = 0) -> list[Hit]:
    lines = text.split("\n")
    out = []
    for m in re.finditer(pattern, text, flags):
        line_no = text.count("\n", 0, m.start()) + 1
        out.append(Hit(line_no, lines[line_no - 1].strip()[:160]))
    return out


DECL_START = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(?P<kw>def|abbrev|instance|theorem|lemma|example|structure|class|inductive|opaque|axiom|namespace|section|end|"
    r"open|variable|universe|import|public import|set_option|#[a-z_]+)\b", re.M)


def lean_blocks(code: str) -> list[tuple[str, int, str]]:
    """(keyword, first line, text) of each top-level command of comment-stripped Lean code."""
    starts = [(m.start(), m.group("kw")) for m in DECL_START.finditer(code)]
    blocks = []
    for k, (pos, kw) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(code)
        blocks.append((kw, code.count("\n", 0, pos) + 1, code[pos:end]))
    return blocks


# --- Suggested.lean scanners ----------------------------------------------------------------------

def scan_set_option(lean: str) -> list[Hit]:
    return _hits(r"\bset_option\b", strip_lean_comments(lean))


def scan_mathlib_subimports(lean: str) -> list[Hit]:
    return _hits(r"^(?:public\s+)?import\s+Mathlib\.\S+", strip_lean_comments(lean), re.M)


def scan_sorry_data(lean: str) -> list[Hit]:
    """Definitions, abbreviations, instances and opaque constants whose body uses `sorry`."""
    out = []
    for kw, line, block in lean_blocks(strip_lean_comments(lean)):
        if kw in ("def", "abbrev", "instance", "opaque") and re.search(r"\bsorry\b", block):
            out.append(Hit(line, block.strip().splitlines()[0][:160]))
    return out


def scan_sorry_examples(lean: str) -> list[Hit]:
    """Examples must be proved: an admitted example tests nothing."""
    return [Hit(line, block.strip().splitlines()[0][:160]) for kw, line, block in lean_blocks(strip_lean_comments(lean))
            if kw == "example" and re.search(r"\bsorry\b", block)]


def scan_lean_residue(lean: str) -> list[Hit]:
    code = strip_lean_comments(lean)
    pattern = r"#print\s+axioms|^#eval\b|^#synth\b|^lemma\b|:\s*True\s*:=|:\s*Prop\s*:=\s*(?:by\s+)?sorry"
    return _hits(pattern, code, re.M)


def scan_missing_mathlib_import(lean: str) -> list[Hit]:
    return [] if re.search(r"^(?:public\s+)?import\s+Mathlib\s*$", lean, re.M) else [Hit(1, "no `import Mathlib` line")]


IMPORT_LINE = re.compile(r"^[ \t]*(?P<text>(?:public\s+)?import\s+(?P<module>[\w.«»]+))", re.M)
IMPORTS_RULE = ("Suggested.lean imports: `import Mathlib` first, then only the individual `TauCeti.*` modules used "
                "(never `Mathlib.<submodule>`); found: ")
IMPORTS_MISSING = "Suggested.lean imports: TauCeti module {name} does not exist at the pinned Tau Ceti"
IMPORTS_LISTED = 12
TAUCETI_SOURCES = Path(".lake") / "packages" / "TauCeti" / "TauCeti"


def lean_imports(lean: str) -> list[tuple[int, str, str]]:
    """Every import of the file, in code only: (line, module, the line's text)."""
    code = strip_lean_comments(lean)
    return [(code.count("\n", 0, m.start()) + 1, m.group("module"), " ".join(m.group("text").split()))
            for m in IMPORT_LINE.finditer(code)]


def tauceti_imports(lean: str) -> list[str]:
    """The `TauCeti.*` modules the file imports, in order, each once."""
    return list(dict.fromkeys(m for _l, m, _t in lean_imports(lean) if m.startswith("TauCeti.")))


def tauceti_sources(checkout: Path | None) -> Path | None:
    """Where a checkout wired to its Lake packages holds the pinned Tau Ceti's sources
    (`.lake/packages/TauCeti/TauCeti`), or None when they are not there (nothing to verify against)."""
    if checkout is None:
        return None
    path = Path(checkout) / TAUCETI_SOURCES
    return path if path.is_dir() else None


def tauceti_module_exists(module: str, sources: Path | None) -> bool:
    """Whether `TauCeti.<Path>` is a module of the pinned Tau Ceti: `<sources>/<Path>.lean`. Without
    the sources the question cannot be answered and the module passes."""
    if sources is None:
        return True
    parts = module.split(".")[1:]
    if not parts or not all(parts):
        return False
    return (Path(sources) / Path(*parts)).with_suffix(".lean").is_file()


def scan_lean_imports(lean: str, tauceti: Path | None = None) -> list[Hit]:
    """The header's imports: `import Mathlib` first, then zero or more `import TauCeti.<Path>` lines,
    each once, nothing else, nothing below the header (the maintainer's rule of 2026-10-19). With the
    pinned Tau Ceti's sources at hand (`tauceti`), every `TauCeti.*` module must exist there. A file
    with no import at all is `no-import-mathlib`'s finding."""
    code = strip_lean_comments(lean)
    imports = lean_imports(lean)
    if not imports:
        return []
    header_end = next((line for kw, line, _ in lean_blocks(code) if kw not in ("import", "public import")), None)
    found, ok, seen = [], imports[0][1] == "Mathlib", set()
    for k, (line, module, text) in enumerate(imports):
        late = header_end is not None and line > header_end
        allowed = module == "Mathlib" if k == 0 else module.startswith("TauCeti.")
        if not allowed or late or module in seen:
            ok = False
        seen.add(module)
        found.append(f"`{text}` (line {line}" + (", after the header" if late else "") + ")")
    hits = []
    if not ok:
        listed = ", ".join(found[:IMPORTS_LISTED]) + (f", … and {len(found) - IMPORTS_LISTED} more" if len(found) > IMPORTS_LISTED else "")
        hits.append(Hit(imports[0][0], IMPORTS_RULE + listed))
    if tauceti is not None:
        for line, module, _text in imports:
            if module.startswith("TauCeti.") and not tauceti_module_exists(module, tauceti):
                hits.append(Hit(line, IMPORTS_MISSING.format(name=module)))
    return hits


def scan_lean_length(lean: str, max_lines: int) -> list[Hit]:
    count = len(lean.splitlines())
    return [Hit(count, f"{count} lines (at most {max_lines})")] if max_lines and count > max_lines else []


# --- Suggested.lean statement hygiene -------------------------------------------------------------
#
# How the targets are stated, checked on a copy of the file with comments, docstrings and strings
# blanked (offsets kept, so an offset gives its line). Every hit names its severity and the repair a
# review quotes. `[prreview] statement_hygiene` turns the scan off.

HYGIENE_REPAIRS = {
    "nonempty-iso": "state the canonical map and `IsIso` of it, or name the isomorphism as data; keep `Nonempty` only "
                    "as a corollary named `nonempty_*`",
    "choice-data": "define the datum through a universal property, by `Quotient.lift`/descent, or take it as an "
                   "argument",
    "local-instance": "use an instance binder or a named instance in the signature",
    "fintype-field": "use `[Finite {k}]`; `Nat.card {k}` is the cardinality",
    "ulift": "state on the carrier in the scheme's universe; cross universes once in a named comparison",
    "instance-removal": "resolve the instance where it arises; no removals or local derived-category instances",
    "transport": "restate without heterogeneous equality or transport in the conclusion",
    "duplicate": "one statement, one declaration",
}
# Top-level commands start in column 0 (as `DECL_START`), with `public` and the commands that end a
# declaration's text (`attribute`, notations, `mutual`) besides.
HYGIENE_COMMAND = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?"
    r"(?P<mods>(?:(?:private|protected|public|noncomputable|partial|unsafe|nonrec|scoped|local)\s+)*)"
    r"(?P<kw>theorem|lemma|def|abbrev|instance|example|structure|class|inductive|opaque|axiom|namespace|section|end|"
    r"mutual|open|variable|universe|import|attribute|set_option|notation|infixl|infixr|infix|prefix|postfix|"
    r"macro_rules|macro|syntax|elab|#[a-z_]+)(?![\w'])", re.M)
HYGIENE_DECLS = ("theorem", "lemma", "def", "abbrev", "instance", "example", "structure", "class", "inductive",
                 "opaque", "axiom")
DECL_NAME = re.compile(r"\s*(?:\(priority\s*:=[^)]*\)\s*)?"                     # an instance's priority
                       r"(?:(?P<name>[^\s(\[{⦃:.]+(?:\.[^\s(\[{⦃:.]+)*)(?:\.\{[^}]*\})?)?")  # `Foo.bar.{u}`
OPENERS, CLOSERS = "([{⟨⦃", ")]}⟩⦄"
ISO = re.compile(r"[≅≃≌]")
CHOICE_CALL = re.compile(r"(?:(?P<pre>[\w'.]*[\w'])|(?P<close>[)\]⟩]))?\.(?P<fn>choose|some|choice|arbitrary)(?![\w'])")
LOCAL_INSTANCE = re.compile(r"(?<![\w'.])(?:letI|haveI)(?![\w'])")
FINTYPE = re.compile(r"\[\s*Fintype\s+(?P<k>[^\]]+?)\s*\]")
ULIFT = re.compile(r"(?<![\w'])ULift(?![\w'])")
INSTANCE_REMOVAL = re.compile(r"\battribute\s*\[[^\]\n]*?(?<![\w'-])-\s*(?:instance|simp)(?![\w'])[^\]\n]*\]"
                              r"|\battribute\s*\[\s*local\s+instance(?![\w'])[^\]\n]*\][^\n]*?\bHasDerivedCategory")
TRANSPORT = re.compile(r"\b(?:HEq|eqToHom)(?![\w'])|≍|(?<![\w'.])cast(?![\w'])")
BINDER_NAMES = re.compile(r"[(\[{⦃]\s*([^\W\d][\w']*(?:\s+[^\W\d][\w']*)*)\s*:(?!=)")
QUANTIFIED_NAMES = re.compile(r"(?<![\w'])(?:∀|∃!?|fun|λ)\s+([^\W\d][\w']*(?:\s+[^\W\d][\w']*)*)\s*(?:[:,∈]|=>|↦)")


@dataclass
class LeanDecl:
    """One declaration of blanked Lean code: where it starts, its signature (binders and conclusion,
    after the name) and body as offsets into the file, and the `variable` commands (offset, text)
    and namespaces in force."""
    kw: str
    name: str
    private: bool
    at: int
    line: int
    sig_at: int
    signature: str
    body_at: int
    body: str
    variables: list[tuple[int, str]]
    namespaces: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.kw} `{self.name}`" if self.name else f"an anonymous {self.kw}"

    @property
    def conclusion_at(self) -> int:
        """The offset in the signature of what the declaration states: after the first `:` at bracket
        depth 0 (the binders before it are bracketed or bare names; a `:` of `∃ e : …` comes later)."""
        colon = depth0_index(self.signature, ":")
        return colon + 1 if colon >= 0 else len(self.signature)

    @property
    def conclusion(self) -> str:
        return self.signature[self.conclusion_at:]


def blank_strings(code: str) -> str:
    """Blank the contents of string literals, keeping the quotes, newlines and offsets."""
    return re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: '"' + re.sub(r"[^\n]", " ", m.group(0)[1:-1]) + '"', code, flags=re.S)


def bracket_depths(text: str) -> list[int]:
    """The bracket depth at each character of `text` (a bracket itself counts as outside)."""
    out, depth = [], 0
    for c in text:
        if c in CLOSERS:
            depth = max(0, depth - 1)
        out.append(depth)
        if c in OPENERS:
            depth += 1
    return out


def depth0_index(text: str, mark: str, start: int = 0) -> int:
    """The first offset from `start` of `mark` (`:` or `,`) at bracket depth 0, a `:` of `:=` or `::`
    not counting; -1 when there is none."""
    for i, depth in enumerate(bracket_depths(text)[start:], start):
        if depth or text[i] != mark:
            continue
        if mark == ":" and (text[i + 1:i + 2] in ("=", ":") or text[i - 1:i] == ":"):
            continue
        return i
    return -1


def signature_end(text: str, inductive: bool = False) -> tuple[int, int]:
    """(where the signature ends, where the body starts) in a declaration's text after its name: the
    first `:=` at bracket depth 0 that is not the binding of a `let`/`have`/`letI`/`haveI` in the
    type, else `where`, else a line opening a match arm (`| … =>`; any `|` line of an inductive)."""
    depths, pending, i = bracket_depths(text), 0, 0
    while i < len(text):
        c, prev = text[i], text[i - 1] if i else " "
        if depths[i] or c in OPENERS or c in CLOSERS:
            pass
        elif text.startswith(":=", i):
            if not pending:
                return i, i + 2
            pending -= 1
            i += 1
        elif c == "|" and not text[text.rfind("\n", 0, i) + 1:i].strip():
            eol = text.find("\n", i)
            if inductive or "=>" in text[i:eol if eol >= 0 else len(text)]:
                return i, i
        elif (c.isalpha() or c == "_") and not (prev.isalnum() or prev in "_'."):
            word = re.match(r"[\w']+", text[i:]).group(0)
            if word == "where":
                return i, i + len(word)
            pending += word in ("let", "have", "letI", "haveI")
            i += len(word) - 1
        i += 1
    return len(text), len(text)


def lean_declarations(code: str) -> tuple[list[LeanDecl], list[tuple[int, str]]]:
    """The declarations of blanked Lean code, and every `variable` command as (offset, text). A
    declaration sees the `variable` commands of its enclosing namespaces and sections, up to the
    `end` that closes them."""
    commands = list(HYGIENE_COMMAND.finditer(code))
    frames: list[list[tuple[int, str]]] = [[]]
    namespaces: list[str] = [""]
    decls, variables = [], []
    for k, m in enumerate(commands):
        kw, at = m.group("kw"), m.end()
        text = code[at:commands[k + 1].start() if k + 1 < len(commands) else len(code)]
        if kw in ("namespace", "section", "mutual"):
            frames.append([])
            namespaces.append(text.split()[0] if kw == "namespace" and text.split() else "")
        elif kw == "end":
            if len(frames) > 1:
                frames.pop()
                namespaces.pop()
        elif kw == "variable":
            frames[-1].append((at, text))
            variables.append((at, text))
        elif kw in HYGIENE_DECLS:
            named = DECL_NAME.match(text) if kw != "example" else None
            name = (named.group("name") or "") if named else ""
            start = named.end() if name else 0
            end, body = signature_end(text[start:], inductive=kw == "inductive")
            decls.append(LeanDecl(kw, name, "private" in m.group("mods").split(), m.start(),
                                  code.count("\n", 0, m.start()) + 1, at + start,
                                  text[start:start + end], at + start + body, text[start + body:],
                                  [v for frame in frames for v in frame], tuple(n for n in namespaces if n)))
    return decls, variables


def brief(text: str, limit: int = 80) -> str:
    """`text` on one line, cut at `limit` characters."""
    one = " ".join(text.split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def distinct_iso(ty: str) -> bool:
    """Whether `ty` is an isomorphism type `A ≅ B` (`≃`, `≌`, decorated as `≃ₐ[K]` or `≃+*`) at bracket
    depth 0 between two different objects. An automorphism `L ≃ₐ[K] L` is an element of a group: that
    one with a property exists is mathematics, not an isomorphism asserted to exist."""
    depths = bracket_depths(ty)
    at = next((i for i, ch in enumerate(ty) if depths[i] == 0 and ISO.match(ch)), -1)
    if at < 0:
        return False
    right = re.sub(r"^[^\s\[]*(?:\[[^\]]*\])?", "", ty[at + 1:])
    return ty[:at].split() != right.split()


def iso_headline(conclusion: str) -> bool:
    """Whether a conclusion is `Nonempty (A ≅ B)` (or `≃`, `≌`; or `Nonempty <| A ≃ B`) or
    `∃ e : A ≅ B, …`: an isomorphism asserted to exist rather than given (`distinct_iso`)."""
    c = " ".join(conclusion.split())
    if m := re.fullmatch(r"Nonempty\s*(?:<\|\s*(?P<bare>.+)|(?P<wrapped>\(.*\)))", c):
        wrapped = m.group("wrapped")
        if wrapped and not all(bracket_depths(wrapped)[1:-1]):
            return False                  # `Nonempty (A ≃ B) ∧ P`: the parenthesis closes before the end
        return distinct_iso(wrapped[1:-1] if wrapped else m.group("bare"))
    if m := re.match(r"∃\s*(?P<paren>\()?\s*[^\W\d][\w']*\s*:", c):
        comma = depth0_index(c, ",", m.end())
        ty = c[m.end():comma if comma >= 0 else len(c)].strip()
        body = c[comma + 1:].strip() if comma >= 0 else ""
        if body not in ("", "True"):
            return False                  # a property of the isomorphism is stated: a characterised existence
        return distinct_iso(ty[:-1] if m.group("paren") and ty.endswith(")") else ty)
    return False


def choice_uses(body: str) -> list[tuple[int, str]]:
    """(offset, call) of the choice functions a definition's body calls: `Classical.choose`/`choice`/`arbitrary`,
    `Nonempty.some`, `Exists.choose`, and `.choose`/`.some` on a local term (`h.some`) or a
    parenthesised one. `Option.some`, `WithTop.some` and an anonymous constructor `.some x` are not."""
    out = []
    for m in CHOICE_CALL.finditer(body):
        owner, fn = (m.group("pre") or "").split(".")[-1], m.group("fn")
        if fn in ("choice", "arbitrary"):
            chosen = owner == "Classical"
        else:
            chosen = bool(m.group("close")) or owner in ("Classical", "Nonempty", "Exists") or owner[:1].islower() \
                or owner[:1] == "_"
        if chosen and fn == "choose" and owner not in ("Classical", "Exists") and re.match(r"[ \t]+[\w(⟨'↑]", body[m.end():]):
            chosen = False                # `n.choose k`: a binomial coefficient takes an argument, a choice never does
        if chosen:
            out.append((m.start(), f"{m.group('pre') or '(…)'}.{fn}"))
    return out


def statement_key(decl: LeanDecl) -> str:
    """A theorem's statement up to whitespace and the names of its binders, with the `variable`
    commands and the namespaces in force: two theorems with one key state one thing. Binder names
    are those of bracketed binders before the conclusion (and in the variables) and of `∀`, `∃`,
    `fun` and `λ`; a type ascription `(x : ℝ)` in the conclusion is not a binder."""
    variables = " ".join(v for _at, v in decl.variables)
    text = f"{variables} ⊢ {decl.signature}"
    names = [n for group in BINDER_NAMES.findall(f"{variables} {decl.signature[:decl.conclusion_at]}")
             for n in group.split()]
    names += [n for group in QUANTIFIED_NAMES.findall(text) for n in group.split()]
    for k, name in enumerate(dict.fromkeys(n for n in names if n != "_")):
        text = re.sub(rf"(?<![\w'.]){re.escape(name)}(?![\w'])", f"#{k}", text)
    return ".".join(decl.namespaces) + "|" + re.sub(r"\s*([()\[\]{}⦃⦄⟨⟩,])\s*", r"\1", " ".join(text.split()))


def fintype_beside_field(decl: LeanDecl) -> list[tuple[int, str, bool]]:
    """(offset, k, whether in the signature) of every `[Fintype k]` in force for a declaration together
    with `[Field k]`: in its signature, or in a `variable` command in scope when the signature mentions
    `k` (so that Lean includes the variable's instances)."""
    in_force = [*decl.variables, (decl.sig_at, decl.signature)]
    out = []
    for at, text in in_force:
        for m in FINTYPE.finditer(text):
            k = " ".join(m.group("k").split())
            field_k = re.compile(r"\[\s*Field\s+" + r"\s+".join(map(re.escape, k.split())) + r"\s*\]")
            own = at == decl.sig_at
            if (own or re.search(rf"(?<![\w'.]){re.escape(k)}(?![\w'])", decl.signature)) \
                    and any(field_k.search(t) for _a, t in in_force):
                out.append((at + m.start(), k, own))
    return out


def scan_lean_statement_hygiene(lean: str, ulift: bool = False) -> list[Hit]:
    """How Suggested.lean states its targets, one hit per occurrence, each `P3 <what>; repair: <how>`
    (a duplicate statement P2). Comments, docstrings and strings are ignored.
    1. a theorem whose conclusion is `Nonempty (A ≅ B)` (`≃`, `≌`) or `∃ e : A ≅ B, …` (a name
       `nonempty_*` or `exists_*` is the corollary, and passes);
    2. a `def`, `abbrev` or `instance` whose body chooses (`Classical.choose`, `h.some`, ...);
    3. `letI` or `haveI` in a signature or a `variable` command;
    4. `[Fintype k]` with `[Field k]` in force (`fintype_beside_field`);
    5. `ULift` in the signature of a theorem, lemma, `def` or `abbrev`;
    6. `attribute [-instance …]`, `attribute [-simp …]`, `attribute [local instance] …HasDerivedCategory…`;
    7. `HEq`, `≍`, `cast` or `eqToHom` in the conclusion of a theorem that is not private;
    8. (P2) a theorem whose statement is an earlier theorem's (`statement_key`)."""
    code = blank_strings(strip_lean_comments(lean))
    found: list[tuple[int, str]] = []

    def hit(at: int, what: str, repair: str, severity: str = "P3") -> None:
        found.append((at, f"{severity} {what}; repair: {repair}"))

    decls, variables = lean_declarations(code)
    statements: dict[str, LeanDecl] = {}
    fintypes: set[int] = set()
    for d in decls:
        theorem, conclusion = d.kw in ("theorem", "lemma"), d.conclusion
        concl_at = d.sig_at + d.conclusion_at
        if theorem and not d.name.split(".")[-1].startswith(("nonempty_", "exists_")) and iso_headline(conclusion):
            hit(concl_at + len(conclusion) - len(conclusion.lstrip()), f"{d.label} concludes `{brief(conclusion)}`",
                HYGIENE_REPAIRS["nonempty-iso"])
        if d.kw in ("def", "abbrev", "instance"):
            for at, call in choice_uses(d.body):
                hit(d.body_at + at, f"{d.label} chooses its datum (`{call}`)", HYGIENE_REPAIRS["choice-data"])
        for m in (LOCAL_INSTANCE.finditer(d.signature) if d.kw != "example" else ()):
            hit(d.sig_at + m.start(), f"`{m.group(0)}` in the signature of {d.label}",
                HYGIENE_REPAIRS["local-instance"])
        if ulift and d.kw in ("theorem", "lemma", "def", "abbrev"):
            for m in ULIFT.finditer(d.signature):
                hit(d.sig_at + m.start(), f"`ULift` in the signature of {d.label}", HYGIENE_REPAIRS["ulift"])
        for at, k, own in fintype_beside_field(d):
            if at not in fintypes:            # a `variable` command's binder: once, at the first declaration it serves
                fintypes.add(at)
                where = d.label if own else f"a `variable` command in force for {d.label}"
                hit(at, f"`[Fintype {k}]` beside `[Field {k}]` ({where})", HYGIENE_REPAIRS["fintype-field"].format(k=k))
        if theorem and not d.private:
            for m in TRANSPORT.finditer(conclusion):
                hit(concl_at + m.start(), f"`{m.group(0)}` in the conclusion of {d.label}",
                    HYGIENE_REPAIRS["transport"])
        if theorem and conclusion.strip():
            first = statements.setdefault(statement_key(d), d)
            if first is not d:
                hit(d.at, f"{d.label} states what {first.label} (line {first.line}) states",
                    HYGIENE_REPAIRS["duplicate"], "P2")
    for at, text in variables:
        for m in LOCAL_INSTANCE.finditer(text):
            hit(at + m.start(), f"`{m.group(0)}` in a `variable` command", HYGIENE_REPAIRS["local-instance"])
    for m in INSTANCE_REMOVAL.finditer(code):
        hit(m.start(), f"`{brief(m.group(0))}`", HYGIENE_REPAIRS["instance-removal"])
    found.sort(key=lambda f: f[0])
    return [Hit(code.count("\n", 0, at) + 1, text) for at, text in found]


# --- README scanners ------------------------------------------------------------------------------

def scan_blob_links(readme: str, repo: str) -> list[Hit]:
    """Links into this repository pinned to a commit: they rot and are process residue."""
    return _hits(rf"github\.com/{re.escape(repo)}/blob/[0-9a-f]{{7,40}}", readme)


def scan_latex(readme: str) -> list[Hit]:
    r"""LaTeX delimiters outside code: `\(`, `\[`, `$` (house style: Unicode and code spans)."""
    return _hits(r"\\\(|\\\[|\$", strip_markdown_code(readme))


def scan_tex_residue(readme: str) -> list[Hit]:
    """TeX-only syntax outside code: `_{`, `^{`, and common macros."""
    return _hits(r"[_^]\{|\\(?:frac|mathbb|mathcal|mathfrak|operatorname|otimes|lim|sum|prod)\b", strip_markdown_code(readme))


def scan_pr_numbers(readme: str) -> list[Hit]:
    return _hits(r"pull request #\d+|\bPR\s*#?\d{2,}|#\d{3,5}\)", readme)


# "packet" only in the process sense (the explorer's source packets, hand-off packets): Arthur packets,
# tempered packets and wave-packets are mathematics (2026-10-29: 29 false hits on one automorphic README).
PROCESS_WORDS = (r"\(removed\)|\b(?:explorer'?s?|sources?|handoff|hand-off|review|fix|repair|context) packets?\b|"
                 r"\bpackets?/|packet\.json|\bcheckpoint\b|\bhandoff\b|lean-check|\bcatalogue\b|\bstand-in\b|\bso far\b|"
                 r"to be checked|<a id=|\bTODO\b|\bXXX\b|\*\*T\d{3,4}\*\*|\bjob id\b|\breview round\b")


def scan_process_residue(readme: str) -> list[Hit]:
    return _hits(PROCESS_WORDS, readme, re.I)


# --- the one residue repaired mechanically: HTML anchor tags ----------------------------------------
#
# Explorer READMEs carry `<a id="..."></a>` before their headings. GitHub gives every heading its own
# id (its slug), so the tags go; an internal link `](#id)` to a tag is pointed at the slug of the
# heading the tag precedes (or, for a tag before a paragraph, of the section that holds it). Nothing
# else of the text changes.

ANCHOR_TAG = re.compile(r"""<a\s+(?:id|name)\s*=\s*(["'])(?P<id>[^"'<>]*)\1\s*>(?P<body>[^<]*)</a>""", re.IGNORECASE)
ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*$")
FENCE_LINE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
LINK_FRAGMENT = re.compile(r"(?P<head>(?<!\\)\]\(\s*<?)#(?P<frag>[^)\s>]+)"                       # [text](#id)
                           r"|(?P<defhead>^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*<?)#(?P<deffrag>[^\s>]+)",  # [ref]: #id
                           re.MULTILINE)


def heading_text(raw: str) -> str:
    """The text of a heading as GitHub renders it: HTML tags dropped, links and images reduced to their
    text, code-span backticks and emphasis markers removed."""
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = re.sub(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);", "", text)
    text = re.sub(r"!?\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"(?<![\w\\])(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)
    return text.replace("*", "").strip()


SLUG_KEPT = ("Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Me", "Nd", "Nl", "Pc")


def github_slug(raw: str) -> str:
    """GitHub's id for a heading (before de-duplication), as github-slugger makes it: lower case;
    letters, marks, decimal and letter digits, `_` and `-` kept; every space a hyphen; everything else
    (punctuation, symbols, superscripts and subscripts) dropped."""
    out = []
    for ch in heading_text(raw).lower():
        if ch == " ":
            out.append("-")
        elif ch == "-" or unicodedata.category(ch) in SLUG_KEPT:
            out.append(ch)
    return "".join(out)


def drop_anchor_tags(line: str) -> str:
    """The line without its anchor tags: a tag wrapping text leaves the text; an empty tag goes, with
    the space after it when a space (or the line's start) precedes it, so that `a <tag> b` reads
    `a b` and `### <tag>0.1` reads `### 0.1`."""
    out, last = [], 0
    for m in ANCHOR_TAG.finditer(line):
        out.append(line[last:m.start()])
        end = m.end()
        if m.group("body"):
            out.append(m.group("body"))
        elif (m.start() == 0 or line[m.start() - 1] in " \t") and line[end:end + 1] in (" ", "\t"):
            end += 1
        last = end
    return ("".join(out) + line[last:]).rstrip() if last else line


@dataclass
class AnchorRepair:
    text: str
    removed: int = 0                                     # anchor tags taken out
    rewritten: int = 0                                   # internal links pointed at a heading slug
    unresolved: list[str] = field(default_factory=list)  # internal fragments that name no heading

    def summary(self, path: str = "README.md") -> str:
        """`497 anchor tags removed from README.md, 12 internal links pointed at heading slugs`."""
        parts = [f"{self.removed} anchor tag{'s' if self.removed != 1 else ''} removed from {path}"]
        if self.rewritten:
            parts.append(f"{self.rewritten} internal link{'s' if self.rewritten != 1 else ''} pointed at heading slugs")
        if self.unresolved:
            parts.append("internal links to no heading: " + ", ".join(f"#{f}" for f in self.unresolved[:10])
                         + (f" and {len(self.unresolved) - 10} more" if len(self.unresolved) > 10 else ""))
        return ", ".join(parts)


def strip_anchors(readme: str) -> AnchorRepair:
    """Remove every `<a id="..."></a>` tag (an anchor wrapping text keeps its text) and point the
    internal links `](#id)` and `[ref]: #id` at the slug of the heading each tag marked: the heading
    on the tag's line, else the next heading when only blank lines or other tags come between, else
    the heading of the section the tag stands in. Headings are ATX headings outside fenced code;
    slugs are de-duplicated as GitHub does (`-1`, `-2`, ...). A line that held only tags goes, with a
    blank line after it when one also precedes it."""
    lines, out, fenced = (readme or "").split("\n"), [], ""
    slugs: list[str] = []
    seen: dict[str, int] = {}
    target: dict[str, int | None] = {}      # anchor id -> index of its heading in `slugs`
    waiting: list[str] = []                 # tags on lines of their own, before the next line of text
    removed, drop_blank = 0, False

    def heading_slug(text: str) -> int:
        base = github_slug(text)
        n = seen.get(base, 0)
        seen[base] = n + 1
        slugs.append(base if n == 0 else f"{base}-{n}")
        return len(slugs) - 1

    for line in lines:
        fence = FENCE_LINE.match(line)
        if fenced:
            fenced = "" if fence and fence.group(1)[0] == fenced[0] and len(fence.group(1)) >= len(fenced) else fenced
        elif fence:
            fenced = fence.group(1)
        ids = [m.group("id") for m in ANCHOR_TAG.finditer(line)]
        if ids:
            removed += len(ids)
            line = drop_anchor_tags(line)
        if ids and not line.strip():
            waiting += ids
            drop_blank = bool(out) and not out[-1].strip()
            continue
        if drop_blank and not line.strip():
            drop_blank = False
            continue
        drop_blank = False
        out.append(line)
        heading = None if fenced or fence else ATX_HEADING.match(line)
        if heading:
            index = heading_slug(heading.group("text") or "")
            for anchor in waiting + ids:
                target.setdefault(anchor, index)
            waiting = []
        elif line.strip():
            enclosing = len(slugs) - 1 if slugs else None
            for anchor in waiting + ids:
                target.setdefault(anchor, enclosing)
            waiting = []
    for anchor in waiting:
        target.setdefault(anchor, len(slugs) - 1 if slugs else None)
    text = "\n".join(out)
    if not removed:
        return AnchorRepair(readme)

    known, rewritten, unresolved = set(slugs), 0, []

    def relink(m: re.Match) -> str:
        nonlocal rewritten
        head, frag = (m.group("head"), m.group("frag")) if m.group("head") else (m.group("defhead"), m.group("deffrag"))
        plain = unquote(frag)
        if plain in known:
            return m.group(0)
        index = target.get(plain)
        if index is None:
            if plain not in unresolved:
                unresolved.append(plain)
            return m.group(0)
        rewritten += 1
        return f"{head}#{slugs[index]}"

    code = strip_markdown_code(text)
    pieces, last = [], 0
    for m in LINK_FRAGMENT.finditer(text):
        if code[m.start():m.end()].strip() != m.group(0).strip():
            continue                       # inside code: not a link
        pieces += [text[last:m.start()], relink(m)]
        last = m.end()
    return AnchorRepair("".join(pieces) + text[last:], removed, rewritten, unresolved)


def scan_private_paths(text: str) -> list[Hit]:
    from .board import PRIVATE_PATH
    return _hits(PRIVATE_PATH.pattern, text)


def scan_forbidden(text: str, patterns: list[str]) -> list[Hit]:
    return [h for p in patterns for h in _hits(p, text)]


def scan_dropped_names(text: str, names: list[str]) -> list[Hit]:
    return [h for n in names if n for h in _hits(rf"\b{re.escape(n)}\b", text)]


# How TauCetiProgress (`progress/layers.py`) and TauCeti's Progress page (`roadmap_progress.py`) find
# a roadmap's layers, copied since this package has no dependencies: worded headings, else short-label
# headings, else bold bullets; a layer's id is its title up to the first `:`, `—`, `–`, `,` or `(`.
_LAYER_WORD = r"(?:Layer|Lane|Part|Stage|Milestone)\b"
_LAYER_SHORT = r"[A-Z]\d+[A-Za-z]?(?=\s*[:—–,])"
_LAYER_FORMS = (re.compile(rf"^#{{2,4}}\s+({_LAYER_WORD}.*?)\s*$", re.M),
                re.compile(rf"^#{{2,4}}\s+({_LAYER_SHORT}.*?)\s*$", re.M),
                re.compile(rf"^- \*\*((?:{_LAYER_WORD}|{_LAYER_SHORT})[^*]*?)\*\*", re.M))
_LAYER_ID = re.compile(rf"^((?:{_LAYER_WORD}|{_LAYER_SHORT})[^:—–,(]*?)\s*(?:[:—–,(]|$)")
_BARE_LAYER_ID = re.compile(r"\A(?:(?:Layer|Lane|Part|Stage|Milestone) [A-Za-z0-9][A-Za-z0-9.\-]*|[A-Z]\d+[A-Za-z]?)\Z")


def scan_layer_ids(readme: str) -> list[Hit]:
    """Layer headings whose id is more than a label. `## Layer 2 Rings` names its layer `Layer 2 Rings`,
    and every progress report on the roadmap then fails its check; `## Layer 2: Rings` names it `Layer 2`."""
    found = next((ms for ms in (list(rx.finditer(readme)) for rx in _LAYER_FORMS) if ms), [])
    out, seen = [], set()
    for m in found:
        title = re.sub(r"\s*\([^()]*\)\s*$", "", m.group(1)).strip().rstrip(".")
        label = _LAYER_ID.match(title)
        lid = (label.group(1) if label else title).strip()
        if not title or lid in seen:
            continue
        seen.add(lid)
        if not _BARE_LAYER_ID.match(lid):
            out.append(Hit(readme.count("\n", 0, m.start()) + 1, f"layer id {lid[:80]!r}; write `Layer N: title`"))
    return out


# --- mechanical README/Lean agreement (mech_check.py) ----------------------------------------------

TARGET_HEADING = re.compile(r"^\*\*([A-Z][^*\n]{2,150})\*\*", re.MULTILINE)
NOT_TARGETS = ("Checks", "Summary", "Dependencies", "Examples", "Verdict", "Needs", "Merge order")
LAYER_SUBSECTION = re.compile(r"^###\s+(\d+\.\d+[a-z]?\b.*)$")
_PREFIXED_SUBSECTION = re.compile(r"^###\s+((?:[A-Z][A-Za-z]*\d*\.?)+\d+(?:\.\d+)*[a-z]?)\b")   # `### PA.0.12`, `### NR0.1`, `### R07.3.1`


def scan_target_ids(readme: str) -> list[Hit]:
    """A layered README whose target subsections are not `### k.n Title`. Two packages were submitted on
    2026-10-24 with "0 targets" because their subsections were numbered `### PA.k.n` and `### NRk.n`: the
    board's target reader, the review sample and every progress report then see no targets at all. Each
    prefixed heading inside a layer is reported; a layered README with no target heading at all gets one
    report on its first layer line."""
    lines = (readme or "").splitlines()
    if not any(line.startswith("## Layer") for line in lines):
        return []
    if readme_targets(readme):
        return []
    out, inside, first_layer = [], False, 0
    for i, line in enumerate(lines, 1):
        if line.startswith("## "):
            inside = line.startswith("## Layer")
            if inside and not first_layer:
                first_layer = i
            continue
        if not inside:
            continue
        if (m := _PREFIXED_SUBSECTION.match(line)) and not m.group(1).startswith(NOT_TARGETS):
            out.append(Hit(i, f"target id {m.group(1)[:40]!r}; write `### k.n Title` (digits only, no prefix)"))
    if not out:
        out.append(Hit(first_layer or 1, "layered README states no targets (`### k.n` subsections or bold target names)"))
    return out


def readme_targets(readme: str) -> list[str]:
    """The targets of a README in TauCetiRoadmap form: the `### k.n` subsections of its layers (the
    README-shape brief), else the bold target names that open paragraphs inside its layers. Bold
    names outside the layers (a consumer named under `## Downstream consumers`) are not targets. A
    README with no `## Layer` sections is read whole."""
    def scan(lines: list[str]) -> list[str]:
        subs, bold, fenced = [], [], False
        for line in lines:
            if re.match(r"^\s*(```|~~~)", line):
                fenced = not fenced
            if fenced:
                continue
            if m := LAYER_SUBSECTION.match(line):
                subs.append(m.group(1).strip())
            elif (m := TARGET_HEADING.match(line)) and not m.group(1).startswith(NOT_TARGETS):
                bold.append(m.group(1).strip().rstrip("."))
        return subs or bold
    lines, layered, inside = (readme or "").splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            inside = line.startswith("## Layer")
            continue
        if inside:
            layered.append(line)
    return scan(layered) if any(line.startswith("## Layer") for line in lines) else scan(lines)


LEAN_DECL_NAME = re.compile(r"^(?:@\[[^\]]*\]\s*)?(?:noncomputable\s+)?(?:private\s+)?"
                            r"(?:theorem|def|structure|class|abbrev|instance|inductive|opaque|axiom)\s+([A-Za-z_][\w.'!?]*)", re.M)


def lean_names(lean: str) -> set[str]:
    return set(LEAN_DECL_NAME.findall(strip_lean_comments(lean)))


def readme_identifiers_missing(readme: str, lean: str) -> list[str]:
    """Backticked README identifiers that this roadmap would have to declare itself (a dotted name
    under one of its namespaces, or `foo_bar` whose stem is one of its declarations) but that the
    Lean file never mentions."""
    names = lean_names(lean)
    short = {n.split(".")[-1] for n in names} | names
    short_lower = {s.lower() for s in short}
    namespaces = set(re.findall(r"^namespace\s+([\w.]+)", lean, re.M))
    for n in names:
        parts = n.split(".")
        namespaces |= {".".join(parts[:k]) for k in range(1, len(parts))}
    ids = set(re.findall(r"`([A-Za-z][\w.']*(?:_[\w.']+)+)`", readme))

    def own(i: str) -> bool:
        if len(i.split("_")[0]) <= 2:
            return False
        if "." in i:
            pre = i.rsplit(".", 1)[0]
            return pre in namespaces or pre in names or pre.split(".")[-1] in short
        stem = i.split("_")[0]
        return stem in short or stem.lower() in short_lower

    return sorted(i for i in ids if own(i) and i not in names and i.split(".")[-1] not in short
                  and re.search(r"\b" + re.escape(i.split(".")[-1]) + r"\b", lean) is None)


def empty_lean_sections(lean: str) -> list[Hit]:
    """`/-! ## Layer ... -/` headers followed by no declaration before the next header."""
    secs = [(m.start(), m.group(1)) for m in re.finditer(r"^/-! ## (.*?) -/", lean, re.M)]
    out = []
    for k, (pos, title) in enumerate(secs):
        body = lean[pos:secs[k + 1][0] if k + 1 < len(secs) else len(lean)]
        if not re.search(r"^(?:@\[[^\]]*\]\s*)?(?:noncomputable\s+)?(?:theorem|def|structure|class|abbrev|instance|example|inductive)\b",
                         body, re.M):
            out.append(Hit(lean.count("\n", 0, pos) + 1, title[:80]))
    return out


def round_labels(text: str) -> list[Hit]:
    """Labels named after the process (`continuation-2`, `round2`, `gate3`, `foo_r2`). Plain words are
    not flagged: "analytic continuation" is mathematics."""
    return _hits(r"\b[\w-]*(?:continuation[-_]\d|round[-_]?\d|gate\d|_r\d)\b", text, re.I)


# --- source material: sources never enter a repository ---------------------------------------------

SOURCE_SUFFIXES = (".pdf", ".djvu", ".djv", ".ps", ".eps", ".epub")
SOURCE_MAGIC = (b"%PDF", b"AT&TFORM", b"%!PS")
TEXT_SUFFIXES = (".txt", ".text")
TEXT_LIMIT = 32 * 1024
EXTRACTION_MARK = re.compile(r"^\s*(?:\d+\s+)?=== page \d+ ===\s*$", re.MULTILINE)
EMBEDDED_KEYS = ("text", "fulltext", "excerpt", "excerpts", "quote", "quotes", "passage", "passages", "content",
                 "abstract", "extract", "extraction")
EMBEDDED_LIMIT = 1000
FENCE_LIMIT = 40
FENCE_OPEN = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^`\s]*)")


def source_material_problems(path: str, data: bytes) -> list[str]:
    """Why a file must not enter a repository: a source document (PDF, DjVu, PostScript or EPUB, by
    name or by content), or more than 32 KB of extracted text (a text file, or text carrying the
    sources service's page markers). Sources live only in the operator's cache."""
    low = path.lower()
    epub = data[:2] == b"PK" and b"application/epub+zip" in data[:120]
    if low.endswith(SOURCE_SUFFIXES) or data.lstrip()[:8].startswith(SOURCE_MAGIC) or epub:
        return [(f"{path}: a source document never enters a repository (the manifest records its hash, locator "
                 "scheme and licence class; the text stays in the operator's cache)")]
    if len(data) > TEXT_LIMIT and (low.endswith(TEXT_SUFFIXES)
                                   or EXTRACTION_MARK.search(data.decode("utf-8", errors="replace"))):
        return [(f"{path}: {len(data) // 1024} KB of extracted text never enters a repository (quote locators, "
                 "never passages)")]
    return []


def manifest_text_problems(path: str, text: str) -> list[str]:
    """Manifest entries that embed a source's text: a field named for one (text, excerpt, quote,
    passage, ...) or a value longer than a citation needs."""
    import tomllib
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []          # reported as invalid TOML elsewhere
    problems = []
    for k, entry in enumerate(data.get("source", []) if isinstance(data.get("source"), list) else [], 1):
        if not isinstance(entry, dict):
            continue
        where = f"{path}: source {k} ({entry.get('id', '?')})"
        for key, value in entry.items():
            if key.lower() in EMBEDDED_KEYS:
                problems.append(f"{where}: `{key}` embeds text; the manifest records locators, never passages")
            elif isinstance(value, str) and (len(value) > EMBEDDED_LIMIT or value.count("\n") >= 3):
                problems.append(f"{where}: `{key}` is {len(value)} characters; the manifest records locators, "
                                "never passages")
    return problems


def scan_long_fences(text: str, limit: int = FENCE_LIMIT) -> list[Hit]:
    """Fenced blocks longer than `limit` lines that are neither Lean nor a table: a passage pasted from
    a source. Reports, briefs and READMEs quote locators, never passages."""
    hits, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        fence, lang = m.group(1), m.group(2).lower()
        close = re.compile(rf"^\s{{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        body = [line for line in lines[i + 1:j] if line.strip()]
        table = bool(body) and all(line.lstrip()[:1] in ("|", "+") for line in body)
        if len(lines[i + 1:j]) > limit and lang not in ("lean", "lean4") and not table:
            hits.append(Hit(i + 1, f"a {j - i - 1}-line fenced block ({lang or 'no language'}): quote a locator, "
                                   f"not a passage (at most {limit} lines unless Lean or a table)"))
        i = j + 1
    return hits


def tree_source_problems(root: Path, folder: str) -> list[str]:
    """Every file under `folder` of a checkout, through the source-material and manifest checks."""
    problems = []
    for path in sorted((root / folder).rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            rel = str(path.relative_to(root))
            data = path.read_bytes()
            problems += source_material_problems(rel, data)
            if path.name == "sources.toml":
                problems += manifest_text_problems(rel, data.decode("utf-8", errors="replace"))
    return problems


# --- the gate -------------------------------------------------------------------------------------

@dataclass
class GateResult:
    name: str
    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, list] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(v == 0 for v in self.counts.values())

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def add(self, key: str, hits: list) -> None:
        self.counts[key] = len(hits)
        if hits:
            self.details[key] = hits

    def summary(self) -> str:
        return f"gate {self.name}: " + " ".join(f"{k}={v}" for k, v in self.counts.items()) + (" OK" if self.ok else " FAIL")

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "counts": self.counts, "notes": self.notes,
                "details": {k: [h.__dict__ if isinstance(h, Hit) else h for h in v] for k, v in self.details.items()}}


ROADMAP_FILES = ("README.md", "Suggested.lean", "metadata.toml")     # `[gates] roadmap_files` by default


@dataclass
class GateOptions:
    content_repo: str = "TauCetiProject/TauCetiRoadmap"
    active_dir: str = "TauCetiRoadmap"
    max_lean_lines: int = 5000
    forbid_mathlib_subimports: bool = True
    forbidden_patterns: list[str] = field(default_factory=list)
    dropped_names: list[str] = field(default_factory=list)
    outside_folder_allow: list[str] = field(default_factory=list)
    order: list[str] | None = None
    sibling_readmes: dict[str, str] = field(default_factory=dict)
    main_extended: list[str] = field(default_factory=list)
    tauceti_sources: Path | None = None      # the pinned Tau Ceti's sources, when a wired checkout holds them
    # The roadmap's folder under `active_dir` when it is not `<Name>`: a nested roadmap
    # (`RepresentationTheory/CompactGroups`, module `TauCetiRoadmap.RepresentationTheory.CompactGroups.Suggested`).
    folder: str = ""
    # The files a roadmap folder may hold besides `*.lean` files and nested roadmap folders.
    roadmap_files: list[str] = field(default_factory=lambda: list(ROADMAP_FILES))
    # The roadmaps on the content repository's main (`merged_roadmaps`): suppliers available to every
    # pull request, never UPWARD, whatever the merge order of open pull requests says of them.
    merged: set[str] = field(default_factory=set)
    # The statement-hygiene scan of Suggested.lean (`[prreview] statement_hygiene`).
    statement_hygiene: bool = True

    @classmethod
    def from_config(cls, cfg, order=None, siblings=None, dropped=None, tauceti=None, merged=None) -> "GateOptions":
        g = cfg.gates
        return cls(content_repo=cfg.repositories.content, active_dir=cfg.roadmap.active_dir,
                   max_lean_lines=g.max_lean_lines, forbid_mathlib_subimports=g.forbid_mathlib_subimports,
                   forbidden_patterns=list(g.forbidden_patterns), dropped_names=list(dropped or []),
                   outside_folder_allow=list(g.outside_folder_allow), order=order if g.require_depcheck else None,
                   sibling_readmes=dict(siblings or {}), main_extended=list(cfg.roadmap.main_extended),
                   tauceti_sources=tauceti, roadmap_files=list(g.roadmap_files), merged=set(merged or ()),
                   statement_hygiene=cfg.prreview.statement_hygiene)


def scan_folder(name: str, readme: str, lean: str, opts: GateOptions) -> GateResult:
    """Every text scanner on one roadmap's README and Suggested.lean (no git, no build)."""
    r = GateResult(name)
    r.add("set_option", scan_set_option(lean))
    r.add("blob", scan_blob_links(readme, opts.content_repo))
    r.add("latex", scan_latex(readme))
    r.add("tex", scan_tex_residue(readme))
    r.add("prnum", scan_pr_numbers(readme))
    r.add("sorry-data", scan_sorry_data(lean))
    r.add("sorry-examples", scan_sorry_examples(lean))
    r.add("residue", scan_process_residue(readme) + scan_lean_residue(lean))
    r.add("private-paths", scan_private_paths(readme) + scan_private_paths(lean))
    r.add("imports", scan_lean_imports(lean, opts.tauceti_sources))
    r.add("mathlib-subimports", scan_mathlib_subimports(lean) if opts.forbid_mathlib_subimports else [])
    r.add("no-import-mathlib", scan_missing_mathlib_import(lean))
    r.add("lean-lines", scan_lean_length(lean, opts.max_lean_lines))
    r.add("statement-hygiene", scan_lean_statement_hygiene(lean) if opts.statement_hygiene else [])
    r.add("forbidden", scan_forbidden(readme, opts.forbidden_patterns) + scan_forbidden(lean, opts.forbidden_patterns))
    r.add("dropped", scan_dropped_names(readme, opts.dropped_names) + scan_dropped_names(lean, opts.dropped_names))
    r.add("readme-ids-missing", readme_identifiers_missing(readme, lean))
    r.add("empty-sections", empty_lean_sections(lean))
    r.add("round-labels", round_labels(readme + "\n" + lean))
    r.add("long-fences", scan_long_fences(readme))
    r.add("layer-ids", scan_layer_ids(readme))
    r.add("target-ids", scan_target_ids(readme))
    if opts.order is not None:
        readmes = dict(opts.sibling_readmes)
        readmes[name] = readme
        edges = depgraph.supplier_edges(readmes, set(opts.order), set(opts.main_extended))
        r.add("depcheck", depgraph.check_order(edges, opts.order, only={name}, merged=opts.merged))
    return r


# The rule behind each count of `scan_folder`, in the words a repair brief quotes.
GATE_RULES = {
    "set_option": "no `set_option` in the code of Suggested.lean",
    "blob": "no link into the roadmap repository pinned to a commit (`github.com/<repo>/blob/<sha>`): link a path "
            "on main, or name the roadmap and section",
    "latex": "no LaTeX delimiters (`\\(`, `\\[`, `$`) outside code spans and code blocks: write the mathematics in "
             "Unicode, or put it in a code span",
    "tex": "no TeX-only syntax (`_{`, `^{`, `\\frac`, `\\mathbb`, `\\sum`, ...) outside code spans and code blocks, "
           "headings included: write it in Unicode or put it in a code span, saying the same mathematics; a heading "
           "you change gets a new slug, so update the internal links `](#...)` that point at it",
    "prnum": "no pull-request numbers in the README",
    "sorry-data": "no `sorry` in the body of a `def`, `abbrev`, `instance` or `opaque`",
    "sorry-examples": "every `example` is proved: an admitted example tests nothing",
    "residue": "no process residue: in the README none of `(removed)`, packet, checkpoint, handoff, lean-check, "
               "catalogue, stand-in, so far, to be checked, `<a id=` anchor tags, TODO, XXX, bold job numbers like "
               "`**T123**`, job id, review round; in Suggested.lean no `#print axioms`, `#eval`, `#synth`, `lemma`, "
               "`: True :=` or `: Prop := sorry`",
    "private-paths": "no machine paths (home directories, temporary folders) in the README or Suggested.lean",
    "imports": "Suggested.lean imports `Mathlib` first, then only the individual `TauCeti.*` modules it uses, each "
               "once and all in the header, and each one exists at the pinned Tau Ceti",
    "mathlib-subimports": "no `import Mathlib.<submodule>`: the single line `import Mathlib`",
    "no-import-mathlib": "Suggested.lean has the line `import Mathlib`",
    "lean-lines": "Suggested.lean stays within the project's line limit (`[gates] max_lean_lines`)",
    "statement-hygiene": "Suggested.lean states its targets cleanly: no theorem concluding `Nonempty (A ≅ B)` or "
                         "`∃ e : A ≃ B, …` unless named `nonempty_*`/`exists_*`, no choice in the body of a `def`, "
                         "`abbrev` or `instance`, no `letI`/`haveI` in a signature, `[Finite k]` and not "
                         "`[Fintype k]` beside `[Field k]`, no `ULift` in the signature of a theorem or definition, no "
                         "`attribute [-instance]`/`[-simp]` or local `HasDerivedCategory` instance, no `HEq`, `≍`, "
                         "`cast` or `eqToHom` in a public theorem's conclusion, and no two theorems with one "
                         "statement; each hit names its severity (P2 for a duplicate statement, else P3) and its "
                         "repair",
    "forbidden": "no match of the project's forbidden patterns (`[gates] forbidden_patterns`)",
    "dropped": "no name of the project's dropped-names list",
    "readme-ids-missing": "every backticked identifier the README presents as this roadmap's own declaration (a "
                          "claim that Suggested.lean declares it) is declared or mentioned in Suggested.lean; repair "
                          "an API bullet or Check by stating its fact in prose without the name, never by adding a "
                          "declaration",
    "empty-sections": "no `/-! ## ... -/` section header in Suggested.lean without a declaration under it",
    "round-labels": "no labels named after the process (`continuation-2`, `round2`, `gate3`, `_r2`)",
    "long-fences": "no fenced block of more than 40 lines that is neither Lean nor a table: quote a locator, not "
                   "a passage",
    "layer-ids": "a layer heading reads `Layer N: title`: its id, before the colon, is a bare label",
    "stray-files": "a roadmap folder holds only README.md, Suggested.lean, metadata.toml (`[gates] roadmap_files`), "
                   "other `*.lean` files and the folders of nested roadmaps: delete anything else (SOURCES.md, "
                   "PROGRESS.md, STATUS.md, notes, scratch, other subfolders); sources are the README's references",
    "depcheck": "every supplier the README names comes before this roadmap in the merge order (an UPWARD edge or a "
                "CYCLE is a design defect: break it by moving ownership), and the roadmap is listed there; a roadmap "
                "on the content repository's main is a supplier available to all, never upward; the merge-order "
                "file is not yours to edit, so a `NOT IN ORDER` line is the board's to settle: report it",
}


def outside_folder(changed: list[str], folder: str, allow: list[str]) -> list[str]:
    """Changed paths outside the roadmap folder that no allow-pattern covers."""
    prefix = folder.rstrip("/") + "/"
    return [p for p in changed if not p.startswith(prefix) and not any(re.search(a, p) for a in allow)]


def _git(worktree: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {out.stderr.strip()[:200]}")
    return out.stdout


def roadmap_names(paths, dirs) -> set[str]:
    """The roadmaps among repository `paths`: the name (last component) of every folder below one of
    `dirs`, at any depth, that holds a README.md and is named like a roadmap."""
    roots = [d.strip("/") + "/" for d in dirs if d and d.strip("/")]
    out = set()
    for path in paths:
        root = next((r for r in roots if path.startswith(r)), None)
        parts = path[len(root):].split("/") if root else []
        if len(parts) >= 2 and parts[-1] == "README.md" and re.fullmatch(NAME_RE, parts[-2]):
            out.add(parts[-2])
    return out


def merged_roadmaps(checkout: Path, active_dir: str, completed_dir: str, branch: str = "main") -> set[str]:
    """The roadmaps on `origin/<branch>` of a content checkout (or a worktree of one), under the active
    and the completed directories (`roadmap_names`): suppliers available to every pull request. Empty
    when the checkout cannot list that branch."""
    try:
        listing = _git(Path(checkout), "ls-tree", "-r", "--name-only", f"origin/{branch}", "--",
                       *(d for d in (active_dir, completed_dir) if d))
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return set()
    return roadmap_names(listing.splitlines(), (active_dir, completed_dir))


def roadmap_module(active_dir: str, folder: str) -> str:
    """The Lean module of a roadmap's Suggested.lean: `<active_dir>.<folder, dotted>.Suggested`."""
    return f"{active_dir}.{folder.strip('/').replace('/', '.')}.Suggested"


def consumer_modules(root: Path, active_dir: str, folder: str) -> list[str]:
    """The Lean modules under `active_dir` outside the roadmap folder that import one of its modules
    (`<active_dir>.<folder, dotted>` or a module below it): the roadmap's consumers in the tree. Their
    build is part of the roadmap's: a change that passes its own targeted build can still rename or
    drop a declaration a sibling roadmap imports, and the main branch breaks on merge."""
    prefix = re.escape(f"{active_dir}.{folder.strip('/').replace('/', '.')}")
    pattern = re.compile(rf"^[ \t]*(?:public\s+)?import\s+{prefix}(?:\.[\w«».]+)?\s*$", re.M)
    base, own = Path(root) / active_dir, Path(root) / active_dir / folder.strip("/")
    out = []
    for path in sorted(base.rglob("*.lean")) if base.is_dir() else []:
        if own in path.parents or ".lake" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(strip_lean_comments(text)):
            out.append(".".join(path.relative_to(root).with_suffix("").parts))
    return out


def build_targets(root: Path, active_dir: str, folder: str) -> str:
    """What the roadmap's targeted build builds: its own module, then its consumers in the tree."""
    return " ".join([roadmap_module(active_dir, folder), *consumer_modules(root, active_dir, folder)])


def metadata_problems(root: Path, active_dir: str, folder: str) -> list[str]:
    """A roadmap folder has its metadata.toml; a nested roadmap may share its family's, in a folder
    above it under the active directory (`RepresentationTheory/metadata.toml`)."""
    rel = folder.removeprefix(active_dir.rstrip("/") + "/").strip("/").split("/")
    if any((root / active_dir / Path(*rel[:k]) / "metadata.toml").is_file() for k in range(len(rel), 0, -1)):
        return []
    return [f"{folder}/metadata.toml missing"]


def stray_files(root: Path, folder: str, allowed: list[str]) -> list[str]:
    """What sits directly in a roadmap folder of a checkout besides the `allowed` files (`[gates]
    roadmap_files`) and `*.lean` files: every other file, and every subfolder that is not a nested
    roadmap (a folder named like a roadmap with a README.md of its own), each as `<path>: not a
    roadmap file`."""
    base = Path(root) / folder
    out = []
    for path in sorted(base.iterdir()) if base.is_dir() else ():
        if path.is_dir():
            if not (re.fullmatch(NAME_RE, path.name) and (path / "README.md").is_file()):
                out.append(f"{folder}/{path.name}/: not a roadmap file")
        elif path.name not in allowed and not path.name.endswith(".lean"):
            out.append(f"{folder}/{path.name}: not a roadmap file")
    return out


def run_gate(worktree: Path, name: str, opts: GateOptions, base: str = "", build_cmd: str = "") -> GateResult:
    """The full gate on a worktree: text scanners, dirty tree, outside-folder diff against the
    merge-base with `base`, and (if given) the targeted build, which must succeed with `sorry` as
    the only warning. The roadmap is `<active_dir>/<name>/`, or `<active_dir>/<opts.folder>/`."""
    folder = f"{opts.active_dir}/{opts.folder.strip('/') or name}"
    root = Path(worktree)
    readme_path, lean_path = root / folder / "README.md", root / folder / "Suggested.lean"
    missing = [str(p.relative_to(root)) for p in (readme_path, lean_path) if not p.is_file()]
    if missing:
        r = GateResult(name)
        r.add("missing-files", missing)
        return r
    lean = lean_path.read_text(encoding="utf-8", errors="replace")
    if opts.tauceti_sources is None:
        opts = _replace(opts, tauceti_sources=tauceti_sources(root))
    r = scan_folder(name, readme_path.read_text(encoding="utf-8", errors="replace"), lean, opts)
    r.add("metadata", metadata_problems(root, opts.active_dir, folder))
    r.add("stray-files", stray_files(root, folder, opts.roadmap_files))
    material = tree_source_problems(root, folder)
    try:
        r.add("dirty", [line for line in _git(root, "status", "--short").splitlines() if line.strip()])
        if base:
            mb = _git(root, "merge-base", base, "HEAD").strip()
            changed = [p for p in _git(root, "diff", "--name-only", mb, "HEAD").splitlines() if p.strip()]
            r.add("outside-folder", outside_folder(changed, folder, opts.outside_folder_allow))
            for p in changed:
                if not p.startswith(folder.rstrip("/") + "/") and (root / p).is_file():
                    material += source_material_problems(p, (root / p).read_bytes())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        r.add("git", [str(exc)])
    r.add("source-material", material)
    if build_cmd:
        built = build_problems(root, build_cmd.format(module=build_targets(root, opts.active_dir, opts.folder or name)),
                               folder=folder)
        r.add("build", explain_build_problems(lean, built, opts.tauceti_sources))
    return r


BUILD_TIMEOUT = 3600


def build_output(worktree: Path, command: str, timeout: int = BUILD_TIMEOUT) -> tuple[str, int | None, str]:
    """Run a build or an elaboration: (its output, its exit code, '' or why it did not run)."""
    try:
        out = subprocess.run(command.split(), cwd=worktree, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", None, f"build did not run: {exc}"
    return out.stdout + out.stderr, out.returncode, ""


def build_problems(worktree: Path, command: str, timeout: int = BUILD_TIMEOUT, folder: str = "") -> list[str]:
    """Run the targeted build; problems are a failure or any warning other than `sorry`. With `folder`
    (the roadmap folder, relative to the worktree) a warning in a file outside it is not a problem: the
    build includes the roadmap's consumers, and their own lint debt is theirs, while an error anywhere is."""
    text, code, failure = build_output(worktree, command, timeout)
    return [failure] if failure else build_output_problems(text, code, folder=folder)


DIAGNOSTIC = re.compile(r"^\s*(?:\S.*?\.lean:\d+:\d+:\s*)?(?P<severity>error|warning)\b")
LAKE_STATUS = re.compile(r"^\s*(?:[✖⚠✔ℹ√×!]\s*\[\d+/\d+\]|Build completed|Some required builds|trace:|info:|- [A-Z]\w*(?:\.\w+)*\s*$)")
SORRY_WARNING = re.compile(r"declaration uses ['`]sorry['`]")
LEAN_FILE = re.compile(r"(\S+\.lean):\d+:\d+")
MESSAGE_CHARS = 1000      # one diagnostic with its continuation lines: never cut mid-path
MAX_DIAGNOSTICS = 40      # the cap is on their number, never on their length


def build_diagnostics(text: str) -> list[tuple[str, str, str]]:
    """(severity, file, message) of every error and warning in a build's or `lean`'s output, each
    message carrying its continuation lines (a goal, a hint) up to MESSAGE_CHARS. The `sorry` warning
    and Lake's `error: build failed` epilogue are not diagnostics."""
    out: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = DIAGNOSTIC.match(line)
        if m:
            if "error: build failed" in line or (m.group("severity") == "warning" and SORRY_WARNING.search(line)):
                out.append([])         # swallow its continuation too
                continue
            fm = LEAN_FILE.search(line)
            out.append([m.group("severity"), fm.group(1) if fm else "", line.strip()])
        elif out and out[-1] and line.strip() and not LAKE_STATUS.match(line):
            out[-1][2] += "\n    " + line.strip()
        elif LAKE_STATUS.match(line):
            out.append([])
    result = []
    for d in out:
        if d:
            sev, file, msg = d
            result.append((sev, file, msg if len(msg) <= MESSAGE_CHARS else msg[:MESSAGE_CHARS - 1] + "…"))
    return result


def build_output_problems(text: str, returncode: int, folder: str = "") -> list[str]:
    """The problems a build reports, each whole. With an error present the warnings are left out
    (they return once the errors are gone); the first error of every file survives the cap. With
    `folder`, warnings in files outside it (consumer modules built alongside) are left out."""
    problems = [] if returncode == 0 else [f"build exited {returncode}"]
    diagnostics = build_diagnostics(text)
    if folder:
        prefix = folder.strip("/") + "/"
        diagnostics = [d for d in diagnostics if d[0] == "error" or not d[1] or d[1].lstrip("./").startswith(prefix)]
    errors = [d for d in diagnostics if d[0] == "error"]
    shown = errors or diagnostics
    if len(shown) > MAX_DIAGNOSTICS:
        keep, seen = set(), set()
        for k, (_sev, file, _msg) in enumerate(shown):
            if file not in seen:
                seen.add(file)
                keep.add(k)
        for k in range(len(shown)):
            if len(keep) >= MAX_DIAGNOSTICS:
                break
            keep.add(k)
        dropped = len(shown) - len(keep)
        shown = [d for k, d in enumerate(shown) if k in keep]
        shown.append(("", "", f"… and {dropped} more {'errors' if errors else 'warnings'} not shown"))
    return problems + [msg for _sev, _file, msg in shown]


IMPORT_EXPLANATION = ("an import that is neither Mathlib nor a module of the pinned Tau Ceti cannot be elaborated "
                      "here; Suggested.lean imports Mathlib and the individual `TauCeti.*` modules it uses")


def foreign_imports(lean: str, tauceti: Path | None = None) -> list[str]:
    """The imported modules that are neither `Mathlib` nor an existing `TauCeti.*` module (with the
    pinned sources at hand; without them every `TauCeti.*` module counts as existing)."""
    return [m for _l, m, _t in lean_imports(lean)
            if m != "Mathlib" and not (m.startswith("TauCeti.") and tauceti_module_exists(m, tauceti))]


def explain_build_problems(lean: str, problems: list[str], tauceti: Path | None = None) -> list[str]:
    """When the first error is a missing object file and the file imports something that is neither
    Mathlib nor an existing `TauCeti.*` module, say what the error means (the agent otherwise reads
    it as a broken machine)."""
    first = next((p for p in problems if re.search(r"\berror\b", p)), "")
    if "object file" in first and foreign_imports(lean, tauceti):
        return problems + [IMPORT_EXPLANATION]
    return problems


# --- padding probe -------------------------------------------------------------------------------
# The reviews' one-tactic probe, run mechanically: a sorried theorem that one trivial tactic closes
# restates a definition, mirrors a Check or computes a value, and is padding in the acceptance test.

PROBE_TACTIC = "by first | rfl | infer_instance | (simp) | sorry"
PROBE_DECL = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|public)\s+)*(?:theorem|lemma)\s+([^\s(:{\[]+)")
PROBE_SORRY = re.compile(r":(\d+):\d+: warning: declaration uses [`']sorry[`']")


def padding_probe_text(lean: str, heartbeats: int = 40000) -> tuple[str, dict[int, str]]:
    """A copy of Suggested.lean in which the `sorry` closing each theorem or lemma is replaced by one
    trivial tactic that falls back to `sorry`, with a heartbeat cap after the imports. Returns the
    text and the probed declarations by the line of their keyword (1-based), as the sorry warnings
    report them. Definitions, instances and examples are left alone."""
    lines = lean.split("\n")
    probed: dict[int, str] = {}
    current: tuple[int, str] | None = None
    for i, line in enumerate(lines):
        m = PROBE_DECL.match(line)
        if m:
            current = (i + 1, m.group(1))
        if current is None:
            continue
        stripped = line.rstrip()
        if stripped.endswith(":= sorry"):
            lines[i] = stripped[: -len("sorry")] + PROBE_TACTIC
        elif stripped.strip() == "sorry" and i > 0 and lines[i - 1].rstrip().endswith(":="):
            lines[i] = line.replace("sorry", PROBE_TACTIC, 1)
        else:
            continue
        probed[current[0]] = current[1]
        current = None
    last_import = max((i for i, l in enumerate(lines) if l.startswith("import ")), default=-1)
    lines.insert(last_import + 1, f"set_option maxHeartbeats {heartbeats}")
    shifted = {k + (1 if k > last_import + 1 else 0): v for k, v in probed.items()}
    return "\n".join(lines), shifted


def padding_hits(output: str, probed: dict[int, str]) -> list[str]:
    """The probed declarations the elaboration output no longer reports as using `sorry`: one
    trivial tactic closed them. Empty when the output has an error (the probe is inconclusive then)."""
    if re.search(r"^\S+:\d+:\d+: error", output, re.M):
        return []
    still_sorry = {int(m.group(1)) for m in PROBE_SORRY.finditer(output)}
    return [f"{name} (line {line})" for line, name in sorted(probed.items()) if line not in still_sorry]
