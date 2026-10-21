"""Push gates for one roadmap folder, as functions returning a structured result.

Port of the maintainer's gate.sh (with the mechanical checks of mech_check.py and readthrough.py),
plus the dependency check and merge chain from depgraph.py. `run_gate` reads
`<worktree>/<active_dir>/<Name>/{README.md,Suggested.lean}`, runs every scanner, and returns a
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
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import depgraph


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


def scan_lean_length(lean: str, max_lines: int) -> list[Hit]:
    count = len(lean.splitlines())
    return [Hit(count, f"{count} lines (at most {max_lines})")] if max_lines and count > max_lines else []


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


PROCESS_WORDS = (r"\(removed\)|\bpacket\b|\bcheckpoint\b|\bhandoff\b|lean-check|\bcatalogue\b|\bstand-in\b|\bso far\b|"
                 r"to be checked|<a id=|\bTODO\b|\bXXX\b|\*\*T\d{3,4}\*\*|\bjob id\b|\breview round\b")


def scan_process_residue(readme: str) -> list[Hit]:
    return _hits(PROCESS_WORDS, readme, re.I)


def scan_private_paths(text: str) -> list[Hit]:
    return _hits(r"/Users/[^\s/]+|/home/[^\s/]+/|/private/(?:tmp|var)/|/tmp/[\w.-]+/|[A-Za-z]:\\Users\\", text)


def scan_forbidden(text: str, patterns: list[str]) -> list[Hit]:
    return [h for p in patterns for h in _hits(p, text)]


def scan_dropped_names(text: str, names: list[str]) -> list[Hit]:
    return [h for n in names if n for h in _hits(rf"\b{re.escape(n)}\b", text)]


# --- mechanical README/Lean agreement (mech_check.py) ----------------------------------------------

TARGET_HEADING = re.compile(r"^\*\*([A-Z][^*\n]{2,150})\*\*", re.MULTILINE)
NOT_TARGETS = ("Checks", "Summary", "Dependencies", "Examples", "Verdict", "Needs", "Merge order")
LAYER_SUBSECTION = re.compile(r"^###\s+(\d+\.\d+[a-z]?\b.*)$")


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


def check_labels_missing(readme: str, lean: str) -> list[str]:
    """README Check labels `(*label*)` with no counterpart in the Lean file."""
    labels = set(re.findall(r"\(\*([a-z0-9][a-z0-9_-]*)\*\)", readme))
    return sorted(lab for lab in labels if lab not in lean and lab.replace("-", "_") not in lean)


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

    @classmethod
    def from_config(cls, cfg, order=None, siblings=None, dropped=None) -> "GateOptions":
        g = cfg.gates
        return cls(content_repo=cfg.repositories.content, active_dir=cfg.roadmap.active_dir,
                   max_lean_lines=g.max_lean_lines, forbid_mathlib_subimports=g.forbid_mathlib_subimports,
                   forbidden_patterns=list(g.forbidden_patterns), dropped_names=list(dropped or []),
                   outside_folder_allow=list(g.outside_folder_allow), order=order if g.require_depcheck else None,
                   sibling_readmes=dict(siblings or {}), main_extended=list(cfg.roadmap.main_extended))


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
    r.add("mathlib-subimports", scan_mathlib_subimports(lean) if opts.forbid_mathlib_subimports else [])
    r.add("no-import-mathlib", scan_missing_mathlib_import(lean))
    r.add("lean-lines", scan_lean_length(lean, opts.max_lean_lines))
    r.add("forbidden", scan_forbidden(readme, opts.forbidden_patterns) + scan_forbidden(lean, opts.forbidden_patterns))
    r.add("dropped", scan_dropped_names(readme, opts.dropped_names) + scan_dropped_names(lean, opts.dropped_names))
    r.add("readme-ids-missing", readme_identifiers_missing(readme, lean))
    r.add("check-labels-missing", check_labels_missing(readme, lean))
    r.add("empty-sections", empty_lean_sections(lean))
    r.add("round-labels", round_labels(readme + "\n" + lean))
    r.add("long-fences", scan_long_fences(readme))
    if opts.order is not None:
        readmes = dict(opts.sibling_readmes)
        readmes[name] = readme
        edges = depgraph.supplier_edges(readmes, set(opts.order), set(opts.main_extended))
        r.add("depcheck", depgraph.check_order(edges, opts.order, only={name}))
    return r


def outside_folder(changed: list[str], folder: str, allow: list[str]) -> list[str]:
    """Changed paths outside the roadmap folder that no allow-pattern covers."""
    prefix = folder.rstrip("/") + "/"
    return [p for p in changed if not p.startswith(prefix) and not any(re.search(a, p) for a in allow)]


def _git(worktree: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {out.stderr.strip()[:200]}")
    return out.stdout


def run_gate(worktree: Path, name: str, opts: GateOptions, base: str = "", build_cmd: str = "") -> GateResult:
    """The full gate on a worktree: text scanners, dirty tree, outside-folder diff against the
    merge-base with `base`, and (if given) the targeted build, which must succeed with `sorry` as
    the only warning."""
    folder = f"{opts.active_dir}/{name}"
    root = Path(worktree)
    readme_path, lean_path = root / folder / "README.md", root / folder / "Suggested.lean"
    missing = [str(p.relative_to(root)) for p in (readme_path, lean_path) if not p.is_file()]
    if missing:
        r = GateResult(name)
        r.add("missing-files", missing)
        return r
    r = scan_folder(name, readme_path.read_text(encoding="utf-8", errors="replace"),
                    lean_path.read_text(encoding="utf-8", errors="replace"), opts)
    r.add("metadata", [] if (root / folder / "metadata.toml").is_file() else [f"{folder}/metadata.toml missing"])
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
        r.add("build", build_problems(root, build_cmd.format(module=f"{opts.active_dir}.{name}.Suggested")))
    return r


def build_problems(worktree: Path, command: str, timeout: int = 3600) -> list[str]:
    """Run the targeted build; problems are a failure or any warning other than `sorry`."""
    try:
        out = subprocess.run(command.split(), cwd=worktree, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"build did not run: {exc}"]
    text = out.stdout + out.stderr
    return build_output_problems(text, out.returncode)


def build_output_problems(text: str, returncode: int) -> list[str]:
    problems = [] if returncode == 0 else [f"build exited {returncode}"]
    for line in text.splitlines():
        if re.search(r"\berror\b", line) and "error: build failed" not in line:
            problems.append(line.strip()[:200])
        elif re.search(r"\bwarning\b", line) and not re.search(r"declaration uses 'sorry'|declaration uses `sorry`", line):
            problems.append(line.strip()[:200])
    return problems
