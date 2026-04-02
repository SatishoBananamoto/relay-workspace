"""Tests for CLI provider adapters (CliClaudeProvider, CliCodexProvider).

All tests monkeypatch subprocess.run to avoid calling real CLI tools.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from relay_discussion.cli_providers import (
    CliClaudeProvider,
    CliCodexProvider,
    _format_continuation,
    _format_prompt,
)
from relay_discussion.models import AgentConfig, Message
from relay_discussion.providers import ProviderError


def _make_message(seq: int, role: str, author: str, content: str) -> Message:
    return Message(
        seq=seq,
        timestamp="2026-04-01T00:00:00+00:00",
        role=role,
        author=author,
        content=content,
    )


# ── Prompt formatting ─────────────────────────────────────────────────────────


def test_format_prompt_includes_instruction_and_messages():
    agent = AgentConfig(name="Claude", instruction="Be concise.")
    transcript = [
        _make_message(1, "moderator", "Satisho", "Discuss X."),
        _make_message(2, "agent", "Claude", "I think X is..."),
    ]
    prompt = _format_prompt(agent, transcript, turn=2)
    assert "Be concise." in prompt
    assert "[Satisho]: Discuss X." in prompt
    assert "[Claude]: I think X is..." in prompt


def test_format_prompt_excludes_system_messages():
    agent = AgentConfig(name="Claude")
    transcript = [
        _make_message(1, "moderator", "Satisho", "Topic"),
        _make_message(2, "system", "relay", "attempt failed"),
        _make_message(3, "agent", "Codex", "Response"),
    ]
    prompt = _format_prompt(agent, transcript, turn=2)
    assert "attempt failed" not in prompt
    assert "[Codex]: Response" in prompt


def test_format_continuation_sends_only_new_messages():
    transcript = [
        _make_message(1, "moderator", "Satisho", "Topic"),
        _make_message(2, "agent", "Claude", "Turn 1 response"),
        _make_message(3, "agent", "Codex", "Turn 2 response"),
        _make_message(4, "moderator", "Satisho", "New input"),
    ]
    cont = _format_continuation(transcript)
    assert "Turn 1 response" not in cont
    assert "Turn 2 response" not in cont
    assert "[Satisho]: New input" in cont


def test_format_continuation_with_no_agent_messages_sends_all():
    transcript = [
        _make_message(1, "moderator", "Satisho", "Topic"),
    ]
    cont = _format_continuation(transcript)
    assert "[Satisho]: Topic" in cont


# ── CliClaudeProvider ─────────────────────────────────────────────────────────


def _claude_json_response(result: str, session_id: str = "test-uuid-123") -> str:
    return json.dumps({
        "type": "result",
        "result": result,
        "session_id": session_id,
        "is_error": False,
    })


def _mock_claude_run(result_text: str, session_id: str = "test-uuid-123"):
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=_claude_json_response(result_text, session_id),
            stderr="",
        )
    return _run


class TestCliClaudeProvider:

    def test_generate_returns_response_text(self):
        provider = CliClaudeProvider(model="haiku", timeout=10)
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        with patch("relay_discussion.cli_providers.subprocess.run", _mock_claude_run("Hello back")):
            response = provider.generate(agent, transcript, turn=1)

        assert response == "Hello back"

    def test_stores_session_id_after_first_call(self):
        provider = CliClaudeProvider()
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        assert provider.session_id is None

        with patch("relay_discussion.cli_providers.subprocess.run", _mock_claude_run("Hi", "abc-123")):
            provider.generate(agent, transcript, turn=1)

        assert provider.session_id == "abc-123"

    def test_resume_uses_stored_session_id(self):
        provider = CliClaudeProvider()
        provider.session_id = "existing-session"
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [
            _make_message(1, "moderator", "Satisho", "Hello"),
            _make_message(2, "agent", "Claude", "Hi"),
            _make_message(3, "agent", "Codex", "Hey"),
        ]

        captured_cmd = []

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=_claude_json_response("response", "existing-session"),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_run):
            provider.generate(agent, transcript, turn=2)

        assert "--resume" in captured_cmd
        assert "existing-session" in captured_cmd

    def test_timeout_raises_provider_error(self):
        provider = CliClaudeProvider(timeout=1)
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        with patch("relay_discussion.cli_providers.subprocess.run", mock_timeout):
            with pytest.raises(ProviderError, match="timed out"):
                provider.generate(agent, transcript, turn=1)

    def test_nonzero_exit_raises_provider_error(self):
        provider = CliClaudeProvider()
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_fail(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="something broke",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_fail):
            with pytest.raises(ProviderError, match="exited with code 1"):
                provider.generate(agent, transcript, turn=1)

    def test_invalid_json_raises_provider_error(self):
        provider = CliClaudeProvider()
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_bad_json(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="not json{{{", stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_bad_json):
            with pytest.raises(ProviderError, match="invalid JSON"):
                provider.generate(agent, transcript, turn=1)

    def test_is_error_flag_raises_provider_error(self):
        provider = CliClaudeProvider()
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_error(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"is_error": True, "result": "rate limited"}),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_error):
            with pytest.raises(ProviderError, match="rate limited"):
                provider.generate(agent, transcript, turn=1)

    def test_empty_result_returns_empty_string(self):
        provider = CliClaudeProvider()
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_empty(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"result": "", "session_id": "s1", "is_error": False}),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_empty):
            response = provider.generate(agent, transcript, turn=1)
            assert response == ""

    def test_workspace_flags_added_in_build_mode(self):
        provider = CliClaudeProvider(workspace_path="/tmp/ws")
        agent = AgentConfig(name="Claude", provider="cli-claude")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        captured_cmd = []

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=_claude_json_response("ok"),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_run):
            provider.generate(agent, transcript, turn=1)

        assert "--add-dir" in captured_cmd
        assert "/tmp/ws" in captured_cmd
        assert "--permission-mode" in captured_cmd
        assert "--allowedTools" in captured_cmd


# ── CliCodexProvider ──────────────────────────────────────────────────────────


def _codex_jsonl_output(text: str, thread_id: str = "codex-thread-456") -> str:
    events = [
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    return "\n".join(events)


class TestCliCodexProvider:

    def test_generate_returns_response_from_output_file(self, tmp_path):
        provider = CliCodexProvider(timeout=10)
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_run(cmd, **kwargs):
            # Find the -o flag and write to that file
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("Hello from Codex")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=_codex_jsonl_output("Hello from Codex"),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_run):
            response = provider.generate(agent, transcript, turn=1)

        assert response == "Hello from Codex"

    def test_stores_thread_id_from_jsonl(self, tmp_path):
        provider = CliCodexProvider()
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        assert provider.session_id is None

        def mock_run(cmd, **kwargs):
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("Hi")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=_codex_jsonl_output("Hi", "thread-xyz"),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_run):
            provider.generate(agent, transcript, turn=1)

        assert provider.session_id == "thread-xyz"

    def test_resume_uses_stored_thread_id(self):
        provider = CliCodexProvider()
        provider.session_id = "existing-thread"
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [
            _make_message(1, "moderator", "Satisho", "Hello"),
            _make_message(2, "agent", "Claude", "Hi"),
            _make_message(3, "agent", "Codex", "Hey"),
            _make_message(4, "agent", "Claude", "New msg"),
        ]

        captured_cmd = []

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("response")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=_codex_jsonl_output("response", "existing-thread"),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_run):
            provider.generate(agent, transcript, turn=2)

        assert "resume" in captured_cmd
        assert "existing-thread" in captured_cmd

    def test_timeout_raises_provider_error(self):
        provider = CliCodexProvider(timeout=1)
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        with patch("relay_discussion.cli_providers.subprocess.run", mock_timeout):
            with pytest.raises(ProviderError, match="timed out"):
                provider.generate(agent, transcript, turn=1)

    def test_nonzero_exit_with_no_output_raises_provider_error(self):
        provider = CliCodexProvider()
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_fail(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="codex crashed",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_fail):
            with pytest.raises(ProviderError, match="exited with code 1"):
                provider.generate(agent, transcript, turn=1)

    def test_empty_output_file_returns_empty_string(self):
        provider = CliCodexProvider()
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_empty(cmd, **kwargs):
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=_codex_jsonl_output(""),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_empty):
            response = provider.generate(agent, transcript, turn=1)
            assert response == ""

    def test_workspace_flags_added_in_build_mode(self):
        provider = CliCodexProvider(workspace_path="/tmp/ws")
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        captured_cmd = []

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text("ok")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=_codex_jsonl_output("ok"),
                stderr="",
            )

        with patch("relay_discussion.cli_providers.subprocess.run", mock_run):
            provider.generate(agent, transcript, turn=1)

        assert "--add-dir" in captured_cmd
        assert "/tmp/ws" in captured_cmd
        assert "--full-auto" in captured_cmd


# ── Provider factory integration ──────────────────────────────────────────────


def test_get_provider_returns_cli_claude():
    from relay_discussion.providers import get_provider

    provider = get_provider("cli-claude", model="haiku")
    assert isinstance(provider, CliClaudeProvider)
    assert provider._model == "haiku"


def test_get_provider_returns_cli_codex():
    from relay_discussion.providers import get_provider

    provider = get_provider("cli-codex", timeout=60)
    assert isinstance(provider, CliCodexProvider)
    assert provider._timeout == 60
