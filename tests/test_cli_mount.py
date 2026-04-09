"""Tests for CLI workspace mounting flags.

All tests use throwaway tmp_path fixtures. Never references real ~/kv-* dirs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay_discussion import session as session_module
from relay_discussion.cli import _cmd_new
from relay_discussion.session import SessionManager


# ---------------------------------------------------------------------------
# Fixtures — duplicate directories under tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_kv_secrets(tmp_path):
    """Throwaway directory simulating ~/kv-secrets."""
    src = tmp_path / "kv_secrets_dup"
    src.mkdir()
    (src / "main.py").write_text("print('kv-secrets')\n")
    (src / "AGENT.md").write_text("# kv-secrets\n")
    return src


@pytest.fixture
def fake_kv_v2(tmp_path):
    """Throwaway directory simulating ~/kv-v2."""
    src = tmp_path / "kv_v2_dup"
    src.mkdir()
    (src / "ARCHITECTURE.md").write_text("# kv-v2\n")
    (src / "main.rs").write_text("fn main() {}\n")
    return src


@pytest.fixture
def isolated_relay_dir(tmp_path, monkeypatch):
    """Isolate session storage to a tmp dir per test."""
    relay_dir = tmp_path / ".relay"
    monkeypatch.setattr(session_module, "DEFAULT_RELAY_DIR", relay_dir)
    return relay_dir


# ---------------------------------------------------------------------------
# CLI flag parsing and session creation
# ---------------------------------------------------------------------------


class TestWorkspaceFlag:
    def test_single_workspace_sandbox_creates_copy(
        self, fake_kv_secrets, isolated_relay_dir
    ):
        rc = _cmd_new([
            "--mode", "discuss",
            "--topic", "Read kv",
            "--workspace", str(fake_kv_secrets),
            "--workspace-mode", "sandbox",
            "--left-provider", "mock",
            "--right-provider", "mock",
            "--turns", "1",
        ])
        assert rc == 0

        # Find the created session
        mgr = SessionManager()
        sessions = mgr.list_sessions()
        assert len(sessions) >= 1
        meta = sessions[0]

        # Mount target exists under workspace
        ws = mgr.get_workspace_path(meta.id)
        target = ws / "kv_secrets_dup"
        assert target.exists()
        assert (target / "main.py").exists()
        # Source untouched
        assert (fake_kv_secrets / "main.py").read_text() == "print('kv-secrets')\n"
        # Mount recorded
        assert len(meta.mount_specs) == 1
        assert meta.mount_specs[0]["mount_mode"] == "sandbox"

    def test_workspace_direct_mode_no_copy(
        self, fake_kv_secrets, isolated_relay_dir
    ):
        rc = _cmd_new([
            "--mode", "discuss",
            "--topic", "Direct mount",
            "--workspace", str(fake_kv_secrets),
            "--workspace-mode", "direct",
            "--left-provider", "mock",
            "--right-provider", "mock",
            "--turns", "1",
        ])
        assert rc == 0

        mgr = SessionManager()
        meta = mgr.list_sessions()[0]
        # Target == source
        assert meta.mount_specs[0]["target"] == str(fake_kv_secrets)
        assert meta.mount_specs[0]["cleanup_kind"] == "none"

    def test_multiple_workspaces(
        self, fake_kv_secrets, fake_kv_v2, isolated_relay_dir
    ):
        rc = _cmd_new([
            "--mode", "discuss",
            "--topic", "Compare both",
            "--workspace", str(fake_kv_secrets),
            "--workspace", str(fake_kv_v2),
            "--workspace-mode", "sandbox",
            "--left-provider", "mock",
            "--right-provider", "mock",
            "--turns", "1",
        ])
        assert rc == 0

        mgr = SessionManager()
        meta = mgr.list_sessions()[0]
        assert len(meta.mount_specs) == 2
        ws = mgr.get_workspace_path(meta.id)
        assert (ws / "kv_secrets_dup" / "main.py").exists()
        assert (ws / "kv_v2_dup" / "main.rs").exists()

    def test_per_entry_mode_override(
        self, fake_kv_secrets, fake_kv_v2, isolated_relay_dir
    ):
        rc = _cmd_new([
            "--mode", "discuss",
            "--topic", "Mixed modes",
            "--workspace", f"{fake_kv_secrets}:sandbox",
            "--workspace", f"{fake_kv_v2}:direct",
            "--workspace-mode", "sandbox",
            "--left-provider", "mock",
            "--right-provider", "mock",
            "--turns", "1",
        ])
        assert rc == 0

        mgr = SessionManager()
        meta = mgr.list_sessions()[0]
        modes = {spec["source"]: spec["mount_mode"] for spec in meta.mount_specs}
        assert modes[str(fake_kv_secrets)] == "sandbox"
        assert modes[str(fake_kv_v2)] == "direct"

    def test_read_only_flag_persists(
        self, fake_kv_secrets, isolated_relay_dir
    ):
        rc = _cmd_new([
            "--mode", "discuss",
            "--topic", "Read only",
            "--workspace", str(fake_kv_secrets),
            "--read-only",
            "--left-provider", "mock",
            "--right-provider", "mock",
            "--turns", "1",
        ])
        assert rc == 0

        mgr = SessionManager()
        meta = mgr.list_sessions()[0]
        assert meta.read_only is True

    def test_nonexistent_workspace_path_errors(
        self, isolated_relay_dir, capsys
    ):
        rc = _cmd_new([
            "--mode", "discuss",
            "--topic", "Bad path",
            "--workspace", "/this/does/not/exist",
            "--left-provider", "mock",
            "--right-provider", "mock",
            "--turns", "1",
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err

    def test_no_workspace_no_mounts(self, isolated_relay_dir):
        rc = _cmd_new([
            "--mode", "discuss",
            "--topic", "Pure discuss",
            "--left-provider", "mock",
            "--right-provider", "mock",
            "--turns", "1",
        ])
        assert rc == 0
        mgr = SessionManager()
        meta = mgr.list_sessions()[0]
        assert meta.mount_specs == []
        assert meta.read_only is False
