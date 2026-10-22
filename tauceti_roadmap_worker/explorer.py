"""`board import-explorer`: carry roadmap work from a checkout of the explorer
(CBirkbeck/tauceti-explorer) onto this board, once per roadmap.

The import reads, under `research/blueprint/` of the checkout: `queue.json` (the explorer's jobs and
their states), `issues.json` (explorer job id -> issue number), `packages/<Name>/` (README.md,
Suggested.lean, metadata.toml and the verdict review.json), `readmes/<Name>.md` and
`packets/<Name>.json` (a design and its plan) and `reviews/REV-DESIGN-*.md` (design verdicts). For
each roadmap the most advanced stage decides:

| Explorer state                                                  | Board job        | Files under packages/<Name>/ |
| --------------------------------------------------------------- | ---------------- | ---------------------------- |
| package accepted by an independent review (`independent-review-`) | RT-<Name>~1a, ~1b | README.md, Suggested.lean, metadata.toml, review.json |
| package accepted only by its fixer (`fix-`), or not reviewed yet | REV-PKG-<Name>   | README.md, Suggested.lean, metadata.toml |
| design accepted by its review, not packaged                      | PKG-<Name>       | design/README.md, design/plan.json |
| design written, its review not done                              | REV-DESIGN-<Name> | design/README.md, design/plan.json |
| design queued, not written                                       | TARGET-<Name>    | none |

A fixer's acceptance never counts, so a self-accepted package gets the independent review here.
An accepted package goes to its red-team rounds, which run on the import pull request itself (kept
open, as a submitted package's pull request is); the port follows the last round.
Skipped, and said so: a roadmap already ported (its folder is on the roadmap repository's main,
active or completed, or an open pull request there adds `<active_dir>/<Name>/README.md`), a roadmap
with an explorer job in flight (`external`, or claimed on its issue), a review that asks for
changes, and a roadmap the board already has (any job of that name, or an open import pull request
for it). Every board job records the explorer job and issue it rests
on (`inputs.explorer`), so a rerun imports nothing twice.

On the way, a README loses its `<a id>` anchor tags (process residue to the package gate; its
internal links are pointed at the heading slugs), and a roadmap whose `packets/<Name>.json` records
sources gets a `sources.toml` made from them (`packet_manifest`: each work `uncertified`, since the
board has fetched none, with the hash of the copy the explorer read). Once the pull requests are
open, the packages and accepted designs among them enter the merge order on the board's main, each
after its last supplier as its README names them (a design under review enters when the board's
review accepts it).

Files reach the board only through a pull request, one per roadmap, from the maintainer's fork,
after the board's own submission gates (source material, privacy, JSON/TOML). Its body carries a
marker naming the jobs; `board intake` merges it (author association, files, checks) and only then
creates the jobs. A TARGET job carries no files and is created at once. Explorer text is data: it
is copied, never read as an instruction.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import board, gates, gitops, sources
from .board import Job, comment_json, make_id, new_job
from .gh import GHError, transient

EXPLORER = "CBirkbeck/tauceti-explorer"
BLUEPRINT = Path("research") / "blueprint"
IN_FLIGHT = ("external", "claimed")
WORK_KINDS = ("design", "package", "review", "fix")
STAGES = ("package", "design", "target")
IMPORT_MARK = "tauceti-roadmap-import"
IMPORT_RE = re.compile(r"<!--\s*" + IMPORT_MARK + r"\s*(\{.*?\})\s*-->", re.DOTALL)
ISSUE_IN_NOTE = re.compile(r"issue #(\d+)")
# A review's verdict: a line that opens with it ("**Accepted as ...", "Verdict: needs changes"), or
# "verdict" followed by it, within the review's head.
VERDICT_LINE = re.compile(r"^\W*(?:verdict\W*)?(accepted|needs[ _-]changes|rejected)\b", re.IGNORECASE | re.MULTILINE)
VERDICT = re.compile(r"verdict\W{0,6}(accepted|needs[ _-]changes|rejected)", re.IGNORECASE)
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")


@dataclass
class Explorer:
    root: Path
    jobs: list[dict]
    issues: dict[str, int]

    @classmethod
    def read(cls, root: Path) -> Explorer:
        bp = Path(root) / BLUEPRINT
        queue = json.loads((bp / "queue.json").read_text(encoding="utf-8"))
        jobs = queue.get("jobs", []) if isinstance(queue, dict) else queue
        try:
            raw = json.loads((bp / "issues.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        issues = {str(k): v for k, v in (raw.items() if isinstance(raw, dict) else []) if isinstance(v, int)}
        return cls(Path(root), [j for j in jobs if isinstance(j, dict) and j.get("id")], issues)

    @property
    def bp(self) -> Path:
        return self.root / BLUEPRINT

    def of(self, name: str) -> list[dict]:
        return [j for j in self.jobs if name in (j.get("roadmapIds") or [])]

    def issue(self, job_id: str) -> int:
        if job_id in self.issues:
            return self.issues[job_id]
        job = next((j for j in self.jobs if j["id"] == job_id), {})
        m = ISSUE_IN_NOTE.search(job.get("note") or "")
        return int(m.group(1)) if m else 0

    def names(self) -> list[str]:
        found = {n for j in self.jobs if j.get("kind") in ("design", "package") for n in j.get("roadmapIds") or []}
        packages = self.bp / "packages"
        if packages.is_dir():
            found |= {p.name for p in packages.iterdir() if p.is_dir()}
        return sorted(n for n in found if re.fullmatch(board.NAME_RE, n))


@dataclass
class Step:
    name: str
    stage: str                       # package | design | target (what --kinds selects)
    job: Job | None = None
    files: dict[str, Path] = field(default_factory=dict)   # board path -> file in the explorer checkout
    source: str = ""                 # the explorer job the step rests on
    issue: int = 0
    skip: str = ""                   # why nothing is imported
    outcome: str = ""                # what the run did
    failed: bool = False             # the run could not carry it out (the import goes on with the next)
    extra: list[Job] = field(default_factory=list)          # more jobs of the same step (red-team passes)
    generated: dict[str, str] = field(default_factory=dict)  # board path -> text made here (the sources manifest)
    readme: str = ""                 # the README that places it in the merge order, once its pull request is open

    def paths(self) -> list[str]:
        """Every board path the step's pull request carries."""
        return sorted({*self.files, *self.generated})

    def generated_count(self) -> str:
        """`12 sources`: the entries of the manifest the step makes."""
        n = sum(text.count("\n[[source]]\n") for text in self.generated.values())
        return f"{n} source" + ("" if n == 1 else "s")



def enters_order(job: Job) -> bool:
    """Whether an imported job's roadmap enters the merge order with its import: a package, or a
    design its review accepted. A design still under review (and a target) enters when the board's
    design review accepts it."""
    return not (job.kind == "target" or (job.kind == "review" and board.artefact_of(job.id) == "DESIGN"))


def finished(job: dict) -> str:
    return str(job.get("finishedAt") or "")


def verdict_of(text: str) -> str:
    """accepted | needs_changes | rejected, read from the first lines of a review, or ''."""
    head = "\n".join((text or "").splitlines()[:40])
    m = VERDICT_LINE.search(head) or VERDICT.search(head)
    return re.sub(r"[ -]", "_", m.group(1).lower()) if m else ""


def explorer_inputs(ex: Explorer, source: str) -> dict:
    return {"explorer": {"repo": EXPLORER, "job": source, "issue": ex.issue(source)}, "origin": f"explorer:{source}"}


# --- the sources manifest an import carries -------------------------------------------------------------

YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")
ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)",
                       re.IGNORECASE)
ARXIV_YEAR = re.compile(r"^(?:[a-z-]+(?:\.[A-Z]{2})?/)?(\d{2})\d{2}", re.IGNORECASE)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEME_WORDS = (("tag", r"\bTags?\b"),
                ("theorem", r"\b(?:Theorem|Thm|Lemma|Proposition|Prop|Corollary|Cor|Definition|Remark|Example)\b"),
                ("section", r"§|\bSections?\b|\bSect\."),
                ("chapter", r"\bChapters?\b|\bCh\."),
                ("page", r"\bpp?\.\s*\d|\bpages?\b"))
NOTE_LIMIT = 300


def _text(value) -> str:
    if isinstance(value, list):
        return "; ".join(_text(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(_text(v) for v in value.values())
    return " ".join(str(value or "").split())


def source_year(entry: dict, arxiv: str):
    """The year of an explorer source: the first one its id, edition or title names, else its arXiv
    id's; `n.d.` when none does."""
    for key in ("id", "edition", "title"):
        m = YEAR.search(_text(entry.get(key)))
        if m:
            return int(m.group(1))
    m = ARXIV_YEAR.match(arxiv)
    if m:
        yy = int(m.group(1))
        return 1900 + yy if yy >= 90 else 2000 + yy
    return "n.d."


def locator_scheme(entry: dict, url: str) -> str:
    """How the roadmap locates statements in the source, read from the explorer's notes on what it read
    in it (the locator words they use most), `tag` for the Stacks project, `section` when they say
    nothing."""
    if sources.host_of(url) == "stacks.math.columbia.edu":
        return "tag"
    read = _text({k: v for k, v in entry.items() if k not in ("id", "title", "authors", "url", "sha256", "edition")})
    counts = [(len(re.findall(rx, read)), -k, scheme) for k, (scheme, rx) in enumerate(SCHEME_WORDS)]
    best = max(counts)
    return best[2] if best[0] else "section"


def packet_manifest(packet: Path, name: str, deny: tuple[str, ...] = ()) -> str:
    """The sources manifest (`sources.toml`) of the works an explorer packet records under `sources`,
    or '' when it records none: each with its id, authors, title, year, a locator scheme, the URL a
    reader finds it at (unless the deny list refuses its host), its arXiv id and DOI, the SHA-256 of
    the copy the explorer read, the access date and the edition as the version note. The board has
    fetched none of them, so each is `uncertified` with licence class `none`; a lane that fetches one
    (`sources fetch --id <id>`) records its text and hash as certified."""
    try:
        data = json.loads(Path(packet).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    found = data.get("sources") if isinstance(data, dict) else None
    manifest, seen = sources.new_manifest(name), set()
    for e in found if isinstance(found, list) else []:
        if not isinstance(e, dict):
            continue
        sid, authors, title = _text(e.get("id")), _text(e.get("authors")), _text(e.get("title"))
        if not (sid and authors and title) or sid.lower() in seen:
            continue
        seen.add(sid.lower())
        url = _text(e.get("url"))
        if url and (not sources.host_of(url) or sources.classify_url(url, {}, list(deny)) == "denied"):
            url = ""
        m = ARXIV_URL.search(url)
        arxiv = re.sub(r"\.pdf$", "", m.group(1)) if m else ""
        entry = {"id": sid, "authors": authors, "title": title, "year": source_year(e, arxiv), "arxiv": arxiv,
                 "locator_scheme": locator_scheme(e, url), "url": url, "licence_class": sources.NO_TEXT,
                 "status": "uncertified"}
        doi = _text(e.get("doi"))
        if sources.DOI_RE.match(doi):
            entry["doi"] = doi
        sha = _text(e.get("sha256")).lower()
        if sources.SHA_RE.match(sha):
            entry["sha256"] = sha
        accessed = next((_text(e.get(k)) for k in ("accessed", "accessDate", "readDate")
                         if ISO_DATE.match(_text(e.get(k)))), "")
        if accessed:
            entry["accessed"] = accessed
        edition = _text(e.get("edition"))
        if edition:
            entry["version_note"] = edition if len(edition) <= NOTE_LIMIT else edition[:NOTE_LIMIT - 1] + "…"
        manifest["source"].append({k: v for k, v in entry.items() if v not in ("", None)})
    return sources.dump_manifest(manifest) if manifest["source"] else ""


def plan_roadmap(ex: Explorer, name: str, packages_dir: str,
                 redteam: tuple[tuple[str, str], ...] = (("codex", ""), ("claude", "")),
                 deny: tuple[str, ...] = ()) -> Step | None:
    """What the import does for one roadmap (None: the explorer has nothing for it). Pure."""
    mine = ex.of(name)
    pkg, dest = ex.bp / "packages" / name, board.package_dir(packages_dir, name)
    readme, packet = ex.bp / "readmes" / f"{name}.md", ex.bp / "packets" / f"{name}.json"
    designs = [j for j in mine if j.get("kind") == "design"]
    stage = ("package" if (pkg / "README.md").is_file() else
             "design" if any(d.get("state") == "done" for d in designs) else "target")
    flying = [j for j in mine if j.get("state") in IN_FLIGHT and j.get("kind") in WORK_KINDS]
    if flying:
        j = flying[0]
        return Step(name, stage, source=j["id"], issue=ex.issue(j["id"]),
                    skip=f"in flight in the explorer: {j['id']} is {j['state']}")
    if stage == "package":
        step = plan_package(ex, name, pkg, dest, mine, redteam)
    elif stage == "design":
        step = plan_design(ex, name, dest, designs, mine, readme, packet)
    else:
        step = plan_target(ex, name, designs)
    if step is not None and step.job is not None and not step.skip and step.files:
        manifest = packet_manifest(packet, name, deny)
        if manifest:
            step.generated[board.manifest_path(packages_dir, name)] = manifest
    return step


def plan_design(ex: Explorer, name: str, dest: str, designs: list[dict], mine: list[dict], readme: Path,
                packet: Path) -> Step:
    """A design the explorer wrote: its review here (REV-DESIGN), or its package once its review accepted it."""
    if not (readme.is_file() and packet.is_file()):
        return Step(name, "design", skip="the explorer's design README or plan packet is missing")
    files = {f"{dest}/design/README.md": readme, f"{dest}/design/plan.json": packet}
    last = max((d for d in designs if d.get("state") == "done"), key=finished)
    reviews = [r for r in mine if r.get("kind") == "review" and r["id"].startswith("REV-DESIGN-")
               and r.get("state") == "done" and finished(r) >= finished(last)]
    if not reviews:
        job = new_job(make_id("review", name, subject="DESIGN"), inputs=explorer_inputs(ex, last["id"]),
                      note=f"Imported from the explorer: the design {last['id']} awaits its independent review.")
        return Step(name, "design", job, files, last["id"], ex.issue(last["id"]))
    review = max(reviews, key=finished)
    path = ex.bp / "reviews" / f"{review['id']}.md"
    verdict = verdict_of(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else ""
    if verdict != "accepted":
        return Step(name, "design", source=review["id"], issue=ex.issue(review["id"]),
                    skip=f"the explorer's design review {review['id']} says {verdict or 'nothing readable'}")
    job = new_job(make_id("package", name), inputs=explorer_inputs(ex, review["id"]),
                  note=f"Imported from the explorer: the design was accepted by {review['id']}.")
    return Step(name, "design", job, files, review["id"], ex.issue(review["id"]))


def plan_target(ex: Explorer, name: str, designs: list[dict]) -> Step | None:
    """A design the explorer only queued: a TARGET job (None when it has not even that)."""
    queued = [d for d in designs if d.get("state") == "pending"]
    if not queued:
        return None
    d = queued[0]
    statement = str(d.get("name") or name) + (f"\n\nScope: {d['scope']}" if isinstance(d.get("scope"), str) else "")
    form = {"statement": statement, "why": f"Queued in the explorer as {d['id']} and moved to this board.",
            "name": name}
    job = new_job(make_id("target", name), inputs={"form": form, **explorer_inputs(ex, d["id"])},
                  note=f"Imported from the explorer's queue ({d['id']}).")
    return Step(name, "target", job, {}, d["id"], ex.issue(d["id"]))


def plan_package(ex: Explorer, name: str, pkg: Path, dest: str, mine: list[dict],
                 redteam: tuple[tuple[str, str], ...] = (("codex", ""), ("claude", ""))) -> Step:
    files = {f"{dest}/{f}": pkg / f for f in ("README.md", "Suggested.lean", "metadata.toml") if (pkg / f).is_file()}
    try:
        review = json.loads((pkg / "review.json").read_text(encoding="utf-8")).get("review", {})
    except (OSError, ValueError, AttributeError):
        review = {}
    status, reviewer = str(review.get("status", "")), str(review.get("reviewer", ""))
    if status == "accepted" and reviewer.startswith("independent-review-"):
        source = reviewer[len("independent-review-"):]
        files[f"{dest}/review.json"] = pkg / "review.json"
        passes = [new_job(rt, requires_agent=agent, requires_model=model, inputs=explorer_inputs(ex, source),
                          note=f"Imported from the explorer: the package was accepted by the independent review {source}; "
                               "its red-team rounds run here.")
                  for rt, (agent, model) in zip(board.redteam_passes(name, 1, len(redteam)), redteam, strict=True)]
        return Step(name, "package", passes[0], files, source, ex.issue(source), extra=passes[1:])
    if status in ("needs_changes", "rejected"):
        return Step(name, "package", source=reviewer, skip=f"the explorer's package review says {status} ({reviewer})")
    if status == "accepted" and reviewer.startswith("fix-"):
        source, why = reviewer[len("fix-"):], "its only acceptance was its fixer's own"
    else:
        done = [j for j in mine if j.get("kind") == "package" and j.get("state") == "done"]
        source, why = (max(done, key=finished)["id"] if done else f"PKG-{name}"), "it has no review yet"
    job = new_job(make_id("review", name, subject="PKG"), inputs=explorer_inputs(ex, source),
                  note=f"Imported from the explorer ({source}): {why}, so this is its independent review.")
    return Step(name, "package", job, files, source, ex.issue(source))


def plan_import(ex: Explorer, packages_dir: str, jobs: list[Job], pending: dict[str, int],
                kinds: tuple[str, ...] = STAGES, names: tuple[str, ...] = (),
                ported: dict[str, str] | None = None,
                redteam: tuple[tuple[str, str], ...] = (("codex", ""), ("claude", "")),
                deny: tuple[str, ...] = ()) -> list[Step]:
    """Every roadmap's step, filtered by stage and name, with what is already done marked as
    skipped. `pending` maps roadmap names to open import pull requests, `ported` to where the roadmap
    repository has them already. Pure."""
    ported = ported or {}
    steps = []
    for name in names or ex.names():
        step = plan_roadmap(ex, name, packages_dir, redteam, deny)
        if step is None:
            if names:
                steps.append(Step(name, "-", skip="the explorer has nothing for it"))
            continue
        if step.stage not in kinds:
            continue
        on_board = sorted(j.id for j in jobs if j.name == name)
        if name in ported:
            step.skip = f"ported: {ported[name]}"
        elif not step.skip and on_board:
            step.skip = "already on the board: " + ", ".join(on_board[:3])
        elif not step.skip and name in pending:
            step.skip = f"waiting in import pull request #{pending[name]}"
        steps.append(step)
    return steps


# --- the pull request and its marker ------------------------------------------------------------------

def encode_import(steps: list[Step]) -> str:
    data = {"source": EXPLORER, "jobs": [j.spec() for s in steps for j in [s.job, *s.extra]],
            "files": sorted(f for s in steps for f in s.paths())}
    return f"<!-- {IMPORT_MARK} " + comment_json(data) + " -->"


def decode_import(body: str) -> dict | None:
    """{jobs: [Job], files: [path]} from an import pull request's marker, or None."""
    found = IMPORT_RE.findall(body or "")
    if not found:
        return None
    try:
        data = json.loads(found[-1])
    except ValueError:
        return None
    jobs = [board.job_from_spec(spec) for spec in data.get("jobs", [])] if isinstance(data, dict) else []
    files = data.get("files", []) if isinstance(data, dict) else []
    if not jobs or any(j is None for j in jobs) or not isinstance(files, list):
        return None
    return {"jobs": jobs, "files": [str(f) for f in files]}


def import_deliverables(found: dict | None, packages_dir: str) -> set[str]:
    """The files an import pull request carries as its own: those its marker declares under the
    package folders of the roadmaps it names (what `intake_import` checks when it takes the pull
    request in). They stay deliverables of the pull request at every later merge of it, beside the
    files of the fixes pushed to it (the explorer's review.json, which no board job writes)."""
    if not found:
        return set()
    folders = tuple(board.package_dir(packages_dir, j.name) + "/" for j in found["jobs"])
    return {f for f in found["files"] if f.startswith(folders)}


def open_imports(ctx) -> dict[str, int]:
    """Roadmap name -> open import pull request on the packages repository."""
    repo = ctx.cfg.repositories.packages_repo()
    out = {}
    for pr in ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True) or []:
        found = decode_import(pr.get("body") or "")
        for job in (found or {}).get("jobs", []):
            out.setdefault(job.name, pr["number"])
    return out


def ported_roadmaps(ctx) -> dict[str, str]:
    """Roadmap name -> where the roadmap repository has it already: a folder on main (active or
    completed), or an open pull request that adds its README. Read once per run: the folder
    listings, then the open pull requests and their files."""
    cfg, repo = ctx.cfg, ctx.cfg.repositories.content
    out: dict[str, str] = {}
    for folder in (cfg.roadmap.active_dir, cfg.roadmap.completed_dir):
        try:
            entries = ctx.gh.api(f"repos/{repo}/contents/{folder}?ref={cfg.project.branch}") or []
        except GHError:
            entries = []
        for e in entries if isinstance(entries, list) else []:
            if e.get("type") == "dir":
                out.setdefault(e["name"], f"on {repo} main ({folder}/{e['name']})")
    readme = re.compile(rf"^{re.escape(cfg.roadmap.active_dir)}/({board.NAME_RE})/README\.md$")
    for pr in ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True) or []:
        for f in ctx.gh.api(f"repos/{repo}/pulls/{pr['number']}/files?per_page=100", paginate=True) or []:
            m = readme.match(f.get("filename", ""))
            if m and f.get("status") != "removed":
                out.setdefault(m.group(1), f"open pull request #{pr['number']} on {repo}")
    return out


def normalise_readme(path: Path) -> str:
    """The README's `<a id>` anchor tags removed, its internal links pointed at the heading slugs
    (`gates.strip_anchors`; nothing else of the text changes). Returns what changed ('' if nothing)."""
    fixed = gates.strip_anchors(path.read_text(encoding="utf-8"))
    if not fixed.removed:
        return ""
    path.write_text(fixed.text, encoding="utf-8")
    return fixed.summary(path.name if path.parent.name != "design" else "design/README.md")


def open_pull_request(ctx, step: Step) -> str:
    """Copy the step's files onto a branch of the maintainer's board checkout (a README with its
    anchor tags removed, and the sources manifest made from the explorer's packet), run the submission
    gates on them, push to the fork and open the pull request. Returns what happened."""
    cfg, repo = ctx.cfg, ctx.cfg.repositories.packages_repo()
    checkout = ctx.state / "import" / "board"
    gitops.sync_checkout(repo, checkout, "main", scrub=(board.package_dir(cfg.board.packages_dir, step.name),))
    branch = f"import/explorer-{gitops.kebab(step.name)}"
    gitops.git(checkout, "checkout", "-q", "-B", branch)
    problems, notes, paths = [], [], step.paths()
    readmes = {}
    for rel in paths:
        (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
        if rel in step.files:
            shutil.copyfile(step.files[rel], checkout / rel)
        else:
            (checkout / rel).write_text(step.generated[rel], encoding="utf-8")
        if rel.endswith("/README.md"):
            notes.append(normalise_readme(checkout / rel))
            readmes[rel] = (checkout / rel).read_text(encoding="utf-8")
        data = (checkout / rel).read_bytes()
        problems += board.file_problems(rel, data.decode("utf-8", errors="replace"), set(paths), data)
    if problems:
        return "refused by the submission gates: " + "; ".join(problems[:3])
    pkg = board.package_dir(cfg.board.packages_dir, step.name)
    placing = readmes.get(f"{pkg}/README.md") or readmes.get(f"{pkg}/design/README.md", "")
    gitops.commit_paths(checkout, paths, f"Import {step.name} from the explorer ({step.source})", ctx.operator)
    fork = retrying(lambda: gitops.ensure_fork(ctx.gh, repo, ctx.operator))
    retrying(lambda: gitops.push(checkout, fork, branch))
    link = f"https://github.com/{EXPLORER}/issues/{step.issue}" if step.issue else EXPLORER
    made = [n for n in notes if n] + ([f"`sources.toml` made from the explorer's packet ({step.generated_count()})"]
                                       if step.generated else [])
    body = (f"Imports `{step.name}` from the explorer: {step.source} ({link}). When this merges, `board intake` "
            f"creates `{step.job.id}`.\n\nFiles: " + ", ".join(f"`{f}`" for f in paths) + "\n\n"
            + ("Changed on the way: " + "; ".join(made) + ".\n\n" if made else "")
            + encode_import([step]))
    pr = retrying(lambda: gitops.open_pull_request(ctx.gh, repo, f"{ctx.operator}:{branch}", "main",
                                                   f"Import {step.name} from the explorer ({step.job.id})", body,
                                                   draft=False))
    step.readme = placing
    return "; ".join([f"pull request #{(pr or {}).get('number', '?')}"] + [n for n in notes if n])


RETRIES, BACKOFF = 2, 30         # a refused push or a GitHub 5xx/timeout is tried again twice, 30 s apart
SLEEP = time.sleep


def retrying(do):
    for attempt in range(RETRIES + 1):
        try:
            return do()
        except (gitops.PushError, GHError) as exc:
            if attempt == RETRIES or (isinstance(exc, GHError) and not transient(exc)):
                raise
            SLEEP(BACKOFF)


def import_explorer(ctx, root: Path, kinds: tuple[str, ...] = STAGES, names: tuple[str, ...] = (),
                    yes: bool = False) -> list[Step]:
    ex = Explorer.read(root)
    steps = plan_import(ex, ctx.cfg.board.packages_dir, ctx.board.all_jobs(), open_imports(ctx), kinds, names,
                        ported_roadmaps(ctx), tuple((p.agent, p.model) for p in ctx.cfg.redteam.passes),
                        tuple(ctx.cfg.sources.deny))
    for step in steps:
        if step.skip:
            step.outcome = "skipped: " + step.skip
        elif not yes:
            step.outcome = "would " + ("open an import pull request" if step.files else "create the job") + " (dry run)"
        else:
            # A failure (a refused push, GitHub slow to answer) is reported, and the import goes on.
            try:
                if step.files:
                    step.outcome = open_pull_request(ctx, step)
                else:
                    number = ctx.board.create(step.job, "available", preamble=(
                        f"Imported from the explorer's queue: {step.source} "
                        f"(https://github.com/{EXPLORER}/issues/{step.issue})." if step.issue
                        else f"Imported from the explorer's queue: {step.source}."))
                    step.outcome = f"created #{number}"
            except (gitops.GitError, GHError, OSError) as exc:
                step.failed, step.outcome = True, f"failed: {str(exc).splitlines()[0][:200]}"
    place_in_order(ctx, [s for s in steps if s.readme and not s.failed and enters_order(s.job)])
    return steps


def place_in_order(ctx, steps: list[Step]) -> None:
    """The roadmaps whose import pull request opened enter the merge order on the board's main, each
    after its last supplier as the imported README names them, in one commit (`board intake` catches
    up an import this misses). A design still under review enters when the board's review accepts it."""
    if not steps:
        return
    from .intake import insert_roadmaps
    try:
        notes = insert_roadmaps(ctx, {s.name: s.readme for s in steps})
    except GHError as exc:
        notes = {s.name: f"merge order not updated ({str(exc).splitlines()[0][:200]}; `board intake` places it)"
                 for s in steps}
    for s in steps:
        s.outcome += "; " + notes.get(s.name, "merge order not updated")


def summary(steps: list[Step]) -> str:
    rows = [("Roadmap", "Stage", "Explorer job", "Issue", "Board job", "Files", "Outcome")]
    for s in steps:
        rows.append((s.name, s.stage, s.source or "-", f"#{s.issue}" if s.issue else "-", s.job.id if s.job and not s.skip
                     else "-", str(len(s.paths())) if not s.skip else "-", s.outcome or s.skip))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r[:-1], widths, strict=True)) + "  " + r[-1] for r in rows]
    counts = {}
    for s in steps:
        key = "skipped" if s.skip else (s.job.kind if s.job else "-")
        counts[key] = counts.get(key, 0) + 1
    failed = [s for s in steps if s.failed]
    tail = [f"failed: {len(failed)} ({', '.join(s.name for s in failed)}); run the import again to retry them"] \
        if failed else []
    return "\n".join(lines + ["", "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing to import"]
                     + tail)


# --- the board side ---------------------------------------------------------------------------------

def intake_import(ctx, number: int, pr: dict, found: dict, yes: bool) -> str:
    """Merge an import pull request and create its jobs. Only a pull request from an owner, member
    or collaborator (or the board's bot) is taken; its files must be the marker's, under the
    named roadmaps' package folders, and pass the submission gates; its checks must have passed."""
    from .intake import _checks
    cfg, repo = ctx.cfg, ctx.cfg.repositories.packages_repo()
    login = pr["user"]["login"]
    if pr.get("author_association") not in TRUSTED and login not in cfg.board.bot_logins:
        return f"#{number}: an import from @{login}, who is not a maintainer of the board; left for the maintainer"
    if pr.get("draft") or pr.get("state") != "open":
        return f"#{number}: an import that is a draft or not open"
    allowed = import_deliverables(found, cfg.board.packages_dir)
    problems = []
    for f in ctx.gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100", paginate=True):
        if f.get("status") == "removed":
            problems.append(f"{f['filename']} is deleted")
            continue
        text = ctx.gh.raw_file(pr["head"]["repo"]["full_name"], f["filename"], pr["head"]["sha"]) \
            if f["filename"] in allowed else ""
        problems += board.file_problems(f["filename"], text, allowed)
    failed, running = _checks(ctx, repo, pr["head"]["sha"])
    if running:
        return f"#{number}: checks still running"
    if failed:
        problems.append("checks failed: " + ", ".join(failed))
    if problems:
        if yes:
            ctx.gh.write("POST", f"repos/{repo}/issues/{number}/comments",
                         {"body": "Intake: import left for the maintainer, because " + "; ".join(problems) + "."})
        return f"#{number}: import not merged: " + "; ".join(problems)
    all_jobs = ctx.board.all_jobs()
    if any(j.kind in ("review", "redteam") for j in found["jobs"]):
        # Reviewed or red-teamed on the import pull request itself, which stays open until then.
        from .boardpr import open_for_review
        return open_for_review(ctx, number, pr, found["jobs"], all_jobs, yes)
    existing = {j.id for j in all_jobs}
    todo = [j for j in found["jobs"] if j.id not in existing]
    if not yes:
        return f"#{number}: would merge the import and create {', '.join(j.id for j in todo) or 'nothing'} (dry run)"
    ctx.gh.write("PUT", f"repos/{repo}/pulls/{number}/merge", {"merge_method": "squash"})
    created = [board.made_note(ctx.board, j, ctx.board.create(j)) for j in todo]
    return f"#{number}: merged the import; created " + (", ".join(created) or "nothing (already on the board)")
