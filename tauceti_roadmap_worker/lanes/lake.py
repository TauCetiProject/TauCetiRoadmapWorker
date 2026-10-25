"""One built set of Lake packages per pin, shared by every checkout of the machine.

A worker's content checkout and its roadmap worktrees (pr/<Name>, port/<Name>) never build Mathlib
of their own: their `.lake/packages` is a link to `<state>/lake/<pin>/packages`, one per pin of the
manifest/toolchain/lakefile, filled once by `lake exe cache get` under a lock (`.lock`) and marked
`.ready` with the pin it was filled for. The pin is computed again on every setup, so a checkout
whose manifest moved (main bumped, a pull request branch checked out) is relinked to its new pin's
packages and never builds into another pin's. The package, review and fix lanes elaborate
`Suggested.lean` in the content checkout wired this way; the port, red-team fix and gate lanes
build in a worktree wired the same way. When the wiring cannot deliver a usable toolchain and
cache, `prepare_lake` says why, and the lane reports it instead of skipping the elaboration.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PIN_FILES = ("lake-manifest.json", "lean-toolchain", "lakefile.lean", "lakefile.toml")
CACHE_GET = ("lake", "exe", "cache", "get")
CACHE_GET_TIMEOUT = 3600
READY_NOTES = ("shared Lake packages ready", "shared Lake packages fetched")
EXTERNAL = "TAUCETI_ROADMAP_LAKE_PACKAGES"

MIN_FREE_GB_BUILD = 20       # a lane that checks out and builds the roadmap is not started below this
MIN_FREE_GB_WARN = 40        # `doctor` warns below this


def free_gb(path: Path) -> float | None:
    """Free space on the filesystem of `path` (its nearest existing ancestor), in GB."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return None


def lake_on_path() -> str | None:
    """Where `lake` is (elan's proxy, which installs the pinned toolchain on first use), or None."""
    return shutil.which("lake")


def lake_pin(worktree: Path) -> str:
    """What decides a checkout's Lake packages (its manifest, toolchain and lakefile), as a short hash."""
    h = hashlib.sha256()
    for name in PIN_FILES:
        path = worktree / name
        h.update(name.encode() + b"\0" + (path.read_bytes() if path.is_file() else b"") + b"\0")
    return h.hexdigest()[:16]


def shared_packages(state: Path, worktree: Path) -> Path:
    """The machine's one Lake package directory for the checkout's pin: `<state>/lake/<pin>/packages`."""
    return Path(state) / "lake" / lake_pin(worktree) / "packages"


def ready_pin(packages: Path) -> str:
    """The pin the packages were fetched for (the first word of `.ready` beside them), '' when they were
    not. A `.ready` written before it held the pin reads as not fetched, and the next fetch rewrites it."""
    try:
        words = (packages.parent / ".ready").read_text(encoding="utf-8").split()
    except OSError:
        return ""
    return words[0] if words else ""


def mark_ready(packages: Path, pin: str) -> None:
    """Mark the packages fetched for `pin` (`.ready` beside them: the pin, then the time)."""
    (packages.parent / ".ready").write_text(f"{pin} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")


def external_packages(checkout: Path) -> tuple[Path | None, str]:
    """The operator's own built packages (`TAUCETI_ROADMAP_LAKE_PACKAGES`) when they were built for the
    checkout's pin, else (None, why they are not used; '' when none are exported). They must be the
    `.lake/packages` of a Lake checkout, whose manifest, toolchain and lakefile are their pin: a tree
    anywhere else cannot be verified, and building into one of another pin would move its packages."""
    value = os.environ.get(EXTERNAL, "")
    if not value:
        return None, ""
    tree = Path(value).expanduser()
    root = tree.parent.parent
    if not tree.is_dir():
        why = "is not a directory"
    elif tree.name != "packages" or tree.parent.name != ".lake" or not (root / "lake-manifest.json").is_file():
        why = "is not the `.lake/packages` of a Lake checkout, so the pin it was built for cannot be verified"
    elif lake_pin(root) != lake_pin(checkout):
        why = f"was built for {root}, whose manifest, toolchain or lakefile differ from those of {checkout}"
    else:
        return tree.resolve(), ""
    return None, f"{EXTERNAL}={value} not used: it {why}; the shared packages of the pin are used instead"


def _link(dst: Path, src: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() and os.path.realpath(dst) == str(src.resolve()):
        return
    if dst.is_symlink() or dst.is_file():
        dst.unlink()                  # a link to another pin's packages, or dangling: never followed
    elif dst.is_dir():
        shutil.rmtree(dst)            # a copy left by an earlier build: the shared one replaces it
    os.symlink(src.resolve(), dst)


def wire_packages(checkout: Path, state: Path | None, content: Path | None = None) -> tuple[Path | None, str]:
    """Link `checkout/.lake/packages` for the pin the checkout has now, and return (the directory it
    uses, why the operator's `TAUCETI_ROADMAP_LAKE_PACKAGES` was not used, '' when it was or none is
    exported). The content checkout (`content` None) keeps packages that are its own directory (its
    own build: nothing of the checkout's own is removed); a worktree of `content` shares those only
    when both have the same pin. Otherwise the operator's packages when they were built for the pin,
    else `<state>/lake/<pin>/packages`. A link left for another pin is replaced; without `state`
    and the operator's packages there is nothing to link (None)."""
    link = checkout / ".lake" / "packages"
    own = (content if content is not None else checkout) / ".lake" / "packages"
    if own.is_dir() and not own.is_symlink() and (content is None or content == checkout
                                                  or lake_pin(content) == lake_pin(checkout)):
        if own != link:
            _link(link, own)
        return own.resolve(), ""
    src, why = external_packages(checkout)
    if src is None:
        if state is None:
            return None, why
        src = shared_packages(state, checkout)
        src.mkdir(parents=True, exist_ok=True)
    _link(link, src)
    return src, why


def link_lake_packages(content: Path, worktree: Path, state: Path | None = None) -> Path | None:
    """Give a worktree of `content` a `.lake/packages` that is a link to one shared copy for its pin
    (Mathlib and Tau Ceti, 16 GB), never a copy of its own (see `wire_packages`). Returns the
    directory linked to."""
    return wire_packages(worktree, state, content)[0]


def link_checkout_packages(checkout: Path, state: Path) -> Path:
    """Wire a content checkout itself to the shared packages for its pin, unless it has packages of its
    own (see `wire_packages`). Returns the directory it uses."""
    return wire_packages(checkout, state)[0]


@contextmanager
def pin_lock(packages: Path | None) -> Iterator[None]:
    """The pin's lock (`<state>/lake/<pin>/.lock`, the one the first fetch takes): two workers of the
    machine never build into the shared packages at once. Without packages there is nothing to lock."""
    if packages is None:
        yield
        return
    packages.parent.mkdir(parents=True, exist_ok=True)
    with open(packages.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def ensure_lake_cache(worktree: Path, shared: Path | None, run=subprocess.run) -> str:
    """Fetch the prebuilt Mathlib (and other) oleans into the shared packages once per pin: `lake exe
    cache get` runs only when the directory is not marked ready for the worktree's pin, under a lock
    so two workers do not fetch at once. Returns what happened."""
    if shared is None:
        return "no shared Lake packages"
    pin = lake_pin(worktree)
    if ready_pin(shared) == pin:
        return "shared Lake packages ready"
    with open(shared.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready_pin(shared) == pin:
            return "shared Lake packages ready"
        try:
            done = run(list(CACHE_GET), cwd=worktree, capture_output=True, text=True, timeout=CACHE_GET_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"lake exe cache get failed ({exc}); the build fetches what it needs"
        if done.returncode != 0:
            return f"lake exe cache get exited {done.returncode}; the build fetches what it needs"
        mark_ready(shared, pin)
        return "shared Lake packages fetched"


@dataclass
class LakeSetup:
    """What a checkout has for `lake`: its packages directory, why it cannot build or elaborate
    (`reason`, '' when it can), and why the operator's own packages were not used (`warning`)."""
    packages: Path | None = None
    reason: str = ""
    note: str = ""
    warning: str = ""

    @property
    def ready(self) -> bool:
        return not self.reason


def prepare_lake(checkout: Path, state: Path, content: Path | None = None, fetch: bool = True,
                 run=subprocess.run, free=free_gb, which=None) -> LakeSetup:
    """Wire `checkout` (a worktree of `content`, or the content checkout itself when `content` is None)
    to the shared packages of the pin it has now and make sure they are fetched for that pin, within
    the disk guard: below MIN_FREE_GB_BUILD no fetch starts (a ready pin costs nothing). Never raises;
    the reason it could not comes back in the setup."""
    try:
        packages, warning = wire_packages(checkout, state, content)
    except OSError as exc:
        return LakeSetup(reason=f"the Lake packages could not be linked ({exc})")
    if not (which("lake") if which is not None else lake_on_path()):
        return LakeSetup(packages, reason="`lake` is not on PATH (install elan; the pinned toolchain follows)",
                         warning=warning)
    if packages is None:
        return LakeSetup(reason="no shared Lake packages", warning=warning)
    if ready_pin(packages) == lake_pin(checkout):
        return LakeSetup(packages, note="shared Lake packages ready", warning=warning)
    if not fetch:
        return LakeSetup(packages, reason="the shared Lake packages of this pin are not fetched yet (no fetch in this run)",
                         warning=warning)
    gb = free(state)
    if gb is not None and gb < MIN_FREE_GB_BUILD:
        return LakeSetup(packages, reason=f"only {gb:.0f} GB free on the state directory's disk; fetching the shared "
                                          f"Lake packages needs {MIN_FREE_GB_BUILD} GB", warning=warning)
    note = ensure_lake_cache(checkout, packages, run)
    if note not in READY_NOTES:
        return LakeSetup(packages, reason=note.split("; the build", 1)[0], note=note, warning=warning)
    return LakeSetup(packages, note=note, warning=warning)
