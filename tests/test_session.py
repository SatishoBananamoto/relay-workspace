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
