"""tauceti-roadmap: the command line.

    tauceti-roadmap --conf <toml path or https URL> doctor
    tauceti-roadmap targets list | file --statement ... --why ...
    tauceti-roadmap work --worker-id ID [--only kind,...] [--agent codex|claude] [--loop]
    tauceti-roadmap tail --worker-id ID [--job JOB]      follow a worker's rounds and its agent's output
    tauceti-roadmap status [--all]                       the board's jobs by state, ages and claimants
    tauceti-roadmap gate <worktree> <Name> [--base origin/main] [--json] [--out FILE]
    tauceti-roadmap gate --depcheck <README.md> --name <Name> [--order FILE]
    tauceti-roadmap merge-order [--list] [--pr N]
    tauceti-roadmap targets import-intentions [--limit N] [--number N] [--yes]
    tauceti-roadmap sources check <sources.toml> | classify <url> | request <needs-references.toml> --job ID
    tauceti-roadmap sources fetch <DOI | arXiv id | URL | "Authors, Title, Year"> [--manifest sources.toml] [--id KEY]
    tauceti-roadmap sources cache <sources.toml> | ocr <file.pdf>
    tauceti-roadmap board intake [--pr N] | references | sync | stale [--release]   (board bot / maintainer; --yes to write)
    tauceti-roadmap board import-explorer --from <explorer checkout> [--kinds package,design,target] [--names A,B] [--yes]
    tauceti-roadmap targets file-many --from <targets.toml | targets.jsonl> [--yes]

Every GitHub write honours --dry-run. `gate` exits 1 when any gate fails: call it directly, never
through a pipe that would hide its exit status (use --out to keep a copy of the result).
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import (
    __version__,
    build,
    claims,
    config,
    depgraph,
    explorer,
    fetcher,
    gates,
    paths,
    prompts,
    references,
    sources,
    watch,
)
from . import targets as target_files
from .board import KINDS, Board, decode_submission
from .gh import GH, GHError

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _load(args) -> config.Config:
    try:
        return config.load(args.conf)
    except config.ConfigError as exc:
        raise SystemExit(f"configuration: {exc}") from exc


def _local_conf(args, cfg: config.Config) -> str:
    """A path children (agents calling `gate`) can use: the saved copy when --conf was a URL."""
    if cfg.path.startswith("https://"):
        return str(cfg.state_dir() / "tauceti-roadmap.toml")
    return str(Path(cfg.path).resolve()) if cfg.path else ""


# --- doctor ---------------------------------------------------------------------------------------

def _tool_version(cmd: str) -> str:
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80] if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return ""


def doctor(args) -> int:
    rows: list[tuple[str, str, str]] = []
    add = rows.append
    add((OK if sys.version_info >= (3, 12) else FAIL, "python", sys.version.split()[0]))
    try:
        cfg = config.load(args.conf)
        add((OK, "configuration", cfg.path))
        for w in cfg.warnings:
            add((WARN, "configuration", w))
    except config.ConfigError as exc:
        add((FAIL, "configuration", str(exc)))
        cfg = None
    for tool, needed in (("git", True), ("gh", True), ("uv", False)):
        version = _tool_version(tool)
        add((OK if version else (FAIL if needed else WARN), tool, version or "not found"))
    agents_found = []
    for agent in ("codex", "claude"):
        version = _tool_version(agent)
        if version:
            agents_found.append(agent)
        add((OK if version else WARN, agent, version or "not installed (lanes cannot use this agent)"))
    if not agents_found:
        add((FAIL, "agents", "neither codex nor claude is installed"))
    if cfg and cfg.agents.default not in agents_found:
        add((WARN, "agents.default", f"{cfg.agents.default} is not installed here; pass --agent"))
    if cfg:
        rows += review_model_rows(cfg, agents_found)
    if cfg and (cfg.gates.build or cfg.gates.elaborate):
        lake = shutil.which("lake")
        add((OK if lake else WARN, "lake", lake or "not found: package elaboration and the port/fix/gate builds are unavailable"))
    gh = GH()
    login = ""
    if shutil.which("gh"):
        ok, line = gh.auth_status()
        add((OK if ok else FAIL, "gh auth", line or "not logged in (run gh auth login)"))
        if ok:
            try:
                login = gh.login()
                add((OK, "github login", login))
            except GHError as exc:
                add((FAIL, "github login", str(exc)))
    if cfg and login:
        r = cfg.repositories
        for label, repo in (("content", r.content), ("board", r.board_repo()), ("packages", r.packages_repo()),
                            ("library", r.library)):
            try:
                data = gh.api(f"repos/{repo}")
                add((OK, f"repository {label}", f"{repo} (push: {data.get('permissions', {}).get('push', False)})"))
            except GHError as exc:
                add((FAIL, f"repository {label}", f"{repo}: {exc}"))
        ns = claims.namespace_for(cfg, gh, login)
        shared = bool(r.claims) and ns == r.claims
        add((OK if shared else WARN, "claims namespace",
             ns + ("" if shared else " (your fork: de-duplication covers only your own workers until you are admitted)")))
        if cfg.roadmap.merge_order_repo != "local":
            repo = r.packages_repo() if cfg.roadmap.merge_order_repo == "packages" else r.content
            try:
                order = depgraph.read_order(gh.raw_file(repo, cfg.roadmap.merge_order_file))
                add((OK, "merge order", f"{repo}:{cfg.roadmap.merge_order_file} ({len(order)} roadmaps)"))
            except GHError:
                add((WARN, "merge order", f"{repo}:{cfg.roadmap.merge_order_file} not found (dependency checks are skipped)"))
    if cfg:
        state = cfg.state_dir()
        try:
            state.mkdir(parents=True, exist_ok=True)
            probe = state / ".doctor"
            probe.write_text("ok")
            probe.unlink()
            add((OK, "state directory", str(state)))
        except OSError as exc:
            add((FAIL, "state directory", f"{state}: {exc}"))
        cache = sources.cache_root(state)
        inside = sources.enclosing_checkout(cache)
        add((FAIL, "sources cache", (f"{cache} is inside the git checkout {inside}: sources never enter a repository "
                                     "(set TAUCETI_PROJECT_STATE or XDG_STATE_HOME outside it)")) if inside
            else (OK, "sources cache", f"{cache} (outside every repository)"))
        dummy = {}
        for lane in prompts.BRIEFS:
            dummy |= {k: "x" for k in prompts.placeholders(lane)}
        try:
            for lane in prompts.BRIEFS:
                prompts.render(lane, dummy)
            add((OK, "prompts", f"{len(prompts.BRIEFS)} briefs render ({paths.PROMPTS})"))
        except prompts.PromptError as exc:
            add((FAIL, "prompts", str(exc)))
        reason = sources.ocr_status()
        if cfg.sources.ocr:
            add((OK if not reason else WARN, "ocr", "RapidOCR available" if not reason else reason))
        else:
            add((OK, "ocr", "off ([sources] ocr = false): image-only scans keep their text layer only"))
        pdf = shutil.which("pdftotext")
        add((OK, "pdf text", pdf) if pdf else (WARN, "pdf text", "pdftotext not found: install poppler-utils, "
                                                                  "or the `pdf` extra for a slower fallback"))
        rows += _sandbox_rows(cfg)
    width = max(len(r[1]) for r in rows)
    for status, item, detail in rows:
        print(f"{status:<4}  {item:<{width}}  {detail}")
    return 1 if any(r[0] == FAIL for r in rows) else 0


def review_model_rows(cfg: config.Config, installed: list[str]) -> list[tuple[str, str, str]]:
    """Whether this host can run the models reviews and red-team passes are pinned to."""
    rows = []
    for kind in ("review", "redteam"):
        pins = cfg.lane_models(kind)
        if kind == "redteam":
            pins = {**pins, **{p.agent: p.model for p in cfg.redteam.passes if p.model}}
        if not pins:
            continue
        named = " or ".join(f"{m} ({a})" for a, m in pins.items())
        usable = [f"{m} ({a})" for a, m in pins.items() if a in installed]
        rows.append((OK, f"{kind} models", ", ".join(usable) + f" (pinned: {named})") if usable else
                    (WARN, f"{kind} models", (f"pinned to {named}, and neither agent is installed: this host takes "
                                              f"no {kind} jobs")))
    return rows


def _sandbox_rows(cfg: config.Config) -> list[tuple[str, str, str]]:
    from .agents import netjail_usable
    s = cfg.sandbox
    rows = [(OK, "sandbox", s.profile)]
    if s.profile in ("proxy", "netjail"):
        u = urlparse(s.proxy_url)
        try:
            with socket.create_connection((u.hostname or "127.0.0.1", u.port or 3128), timeout=3):
                rows.append((OK, "proxy", s.proxy_url))
        except OSError:
            rows.append((FAIL, "proxy", f"{s.proxy_url} is not listening (start sandbox/allowproxy.py)"))
    if s.profile == "netjail":
        rows.append((OK, "netjail", s.netjail) if netjail_usable(s.netjail)
                    else (WARN, "netjail", f"{s.netjail or 'not set'} unusable; agents fall back to the proxy profile"))
    return rows


# --- targets --------------------------------------------------------------------------------------

def targets(args) -> int:
    cfg = _load(args)
    if args.action == "import-intentions":
        from .intentions import import_intentions
        ctx = _bot_context(args, cfg, args.dry_run or not args.yes)
        for line in import_intentions(ctx, yes=args.yes and not args.dry_run, limit=args.limit, number=args.number):
            print(line)
        return 0
    gh = GH(dry_run=args.dry_run)
    b = Board(cfg, gh)
    if args.action == "list":
        for job in b.list_jobs(("available", "claimed", "submitted", "needs-references", "blocked"), ("target", "design")):
            print(f"#{job.number:<6} {job.id:<40} {job.state}")
        repo = cfg.repositories.board_repo()
        raw = gh.api(f"repos/{repo}/issues?labels={cfg.board.namespace_label},kind:target&state=open&per_page=100", paginate=True)
        for issue in raw:
            if "tauceti-roadmap-job" not in (issue.get("body") or ""):
                print(f"#{issue['number']:<6} (filed, awaiting `board sync`)  {issue['title'][:70]}")
        if cfg.board.intentions:
            intents = gh.api(f"repos/{cfg.repositories.content}/issues?labels=intention&state=open&per_page=100", paginate=True)
            for issue in intents:
                print(f"intention {cfg.repositories.content}#{issue['number']}: {issue['title'][:70]}")
        return 0
    if args.action == "file-many":
        if not args.source:
            raise SystemExit("targets file-many needs --from <targets.toml | targets.jsonl>")
        ctx = _bot_context(args, cfg, args.dry_run or not args.yes)
        for line in target_files.file_many(ctx, target_files.read_list(Path(args.source)), yes=args.yes and not args.dry_run):
            print(line)
        return 0
    if not args.statement or not args.why:
        raise SystemExit("targets file needs --statement and --why")
    fields = {k: getattr(args, k) for k in target_files.FIELDS}
    created = gh.write("POST", f"repos/{cfg.repositories.board_repo()}/issues", target_files.issue_payload(cfg, fields))
    print(created["html_url"] if created else "(dry run)")
    return 0


# --- work -----------------------------------------------------------------------------------------

def _stop(signum, frame):
    raise SystemExit(128 + signum)


def work(args) -> int:
    from .lanes import Context
    from .mergeorder import _order
    from .worker import work_loop, work_once
    cfg = _load(args)
    gh = GH(dry_run=args.dry_run)
    try:
        operator = gh.login()
    except GHError as exc:
        raise SystemExit(f"gh: {exc} (run gh auth login)") from exc
    only = tuple(k.strip() for k in args.only.split(",") if k.strip()) if args.only else None
    unknown = [k for k in only or () if k not in KINDS]
    if unknown:
        raise SystemExit(f"--only: unknown kind(s) {', '.join(unknown)} (kinds: {', '.join(KINDS)})")
    agent = args.agent or cfg.agents.default
    model = args.model or cfg.model_for(agent)
    cl = claims.open_claims(cfg, gh, operator, paths.sanitize_worker_id(args.worker_id), dry_run=args.dry_run)
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=cl, worker_id=paths.sanitize_worker_id(args.worker_id),
                  operator=operator, agent=agent, model=model, conf=_local_conf(args, cfg), dry_run=args.dry_run,
                  agent_fixed=bool(args.agent), model_fixed=bool(args.model))
    log = watch.round_logger(ctx.wdir)
    started = build.build_id()
    build.snapshot(ctx.wdir, started)
    log(f"worker {ctx.worker_id} as @{operator}, {agent} ({model}), claims in {cl.repo}, state {ctx.wdir}, "
        f"build {started}")
    # A stop signal (systemd, kill, a container stop) ends the round like Ctrl-C: the job is released.
    signal.signal(signal.SIGTERM, _stop)
    if args.loop:
        try:
            work_loop(ctx, only, lambda: _order(ctx), log=log, build=started)
        except KeyboardInterrupt:
            return 130
        return 0
    result = work_once(ctx, only, job_id=args.job or "", order=_order(ctx), log=log)
    print(f"{result.job or '-'} {result.status}: {result.detail}" + (f" {result.url}" if result.url else ""))
    return 0 if result.status in ("submitted", "idle", "dry-run", "needs-references") else 1


# --- gate -----------------------------------------------------------------------------------------

def _order_for_gate(cfg: config.Config, explicit: str) -> list[str] | None:
    if explicit:
        return depgraph.read_order(Path(explicit).read_text(encoding="utf-8"))
    if cfg.roadmap.merge_order_repo == "local":
        path = cfg.state_dir() / cfg.roadmap.merge_order_file
        return depgraph.read_order(path.read_text(encoding="utf-8")) if path.is_file() else None
    repo = cfg.repositories.packages_repo() if cfg.roadmap.merge_order_repo == "packages" else cfg.repositories.content
    try:
        return depgraph.read_order(GH().raw_file(repo, cfg.roadmap.merge_order_file))
    except GHError:
        return None


def gate(args) -> int:
    from .lanes.base import dropped_names, sibling_readmes
    cfg = _load(args)
    siblings = sibling_readmes(cfg.state_dir(), cfg.roadmap.active_dir)
    order = _order_for_gate(cfg, args.order)
    if args.depcheck:
        if not args.name:
            raise SystemExit("gate --depcheck needs --name")
        if order is None:
            print("depcheck: no merge-order file; nothing to check")
            return 0
        readmes = {**siblings, args.name: Path(args.depcheck).read_text(encoding="utf-8")}
        edges = depgraph.supplier_edges(readmes, set(order), set(cfg.roadmap.main_extended))
        problems = depgraph.check_order(edges, order, only={args.name})
        for p in problems:
            print(p)
        print(f"depcheck: {len(readmes)} roadmaps, {sum(len(v) for v in edges.values())} edges, {len(problems)} problem(s)")
        return 1 if problems else 0
    if not args.worktree or not args.name_pos:
        raise SystemExit("usage: gate <worktree> <Name> [--base REF] | gate --depcheck README --name Name")
    opts = gates.GateOptions.from_config(cfg, order=order, siblings=siblings, dropped=dropped_names(cfg))
    result = gates.run_gate(Path(args.worktree), args.name_pos, opts, base=args.base,
                            build_cmd=cfg.gates.build if args.build else "")
    if args.out:
        Path(args.out).write_text(json.dumps(result.to_dict(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result.to_dict(), indent=1, ensure_ascii=False))
    else:
        print(result.summary())
        for key, hits in result.details.items():
            for h in hits[:10]:
                print(f"  {key}: " + (f"line {h.line}: {h.text}" if isinstance(h, gates.Hit) else str(h)))
    return result.exit_code


# --- merge-order, sources, board ------------------------------------------------------------------

def _bot_context(args, cfg, dry: bool):
    from .lanes import Context
    gh = GH(dry_run=dry)
    try:
        login = gh.login()
    except GHError:
        login = "unknown"
    return Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=None, worker_id="board", operator=login,
                   agent="", model="", conf=_local_conf(args, cfg), dry_run=dry)


def merge_order(args) -> int:
    from .mergeorder import refresh
    cfg = _load(args)
    ctx = _bot_context(args, cfg, args.dry_run)
    for line in refresh(ctx, only={args.pr} if args.pr else None, listing=args.list):
        print(line)
    return 0


def sources_cmd(args) -> int:
    cfg = _load(args)
    s = cfg.sources
    deny = sources.deny_list(s.deny)
    if args.action == "classify":
        print(sources.classify_url(args.target, s.allow, deny))
        return 0
    if args.action == "check":
        problems = sources.validate_manifest(sources.load_manifest(Path(args.target)), s.allow, deny, s.uncertified_ok)
        for p in problems:
            print(p)
        print(f"{args.target}: {len(problems)} problem(s)")
        return 1 if problems else 0
    if args.action == "request":
        items = references.read_missing(Path(args.target))
        text = references.request_comment(args.job or "<job>", args.roadmap or "<roadmap>", items)
        if not args.post:
            print(text)
            return 0
        ctx = _bot_context(args, cfg, args.dry_run)
        job = ctx.board.find(args.job)
        if not job:
            raise SystemExit(f"unknown job {args.job}")
        ctx.board.comment(job.number, text)
        ctx.board.set_state(job, "needs-references")
        return 0
    if args.action == "ocr":
        return _ocr(cfg, Path(args.target), args.out)
    fet = fetcher.Fetcher(fetcher.client_from_config(cfg), sources.cache_root(cfg.state_dir()), s)
    if args.action == "cache":
        failed = 0
        for a in fet.materialize(sources.load_manifest(Path(args.target))):
            failed += bool(a.permanent)
            print(f"{a.id}\t{a.text if a.text else 'unavailable: ' + a.problem}")
        return 1 if failed else 0
    return _fetch(args, cfg, fet)


def _fetch(args, cfg: config.Config, fet: fetcher.Fetcher) -> int:
    """`sources fetch`: resolve, download, extract; record the entry; on failure, the [[missing]] table."""
    s = cfg.sources
    if args.licence_class and args.licence_class not in config.LICENCE_CLASSES:
        raise SystemExit(f"--licence-class must be one of {', '.join(config.LICENCE_CLASSES)}")
    cit = fetcher.parse_citation(args.target, authors=args.authors, title=args.title, year=args.year)
    out = fet.fetch(cit, licence_class=args.licence_class)
    manifest = path = None
    if args.manifest:
        path = Path(args.manifest)
        manifest = sources.load_manifest(path) if path.is_file() else sources.new_manifest(path.parent.name)
    entry = dict(out.entry)
    existing = None
    if manifest is not None:
        existing = sources.find_source(manifest, id=args.id, doi=entry.get("doi", ""), arxiv=entry.get("arxiv", ""),
                                       sha256=entry.get("sha256", ""))
    taken = {str(e.get("id", "")) for e in (manifest or {}).get("source", [])}
    entry["id"] = args.id or (existing or {}).get("id") or sources.citation_key(entry.get("authors", ""), entry.get("year", ""), taken)
    for key, value in (("locator_scheme", args.locator_scheme), ("locator", args.locator)):
        if value:
            entry[key] = value
    if args.cited_for:
        entry["cited_for"] = list(args.cited_for)
    if args.json:
        print(json.dumps({"status": out.status, "entry": entry, "file": str(out.file or ""), "text": str(out.text or ""),
                          "tried": out.tried, "note": out.note}, indent=1, ensure_ascii=False))
    else:
        for line in out.tried:
            print(f"  tried: {line}")
    accepted = out.status == "fetched" or (out.status == "uncertified" and s.uncertified_ok)
    if accepted and manifest is not None:
        action = sources.upsert_source(manifest, entry, add_only=args.add_only)
        sources.save_manifest(path, manifest)
        if not args.json:
            print(f"{action} {entry['id']} in {path}")
        if not (existing or {}).get("locator_scheme") and not entry.get("locator_scheme"):
            print(f"warning: {entry['id']} has no locator_scheme yet (pass --locator-scheme)", file=sys.stderr)
    if not args.json:
        if out.status == "fetched":
            print(f"{entry['id']}: {out.entry.get('licence_class')} text from {out.entry.get('fetched_from') or 'a local file'}")
            print(f"file: {out.file}")
            print(f"text: {out.text}" + (f" ({out.note})" if out.note else ""))
        else:
            print(f"{entry['id']}: {out.status}: {out.note}")
    if accepted:
        return 0
    if args.missing_out and not out.transient:
        references.add_missing(Path(args.missing_out), references.MissingReference(
            authors=str(entry.get("authors") or args.authors), title=str(entry.get("title") or args.title or args.target),
            year=str(entry.get("year") or args.year), needed_for=", ".join(args.cited_for) or "see the report",
            tried=out.tried[-6:], key=entry["id"], doi=str(entry.get("doi", "")),
            url=args.target if cit.kind == "url" else "", locator_scheme=args.locator_scheme, locator=args.locator))
        print(f"recorded {entry['id']} in {args.missing_out}", file=sys.stderr if args.json else sys.stdout)
    return 1


def _ocr(cfg: config.Config, pdf: Path, out: str) -> int:
    inside = sources.enclosing_checkout(Path(out)) if out else None
    if inside:
        print(f"--out {out} is inside the git checkout {inside}: extracted text never enters a repository "
              "(write it outside, or let the sources cache keep it)", file=sys.stderr)
        return 2
    reason = sources.ocr_status()
    if reason:
        print(reason, file=sys.stderr)
        return 2
    pages = sources.ocr_pdf_pages(pdf, dpi=cfg.sources.ocr_dpi)
    text = sources.numbered_text(pages, [f"OCR of {pdf.name} (RapidOCR, {cfg.sources.ocr_dpi} dpi; {len(pages)} pages)."])
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(out)
    else:
        sys.stdout.write(text)
    return 0


def tail_cmd(args) -> int:
    cfg = _load(args)
    wdir = paths.worker_dir(cfg.project.name, args.worker_id)
    if not wdir.is_dir():
        raise SystemExit(f"no worker {args.worker_id!r} on this host ({wdir})")
    try:
        watch.tail(wdir, job=args.job)
    except KeyboardInterrupt:
        return 0
    return 0


def status_cmd(args) -> int:
    cfg = _load(args)
    gh = GH()
    issues = gh.api(f"repos/{cfg.repositories.board_repo()}/issues?labels={cfg.board.namespace_label}"
                    f"&state={'all' if args.all else 'open'}&per_page=100", paginate=True)
    leases = {}
    if cfg.repositories.claims:
        shared = claims.Claims(repo=cfg.repositories.claims,
                               gitdir=cfg.state_dir() / "claims" / (cfg.repositories.claims.replace("/", "__") + ".git"),
                               owner="status", operator="", project=cfg.project.name)
        try:
            keys = shared.live_keys(f"roadmap/{cfg.project.name}/job/")
        except (OSError, subprocess.SubprocessError):
            keys = []
        leases = {k: lease for k in keys if (lease := shared.read(k))}
    print(watch.status(Board(cfg, gh), issues, leases, cfg.project.name, include_done=args.all))
    return 0


def board_cmd(args) -> int:
    from . import intake
    cfg = _load(args)
    ctx = _bot_context(args, cfg, dry=args.dry_run or not args.yes)
    shared = None
    if cfg.repositories.claims:
        shared = claims.Claims(repo=cfg.repositories.claims,
                               gitdir=cfg.state_dir() / "claims" / (cfg.repositories.claims.replace("/", "__") + ".git"),
                               owner=f"{ctx.operator}/board", operator=ctx.operator, project=cfg.project.name,
                               dry_run=ctx.dry_run)
    write = args.yes and not args.dry_run
    if args.action in ("intake", "references"):
        if args.action == "intake":
            repo = cfg.repositories.packages_repo()
            numbers = [args.pr] if args.pr else [p["number"] for p in ctx.gh.api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True)
                                                 if decode_submission(p.get("body") or "")
                                                 or explorer.decode_import(p.get("body") or "")]
            for n in numbers:
                print(intake.intake(ctx, n, yes=write))
        if not args.pr:
            for line in references.resume_all(ctx, ctx.sources_fetcher(), yes=write):
                print(line)
        return 0
    if args.action == "import-explorer":
        if not args.source:
            raise SystemExit("board import-explorer needs --from <path to a checkout of the explorer>")
        kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
        unknown = [k for k in kinds if k not in explorer.STAGES]
        if unknown:
            raise SystemExit(f"--kinds: unknown {', '.join(unknown)} (kinds: {', '.join(explorer.STAGES)})")
        names = tuple(n.strip() for n in args.names.split(",") if n.strip())
        steps = explorer.import_explorer(ctx, Path(args.source).expanduser(), kinds or explorer.STAGES, names, yes=write)
        print(explorer.summary(steps))
        return 0
    if args.action == "sync":
        for line in (intake.sync_targets(ctx) + intake.sync_states(ctx, shared) + intake.sync_leases(ctx, shared)
                     + intake.sync_pull_requests(ctx)):
            print(line)
        return 0
    for line in intake.stale(ctx, shared, release=args.release):
        print(line)
    return 0


# --- parser ---------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tauceti-roadmap", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", help="tauceti-roadmap.toml path or https URL")
    ap.add_argument("--dry-run", action="store_true", help="print GitHub writes instead of making them")
    ap.add_argument("--version", action="version", version=f"tauceti-roadmap {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="report the tools, credentials and configuration this host can use")

    t = sub.add_parser("targets", help="list targets, file one, or import the roadmap repository's intentions")
    t.add_argument("action", choices=["list", "file", "file-many", "import-intentions"])
    t.add_argument("--yes", action="store_true", help="import-intentions, file-many: write (default: dry run)")
    t.add_argument("--from", dest="source", default="", help="file-many: a TOML ([[target]]) or JSON Lines list")
    t.add_argument("--limit", type=int, default=10, help="import-intentions: at most this many per run (0: all)")
    t.add_argument("--number", type=int, default=0, help="import-intentions: only this intention issue")
    t.add_argument("--statement", default="")
    t.add_argument("--why", default="")
    t.add_argument("--sources", default="")
    t.add_argument("--name", default="")
    t.add_argument("--licence", default="")
    t.add_argument("--title", default="")

    w = sub.add_parser("work", help="run one round (or --loop) of the worker")
    w.add_argument("--worker-id", required=True)
    w.add_argument("--only", default="", help=f"comma list of kinds: {','.join(KINDS)}")
    w.add_argument("--agent", choices=config.AGENTS, default="")
    w.add_argument("--model", default="")
    w.add_argument("--job", default="", help="take only this job id")
    w.add_argument("--loop", action="store_true")

    g = sub.add_parser("gate", help="run the push gates on a roadmap folder (exit 1 on any failure)")
    g.add_argument("worktree", nargs="?")
    g.add_argument("name_pos", nargs="?", metavar="Name")
    g.add_argument("--base", default="", help="merge-base reference for the outside-folder diff, e.g. origin/main")
    g.add_argument("--build", action="store_true", help="also run the configured targeted build")
    g.add_argument("--json", action="store_true")
    g.add_argument("--out", default="")
    g.add_argument("--depcheck", default="", metavar="README", help="only the dependency check of this README")
    g.add_argument("--name", default="")
    g.add_argument("--order", default="", help="merge-order file (default: the configured one)")

    m = sub.add_parser("merge-order", help="refresh the merge-order chain in every open roadmap pull request")
    m.add_argument("--list", action="store_true")
    m.add_argument("--pr", type=int, default=0)

    s = sub.add_parser("sources", help="fetch a legal source, check a manifest, classify a URL, cache a manifest's "
                                       "texts, OCR a scan, render a needs-references request")
    s.add_argument("action", choices=["fetch", "check", "classify", "cache", "ocr", "request"])
    s.add_argument("target", help="fetch: DOI, arXiv id, URL, local file or 'Authors, Title, Year'; check/cache: "
                                  "sources.toml; classify: URL; ocr: PDF; request: needs-references.toml")
    s.add_argument("--manifest", default="", help="fetch: record the entry in this sources.toml")
    s.add_argument("--add-only", action="store_true",
                   help="fetch: an entry the manifest has keeps its values (a review adds, never rewrites)")
    s.add_argument("--id", default="", help="fetch: the citation key (default: the manifest's, else derived)")
    s.add_argument("--authors", default="")
    s.add_argument("--title", default="")
    s.add_argument("--year", default="")
    s.add_argument("--locator-scheme", default="", choices=["", *sources.LOCATOR_SCHEMES])
    s.add_argument("--locator", default="")
    s.add_argument("--cited-for", action="append", default=[], help="a README section or target citing it (repeatable)")
    s.add_argument("--licence-class", default="", help="fetch: the class of a URL on a host no list names, once checked")
    s.add_argument("--missing-out", default="", help="fetch: on failure, add a [[missing]] table to this file")
    s.add_argument("--json", action="store_true")
    s.add_argument("--out", default="", help="ocr: write the numbered text here")
    s.add_argument("--job", default="")
    s.add_argument("--roadmap", default="")
    s.add_argument("--post", action="store_true", help="post the request on the job and park it")

    tl = sub.add_parser("tail", help="follow a worker's round log and its current job's agent output")
    tl.add_argument("--worker-id", required=True)
    tl.add_argument("--job", default="", help="follow this job (default: the one the worker started last)")

    st = sub.add_parser("status", help="the board's jobs by state, with their ages and claimants")
    st.add_argument("--all", action="store_true", help="include done jobs and closed issues")

    b = sub.add_parser("board", help="board maintenance for the bot or a maintainer (dry run unless --yes)")
    b.add_argument("action", choices=["intake", "references", "sync", "stale", "import-explorer"])
    b.add_argument("--pr", type=int, default=0)
    b.add_argument("--from", dest="source", default="", help="import-explorer: a checkout of CBirkbeck/tauceti-explorer")
    b.add_argument("--kinds", default="", help="import-explorer: package,design,target (default: all)")
    b.add_argument("--names", default="", help="import-explorer: only these roadmaps (comma list)")
    b.add_argument("--yes", action="store_true")
    b.add_argument("--release", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handlers = {"doctor": doctor, "targets": targets, "work": work, "gate": gate, "merge-order": merge_order,
                "sources": sources_cmd, "board": board_cmd, "tail": tail_cmd, "status": status_cmd}
    try:
        return handlers[args.command](args)
    except GHError as exc:
        print(f"GitHub: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

