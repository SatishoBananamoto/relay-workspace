"""Workspace mounting primitives.

Two strategies for exposing a source directory to relay agents:

- **sandbox**: Create an isolated copy. If source is a git repo, use
  `git worktree add` (instant, shares git database). Otherwise use
  `shutil.copytree` with sensible excludes.
- **direct**: Use the source directly. No copy. Risky — agents can
  modify the original.

Cleanup is recorded per-mount so `relay delete <session>` can reverse
the operation correctly (rmtree for copies, `git worktree remove` for
worktrees, no-op for direct).

Test fixtures must use throwaway directories under tmp_path. Never
reference real user repos.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


# Directories to skip when copying non-git sources
_COPYTREE_EXCLUDES = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    "target",  # Rust build cache
    "dist",
    "build",
    ".mypy_cache",
)

# Mount cleanup kinds
CLEANUP_COPY = "copy"
CLEANUP_WORKTREE = "worktree"
CLEANUP_NONE = "none"

# Mount modes
MODE_SANDBOX = "sandbox"
MODE_DIRECT = "direct"


@dataclass(frozen=True, slots=True)
class MountSpec:
    """User-supplied mount request (before resolution)."""
    source: Path
    mount_mode: str = MODE_SANDBOX
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class MountPoint:
    """A resolved mount: source + actual target on disk + cleanup info."""
    source: Path
    target: Path
    mount_mode: str
    cleanup_kind: str
    read_only: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "target": str(self.target),
            "mount_mode": self.mount_mode,
            "cleanup_kind": self.cleanup_kind,
            "read_only": self.read_only,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MountPoint":
        return cls(
            source=Path(data["source"]),
            target=Path(data["target"]),
            mount_mode=data["mount_mode"],
            cleanup_kind=data["cleanup_kind"],
            read_only=bool(data.get("read_only", False)),
        )


def resolve_mount_spec(spec_str: str, default_mode: str = MODE_SANDBOX) -> MountSpec:
    """Parse a CLI mount spec into a MountSpec.

    Syntax:
        path                    -> sandbox, read_only=False (uses default_mode)
        path:direct             -> direct, read_only=False
        path:sandbox            -> sandbox, read_only=False
        path:sandbox:ro         -> sandbox, read_only=True
        path:direct:ro          -> direct, read_only=True
        path::ro                -> default_mode, read_only=True

    Tilde is expanded.
    """
    parts = spec_str.split(":")
    raw_path = parts[0].strip()
    if not raw_path:
        raise ValueError(f"Empty path in mount spec: {spec_str!r}")

    source = Path(raw_path).expanduser().resolve()

    mount_mode = default_mode
    read_only = False

    for token in parts[1:]:
        token = token.strip().lower()
        if token == "":
            continue
        if token in (MODE_SANDBOX, MODE_DIRECT):
            mount_mode = token
        elif token == "ro":
            read_only = True
        else:
            raise ValueError(f"Unknown mount spec token: {token!r} in {spec_str!r}")

    return MountSpec(source=source, mount_mode=mount_mode, read_only=read_only)


def _is_git_repo(path: Path) -> bool:
    """Return True if path is inside a git repository."""
    git_dir = path / ".git"
    if git_dir.exists():
        return True
    # Check parents in case path is a subdirectory of a repo
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _unique_target(target_parent: Path, basename: str) -> Path:
    """Return a non-colliding target path under target_parent."""
    target_parent.mkdir(parents=True, exist_ok=True)
    candidate = target_parent / basename
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = target_parent / f"{basename}_{n}"
        if not candidate.exists():
            return candidate
        n += 1


def mount_sandbox(
    source: Path,
    target_parent: Path,
    *,
    read_only: bool = False,
) -> MountPoint:
    """Create an isolated sandbox copy of source under target_parent.

    Tries git worktree first (fast, branch-based). Falls back to copytree
    if source is not a git repo or git fails.
    """
    if not source.exists():
        raise FileNotFoundError(f"Mount source does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Mount source is not a directory: {source}")

    target = _unique_target(target_parent, source.name)

    if _is_git_repo(source):
        try:
            # git worktree add --detach <target>
            # Detached HEAD avoids branch name conflicts; agents work on a snapshot
            result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(target)],
                cwd=source,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return MountPoint(
                    source=source,
                    target=target,
                    mount_mode=MODE_SANDBOX,
                    cleanup_kind=CLEANUP_WORKTREE,
                    read_only=read_only,
                )
            # Fall through to copytree on git failure
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # git not available or hung — use copytree

    # Copytree fallback
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(*_COPYTREE_EXCLUDES),
        symlinks=False,  # materialize symlinks for safety
        dirs_exist_ok=False,
    )
    return MountPoint(
        source=source,
        target=target,
        mount_mode=MODE_SANDBOX,
        cleanup_kind=CLEANUP_COPY,
        read_only=read_only,
    )


def mount_direct(source: Path, *, read_only: bool = False) -> MountPoint:
    """Use source directly with no copy. Agents can modify the original."""
    if not source.exists():
        raise FileNotFoundError(f"Mount source does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Mount source is not a directory: {source}")
    return MountPoint(
        source=source,
        target=source,
        mount_mode=MODE_DIRECT,
        cleanup_kind=CLEANUP_NONE,
        read_only=read_only,
    )


def mount(
    spec: MountSpec,
    target_parent: Path,
) -> MountPoint:
    """Dispatch to the right mount strategy based on spec.mount_mode."""
    if spec.mount_mode == MODE_SANDBOX:
        return mount_sandbox(spec.source, target_parent, read_only=spec.read_only)
    if spec.mount_mode == MODE_DIRECT:
        return mount_direct(spec.source, read_only=spec.read_only)
    raise ValueError(f"Unknown mount mode: {spec.mount_mode!r}")


def cleanup_mount(point: MountPoint) -> None:
    """Reverse a mount. Removes copies and worktrees, leaves direct mounts alone."""
    if point.cleanup_kind == CLEANUP_NONE:
        return  # direct mount — never touch source
    if not point.target.exists():
        return  # already gone

    if point.cleanup_kind == CLEANUP_WORKTREE:
        try:
            subprocess.run(
                ["git", "worktree", "remove", str(point.target), "--force"],
                cwd=point.source,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # If the directory still exists (worktree command failed), force-remove
            if point.target.exists():
                shutil.rmtree(point.target, ignore_errors=True)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.rmtree(point.target, ignore_errors=True)
        return

    if point.cleanup_kind == CLEANUP_COPY:
        shutil.rmtree(point.target, ignore_errors=True)
        return

    raise ValueError(f"Unknown cleanup_kind: {point.cleanup_kind!r}")
