"""Tests for session management and config loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay_discussion.session import SessionManager, SessionMeta
from relay_discussion.config import RelayDefaults, load_config


# ── SessionMeta ───────────────────────────────────────────────────────────────


def test_session_meta_round_trips():
    meta = SessionMeta(
        id="abc-123",
        topic="Test topic",
        left_agent_name="Claude",
        right_agent_name="Codex",
        moderator="Satisho",
        status="new",
        created="2026-04-01T00:00:00Z",
        updated="2026-04-01T00:00:00Z",
        mode="build",
    )
    data = meta.to_dict()
    restored = SessionMeta.from_dict(data)
    assert restored.id == "abc-123"
    assert restored.topic == "Test topic"
    assert restored.mode == "build"


def test_session_meta_backward_compat_build_mode():
    """Old sessions with build_mode=True should migrate to mode='build'."""
    old_data = {
        "id": "old-123",
        "topic": "Old session",
        "left_agent_name": "Claude",
        "right_agent_name": "Codex",
        "moderator": "Satisho",
        "status": "completed",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
        "build_mode": True,
    }
    restored = SessionMeta.from_dict(old_data)
    assert restored.mode == "build"


def test_session_meta_from_dict_ignores_unknown_fields():
    data = {
        "id": "x",
        "topic": "t",
        "left_agent_name": "a",
        "right_agent_name": "b",
        "moderator": "m",
        "status": "new",
        "created": "now",
        "updated": "now",
        "some_future_field": 42,
    }
    meta = SessionMeta.from_dict(data)
    assert meta.id == "x"


# ── SessionManager ────────────────────────────────────────────────────────────


@pytest.fixture
def mgr(tmp_path: Path) -> SessionManager:
    return SessionManager(relay_dir=tmp_path / "relay")


def test_create_session_returns_meta(mgr: SessionManager):
    meta = mgr.create_session(
        topic="Test topic",
        left_agent_name="Claude",
        right_agent_name="Codex",
    )
    assert meta.topic == "Test topic"
    assert meta.status == "new"
    assert meta.left_agent_name == "Claude"
    assert meta.right_agent_name == "Codex"
    assert meta.moderator == "Satisho"


def test_create_session_creates_directory_and_meta_json(mgr: SessionManager):
    meta = mgr.create_session(
        topic="Test",
        left_agent_name="A",
        right_agent_name="B",
    )
    meta_path = mgr.relay_dir / "sessions" / meta.id / "meta.json"
    assert meta_path.exists()
    data = json.loads(meta_path.read_text())
    assert data["topic"] == "Test"
    assert data["status"] == "new"


def test_create_session_build_mode_creates_workspace(mgr: SessionManager):
    meta = mgr.create_session(
        topic="Build test",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="build",
    )
    ws = mgr.get_workspace_path(meta.id)
    assert (ws / "shared").is_dir()
    assert (ws / "claude" / "inbox.md").exists()
    assert (ws / "claude" / "outbox.md").exists()
    assert (ws / "codex" / "inbox.md").exists()
    assert (ws / "codex" / "outbox.md").exists()
    assert (ws / "reviews").is_dir()


def test_get_session_returns_stored_meta(mgr: SessionManager):
    created = mgr.create_session(
        topic="Roundtrip",
        left_agent_name="A",
        right_agent_name="B",
    )
    loaded = mgr.get_session(created.id)
    assert loaded.id == created.id
    assert loaded.topic == "Roundtrip"


def test_get_session_raises_for_missing(mgr: SessionManager):
    with pytest.raises(ValueError, match="not found"):
        mgr.get_session("nonexistent-id")


def test_list_sessions_returns_all(mgr: SessionManager):
    mgr.create_session(topic="A", left_agent_name="L", right_agent_name="R")
    mgr.create_session(topic="B", left_agent_name="L", right_agent_name="R")
    sessions = mgr.list_sessions()
    assert len(sessions) == 2


def test_list_sessions_filters_by_status(mgr: SessionManager):
    m1 = mgr.create_session(topic="A", left_agent_name="L", right_agent_name="R")
    mgr.create_session(topic="B", left_agent_name="L", right_agent_name="R")
    mgr.update_status(m1.id, "running")

    running = mgr.list_sessions(status_filter="running")
    assert len(running) == 1
    assert running[0].id == m1.id


def test_update_status_persists(mgr: SessionManager):
    meta = mgr.create_session(topic="T", left_agent_name="L", right_agent_name="R")
    mgr.update_status(meta.id, "paused", turns_completed=5)

    loaded = mgr.get_session(meta.id)
    assert loaded.status == "paused"
    assert loaded.turns_completed == 5


def test_archive_session_moves_to_archive(mgr: SessionManager):
    meta = mgr.create_session(topic="T", left_agent_name="L", right_agent_name="R")
    session_dir = mgr.relay_dir / "sessions" / meta.id
    assert session_dir.exists()

    mgr.archive_session(meta.id)

    assert not session_dir.exists()
    archived = mgr.relay_dir / "archive" / meta.id / "meta.json"
    assert archived.exists()
    data = json.loads(archived.read_text())
    assert data["status"] == "archived"


def test_archive_raises_for_missing(mgr: SessionManager):
    with pytest.raises(ValueError, match="not found"):
        mgr.archive_session("nonexistent")


def test_get_transcript_path(mgr: SessionManager):
    meta = mgr.create_session(topic="T", left_agent_name="L", right_agent_name="R")
    path = mgr.get_transcript_path(meta.id)
    assert path.name == "transcript.jsonl"
    assert meta.id in str(path)


def test_list_sessions_sorted_by_updated(mgr: SessionManager):
    m1 = mgr.create_session(topic="First", left_agent_name="L", right_agent_name="R")
    m2 = mgr.create_session(topic="Second", left_agent_name="L", right_agent_name="R")
    # Update m1 so it becomes most recent
    mgr.update_status(m1.id, "running")

    sessions = mgr.list_sessions()
    assert sessions[0].id == m1.id


def test_create_session_stores_provider_and_model(mgr: SessionManager):
    meta = mgr.create_session(
        topic="T",
        left_agent_name="Claude",
        right_agent_name="Codex",
        left_provider="cli-claude",
        left_model="opus",
        right_provider="cli-codex",
        right_model="gpt-5.4",
    )
    loaded = mgr.get_session(meta.id)
    assert loaded.left_provider == "cli-claude"
    assert loaded.left_model == "opus"
    assert loaded.right_provider == "cli-codex"
    assert loaded.right_model == "gpt-5.4"


# ── Config loader ─────────────────────────────────────────────────────────────


def test_load_config_returns_defaults_when_no_file():
    cfg = load_config(Path("/nonexistent/config.toml"))
    assert isinstance(cfg, RelayDefaults)
    assert cfg.moderator == "Satisho"
    assert cfg.left_provider == "cli-claude"
    assert cfg.right_provider == "cli-codex"


def test_load_config_reads_toml(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[defaults]
moderator = "TestMod"
turns = 50

[agents.claude]
model = "sonnet"
effort = "high"

[agents.codex]
model = "o3"
""")
    cfg = load_config(config_file)
    assert cfg.moderator == "TestMod"
    assert cfg.turns == 50
    assert cfg.left_model == "sonnet"
    assert cfg.claude_effort == "high"
    assert cfg.right_model == "o3"


def test_load_config_partial_overrides(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[agents.claude]
model = "haiku"
""")
    cfg = load_config(config_file)
    # Overridden
    assert cfg.left_model == "haiku"
    # Defaults preserved
    assert cfg.moderator == "Satisho"
    assert cfg.right_model == "gpt-5.4"
    assert cfg.turns == 20


# ── Mount specs persistence ──────────────────────────────────────────────────


@pytest.fixture
def fake_kv_dir(tmp_path: Path):
    """Throwaway directory simulating ~/kv-secrets — never touches the real one."""
    src = tmp_path / "kv_secrets_dup"
    src.mkdir()
    (src / "main.py").write_text("print('fake')\n")
    (src / "secrets.env").write_text("KEY=fake\n")
    return src


def test_session_meta_mount_specs_default_empty():
    meta = SessionMeta(
        id="x",
        topic="t",
        left_agent_name="A",
        right_agent_name="B",
        moderator="m",
        status="new",
        created="2026-04-01T00:00:00Z",
        updated="2026-04-01T00:00:00Z",
    )
    assert meta.mount_specs == []
    assert meta.read_only is False


def test_session_meta_mount_specs_persist_round_trip():
    spec = {
        "source": "/tmp/src",
        "target": "/tmp/tgt",
        "mount_mode": "sandbox",
        "cleanup_kind": "copy",
        "read_only": True,
    }
    meta = SessionMeta(
        id="x", topic="t",
        left_agent_name="A", right_agent_name="B",
        moderator="m", status="new",
        created="2026-04-01T00:00:00Z",
        updated="2026-04-01T00:00:00Z",
        mount_specs=[spec],
        read_only=True,
    )
    data = meta.to_dict()
    restored = SessionMeta.from_dict(data)
    assert restored.mount_specs == [spec]
    assert restored.read_only is True


def test_session_meta_backward_compat_no_mounts():
    """Old session JSON without mount_specs should default to []."""
    old = {
        "id": "old", "topic": "t",
        "left_agent_name": "A", "right_agent_name": "B",
        "moderator": "m", "status": "new",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
    }
    restored = SessionMeta.from_dict(old)
    assert restored.mount_specs == []
    assert restored.read_only is False


def test_create_session_with_sandbox_mount(mgr: SessionManager, fake_kv_dir):
    from relay_discussion.mount import MountSpec
    meta = mgr.create_session(
        topic="Read kv",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="discuss",
        mount_specs=[MountSpec(source=fake_kv_dir, mount_mode="sandbox")],
    )
    # Mount target exists under workspace
    ws = mgr.get_workspace_path(meta.id)
    target = ws / "kv_secrets_dup"
    assert target.exists()
    assert (target / "main.py").read_text() == "print('fake')\n"
    # Source untouched
    assert (fake_kv_dir / "main.py").exists()
    # Meta records the mount
    assert len(meta.mount_specs) == 1
    assert meta.mount_specs[0]["mount_mode"] == "sandbox"
    assert meta.mount_specs[0]["cleanup_kind"] == "copy"


def test_create_session_with_direct_mount(mgr: SessionManager, fake_kv_dir):
    from relay_discussion.mount import MountSpec
    meta = mgr.create_session(
        topic="Read kv directly",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="discuss",
        mount_specs=[MountSpec(source=fake_kv_dir, mount_mode="direct")],
    )
    # No copy made
    ws = mgr.get_workspace_path(meta.id)
    assert not (ws / "kv_secrets_dup").exists() if ws.exists() else True
    # Meta records target == source
    assert meta.mount_specs[0]["target"] == str(fake_kv_dir)
    assert meta.mount_specs[0]["cleanup_kind"] == "none"


def test_delete_session_removes_sandbox_copy(mgr: SessionManager, fake_kv_dir):
    from relay_discussion.mount import MountSpec
    meta = mgr.create_session(
        topic="Test",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="discuss",
        mount_specs=[MountSpec(source=fake_kv_dir, mount_mode="sandbox")],
    )
    ws = mgr.get_workspace_path(meta.id)
    target = ws / "kv_secrets_dup"
    assert target.exists()

    mgr.delete_session(meta.id)

    # Session gone
    assert not (mgr.relay_dir / "sessions" / meta.id).exists()
    # Source untouched
    assert (fake_kv_dir / "main.py").exists()


def test_delete_session_does_not_touch_direct_mount(mgr: SessionManager, fake_kv_dir):
    from relay_discussion.mount import MountSpec
    meta = mgr.create_session(
        topic="Test",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="discuss",
        mount_specs=[MountSpec(source=fake_kv_dir, mount_mode="direct")],
    )
    mgr.delete_session(meta.id)
    # Source must still exist
    assert fake_kv_dir.exists()
    assert (fake_kv_dir / "main.py").read_text() == "print('fake')\n"


def test_build_mode_with_mount_coexist(mgr: SessionManager, fake_kv_dir):
    """Build mode creates inbox/outbox AND mount lives alongside."""
    from relay_discussion.mount import MountSpec
    meta = mgr.create_session(
        topic="Build with mount",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="build",
        mount_specs=[MountSpec(source=fake_kv_dir, mount_mode="sandbox")],
    )
    ws = mgr.get_workspace_path(meta.id)
    # Build scaffolding
    assert (ws / "shared").is_dir()
    assert (ws / "claude" / "inbox.md").exists()
    assert (ws / "reviews").is_dir()
    # Mount sibling
    assert (ws / "kv_secrets_dup" / "main.py").exists()


def test_get_mount_points_returns_objects(mgr: SessionManager, fake_kv_dir):
    from relay_discussion.mount import MountSpec, MountPoint
    meta = mgr.create_session(
        topic="t",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="discuss",
        mount_specs=[MountSpec(source=fake_kv_dir, mount_mode="sandbox")],
    )
    points = mgr.get_mount_points(meta.id)
    assert len(points) == 1
    assert isinstance(points[0], MountPoint)
    assert points[0].mount_mode == "sandbox"


def test_add_mount_appends_to_existing_session(mgr: SessionManager, fake_kv_dir):
    meta = mgr.create_session(
        topic="t",
        left_agent_name="Claude",
        right_agent_name="Codex",
        mode="discuss",
    )
    assert meta.mount_specs == []
    new_mount = {
        "source": str(fake_kv_dir),
        "target": str(fake_kv_dir),
        "mount_mode": "direct",
        "cleanup_kind": "none",
        "read_only": False,
    }
    mgr.add_mount(meta.id, new_mount)
    reloaded = mgr.get_session(meta.id)
    assert len(reloaded.mount_specs) == 1
    assert reloaded.mount_specs[0]["source"] == str(fake_kv_dir)
