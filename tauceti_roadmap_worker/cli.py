"""tauceti-roadmap: the command line.

    tauceti-roadmap --conf <toml path or https URL> doctor
    tauceti-roadmap targets list | file --statement ... --why ...
    tauceti-roadmap work --worker-id ID [--only kind,...] [--agent codex|claude] [--loop]
    tauceti-roadmap tail --worker-id ID [--job JOB]      follow a worker's rounds and its agent's output
    tauceti-roadmap status [--all]                       the board's jobs by state, ages and claimants
    tauceti-roadmap roadmaps refresh | list | grep <regex> | show <Name> [--file F] | resolve <README.md>
    tauceti-roadmap usage [--worker-id ID] [--by lane|kind|job|agent|model|worker|day|label] [--since DATE]
    tauceti-roadmap gate <worktree> <Name> [--base origin/main] [--json] [--out FILE]
    tauceti-roadmap gate --depcheck <README.md> --name <Name> [--order FILE] [--movable]
    tauceti-roadmap gate --candidate <folder | README.md> --only <Name> [--order FILE]
    tauceti-roadmap merge-order [--list] [--pr N]
    tauceti-roadmap targets import-intentions [--limit N] [--number N] [--yes]
    tauceti-roadmap sources check <sources.toml> | classify <url> | request <needs-references.toml> --job ID
    tauceti-roadmap sources fetch <DOI | arXiv id | URL | "Authors, Title, Year"> [--manifest sources.toml] [--id KEY]
    tauceti-roadmap sources cache <sources.toml> | ocr <file.pdf>
    tauceti-roadmap board intake [--pr N] | references | sync | stale [--release]   (board bot / maintainer; --yes to write)
    tauceti-roadmap board errata seed [--limit N] [--yes]
    tauceti-roadmap board import-explorer --from <explorer checkout> [--kinds package,design,target] [--names A,B] [--yes]
    tauceti-roadmap targets file-many --from <targets.toml | targets.jsonl> [--yes]

Every GitHub write honours --dry-run. `gate` exits 1 when any gate fails: call it directly, never
through a pipe that would hide its exit status (use --out to keep a copy of the result).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import (
    __version__,
    board,
    boardpr,
    build,
    claims,
    config,
    depgraph,
    errata,
    explorer,
    fetcher,
    filers,
    gates,
    gitops,
    openroadmaps,
    paths,
    prompts,
    references,
    sources,
    watch, throttle
)
from . import targets as target_files
from . import usage as usage_mod
from .board import KINDS, Board
from .gh import GH, GHError, GHHalt, GHRateLimited

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _load(args) -> config.Config:
    try:
        cfg = config.load(args.conf, overlay=getattr(args, "overlay", None))
    except config.ConfigError as exc:
        raise SystemExit(f"configuration: {exc}") from exc
    GH.throttle = throttle.for_config(cfg)      # every GH in this process shares the host's write throttle
    return cfg


def throttle_cmd(args) -> int:
    """Take one of the host's GitHub content-write slots (waiting for it), for scripts that write with
    `gh` themselves; prints the seconds waited."""
    cfg = _load(args)
    t = throttle.for_config(cfg)
    print(f"{t.wait():.1f}" if t else "0.0")
    return 0


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


def free_space_row(state: Path) -> tuple[str, str, str] | None:
    """The doctor's line on free space where the state directory lives (a warning below 40 GB)."""
    from .lanes.base import MIN_FREE_GB_BUILD, MIN_FREE_GB_WARN, free_gb
    free = free_gb(state)
    if free is None:
        return None
    if free >= MIN_FREE_GB_WARN:
        return OK, "free space", f"{free:.0f} GB free on the state directory's disk"
    return WARN, "free space", (f"{free:.0f} GB free on the state directory's disk (below {MIN_FREE_GB_WARN} GB; port, "
                                f"red-team and gate lanes stop below {MIN_FREE_GB_BUILD} GB)")


def claims_row(cfg: config.Config, ns: str) -> tuple[str, str, str]:
    """The doctor's line on the claims namespace: FAIL when the configured shared one is not writable,
    since `work` then refuses to start."""
    if why := claims.fallback_problem(cfg, ns):
        return FAIL, "claims namespace", f"{why}; `work` refuses to start without --private-claims"
    if cfg.repositories.claims and ns == cfg.repositories.claims:
        return OK, "claims namespace", ns
    return WARN, "claims namespace", f"{ns} (de-duplication covers only the workers that use it)"


def board_labels_row(repo: str, permissions: dict) -> tuple[str, str, str]:
    """The doctor's line on the board's state labels, which need triage."""
    if any(permissions.get(p) for p in ("triage", "push", "maintain", "admin")):
        return OK, "board labels", f"{repo}: this login sets state labels"
    return WARN, "board labels", (f"{repo}: this login cannot set state labels; its claims, parks and releases "
                                  "reach the board only when `board sync` mirrors them")


def doctor(args) -> int:
    rows: list[tuple[str, str, str]] = []
    add = rows.append
    add((OK if sys.version_info >= (3, 12) else FAIL, "python", sys.version.split()[0]))
    try:
        cfg = config.load(args.conf, overlay=getattr(args, "overlay", None))
        add((OK, "configuration", cfg.path))
        if cfg.overlay_path:
            add((OK, "overlay", f"{cfg.overlay_path} sets {', '.join(cfg.overlaid) or 'nothing'}"))
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
                if label == "board":
                    add(board_labels_row(repo, data.get("permissions") or {}))
            except GHError as exc:
                add((FAIL, f"repository {label}", f"{repo}: {exc}"))
        add(claims_row(cfg, claims.namespace_for(cfg, gh, login)))
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
        row = free_space_row(state)
        if row:
            add(row)
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
        gs = shutil.which("gs")
        add((OK, "ghostscript", gs) if gs else (WARN, "ghostscript", (
            "gs not found: PostScript sources (.ps, .ps.gz) and PDFs whose text layer is glyph names cannot be "
            "read; see docs/SOURCES.md")))
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


SANDBOX_GUARANTEES = {
    "off": "agents run unconfined, as this user with its home directory, files and credentials",
    "proxy": "agents' HTTP(S) goes through the logging proxy (a program that ignores the proxy variables goes "
             "direct); no filesystem or credential isolation",
    "netjail": "agents reach the network only through the logging proxy; no filesystem or credential isolation",
}


def _sandbox_rows(cfg: config.Config) -> list[tuple[str, str, str]]:
    from .agents import netjail_usable, proxy_listening
    s = cfg.sandbox
    rows = [(OK, "sandbox", f"{s.profile}: {SANDBOX_GUARANTEES[s.profile]}")]
    if s.profile in ("proxy", "netjail"):
        rows.append((OK, "proxy", s.proxy_url) if proxy_listening(s.proxy_url) else
                    (FAIL, "proxy", f"{s.proxy_url} is not listening (start sandbox/allowproxy.py); workers will "
                                    "not start an agent"))
    if s.profile == "netjail":
        rows.append((OK, "netjail", s.netjail) if netjail_usable(s.netjail) else
                    (FAIL, "netjail", (f"{s.netjail} cannot start a jail here" if s.netjail else
                                       "[sandbox] netjail is not set") + "; workers will not start an agent"))
    return rows


# --- targets --------------------------------------------------------------------------------------

def _check_filer(ctx) -> None:
    """Refuse to file targets for a login that may not file them (warn when that is unknown)."""
    ok, note = filers.allowed(ctx, ctx.operator)
    if ok is False:
        raise SystemExit(note)
    if ok is None:
        print(f"warning: cannot tell whether @{ctx.operator} may file targets ({note}); `board sync` decides",
              file=sys.stderr)


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
    if args.action in ("file", "file-many"):
        _check_filer(_bot_context(args, cfg, True))
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
    from .agents import SandboxUnavailable, sandbox_problem
    from .lanes import Context, LaneResult
    from .mergeorder import _order
    from .worker import (
        EX_HALTED,
        WorkerBusy,
        halt_path,
        halt_record,
        hold_worker_dir,
        record_halt,
        release_leftovers,
        sweep_worktrees,
        work_loop,
        work_once,
    )
    cfg = _load(args)
    sandbox = "" if args.dry_run else sandbox_problem(cfg.sandbox.profile, cfg.sandbox.proxy_url, cfg.sandbox.netjail)
    if sandbox:
        raise SystemExit(f"sandbox: {sandbox}; no agent runs without it (`tauceti-roadmap doctor` checks it)")
    gh = GH(dry_run=args.dry_run)
    operator, last = "", None
    for delay in (0, *config.RETRY_DELAYS):        # a network blip at startup must not stop a worker
        if delay:
            time.sleep(delay)
        try:
            operator = gh.login()
            break
        except GHHalt as exc:
            raise SystemExit(f"gh: GitHub refused the account: {exc}") from exc
        except (GHError, GHRateLimited) as exc:
            last = exc
    if not operator:
        raise SystemExit(f"gh: {last} (run gh auth login)")
    for warning in cfg.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    only = tuple(k.strip() for k in args.only.split(",") if k.strip()) if args.only else None
    unknown = [k for k in only or () if k not in KINDS]
    if unknown:
        raise SystemExit(f"--only: unknown kind(s) {', '.join(unknown)} (kinds: {', '.join(KINDS)})")
    agent = args.agent or cfg.agents.default
    model = args.model or cfg.model_for(agent)
    cl = claims.open_claims(cfg, gh, operator, paths.sanitize_worker_id(args.worker_id), dry_run=args.dry_run)
    if (why := claims.fallback_problem(cfg, cl.repo)) and not args.private_claims:
        why += ("; ask the maintainer for push access, or pass --private-claims (or set CLAIM_REPO) to work with "
                "private leases on purpose")
        if not args.dry_run:
            raise SystemExit(f"claims: {why}")
        print(f"warning: claims: {why}", file=sys.stderr)
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=cl, worker_id=paths.sanitize_worker_id(args.worker_id),
                  operator=operator, agent=agent, model=model, conf=_local_conf(args, cfg), dry_run=args.dry_run,
                  agent_fixed=bool(args.agent), model_fixed=bool(args.model))
    try:
        held = hold_worker_dir(ctx.wdir)
    except WorkerBusy as exc:
        raise SystemExit(str(exc)) from None
    with held:
        log = watch.round_logger(ctx.wdir)
        started = build.build_id()
        build.snapshot(ctx.wdir, started)
        log(f"worker {ctx.worker_id} as @{operator}, {agent} ({model}), claims in {cl.repo}, state {ctx.wdir}, "
            f"build {started}")
        if args.loop and args.status_file:
            raise SystemExit("--status-file describes one round; it does not go with --loop")
        began = dt.datetime.now(dt.UTC)
        if halt := halt_record(ctx):
            print(f"halted at {halt.get('at', '')}: {halt.get('detail', '')}; look at the account, then delete "
                  f"{halt_path(ctx)}", file=sys.stderr)
            return round_end(args, ctx, LaneResult("", "halted", halt.get("detail", "")), began)
        try:
            release_leftovers(ctx, log)
        except GHHalt as exc:
            record_halt(ctx, str(exc))
            return round_end(args, ctx, LaneResult("", "halted", str(exc)), began)
        except GHRateLimited as exc:
            log(f"GitHub is rate limited at startup: {exc}")
            return round_end(args, ctx, LaneResult("", "github-limited", str(exc)), began)
        sweep_worktrees(ctx, log)
        # A stop signal (systemd, kill, a container stop) ends the round like Ctrl-C: the job is released.
        signal.signal(signal.SIGTERM, _stop)
        if args.loop:
            try:
                ended = work_loop(ctx, only, lambda: _order(ctx), log=log, build=started)
            except KeyboardInterrupt:
                return 130
            except SandboxUnavailable as exc:
                log(f"{exc}: the worker stops")
                raise
            return EX_HALTED if ended == "halted" else 0
        try:
            result = work_once(ctx, only, job_id=args.job or "", order=_order(ctx), log=log)
        except GHHalt as exc:
            result = LaneResult("", "halted", str(exc))
        except GHRateLimited as exc:
            result = LaneResult("", "github-limited", str(exc), retry_at=dt.datetime.fromtimestamp(exc.until, dt.UTC))
        except KeyboardInterrupt:
            result = LaneResult("", "interrupted", "KeyboardInterrupt")
        if result.status == "halted":
            record_halt(ctx, result.detail)
        print(f"{result.job or '-'} {result.describe()}" + (f" {result.url}" if result.url else ""))
        return round_end(args, ctx, result, began)


# The exit status of a one-shot `work` round, by outcome; anything else (failed, setup-failed) is 1. A
# supervisor that starts one round at a time tells waiting from failing without reading the log.
# 75-78 are TauCetiWorker's codes for no progress, an admission wait, a GitHub wait and a halt.
ROUND_EXIT = {"submitted": 0, "needs-references": 0, "blocked": 0, "dry-run": 0, "idle": 75, "skipped": 75,
              "limited": 76, "github-limited": 77, "halted": 78, "interrupted": 130}


def round_end(args, ctx, result, began: dt.datetime) -> int:
    """The round's exit status; with `--status-file`, also its outcome as JSON, replaced atomically."""
    code = ROUND_EXIT.get(result.status, 1)
    if getattr(args, "status_file", ""):
        write_round_status(Path(args.status_file), ctx, result, began, code)
    return code


def write_round_status(path: Path, ctx, result, began: dt.datetime, code: int) -> None:
    rows = usage_mod.summarize(usage_mod.load(ctx.state, ctx.worker_id), by="worker",
                               since=f"{began:%Y-%m-%dT%H:%M:%SZ}")
    spent = {k: v for k, v in rows[0].items() if k != "worker"} if rows else {}
    data = {"job": result.job, "stage": board.stage_of(result.job) if result.job else "", "status": result.status,
            "detail": result.detail, "url": result.url, "agent": result.agent, "exit": code,
            "retry_at": f"{result.retry_at:%Y-%m-%dT%H:%M:%SZ}" if result.retry_at else None,
            "worked_seconds": round(result.worked), "started_at": f"{began:%Y-%m-%dT%H:%M:%SZ}",
            "ended_at": f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ}", "usage": spent}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"warning: --status-file {path}: {exc}", file=sys.stderr)


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


def candidate_readme(path: Path) -> Path:
    """The README a `--candidate` path names: the file itself, or README.md (else design/README.md)
    in the folder."""
    if path.is_dir():
        return next((p for p in (path / "README.md", path / "design" / "README.md") if p.is_file()), path / "README.md")
    return path


def depcheck_lines(name: str, readme: str, siblings: dict[str, str], order: list[str], main_extended: set[str],
                   candidate: bool = False, movable: bool = False) -> tuple[list[str], list[str]]:
    """(the lines to print, the problems) of the dependency check of one roadmap. A candidate that the
    merge order does not list yet (a design) is placed right after its last supplier for the check:
    the intake inserts it there when its design is accepted, so its absence is not a problem.
    `movable` (the agents' check): a roadmap the order lists above one of its suppliers is checked as
    moved right after the last of them, as the worker moves it when it submits, unless that would put
    one of its consumers before it (then the UPWARD line stands, with why)."""
    readmes = {**siblings, name: readme}
    edges = depgraph.supplier_edges(readmes, set(order) | {name}, main_extended)
    lines = []
    if movable and name in order:
        consumers = {c for c, sup in edges.items() if name in sup and c != name}
        try:
            text, note = depgraph.move_after_suppliers("\n".join(order) + "\n", name, set(edges.get(name, {})),
                                                       consumers)
        except depgraph.OrderCycle as exc:
            lines.append(f"not movable: {exc}")
        else:
            if note:
                order = depgraph.read_order(text)
                lines.append(f"moved: {note}; checked as moved (the worker moves it there when it submits)")
    if candidate and name not in order:
        consumers = {c for c, sup in edges.items() if name in sup}
        try:
            text, notes = depgraph.place_in_order("\n".join(order) + "\n", name, set(edges.get(name, {})), consumers)
        except depgraph.OrderCycle as exc:
            problem = f"CYCLE: {exc}"
            return [problem, f"depcheck: {len(readmes)} roadmaps, 1 problem(s)"], [problem]
        order = depgraph.read_order(text)
        after = order[order.index(name) - 1] if order.index(name) else "the start"
        lines.append(f"candidate: {name} is not in the merge order yet; checked as if placed after {after} (the intake "
                     "inserts it when its design is accepted)")
        lines += [f"note: {n}" for n in notes]
    problems = depgraph.check_order(edges, order, only={name})
    lines += problems
    lines.append(f"depcheck: {len(readmes)} roadmaps, {sum(len(v) for v in edges.values())} edges, {len(problems)} problem(s)")
    return lines, problems


def gate(args) -> int:
    from .lanes.base import dropped_names, sibling_readmes
    cfg = _load(args)
    siblings = sibling_readmes(cfg.state_dir(), cfg.roadmap.active_dir)
    order = _order_for_gate(cfg, args.order)
    if args.depcheck or args.candidate:
        if not args.name:
            raise SystemExit("gate --depcheck / --candidate needs --name (or --only)")
        if order is None:
            print("depcheck: no merge-order file; nothing to check")
            return 0
        readme = candidate_readme(Path(args.candidate)) if args.candidate else Path(args.depcheck)
        lines, problems = depcheck_lines(args.name, readme.read_text(encoding="utf-8"), siblings, order,
                                         set(cfg.roadmap.main_extended), candidate=bool(args.candidate),
                                         movable=args.movable)
        for line in lines:
            print(line)
        return 1 if problems else 0
    if not args.worktree or not args.name_pos:
        raise SystemExit("usage: gate <worktree> <Name> [--base REF] | gate --depcheck README --name Name")
    opts = gates.GateOptions.from_config(cfg, order=order, siblings=siblings, dropped=dropped_names(cfg))
    opts.folder = args.folder
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
        cls, entry = sources.classify_match(args.target, s.allow, deny)
        print(f"{cls} ({entry})" if entry else cls)
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


def roadmaps_cmd(args) -> int:
    """The open roadmap pull requests: refresh the host's index, list, search, show a file, resolve a
    README's citations. Only `refresh` reads GitHub; the rest works from the local mirror."""
    cfg = _load(args)
    state = cfg.state_dir()
    if args.action == "refresh":
        index = openroadmaps.refresh(GH(), cfg, force=True)
        print(f"{len(index['prs'])} open roadmap pull requests indexed at {index['at']} (main {index['main'][:7]})")
        return 0
    index = openroadmaps.load(state)
    if not index:
        raise SystemExit("no index of the open roadmap pull requests on this host: run `roadmaps refresh`")
    if args.action == "list":
        print(f"{len(index['prs'])} open roadmap pull requests, index of {index['at']}:")
        print("\n".join(openroadmaps.rows(index)))
        return 0
    if not args.target:
        wanted = {"grep": "pattern", "show": "roadmap name"}.get(args.action, "README path")
        raise SystemExit(f"roadmaps {args.action}: give a {wanted}")
    if args.action == "grep":
        try:
            lines = openroadmaps.grep(state, index, args.target, fixed=args.fixed, ignore_case=args.ignore_case,
                                      lean=not args.readme_only, readme=not args.lean_only, limit=args.limit)
        except ValueError as exc:
            raise SystemExit(f"roadmaps grep: {exc}") from exc
        print("\n".join(lines) if lines else "no match on main or in any open roadmap pull request")
        return 0
    if args.action == "show":
        places = openroadmaps.sources_for(index, args.target)
        if args.pr:
            places = [(label, ref) for label, ref in places if label == f"#{args.pr}"]
        if not places:
            raise SystemExit(f"no roadmap {args.target} on main or in an open roadmap pull request"
                             + (f" #{args.pr}" if args.pr else ""))
        label, ref = places[0]
        text = openroadmaps.read(state, index, ref, args.target, args.file)
        if not text:
            raise SystemExit(f"{args.target} has no {args.file} at {label}")
        sys.stdout.write(f"<!-- {args.target}/{args.file} at {label} -->\n" + text)
        return 0
    readme = Path(args.target)
    lean = Path(args.lean) if args.lean else readme.parent / "Suggested.lean"
    packages = Path(args.packages) if args.packages else openroadmaps.packages_dir()
    citations, names, note = openroadmaps.resolve(
        state, index, readme.read_text(encoding="utf-8"), own=args.name or readme.parent.name,
        lean=lean.read_text(encoding="utf-8") if lean.is_file() else "", packages=packages)
    text = openroadmaps.report(citations, names, note, index, str(readme))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


def usage_cmd(args) -> int:
    """The agent runs' token usage on this host, summed by lane (or job, agent, model, worker, day)."""
    cfg = _load(args)
    worker = paths.sanitize_worker_id(args.worker_id) if args.worker_id else ""
    records = usage_mod.load(cfg.state_dir(), worker)
    rows = usage_mod.summarize(records, by=args.by, since=args.since)
    if args.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
    elif not rows:
        print("no agent run recorded yet" + (f" by {args.worker_id}" if args.worker_id else "") + " on this host")
    else:
        print(usage_mod.table(rows, args.by))
    return 0


RATE_WARN = 200      # a board run warns when fewer REST requests than this are left this hour


def api_budget(gh, repo: str) -> list[str]:
    """The lines a board run ends with: the REST requests it made and what the token has left."""
    calls = getattr(gh, "calls", 0)
    budget = gh.rate_limit(repo) if hasattr(gh, "rate_limit") else {}
    if not budget.get("limit"):
        return [f"GitHub API calls this run: {calls} (rate limit not readable)"]
    resets = time.strftime("%H:%MZ", time.gmtime(budget.get("reset", 0)))
    first = (f"GitHub API calls this run: {calls} (rate limit remaining {budget.get('remaining', '?')}/{budget['limit']}, "
             f"resets {resets})")
    lines = [first]
    if budget.get("remaining", RATE_WARN) < RATE_WARN:
        lines.append(f"WARN: only {budget['remaining']} GitHub API requests left until {resets}; runs of the board "
                     "share this budget")
    return lines


def board_cmd(args) -> int:
    """A board maintenance run, ending with the REST requests it made and the token's budget."""
    if args.step and args.action != "errata":
        raise SystemExit(f"board {args.action}: unexpected argument {args.step!r}")
    cfg = _load(args)
    ctx = _bot_context(args, cfg, dry=args.dry_run or not args.yes)
    try:
        return board_run(args, cfg, ctx)
    finally:
        for line in api_budget(ctx.gh, cfg.repositories.board_repo()):
            print(line)


def board_run(args, cfg, ctx) -> int:
    """Every `board` run that writes (`--yes`; `stale` only with `--release`) holds the board lock for
    its whole length, one run at a time, hosted or local: a heartbeat renews it, and once it is lost
    (another run took it over, or no renewal succeeded before it expired) every further GitHub write
    raises and the run stops, exit 1. A run that finds the lock held says so and exits 0 having done
    nothing; one that cannot take it (no push access, no network) does nothing either, exit 1."""
    from . import intake
    write = args.yes and not args.dry_run
    if not write or (args.action == "stale" and not args.release):
        return _board_steps(args, cfg, ctx, intake, write)
    lock, key = intake.board_lock(cfg, ctx), intake.lock_key(cfg.project.name)
    status, line = intake.take_lock(lock, cfg.project.name)
    if line:
        print(line)
    if status != claims.ACQUIRED:
        return 0 if status == claims.HELD else 1
    try:
        with claims.Heartbeat(lock, key, intake.LOCK_TTL, intake.LOCK_RENEW, gh=ctx.gh, log=print):
            return _board_steps(args, cfg, ctx, intake, write)
    except claims.LeaseLost as exc:
        print(f"stopped: {exc}; the next run carries on")
        return 1
    finally:
        lock.release(key)


def _board_steps(args, cfg, ctx, intake, write: bool) -> int:
    shared = None
    if cfg.repositories.claims:
        shared = claims.Claims(repo=cfg.repositories.claims,
                               gitdir=cfg.state_dir() / "claims" / (cfg.repositories.claims.replace("/", "__") + ".git"),
                               owner=f"{ctx.operator}/board", operator=ctx.operator, project=cfg.project.name,
                               dry_run=ctx.dry_run)
    if args.action in ("intake", "references"):
        skipped: list[str] = []
        if args.action == "intake":
            try:
                for line in intake.repair_duplicates(ctx, shared, yes=write):
                    print(line)
            except GHError as exc:
                print(f"duplicates: skipped: GitHub did not answer ({str(exc).splitlines()[0][:200]})")
            skipped += [f"#{n}" for n in intake.intake_open(ctx, write, only=args.pr or 0, shared=shared)]
            if not args.pr:
                try:
                    for line in intake.reconcile_merged(ctx, yes=write):
                        print(line)
                except GHError as exc:
                    print(f"merged submissions: skipped: GitHub did not answer ({str(exc).splitlines()[0][:200]})")
        if not args.pr:
            for line in references.resume_all(ctx, ctx.sources_fetcher(), yes=write):
                print(line)
        if args.action == "intake":
            for line in boardpr.scan(ctx, shared, yes=write):
                print(line)
                if ": skipped: GitHub did not answer" in line:
                    skipped.append(line.split(":", 1)[0])
            if not args.pr:
                # A roadmap the order lists above one of its suppliers moves after the last of them (a
                # catch-up: its trouble is logged, as the import backfill's is, and fails no run).
                try:
                    for line in intake.replace_upward_roadmaps(ctx, yes=write):
                        print(line)
                except (GHError, gitops.GitError, OSError) as exc:
                    print(f"merge order: suppliers not checked ({str(exc).splitlines()[0][:200]}); the next run "
                          "tries again")
        if skipped:
            print("skipped (GitHub errors; the next run tries again): " + ", ".join(dict.fromkeys(skipped)))
            return 1
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
        return 1 if any(s.failed for s in steps) else 0
    if args.action == "errata":
        # The errata of the roadmaps' sources: `seed` creates one job per roadmap not yet covered.
        if args.step != "seed":
            raise SystemExit("board errata: give the step: `board errata seed [--limit N] [--yes]`")
        for line in errata.seed(ctx, yes=write, limit=args.limit):
            print(line)
        return 0
    if args.action == "sync":
        steps = [("filers", lambda: filers.refresh(ctx)), ("targets", lambda: intake.sync_targets(ctx)),
                 ("states", lambda: intake.sync_states(ctx, shared)), ("expired", lambda: intake.sync_expired(ctx, shared)),
                 ("leases", lambda: intake.sync_leases(ctx, shared)), ("pull requests", lambda: intake.sync_pull_requests(ctx)),
                 ("pull-request reviews", lambda: intake.sync_pr_reviews(ctx)),
                 ("max rounds", lambda: intake.repark_capped(ctx)), ("origins", lambda: intake.sync_origins(ctx)),
                 ("gap registry", lambda: intake.note_gap_registry_blocks(ctx)),
                 ("review queue", lambda: boardpr.review_queue(ctx, yes=write)),
                 ("errata", lambda: errata.sync_errata(ctx, shared))]
        failed = []
        for name, step in steps:      # one step's GitHub trouble never stops the others
            try:
                for line in step():
                    print(line)
            except GHError as exc:
                failed.append(name)
                print(f"sync {name}: skipped: GitHub did not answer ({str(exc).splitlines()[0][:200]})")
        if failed:
            print("skipped (GitHub errors; the next run tries again): " + ", ".join(failed))
            return 1
        return 0
    for line in intake.stale(ctx, shared, release=args.release):
        print(line)
    return 0


# --- parser ---------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tauceti-roadmap", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", help="tauceti-roadmap.toml path or https URL")
    ap.add_argument("--overlay", help="this operator's host settings over the project file (default: $TAUCETI_ROADMAP_OVERLAY)")
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
    w.add_argument("--status-file", default="", metavar="PATH",
                   help="one round only: write its outcome, exit status and token usage to PATH as JSON")
    w.add_argument("--private-claims", action="store_true",
                   help="work with leases in your own fork although the configured claims namespace is not writable")

    g = sub.add_parser("gate", help="run the push gates on a roadmap folder (exit 1 on any failure)")
    g.add_argument("worktree", nargs="?")
    g.add_argument("name_pos", nargs="?", metavar="Name")
    g.add_argument("--base", default="", help="merge-base reference for the outside-folder diff, e.g. origin/main")
    g.add_argument("--folder", default="", metavar="PATH",
                   help="the roadmap's folder under the active directory when it is nested, e.g. "
                        "RepresentationTheory/CompactGroups (default: Name)")
    g.add_argument("--build", action="store_true", help="also run the configured targeted build")
    g.add_argument("--json", action="store_true")
    g.add_argument("--out", default="")
    g.add_argument("--depcheck", default="", metavar="README", help="only the dependency check of this README")
    g.add_argument("--movable", action="store_true",
                   help="with --depcheck: check a roadmap listed above one of its suppliers as moved right after the "
                        "last of them (the worker moves it so when it submits), unless a consumer of it comes earlier")
    g.add_argument("--candidate", default="", metavar="PATH",
                   help="the dependency check of a roadmap that may not be in the merge order yet (a design): its "
                        "README (or a folder holding README.md or design/README.md); when the order does not list it, "
                        "it is placed right after its last supplier for the check")
    g.add_argument("--name", "--only", dest="name", default="")
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

    sub.add_parser("throttle", help="wait for one of this host's GitHub content-write slots ([worker] writes_per_minute, "
                                    "writes_per_hour), for scripts that write with gh themselves")
    st = sub.add_parser("status", help="the board's jobs by state, with their ages and claimants")
    st.add_argument("--all", action="store_true", help="include done jobs and closed issues")

    r = sub.add_parser("roadmaps", help="the open roadmap pull requests: refresh the index, list, search them, show a "
                                        "file, resolve a README's citations")
    r.add_argument("action", choices=["refresh", "list", "grep", "show", "resolve"])
    r.add_argument("target", nargs="?", default="", help="grep: a pattern (extended regex); show: a roadmap name; "
                                                         "resolve: a README.md")
    r.add_argument("--fixed", action="store_true", help="grep: the pattern is a fixed string")
    r.add_argument("-i", "--ignore-case", action="store_true")
    r.add_argument("--readme-only", action="store_true")
    r.add_argument("--lean-only", action="store_true")
    r.add_argument("--limit", type=int, default=200, help="grep: at most this many lines")
    r.add_argument("--pr", type=int, default=0, help="show: this pull request's copy")
    r.add_argument("--file", default="README.md", choices=["README.md", "Suggested.lean"], help="show: which file")
    r.add_argument("--name", default="", help="resolve: the roadmap's own name (default: the README's folder)")
    r.add_argument("--lean", default="", help="resolve: its Suggested.lean (default: beside the README)")
    r.add_argument("--packages", default="", help="resolve: the pinned Lake packages (default: the host's shared ones)")
    r.add_argument("--out", default="", help="resolve: also write the report here")

    u = sub.add_parser("usage", help="the agent runs' token usage on this host, summed by lane, job, agent, ...")
    u.add_argument("--worker-id", default="", help="only this worker's runs")
    u.add_argument("--by", choices=usage_mod.GROUPS, default="lane")
    u.add_argument("--since", default="", help="only runs at or after this UTC date or time (2026-10-21)")
    u.add_argument("--json", action="store_true")

    b = sub.add_parser("board", help="board maintenance for the bot or a maintainer (dry run unless --yes)")
    b.add_argument("action", choices=["intake", "references", "sync", "stale", "import-explorer", "errata"])
    b.add_argument("step", nargs="?", default="", help="errata: seed (an errata job per roadmap not yet covered)")
    b.add_argument("--pr", type=int, default=0)
    b.add_argument("--from", dest="source", default="", help="import-explorer: a checkout of CBirkbeck/tauceti-explorer")
    b.add_argument("--kinds", default="", help="import-explorer: package,design,target (default: all)")
    b.add_argument("--names", default="", help="import-explorer: only these roadmaps (comma list)")
    b.add_argument("--limit", type=int, default=0, help="errata seed: at most this many jobs per run (0: all)")
    b.add_argument("--yes", action="store_true")
    b.add_argument("--release", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handlers = {"doctor": doctor, "targets": targets, "work": work, "gate": gate, "merge-order": merge_order,
                "sources": sources_cmd, "board": board_cmd, "tail": tail_cmd, "status": status_cmd,
                "usage": usage_cmd, "roadmaps": roadmaps_cmd, "throttle": throttle_cmd}
    try:
        return handlers[args.command](args)
    except GHRateLimited as exc:
        print(f"GitHub: rate limited: {exc}", file=sys.stderr)
        return ROUND_EXIT["github-limited"]
    except GHHalt as exc:
        print(f"GitHub: halted: {exc}", file=sys.stderr)
        return ROUND_EXIT["halted"]
    except GHError as exc:
        print(f"GitHub: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

