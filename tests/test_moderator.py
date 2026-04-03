"""Tests for the live moderator input system."""

from __future__ import annotations

from pathlib import Path

import pytest

from relay_discussion.moderator import (
    ControlCommand,
    ModeratorDaemon,
    ModeratorInputQueue,
    ModeratorMessage,
    parse_input,
)
from relay_discussion.models import AgentConfig, RelayConfig
from relay_discussion.engine import RelayRunner


# ── parse_input ───────────────────────────────────────────────────────────────


def test_parse_stop():
    assert parse_input("stop") == ControlCommand(command="stop")


def test_parse_stop_case_insensitive():
    assert parse_input("STOP") == ControlCommand(command="stop")


def test_parse_pause():
    assert parse_input("pause") == ControlCommand(command="pause")


def test_parse_resume():
    assert parse_input("resume") == ControlCommand(command="resume")


def test_parse_nolimit():
    assert parse_input("nolimit") == ControlCommand(command="nolimit")


def test_parse_more_with_number():
    result = parse_input("more 20")
    assert result == ControlCommand(command="more", value=20)


def test_parse_more_without_number():
    result = parse_input("more")
    assert result == ControlCommand(command="more", value=10)


def test_parse_more_with_bad_number():
    result = parse_input("more abc")
    assert isinstance(result, ModeratorMessage)
    assert result.content == "more abc"


def test_parse_regular_message():
    result = parse_input("Focus on error handling")
    assert isinstance(result, ModeratorMessage)
    assert result.content == "Focus on error handling"


# ── New structured commands ──────────────────────────────────────────────────


def test_parse_deny_tool():
    result = parse_input("deny Claude Write")
    assert result.command == "deny_tool"
    assert result.params == {"agent": "Claude", "tool": "Write"}


def test_parse_allow_tool():
    result = parse_input("allow Codex Bash")
    assert result.command == "allow_tool"
    assert result.params == {"agent": "Codex", "tool": "Bash"}


def test_parse_skip():
    result = parse_input("skip Claude")
    assert result.command == "skip"
    assert result.params == {"agent": "Claude"}


def test_parse_force():
    result = parse_input("force Codex")
    assert result.command == "force_next"
    assert result.params == {"agent": "Codex"}


def test_parse_instruction():
    result = parse_input("instruction Claude Focus on security vulnerabilities")
    assert result.command == "set_instruction"
    assert result.params["agent"] == "Claude"
    assert result.params["instruction"] == "Focus on security vulnerabilities"


def test_parse_timeout_global():
    result = parse_input("timeout 120")
    assert result.command == "set_timeout"
    assert result.params == {"seconds": 120}


def test_parse_timeout_per_agent():
    result = parse_input("timeout Claude 300")
    assert result.command == "set_timeout"
    assert result.params == {"agent": "Claude", "seconds": 300}


def test_parse_retry():
    result = parse_input("retry 5 20.0")
    assert result.command == "set_retry"
    assert result.params["attempts"] == 5
    assert result.params["backoff"] == 20.0


def test_parse_budget():
    result = parse_input("budget $5 max for this session")
    assert result.command == "set_budget"
    assert result.params["note"] == "$5 max for this session"


def test_parse_harness_on():
    result = parse_input("harness on")
    assert result.command == "harness_toggle"
    assert result.params["enabled"] is True


def test_parse_harness_off():
    result = parse_input("harness off")
    assert result.command == "harness_toggle"
    assert result.params["enabled"] is False


def test_parse_approve():
    result = parse_input("approve")
    assert result.command == "harness_approve"


def test_parse_reject():
    result = parse_input("reject")
    assert result.command == "harness_reject"


def test_parse_satisfy():
    result = parse_input("satisfy obl-123")
    assert result.command == "obligation_satisfy"
    assert result.params["obligation_id"] == "obl-123"


def test_parse_breach():
    result = parse_input("breach obl-456")
    assert result.command == "obligation_breach"
    assert result.params["obligation_id"] == "obl-456"


def test_parse_harness_state():
    result = parse_input("harness state")
    assert result.command == "harness_state"


def test_parse_permission_mode():
    result = parse_input("permission-mode Claude auto")
    assert result.command == "set_permission_mode"
    assert result.params == {"agent": "Claude", "mode": "auto"}


# ── parse_structured_input ───────────────────────────────────────────────────

def test_structured_command():
    from relay_discussion.moderator import parse_structured_input
    result = parse_structured_input({"command": "deny_tool", "params": {"agent": "Claude", "tool": "Write"}})
    assert result.command == "deny_tool"
    assert result.params["agent"] == "Claude"


def test_structured_message():
    from relay_discussion.moderator import parse_structured_input
    result = parse_structured_input({"message": "Focus harder"})
    assert isinstance(result, ModeratorMessage)
    assert result.content == "Focus harder"


def test_structured_empty():
    from relay_discussion.moderator import parse_structured_input
    result = parse_structured_input({})
    assert result.command == "noop"


def test_parse_empty_is_noop():
    result = parse_input("")
    assert isinstance(result, ControlCommand)
    assert result.command == "noop"


def test_parse_whitespace_is_noop():
    result = parse_input("   ")
    assert isinstance(result, ControlCommand)
    assert result.command == "noop"


# ── ModeratorInputQueue ──────────────────────────────────────────────────────


def test_queue_put_and_get():
    q = ModeratorInputQueue()
    q.put(ModeratorMessage(content="hello"))
    entry = q.get_nowait()
    assert isinstance(entry, ModeratorMessage)
    assert entry.content == "hello"


def test_queue_get_returns_none_when_empty():
    q = ModeratorInputQueue()
    assert q.get_nowait() is None


def test_queue_drain_returns_all():
    q = ModeratorInputQueue()
    q.put(ModeratorMessage(content="a"))
    q.put(ControlCommand(command="stop"))
    q.put(ModeratorMessage(content="b"))

    entries = q.drain()
    assert len(entries) == 3
    assert isinstance(entries[0], ModeratorMessage)
    assert isinstance(entries[1], ControlCommand)
    assert q.empty


def test_queue_empty_property():
    q = ModeratorInputQueue()
    assert q.empty
    q.put(ModeratorMessage(content="x"))
    assert not q.empty


# ── Engine integration ────────────────────────────────────────────────────────


def _make_config(turns: int = 4) -> RelayConfig:
    return RelayConfig(
        topic="Test topic",
        turns=turns,
        left_agent=AgentConfig(name="Claude", provider="mock"),
        right_agent=AgentConfig(name="Codex", provider="mock"),
    )


def test_engine_stop_command_ends_run(tmp_path: Path):
    q = ModeratorInputQueue()
    q.put(ControlCommand(command="stop"))

    config = _make_config(turns=10)
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out, moderator_queue=q)
    result = runner.run()

    # Stop before any agent messages — only the topic message
    assert result.status == "completed"
    agent_messages = [m for m in result.messages if m.role == "agent"]
    assert len(agent_messages) == 0


def test_engine_pause_command_pauses_run(tmp_path: Path):
    q = ModeratorInputQueue()
    q.put(ControlCommand(command="pause"))

    config = _make_config(turns=10)
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out, moderator_queue=q)
    result = runner.run()

    assert result.status == "paused"
    assert "moderator" in result.pause_reason.lower()


def test_engine_message_injected_as_interjection(tmp_path: Path):
    q = ModeratorInputQueue()
    q.put(ModeratorMessage(content="Push harder on failure modes"))

    config = _make_config(turns=2)
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out, moderator_queue=q)
    result = runner.run()

    interjections = [
        m for m in result.messages
        if m.role == "moderator" and m.metadata.get("kind") == "interjection"
    ]
    assert len(interjections) >= 1
    assert interjections[0].content == "Push harder on failure modes"
    assert interjections[0].author == "Satisho"


def test_engine_more_command_extends_turns(tmp_path: Path):
    q = ModeratorInputQueue()
    q.put(ControlCommand(command="more", value=4))

    config = _make_config(turns=2)
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out, moderator_queue=q)
    result = runner.run()

    # Original 2 turns + 4 more = 6 turns
    agent_messages = [m for m in result.messages if m.role == "agent"]
    assert len(agent_messages) == 6


def test_engine_nolimit_command_extends_massively(tmp_path: Path):
    q = ModeratorInputQueue()
    q.put(ControlCommand(command="nolimit"))

    # Start with 2 turns, but nolimit sets it to 999999
    # The mock provider will just keep going, so use fault scripts to stop after a few turns
    config = RelayConfig(
        topic="Test",
        turns=2,
        left_agent=AgentConfig(
            name="Claude", provider="mock",
            fault_script=["ok", "ok", "ok", "timeout", "timeout", "timeout"],
        ),
        right_agent=AgentConfig(name="Codex", provider="mock"),
    )
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out, moderator_queue=q)
    result = runner.run()

    # Should have run past the original 2 turns since nolimit extended it
    agent_messages = [m for m in result.messages if m.role == "agent"]
    assert len(agent_messages) > 2


def test_engine_on_commit_callback_called(tmp_path: Path):
    committed: list = []

    config = _make_config(turns=2)
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out, on_commit=committed.append)
    result = runner.run()

    assert len(committed) == len(result.messages)
    assert committed[0].role == "moderator"  # topic message


def test_engine_without_queue_works_as_before(tmp_path: Path):
    config = _make_config(turns=2)
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out)
    result = runner.run()

    assert result.status == "completed"
    agent_messages = [m for m in result.messages if m.role == "agent"]
    assert len(agent_messages) == 2
