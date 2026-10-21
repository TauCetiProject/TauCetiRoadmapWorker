"""One built set of Lake packages per pin, shared by every checkout of the machine.

A worker's content checkout and its roadmap worktrees (pr/<Name>, port/<Name>) never build Mathlib
of their own: their `.lake/packages` is a link to `<state>/lake/<pin>/packages`, one per pin of the
manifest/toolchain/lakefile, filled once by `lake exe cache get` under a lock (`.lock`) and marked
`.ready`. The package, review and fix lanes elaborate `Suggested.lean` in the content checkout wired
this way; the port, red-team fix and gate lanes build in a worktree wired the same way. When the
wiring cannot deliver a usable toolchain and cache, `prepare_lake` says why, and the lane reports it
instead of skipping the elaboration.
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


def _link(dst: Path, src: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)            # a copy left by an earlier build: the shared one replaces it
    os.symlink(src.resolve(), dst)


def link_lake_packages(content: Path, worktree: Path, state: Path | None = None) -> Path | None:
    """Give a worktree a `.lake/packages` that is a link to one shared copy (Mathlib and Tau Ceti,
    16 GB), never a copy of its own: the content checkout's own packages when it has them (the
    `TAUCETI_ROADMAP_LAKE_PACKAGES` setup, or the checkout already wired to the shared copy), else
    `<state>/lake/<pin>/packages`, one per pin of the manifest, shared by every worker of the
    machine. Returns the directory linked to."""
    own = content / ".lake" / "packages"
    src = own.resolve() if own.is_dir() else (shared_packages(state, worktree) if state is not None else None)
    if src is None:
        return None
    src.mkdir(parents=True, exist_ok=True)
    _link(worktree / ".lake" / "packages", src)
    return src


def link_checkout_packages(checkout: Path, state: Path) -> Path:
    """Wire a content checkout itself to the machine's shared packages for its pin, unless it already
    has packages (its own build, the `TAUCETI_ROADMAP_LAKE_PACKAGES` link, or the shared link of an
    earlier round): nothing of the checkout's own is ever removed. Returns the directory it uses."""
    own = checkout / ".lake" / "packages"
    if own.is_dir():
        return own.resolve()
    src = shared_packages(state, checkout)
    src.mkdir(parents=True, exist_ok=True)
    if own.is_symlink() or own.is_file():
        own.unlink()                  # a dangling link (the shared directory was removed)
    own.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(src.resolve(), own)
    return src


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
    cache get` runs only when the pin's directory is not marked ready, under a lock so two workers do
    not fetch at once. Returns what happened."""
    if shared is None:
        return "no shared Lake packages"
    ready = shared.parent / ".ready"
    if ready.exists():
        return "shared Lake packages ready"
    with open(shared.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready.exists():
            return "shared Lake packages ready"
        try:
            done = run(list(CACHE_GET), cwd=worktree, capture_output=True, text=True, timeout=CACHE_GET_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"lake exe cache get failed ({exc}); the build fetches what it needs"
        if done.returncode != 0:
            return f"lake exe cache get exited {done.returncode}; the build fetches what it needs"
        ready.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        return "shared Lake packages fetched"


@dataclass
class LakeSetup:
    """What a checkout has for `lake`: its packages directory, and why it cannot build or elaborate
    (`reason`, '' when it can)."""
    packages: Path | None = None
    reason: str = ""
    note: str = ""

    @property
    def ready(self) -> bool:
        return not self.reason


def prepare_lake(checkout: Path, state: Path, content: Path | None = None, fetch: bool = True,
                 run=subprocess.run, free=free_gb, which=None) -> LakeSetup:
    """Wire `checkout` (a worktree of `content`, or the content checkout itself when `content` is None)
    to the shared packages and make sure they are fetched, within the disk guard: below
    MIN_FREE_GB_BUILD no fetch starts (a ready pin costs nothing). Never raises; the reason it could
    not comes back in the setup."""
    try:
        packages = (link_lake_packages(content, checkout, state) if content is not None and content != checkout
                    else link_checkout_packages(checkout, state))
    except OSError as exc:
        return LakeSetup(reason=f"the Lake packages could not be linked ({exc})")
    if not (which("lake") if which is not None else lake_on_path()):
        return LakeSetup(packages, reason="`lake` is not on PATH (install elan; the pinned toolchain follows)")
    if packages is None:
        return LakeSetup(reason="no shared Lake packages")
    if (packages.parent / ".ready").exists():
        return LakeSetup(packages, note="shared Lake packages ready")
    if not fetch:
        return LakeSetup(packages, reason="the shared Lake packages of this pin are not fetched yet (no fetch in this run)")
    gb = free(state)
    if gb is not None and gb < MIN_FREE_GB_BUILD:
        return LakeSetup(packages, reason=f"only {gb:.0f} GB free on the state directory's disk; fetching the shared "
                                          f"Lake packages needs {MIN_FREE_GB_BUILD} GB")
    note = ensure_lake_cache(checkout, packages, run)
    if note not in READY_NOTES:
        return LakeSetup(packages, reason=note.split("; the build", 1)[0], note=note)
    return LakeSetup(packages, note=note)
