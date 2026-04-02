"""Tests for TUI formatting and logic (no actual terminal required)."""

from __future__ import annotations

from relay_discussion.models import Message
from relay_discussion.moderator import ControlCommand, ModeratorInputQueue, ModeratorMessage
from relay_discussion.tui import RelayTUI, format_message


def _make_message(
    seq: int,
    role: str,
    author: str,
    content: str,
    metadata: dict | None = None,
) -> Message:
    return Message(
        seq=seq,
        timestamp="2026-04-01T00:00:00+00:00",
        role=role,
        author=author,
        content=content,
        metadata=metadata or {},
    )


# ── format_message ────────────────────────────────────────────────────────────


def test_format_topic_message():
    msg = _make_message(1, "moderator", "Satisho", "Discuss AI safety", {"kind": "topic"})
    result = format_message(msg)
    assert "TOPIC" in result
    assert "Discuss AI safety" in result


def test_format_interjection():
    msg = _make_message(5, "moderator", "Satisho", "Push harder", {"kind": "interjection"})
    result = format_message(msg)
    assert "[Satisho]" in result
    assert "Push harder" in result


def test_format_agent_message():
    msg = _make_message(3, "agent", "Claude", "I think...", {"turn": 2, "provider": "mock", "model": "mirror"})
    result = format_message(msg)
    assert "Turn 2" in result
    assert "[Claude]" in result
    assert "I think..." in result


def test_format_pause_message():
    msg = _make_message(10, "system", "relay", "Paused: circuit breaker", {"kind": "pause"})
    result = format_message(msg)
    assert "PAUSED" in result
    assert "circuit breaker" in result


def test_format_attempt_failed():
    msg = _make_message(8, "system", "relay", "Claude timeout", {"kind": "attempt_failed", "speaker": "Claude"})
    result = format_message(msg)
    assert "Claude" in result
    assert "FAILED" in result


def test_format_system_other_returns_empty():
    msg = _make_message(7, "system", "relay", "trace", {"kind": "provider_request"})
    result = format_message(msg)
    assert result == ""


# ── RelayTUI state tracking ──────────────────────────────────────────────────


def test_tui_on_commit_updates_state():
    q = ModeratorInputQueue()
    tui = RelayTUI(moderator_queue=q, session_id="test-session", topic="Test")

    agent_msg = _make_message(3, "agent", "Claude", "Response", {"turn": 2, "provider": "mock", "model": "m"})
    tui.on_commit(agent_msg)

    assert tui._current_turn == 2
    assert tui._current_agent == "Claude"
    assert tui._status == "running"


def test_tui_on_commit_pause_updates_status():
    q = ModeratorInputQueue()
    tui = RelayTUI(moderator_queue=q)

    pause_msg = _make_message(10, "system", "relay", "Paused", {"kind": "pause"})
    tui.on_commit(pause_msg)

    assert tui._status == "paused"


def test_tui_on_commit_appends_to_output():
    q = ModeratorInputQueue()
    tui = RelayTUI(moderator_queue=q)

    topic_msg = _make_message(1, "moderator", "Satisho", "Discuss X", {"kind": "topic"})
    tui.on_commit(topic_msg)

    assert "Discuss X" in tui._output_text


def test_tui_on_stream_chunk_appends():
    q = ModeratorInputQueue()
    tui = RelayTUI(moderator_queue=q)

    tui.on_stream_chunk("Hello ")
    tui.on_stream_chunk("world")

    assert "Hello world" in tui._output_text


def test_tui_update_status():
    q = ModeratorInputQueue()
    tui = RelayTUI(moderator_queue=q)

    tui.update_status("paused")
    assert tui._status == "paused"

    tui.update_status("done")
    assert tui._status == "done"


def test_tui_session_id_truncated():
    q = ModeratorInputQueue()
    tui = RelayTUI(moderator_queue=q, session_id="abcdef12-3456-7890-abcd-ef1234567890")
    assert tui._session_id == "abcdef12"
