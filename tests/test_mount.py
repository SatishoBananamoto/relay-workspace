"""Tests for workspace mounting primitives.

All tests use tmp_path fixtures. None reference real user directories.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from relay_discussion.mount import (
    CLEANUP_COPY,
    CLEANUP_NONE,
    CLEANUP_WORKTREE,
    MODE_DIRECT,
    MODE_SANDBOX,
    MountPoint,
    MountSpec,
    cleanup_mount,
    mount,
    mount_direct,
    mount_sandbox,
    resolve_mount_spec,
)


# ---------------------------------------------------------------------------
# Fixtures — all under tmp_path, never touching real directories
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_codebase(tmp_path):
    """A non-git directory with sample files."""
    src = tmp_path / "fake_codebase"
    src.mkdir()
    (src / "main.py").write_text("print('hello')\n")
    (src / "config.json").write_text('{"key": "value"}\n')
    sub = src / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content\n")
    # Create a .git dir that should be excluded
    git_dir = src / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    return src


@pytest.fixture
def git_codebase(tmp_path):
    """A real git repo with one commit."""
    if not shutil.which("git"):
        pytest.skip("git not installed")
    src = tmp_path / "git_codebase"
    src.mkdir()
    (src / "README.md").write_text("# Test repo\n")
    (src / "main.py").write_text("print('git')\n")
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.test", "-c", "user.name=test",
         "add", "."],
        cwd=src, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=test@test.test", "-c", "user.name=test",
         "commit", "-q", "-m", "initial"],
        cwd=src, check=True,
    )
    return src


# ---------------------------------------------------------------------------
# resolve_mount_spec
# ---------------------------------------------------------------------------


class TestResolveMountSpec:
    def test_plain_path(self, tmp_path):
        spec = resolve_mount_spec(str(tmp_path))
        assert spec.source == tmp_path.resolve()
        assert spec.mount_mode == MODE_SANDBOX
        assert spec.read_only is False

    def test_direct_mode(self, tmp_path):
        spec = resolve_mount_spec(f"{tmp_path}:direct")
        assert spec.mount_mode == MODE_DIRECT

    def test_sandbox_mode_explicit(self, tmp_path):
        spec = resolve_mount_spec(f"{tmp_path}:sandbox")
        assert spec.mount_mode == MODE_SANDBOX

    def test_read_only_flag(self, tmp_path):
        spec = resolve_mount_spec(f"{tmp_path}:sandbox:ro")
        assert spec.read_only is True

    def test_direct_with_ro(self, tmp_path):
        spec = resolve_mount_spec(f"{tmp_path}:direct:ro")
        assert spec.mount_mode == MODE_DIRECT
        assert spec.read_only is True

    def test_default_mode_override(self, tmp_path):
        spec = resolve_mount_spec(str(tmp_path), default_mode=MODE_DIRECT)
        assert spec.mount_mode == MODE_DIRECT

    def test_explicit_mode_overrides_default(self, tmp_path):
        spec = resolve_mount_spec(f"{tmp_path}:sandbox", default_mode=MODE_DIRECT)
        assert spec.mount_mode == MODE_SANDBOX

    def test_tilde_expansion(self):
        spec = resolve_mount_spec("~")
        assert str(spec.source).startswith("/")
        assert "~" not in str(spec.source)

    def test_empty_path_raises(self):
        with pytest.raises(ValueError, match="Empty path"):
            resolve_mount_spec(":sandbox")

    def test_unknown_token_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown mount spec token"):
            resolve_mount_spec(f"{tmp_path}:nonsense")


# ---------------------------------------------------------------------------
# mount_sandbox (non-git copytree path)
# ---------------------------------------------------------------------------


class TestMountSandboxCopy:
    def test_copies_files(self, fake_codebase, tmp_path):
        target_parent = tmp_path / "mounts"
        point = mount_sandbox(fake_codebase, target_parent)

        assert point.target.exists()
        assert (point.target / "main.py").read_text() == "print('hello')\n"
        assert (point.target / "subdir" / "nested.txt").read_text() == "nested content\n"
        assert point.cleanup_kind == CLEANUP_COPY
        assert point.mount_mode == MODE_SANDBOX

    def test_excludes_git_dir(self, fake_codebase, tmp_path):
        point = mount_sandbox(fake_codebase, tmp_path / "mounts")
        assert not (point.target / ".git").exists()

    def test_source_untouched(self, fake_codebase, tmp_path):
        point = mount_sandbox(fake_codebase, tmp_path / "mounts")
        # Modify the target
        (point.target / "main.py").write_text("modified\n")
        # Source must remain unchanged
        assert (fake_codebase / "main.py").read_text() == "print('hello')\n"

    def test_excludes_node_modules(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "package.json").write_text("{}")
        nm = src / "node_modules"
        nm.mkdir()
        (nm / "garbage.js").write_text("x")
        point = mount_sandbox(src, tmp_path / "mounts")
        assert (point.target / "package.json").exists()
        assert not (point.target / "node_modules").exists()

    def test_name_collision_gets_suffix(self, fake_codebase, tmp_path):
        target_parent = tmp_path / "mounts"
        first = mount_sandbox(fake_codebase, target_parent)
        second = mount_sandbox(fake_codebase, target_parent)
        assert first.target != second.target
        assert second.target.name == "fake_codebase_2"

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            mount_sandbox(tmp_path / "nonexistent", tmp_path / "mounts")

    def test_file_source_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("not a dir")
        with pytest.raises(NotADirectoryError):
            mount_sandbox(f, tmp_path / "mounts")


# ---------------------------------------------------------------------------
# mount_sandbox (git worktree path)
# ---------------------------------------------------------------------------


class TestMountSandboxGit:
    def test_creates_worktree(self, git_codebase, tmp_path):
        point = mount_sandbox(git_codebase, tmp_path / "mounts")
        assert point.target.exists()
        assert (point.target / "README.md").exists()
        assert point.cleanup_kind == CLEANUP_WORKTREE

    def test_worktree_source_untouched(self, git_codebase, tmp_path):
        point = mount_sandbox(git_codebase, tmp_path / "mounts")
        # Modify the worktree
        (point.target / "README.md").write_text("modified in worktree\n")
        # Source untouched
        assert (git_codebase / "README.md").read_text() == "# Test repo\n"

    def test_worktree_registered(self, git_codebase, tmp_path):
        point = mount_sandbox(git_codebase, tmp_path / "mounts")
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=git_codebase,
            capture_output=True,
            text=True,
        )
        assert str(point.target) in result.stdout


# ---------------------------------------------------------------------------
# mount_direct
# ---------------------------------------------------------------------------


class TestMountDirect:
    def test_target_equals_source(self, fake_codebase):
        point = mount_direct(fake_codebase)
        assert point.target == fake_codebase
        assert point.source == fake_codebase
        assert point.cleanup_kind == CLEANUP_NONE
        assert point.mount_mode == MODE_DIRECT

    def test_no_copy_made(self, fake_codebase, tmp_path):
        # mount_direct doesn't take a target_parent
        before = list(tmp_path.iterdir())
        mount_direct(fake_codebase)
        after = list(tmp_path.iterdir())
        assert before == after

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            mount_direct(tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# mount() dispatch
# ---------------------------------------------------------------------------


class TestMountDispatch:
    def test_sandbox_dispatch(self, fake_codebase, tmp_path):
        spec = MountSpec(source=fake_codebase, mount_mode=MODE_SANDBOX)
        point = mount(spec, tmp_path / "mounts")
        assert point.mount_mode == MODE_SANDBOX

    def test_direct_dispatch(self, fake_codebase, tmp_path):
        spec = MountSpec(source=fake_codebase, mount_mode=MODE_DIRECT)
        point = mount(spec, tmp_path / "mounts")
        assert point.mount_mode == MODE_DIRECT
        assert point.target == fake_codebase

    def test_unknown_mode_raises(self, fake_codebase, tmp_path):
        spec = MountSpec(source=fake_codebase, mount_mode="weird")
        with pytest.raises(ValueError, match="Unknown mount mode"):
            mount(spec, tmp_path / "mounts")


# ---------------------------------------------------------------------------
# cleanup_mount
# ---------------------------------------------------------------------------


class TestCleanupMount:
    def test_cleanup_copy_removes_target(self, fake_codebase, tmp_path):
        point = mount_sandbox(fake_codebase, tmp_path / "mounts")
        assert point.target.exists()
        cleanup_mount(point)
        assert not point.target.exists()
        # Source untouched
        assert fake_codebase.exists()
        assert (fake_codebase / "main.py").exists()

    def test_cleanup_worktree_removes_target(self, git_codebase, tmp_path):
        point = mount_sandbox(git_codebase, tmp_path / "mounts")
        assert point.target.exists()
        cleanup_mount(point)
        assert not point.target.exists()
        # Source intact
        assert (git_codebase / "README.md").exists()
        # Worktree no longer registered
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=git_codebase,
            capture_output=True,
            text=True,
        )
        assert str(point.target) not in result.stdout

    def test_cleanup_direct_is_noop(self, fake_codebase):
        point = mount_direct(fake_codebase)
        cleanup_mount(point)
        # Source must still be intact
        assert fake_codebase.exists()
        assert (fake_codebase / "main.py").read_text() == "print('hello')\n"

    def test_cleanup_missing_target_no_error(self, tmp_path):
        point = MountPoint(
            source=tmp_path / "src",
            target=tmp_path / "missing",
            mount_mode=MODE_SANDBOX,
            cleanup_kind=CLEANUP_COPY,
            read_only=False,
        )
        # Should not raise
        cleanup_mount(point)


# ---------------------------------------------------------------------------
# MountPoint serialization
# ---------------------------------------------------------------------------


class TestMountPointSerialization:
    def test_round_trip(self, tmp_path):
        original = MountPoint(
            source=tmp_path / "src",
            target=tmp_path / "tgt",
            mount_mode=MODE_SANDBOX,
            cleanup_kind=CLEANUP_COPY,
            read_only=True,
        )
        d = original.to_dict()
        restored = MountPoint.from_dict(d)
        assert restored == original

    def test_dict_keys(self, tmp_path):
        point = MountPoint(
            source=tmp_path / "src",
            target=tmp_path / "tgt",
            mount_mode=MODE_DIRECT,
            cleanup_kind=CLEANUP_NONE,
            read_only=False,
        )
        d = point.to_dict()
        assert set(d.keys()) == {"source", "target", "mount_mode", "cleanup_kind", "read_only"}
