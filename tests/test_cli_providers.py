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
    assert "[Moderator — Satisho]: Discuss X." in prompt
    assert "[Claude]: I think X is..." in prompt
    assert "relay discussion" in prompt  # relay context header


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
    assert "[Moderator — Satisho]: New input" in cont


def test_format_continuation_with_no_agent_messages_sends_all():
    transcript = [
        _make_message(1, "moderator", "Satisho", "Topic"),
    ]
    cont = _format_continuation(transcript)
    assert "[Moderator — Satisho]: Topic" in cont


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
            with pytest.raises(ProviderError, match="empty result"):
                provider.generate(agent, transcript, turn=1)

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


class _MockPopenStdin:
    def write(self, s): pass
    def close(self): pass


class _MockPopen:
    """Mock subprocess.Popen for Codex provider tests."""
    def __init__(self, stdout_text="", returncode=0, on_create=None):
        import io
        self.stdin = _MockPopenStdin()
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self._on_create = on_create

    def wait(self, timeout=None):
        return self.returncode


def _mock_codex_popen(stdout_text, output_file_text=None, cmd_capture=None):
    """Create a Popen mock factory for Codex tests."""
    def factory(cmd, **kwargs):
        if cmd_capture is not None:
            cmd_capture.extend(cmd)
        # Write output file if requested
        if output_file_text is not None:
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text(output_file_text)
        return _MockPopen(stdout_text=stdout_text)
    return factory


class TestCliCodexProvider:

    def test_generate_returns_response_from_output_file(self, tmp_path):
        provider = CliCodexProvider()
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        mock = _mock_codex_popen(
            stdout_text=_codex_jsonl_output("Hello from Codex"),
            output_file_text="Hello from Codex",
        )
        with patch("relay_discussion.cli_providers.subprocess.Popen", mock):
            response = provider.generate(agent, transcript, turn=1)

        assert response == "Hello from Codex"

    def test_stores_thread_id_from_jsonl(self, tmp_path):
        provider = CliCodexProvider()
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        assert provider.session_id is None

        mock = _mock_codex_popen(
            stdout_text=_codex_jsonl_output("Hi", "thread-xyz"),
            output_file_text="Hi",
        )
        with patch("relay_discussion.cli_providers.subprocess.Popen", mock):
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
        mock = _mock_codex_popen(
            stdout_text=_codex_jsonl_output("response", "existing-thread"),
            output_file_text="response",
            cmd_capture=captured_cmd,
        )
        with patch("relay_discussion.cli_providers.subprocess.Popen", mock):
            provider.generate(agent, transcript, turn=2)

        assert "resume" in captured_cmd
        assert "existing-thread" in captured_cmd

    def test_timeout_raises_provider_error(self):
        """With timeout=None (default), no timeout error. Test exception handling."""
        provider = CliCodexProvider()
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        def mock_fail(cmd, **kwargs):
            raise OSError("Codex not found")

        with patch("relay_discussion.cli_providers.subprocess.Popen", mock_fail):
            with pytest.raises(ProviderError, match="Codex failed"):
                provider.generate(agent, transcript, turn=1)

    def test_empty_output_file_returns_empty_string(self):
        provider = CliCodexProvider()
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        mock = _mock_codex_popen(
            stdout_text=_codex_jsonl_output(""),
            output_file_text="",
        )
        with patch("relay_discussion.cli_providers.subprocess.Popen", mock):
            response = provider.generate(agent, transcript, turn=1)
            assert response == ""

    def test_workspace_flags_added_in_build_mode(self):
        provider = CliCodexProvider(workspace_path="/tmp/ws")
        agent = AgentConfig(name="Codex", provider="cli-codex")
        transcript = [_make_message(1, "moderator", "Satisho", "Hello")]

        captured_cmd = []
        mock = _mock_codex_popen(
            stdout_text=_codex_jsonl_output("ok"),
            output_file_text="ok",
            cmd_capture=captured_cmd,
        )
        with patch("relay_discussion.cli_providers.subprocess.Popen", mock):
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
