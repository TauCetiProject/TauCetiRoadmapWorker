"""The elaborated type of every declaration of a Suggested.lean, and the section binders it lost.

A `def`, an `instance` or a `sorry`'d declaration keeps only the section `variable`s its type
mentions, so a hypothesis such as `[CompleteSpace X]` or `(hp : p.Prime)` written in a `variable`
block silently disappears from what contributors will be held to. Reviews find this defect again
after it was "fixed" more often than any other Lean defect, because nothing shows the elaborated
type. The worker shows it: it elaborates a copy of the file with `#check @<name>` appended for every
declaration, and writes a transcript with each elaborated type and the section binders that look
dropped (an instance binder whose class occurs nowhere in the type, or a named hypothesis `h…` whose
name does not). The flags are leads for the reviewer, never a gate.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT = 1800
DECL = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable|nonrec|unsafe|partial)\s+)*"
                  r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque)\b\s*([^\s:({\[⦃]*)")
SCOPE = re.compile(r"^\s*(namespace|section|end)\b\s*([^\s]*)")
VARIABLE = re.compile(r"^\s*variable\b(.*)$")
MESSAGE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<kind>info|warning|error): ?(?P<text>.*)$")
OPEN, CLOSE = "([{⦃", ")]}⦄"


@dataclass
class Decl:
    name: str              # fully qualified
    kind: str
    line: int              # 1-based, in the original file
    binders: list[str] = field(default_factory=list)   # section binders in scope, as written


def binder_groups(text: str) -> list[str]:
    """The bracketed binder groups of a `variable` command's text, outermost only."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in OPEN:
            if depth == 0:
                start = i
            depth += 1
        elif ch in CLOSE and depth:
            depth -= 1
            if depth == 0:
                out.append(text[start:i + 1])
    return out


def declarations(text: str) -> list[Decl]:
    """The named declarations of a Lean file with their full names and the section binders in scope.
    Anonymous instances are skipped (nothing to `#check`)."""
    scopes: list[tuple[str, str, list[str]]] = []     # (kind, name, binders)
    out: list[Decl] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = SCOPE.match(line)
        if m:
            kind, name = m.group(1), m.group(2)
            if kind == "end":
                if scopes:
                    scopes.pop()
            else:
                scopes.append((kind, name, []))
            i += 1
            continue
        v = VARIABLE.match(line)
        if v:
            chunk, j = v.group(1), i + 1
            # The command goes on while a bracket is open, or on indented lines that start a binder.
            while j < len(lines) and (
                    chunk.count("(") + chunk.count("[") + chunk.count("{") + chunk.count("⦃") >
                    chunk.count(")") + chunk.count("]") + chunk.count("}") + chunk.count("⦄")
                    or (lines[j][:1].isspace() and lines[j].lstrip()[:1] in OPEN)):
                chunk += " " + lines[j].strip()
                j += 1
            groups = binder_groups(chunk)
            if scopes:
                scopes[-1][2].extend(groups)
            else:
                scopes.append(("file", "", groups))
            i = j
            continue
        d = DECL.match(line)
        if d and d.group(2) and not d.group(2).startswith(("[", "(", "{")):
            name = d.group(2).rstrip(".")           # `foo.{u}`: the universe list is not part of the name
            if name.startswith("_root_."):
                full = name.removeprefix("_root_.")
            else:
                prefix = ".".join(s[1] for s in scopes if s[0] == "namespace" and s[1])
                full = f"{prefix}.{name}" if prefix else name
            out.append(Decl(full, d.group(1), i + 1, [b for s in scopes for b in s[2]]))
        i += 1
    return out


def augmented(text: str, decls: list[Decl]) -> tuple[str, dict[int, Decl]]:
    """The file with `#check @<name>` appended for each declaration, and the line of each check."""
    base = text.rstrip("\n").split("\n")
    lines, at = list(base) + [""], {}
    for d in decls:
        lines.append(f"#check @{d.name}")
        at[len(lines)] = d
    return "\n".join(lines) + "\n", at


SEVERITY = {"information": "info", "warning": "warning", "error": "error"}


def messages(output: str) -> list[tuple[int, str, str]]:
    """(line, kind, text) of each Lean message: from `lean --json` output (one JSON object per line),
    else from text output (`file:line:col: kind: text`, continuation lines joined)."""
    parsed = []
    for raw in output.split("\n"):
        if raw.startswith("{"):
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if isinstance(m, dict) and "pos" in m:
                parsed.append((int(m["pos"].get("line", 0)), SEVERITY.get(m.get("severity", ""), m.get("severity", "")),
                               str(m.get("data", "")).rstrip()))
    if parsed:
        return parsed
    out: list[list] = []
    for raw in output.split("\n"):
        m = MESSAGE.match(raw)
        if m:
            out.append([int(m.group("line")), m.group("kind"), m.group("text")])
        elif out:
            out[-1][2] += "\n" + raw
    return [(line, kind, text.rstrip()) for line, kind, text in out]


def json_command(command: str) -> str:
    """The elaboration command with `--json` after its `lean` (`lake env lean {file}` → `lake env lean
    --json {file}`), so that every message carries its position; unchanged when there is no `lean`."""
    return re.sub(r"(?<![\w-])lean(?=\s)", "lean --json", command, count=1) if "--json" not in command else command


IDENT = re.compile(r"[^\W\d][\w'₀-₉]*")


def _names(binder: str) -> list[str]:
    """The variable names a binder group introduces (`(p q : ℕ)` → p, q; `[inst : Foo X]` → inst;
    `[Foo X]` → none)."""
    inner = binder[1:-1]
    if ":" not in inner:
        return []
    return [n for n in inner.split(":", 1)[0].split() if IDENT.fullmatch(n)]


def _occurs(name: str, text: str) -> bool:
    return re.search(r"(?<![\w'.])" + re.escape(name) + r"(?![\w'])", text) is not None


def dropped(decl: Decl, signature: str) -> list[str]:
    """The section binders that are hypotheses about objects the declaration keeps, but that its
    elaborated type lost: an instance binder (`[CompleteSpace X]`) or a named hypothesis (`(hp : p.Prime)`)
    every variable of which occurs in the type while the binder itself does not. This is how Lean
    drops them: a `def` keeps only the binders its type or body uses, and any declaration loses a
    hypothesis it does not mention. Binders about objects the declaration does not use are not
    flagged."""
    variables = {n for b in decl.binders for n in _names(b)}
    out = []
    for binder in decl.binders:
        inner = binder[1:-1].strip()
        body = inner.split(":", 1)[1] if ":" in inner and _names(binder) else inner
        deps = [v for v in variables if _occurs(v, body)]
        if not deps or not all(_occurs(v, signature) for v in deps):
            continue
        if binder.startswith("["):
            head = body.strip().split()[0].lstrip("@") if body.strip() else ""
            if head and not _occurs(head, signature):
                out.append(binder)
            continue
        for name in _names(binder):
            if name not in deps and name.startswith("h") and not _occurs(name, signature):
                out.append(f"{binder[0]}{name} :{body}{binder[-1]}")
    return out


@dataclass
class Transcript:
    entries: list[tuple[Decl, str, list[str]]]   # (declaration, elaborated type or '', binders that look dropped)
    errors: list[str]                             # error messages of the elaboration (first lines)
    reason: str = ""                              # why there is no transcript at all

    @property
    def flagged(self) -> int:
        return sum(1 for _d, _s, gone in self.entries if gone)


def transcript(text: str, output: str, decls: list[Decl], at: dict[int, Decl]) -> Transcript:
    """Pair each `#check` line's message with its declaration. Text output prints a `#check` result
    without a position: then it is found by the declaration's name at the start of a line."""
    found: dict[str, str] = {}
    errors = []
    for line, kind, msg in messages(output):
        if line in at and kind == "info":
            found[at[line].name] = msg.strip()
        elif kind == "error":
            errors.append(f"line {line}: {msg.splitlines()[0][:200] if msg else ''}")
    if not found:
        lines = output.split("\n")
        for d in decls:
            for i, raw in enumerate(lines):
                if raw.startswith((f"{d.name} :", f"@{d.name} :")):
                    block = [raw] + [x for x in lines[i + 1:i + 40] if x.startswith(" ")][:39]
                    found[d.name] = "\n".join(block)
                    break
    entries = [(d, found.get(d.name, ""), dropped(d, found[d.name]) if d.name in found else []) for d in decls]
    return Transcript(entries, errors)


def elaborate(command: str, lean: Path, out_dir: Path, cwd: Path, timeout: int = TIMEOUT) -> Transcript:
    """Elaborate a copy of `lean` with the checks appended (`command` holds `{file}`), in `cwd`."""
    text = lean.read_text(encoding="utf-8")
    decls = declarations(text)
    if not decls:
        return Transcript([], [], "no named declaration")
    copy_text, at = augmented(text, decls)
    out_dir.mkdir(parents=True, exist_ok=True)
    copy = out_dir / "SignatureCheck.lean"
    copy.write_text(copy_text, encoding="utf-8")
    try:
        result = subprocess.run(json_command(command).format(file=str(copy.resolve())), shell=True, cwd=cwd,
                                capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Transcript([], [], f"the elaboration took longer than {timeout // 60} minutes")
    except OSError as exc:
        return Transcript([], [], f"the elaboration did not run ({exc})")
    output = result.stdout + "\n" + result.stderr
    found = transcript(text, output, decls, at)
    if not any(sig for _d, sig, _g in found.entries):
        first = next((line for line in output.splitlines() if line.strip()), "no output")
        found.reason = f"the elaboration printed no signature (exit {result.returncode}: {first[:200]})"
    return found


def report(t: Transcript, what: str) -> str:
    """The transcript as Markdown, for the job's scratch folder."""
    if t.reason:
        return f"# Elaborated signatures of {what}\n\nNot available: {t.reason}.\n"
    lines = [f"# Elaborated signatures of {what}", "",
             f"{len(t.entries)} declarations; {t.flagged} with section binders that look dropped from the elaborated "
             "type (an instance whose class does not occur in it, a hypothesis `h…` whose name does not). These are "
             "leads: check each against the README statement.", ""]
    if t.errors:
        lines += ["Elaboration errors (the transcript may be incomplete):", ""] + [f"- {e}" for e in t.errors[:20]] + [""]
    for d, sig, gone in t.entries:
        lines.append(f"## `{d.name}` ({d.kind}, line {d.line})")
        lines.append("")
        lines += ["```lean", sig or "(no signature printed)", "```"]
        if gone:
            lines += ["", "Looks dropped: " + ", ".join(f"`{g}`" for g in gone)]
        lines.append("")
    return "\n".join(lines)
