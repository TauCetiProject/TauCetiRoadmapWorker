"""Project configuration: `tauceti-roadmap.toml`.

The file is human-owned (CODEOWNERS) and public: it never contains credentials. It is selected in
this order: `--conf PATH_OR_HTTPS_URL`, `$TAUCETI_ROADMAP_CONF`, `./tauceti-roadmap.toml`, then
`tauceti-roadmap.toml` at the root of the current git checkout. A URL is fetched once and saved
in the project state directory, as TauCetiTools does with `tauceti.toml`.

Every field is documented in `tauceti-roadmap.toml.example`; the dataclasses below carry the
defaults, so a minimal file needs only `[project] name` and `[repositories] content`.
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

AGENTS = ("codex", "claude")
SANDBOX_PROFILES = ("off", "proxy", "netjail")
LICENCE_CLASSES = ("open-repository", "publisher-open", "author-page", "institutional", "operator-local")
# GitHub's author_association values: who may answer a `needs-references` request.
ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(Exception):
    pass


@dataclass
class Project:
    name: str = ""
    branch: str = "main"


@dataclass
class Repositories:
    content: str = "TauCetiProject/TauCetiRoadmap"
    board: str = ""
    packages: str = ""
    claims: str = ""
    library: str = "TauCetiProject/TauCeti"

    def board_repo(self) -> str:
        return self.board or self.content

    def packages_repo(self) -> str:
        return self.packages or self.board_repo()


@dataclass
class Board:
    namespace_label: str = "roadmap-job"
    packages_dir: str = "packages"
    max_rounds: int = 3
    bot_logins: list[str] = field(default_factory=lambda: ["github-actions[bot]"])
    stale_hours: float = 24.0
    intentions: bool = True
    # `targets import-intentions` imports open issues of the content repository carrying all of these.
    intention_labels: list[str] = field(default_factory=lambda: ["intention"])


@dataclass
class Roadmap:
    active_dir: str = "TauCetiRoadmap"
    completed_dir: str = "Completed"
    merge_order_repo: str = "packages"
    merge_order_file: str = "merge-order/ORDER.txt"
    pr_label: str = "roadmap-worker"
    open_as_draft: bool = True
    main_extended: list[str] = field(default_factory=list)


@dataclass
class Gates:
    build: str = "lake build {module}"
    elaborate: str = "lake env lean {file}"
    max_lean_lines: int = 5000
    forbid_mathlib_subimports: bool = True
    forbidden_patterns: list[str] = field(default_factory=list)
    dropped_names_file: str = ""
    outside_folder_allow: list[str] = field(default_factory=list)
    require_depcheck: bool = True


@dataclass
class RedTeamPass:
    agent: str = "codex"
    model: str = ""


@dataclass
class RedTeam:
    rounds: int = 2
    passes: list[RedTeamPass] = field(default_factory=lambda: [RedTeamPass("codex"), RedTeamPass("claude")])
    sharded: bool = False
    shard_max: int = 8
    ready_after: bool = False


@dataclass
class Design:
    daily_cap: int = 2


@dataclass
class LaneAgent:
    agent: str = ""          # pin the family (codex | claude); '' leaves it to the worker
    model: str = ""          # pin the model of `agent` (or of the default family)
    codex_model: str = ""    # pin the model per family, leaving the family free
    claude_model: str = ""


@dataclass
class Agents:
    default: str = "codex"
    codex_model: str = "gpt-6.1-sol"
    claude_model: str = "claude-opus-5-5"
    timeout_minutes: int = 360
    lanes: dict[str, LaneAgent] = field(default_factory=dict)


@dataclass
class Sources:
    allow: dict[str, list[str]] = field(default_factory=lambda: {
        "open-repository": ["arxiv.org", ".arxiv.org", "hal.science", ".hal.science", ".archives-ouvertes.fr",
                            "numdam.org", ".numdam.org",
                            "gdz.sub.uni-goettingen.de", "eudml.org", ".eudml.org", "projecteuclid.org",
                            "stacks.math.columbia.edu", "zbmath.org", ".zbmath.org"],
        "publisher-open": ["doi.org"],
        "author-page": [],
        "institutional": [],
    })
    deny: list[str] = field(default_factory=lambda: [
        ".libgen.is", ".libgen.rs", ".libgen.li", ".sci-hub.se", ".sci-hub.st", ".sci-hub.ru",
        ".z-lib.org", ".z-library.sk", ".annas-archive.org", ".library.lol", ".booksc.org",
    ])
    uncertified_ok: bool = True
    # Directories of texts the operator already holds (licence class operator-local); `~` is expanded.
    local_dirs: list[str] = field(default_factory=list)
    # Hosts whose pages are paywalled: never fetched, so a citation that resolves only there is
    # recorded as uncertified. DOIs with these prefixes are not even resolved.
    paywalled: list[str] = field(default_factory=lambda: [
        ".sciencedirect.com", ".elsevier.com", ".springer.com", ".springerlink.com", ".springernature.com"])
    paywalled_doi_prefixes: list[str] = field(default_factory=lambda: ["10.1016", "10.1007"])
    user_agent: str = ""
    max_megabytes: int = 200
    ocr: bool = False
    ocr_dpi: int = 300
    ocr_min_chars: int = 250
    # Who may answer a needs-references request (name a source, name a local file, waive).
    answer_associations: list[str] = field(default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"])
    # Whose request comments the intake reads (the board's bot logins always count): operators post
    # them, and an operator whose submissions were merged is at least a CONTRIBUTOR.
    request_associations: list[str] = field(default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR",
                                                                      "CONTRIBUTOR"])


@dataclass
class Sandbox:
    profile: str = "off"
    proxy_url: str = "http://127.0.0.1:3128"
    netjail: str = ""
    claude_hook: bool = True


@dataclass
class Replies:
    review_markers: list[str] = field(default_factory=lambda: [
        "ChatGPT", "GPT-", "Astra", "\U0001f916", ":robot:", "AI-assisted", "Deep re-review", "Deep review"])
    approvers: list[str] = field(default_factory=list)
    ignore_logins: list[str] = field(default_factory=lambda: ["github-actions[bot]"])
    awaiting_author: str = "awaiting-author"
    awaiting_review: str = "awaiting-review"
    housekeeping_prefixes: list[str] = field(default_factory=lambda: [
        "Red-team repairs at", "Scope change at", "Supplier change at", "Ownership change at", "Housekeeping at"])


@dataclass
class Prompts:
    extra_rules_file: str = ""


@dataclass
class Worker:
    # Seconds a `--loop` worker sleeps when no job is eligible; every poll is a board listing on the
    # operator's GitHub quota. After a failure, and once the board answers a job the worker parked
    # for references, the next poll comes within a minute whatever this says.
    idle_seconds: int = 600


@dataclass
class Config:
    schema_version: int = 1
    project: Project = field(default_factory=Project)
    repositories: Repositories = field(default_factory=Repositories)
    board: Board = field(default_factory=Board)
    roadmap: Roadmap = field(default_factory=Roadmap)
    gates: Gates = field(default_factory=Gates)
    redteam: RedTeam = field(default_factory=RedTeam)
    design: Design = field(default_factory=Design)
    agents: Agents = field(default_factory=Agents)
    sources: Sources = field(default_factory=Sources)
    sandbox: Sandbox = field(default_factory=Sandbox)
    replies: Replies = field(default_factory=Replies)
    prompts: Prompts = field(default_factory=Prompts)
    worker: Worker = field(default_factory=Worker)
    path: str = ""
    warnings: list[str] = field(default_factory=list)

    def state_dir(self) -> Path:
        return paths.state_root(self.project.name)

    def lane_agent(self, kind: str) -> tuple[str, str]:
        """(agent, model) for a lane: the lane override, else the default agent and its model."""
        lane = self.agents.lanes.get(kind)
        agent = (lane.agent if lane and lane.agent else self.agents.default)
        return agent, self.lane_model(kind, agent) or self.model_for(agent)

    def lane_model(self, kind: str, agent: str) -> str:
        """The model a lane pins for an agent family, or '' when it pins none for that family."""
        lane = self.agents.lanes.get(kind)
        if not lane:
            return ""
        if lane.model and (not lane.agent or lane.agent == agent):
            return lane.model
        return {"codex": lane.codex_model, "claude": lane.claude_model}.get(agent, "")

    def lane_models(self, kind: str) -> dict[str, str]:
        """agent family -> the model the lane pins for it (only pinned families)."""
        return {a: m for a in AGENTS if (m := self.lane_model(kind, a))}

    def model_for(self, agent: str) -> str:
        return self.agents.claude_model if agent == "claude" else self.agents.codex_model


# --- loading -------------------------------------------------------------------------------------

def _build(cls, data, where: str, warnings: list[str]):
    """Instantiate dataclass `cls` from a TOML table, recursing into nested dataclasses and lists of
    them. Unknown keys are reported as warnings rather than errors, so an older worker keeps
    running against a newer file."""
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a table")
    kwargs = {}
    hints = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in hints or key in ("path", "warnings"):
            warnings.append(f"unknown key {where}.{key}" if where else f"unknown key {key}")
            continue
        default = getattr(cls(), key)
        if dataclasses.is_dataclass(default):
            kwargs[key] = _build(type(default), value, f"{where}.{key}".strip("."), warnings)
        elif key == "passes" and cls is RedTeam:
            kwargs[key] = [_build(RedTeamPass, item, f"{where}.passes", warnings) for item in value]
        elif key == "lanes" and cls is Agents:
            kwargs[key] = {k: _build(LaneAgent, v, f"agents.lanes.{k}", warnings) for k, v in value.items()}
        else:
            if default is not None and not isinstance(value, type(default)) and not (
                    isinstance(default, float) and isinstance(value, int)):
                raise ConfigError(f"{where}.{key}: expected {type(default).__name__}, got {type(value).__name__}")
            kwargs[key] = value
    return cls(**kwargs)


def parse(text: str, path: str = "<string>") -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    warnings: list[str] = []
    cfg = _build(Config, data, "", warnings)
    cfg.path, cfg.warnings = path, warnings
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    problems = []
    if not cfg.project.name or not re.match(r"^[A-Za-z0-9_.-]+$", cfg.project.name):
        problems.append("project.name must be a non-empty [A-Za-z0-9_.-] string")
    for key in ("content", "board", "packages", "claims", "library"):
        value = getattr(cfg.repositories, key)
        if value and not REPO_RE.match(value):
            problems.append(f"repositories.{key} must be OWNER/NAME, got {value!r}")
    if cfg.sandbox.profile not in SANDBOX_PROFILES:
        problems.append(f"sandbox.profile must be one of {SANDBOX_PROFILES}")
    if cfg.agents.default not in AGENTS:
        problems.append(f"agents.default must be one of {AGENTS}")
    for kind, lane in cfg.agents.lanes.items():
        if lane.agent and lane.agent not in AGENTS:
            problems.append(f"agents.lanes.{kind}.agent must be one of {AGENTS}")
    if cfg.redteam.rounds < 1:
        problems.append("redteam.rounds must be at least 1")
    if cfg.redteam.shard_max < 1:
        problems.append("redteam.shard_max must be at least 1")
    if not cfg.redteam.passes:
        problems.append("redteam.passes must list at least one pass")
    for p in cfg.redteam.passes:
        if p.agent not in AGENTS:
            problems.append(f"redteam.passes: agent must be one of {AGENTS}")
    if cfg.board.max_rounds < 1:
        problems.append("board.max_rounds must be at least 1")
    if cfg.design.daily_cap < 0:
        problems.append("design.daily_cap must be >= 0")
    if cfg.worker.idle_seconds < 30:
        problems.append("worker.idle_seconds must be at least 30")
    if cfg.roadmap.merge_order_repo not in ("packages", "content", "local"):
        problems.append("roadmap.merge_order_repo must be packages, content or local")
    for cls in cfg.sources.allow:
        if cls not in LICENCE_CLASSES:
            problems.append(f"sources.allow: unknown licence class {cls!r} (known: {', '.join(LICENCE_CLASSES)})")
    for key in ("answer_associations", "request_associations"):
        for assoc in getattr(cfg.sources, key):
            if assoc not in ASSOCIATIONS:
                problems.append(f"sources.{key}: unknown association {assoc!r} (known: {', '.join(ASSOCIATIONS)})")
    if cfg.sources.max_megabytes < 1 or cfg.sources.ocr_dpi < 72:
        problems.append("sources.max_megabytes must be at least 1 and sources.ocr_dpi at least 72")
    if not cfg.board.intention_labels:
        problems.append("board.intention_labels must name at least one label")
    for pattern in cfg.gates.forbidden_patterns + cfg.gates.outside_folder_allow:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"gates: bad regular expression {pattern!r}: {exc}")
    if problems:
        raise ConfigError("; ".join(problems))


def discover(conf: str | None, cwd: Path | None = None) -> str:
    """The configuration location by the documented precedence, or raise ConfigError."""
    if conf:
        return conf
    if os.environ.get("TAUCETI_ROADMAP_CONF"):
        return os.environ["TAUCETI_ROADMAP_CONF"]
    cwd = cwd or Path.cwd()
    local = cwd / "tauceti-roadmap.toml"
    if local.is_file():
        return str(local)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, timeout=10)
        if top.returncode == 0:
            candidate = Path(top.stdout.strip()) / "tauceti-roadmap.toml"
            if candidate.is_file():
                return str(candidate)
    except (OSError, subprocess.SubprocessError):
        pass
    raise ConfigError("no configuration: pass --conf PATH_OR_URL or set TAUCETI_ROADMAP_CONF")


def load(conf: str | None = None) -> Config:
    where = discover(conf)
    if where.startswith("https://"):
        with urllib.request.urlopen(where, timeout=30) as response:  # noqa: S310 - https only
            text = response.read().decode("utf-8")
        cfg = parse(text, where)
        saved = cfg.state_dir() / "tauceti-roadmap.toml"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(text, encoding="utf-8")
        return cfg
    if where.startswith("http://"):
        raise ConfigError("refusing a plain-http configuration URL; use https")
    path = Path(where).expanduser()
    if not path.is_file():
        raise ConfigError(f"configuration not found: {path}")
    return parse(path.read_text(encoding="utf-8"), str(path))
