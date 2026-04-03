"""CLI provider adapters wrapping claude and codex command-line tools.

Each provider maintains a persistent session (one per relay conversation)
so the agent keeps its own conversation context across turns.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from .models import AgentConfig, Message
from .providers import BaseProvider, ProviderError


def _format_prompt(
    agent: AgentConfig,
    transcript: Sequence[Message],
    turn: int,
    workspace_summary: str = "",
) -> str:
    """Build a prompt string from the agent config and recent transcript."""
    parts: list[str] = []
    if agent.instruction:
        parts.append(agent.instruction)

    if workspace_summary:
        parts.append(workspace_summary)

    # Include non-system messages as conversation context
    for msg in transcript:
        if msg.role == "system":
            continue
        parts.append(f"[{msg.author}]: {msg.content}")

    return "\n\n".join(parts)


def _format_continuation(transcript: Sequence[Message]) -> str:
    """Build a short continuation prompt with only new messages since last turn.

    When resuming a persistent session, the agent already has prior context.
    We only need to send what happened since its last response.
    """
    # Find the last message from any agent (the previous turn's response)
    # and send everything after it
    last_agent_idx = -1
    for i, msg in enumerate(transcript):
        if msg.role == "agent":
            last_agent_idx = i

    if last_agent_idx < 0:
        # No agent messages yet — send the full prompt
        return "\n\n".join(
            f"[{msg.author}]: {msg.content}"
            for msg in transcript
            if msg.role != "system"
        )

    # Send only messages after the last agent response
    new_messages = transcript[last_agent_idx + 1 :]
    if not new_messages:
        return ""

    return "\n\n".join(
        f"[{msg.author}]: {msg.content}"
        for msg in new_messages
        if msg.role != "system"
    )


_DEFAULT_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep"]


class CliClaudeProvider(BaseProvider):
    """Wraps ``claude -p`` with persistent session support."""

    def __init__(
        self,
        *,
        model: str = "opus",
        effort: str = "max",
        workspace_path: Path | str | None = None,
        timeout: int | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._workspace_path = Path(workspace_path) if workspace_path else None
        self._timeout = timeout
        self._workspace_mgr = None
        if self._workspace_path:
            from .workspace import WorkspaceManager
            self._workspace_mgr = WorkspaceManager(self._workspace_path)
        self._session_id: str | None = None
        self._allowed_tools: list[str] = list(_DEFAULT_TOOLS)
        self._denied_tools: set[str] = set()
        self._permission_mode: str = "auto"

    def set_model(self, model: str) -> None:
        self._model = model

    def set_effort(self, effort: str) -> None:
        self._effort = effort

    def deny_tool(self, tool: str) -> None:
        self._denied_tools.add(tool)

    def allow_tool(self, tool: str) -> None:
        self._denied_tools.discard(tool)
        if tool not in self._allowed_tools:
            self._allowed_tools.append(tool)

    def set_permission_mode(self, mode: str) -> None:
        self._permission_mode = mode

    def set_timeout(self, seconds: int | None) -> None:
        self._timeout = seconds

    def get_effective_tools(self) -> list[str]:
        return [t for t in self._allowed_tools if t not in self._denied_tools]

    @property
    def on_tool_event(self):
        return getattr(self, "_on_tool_event", None)

    @on_tool_event.setter
    def on_tool_event(self, callback):
        self._on_tool_event = callback

    def _permission_flags(self) -> list[str]:
        """Build CLI flags for permissions. Called each turn."""
        flags: list[str] = []
        if self._workspace_path:
            flags += ["--add-dir", str(self._workspace_path)]
        if self._permission_mode == "dangerously-skip-permissions":
            flags.append("--dangerously-skip-permissions")
        else:
            effective = self.get_effective_tools()
            if effective:
                flags += ["--permission-mode", self._permission_mode]
                flags += ["--allowedTools", " ".join(effective)]
            else:
                # No tools allowed — deny everything via empty allowedTools
                flags += ["--permission-mode", self._permission_mode]
                flags += ["--allowedTools", ""]
        return flags

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self._session_id = value

    def generate(
        self,
        agent: AgentConfig,
        transcript: Sequence[Message],
        turn: int,
    ) -> str:
        is_first_call = self._session_id is None
        ws_summary = self._workspace_mgr.workspace_summary() if self._workspace_mgr else ""
        prompt = (
            _format_prompt(agent, transcript, turn, workspace_summary=ws_summary)
            if is_first_call
            else _format_continuation(transcript)
        )
        if not prompt:
            prompt = "Continue."

        cmd = [
            "claude",
            "-p",
            "--model", self._model,
            "--effort", self._effort,
            "--output-format", "json",
        ]

        if self._session_id:
            cmd += ["--resume", self._session_id]

        cmd += self._permission_flags()

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"Claude timed out after {self._timeout}s") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip()[:200] if result.stderr else "no stderr"
            raise ProviderError(f"Claude exited with code {result.returncode}: {stderr}")

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(f"Claude returned invalid JSON: {exc}") from exc

        if data.get("is_error"):
            raise ProviderError(f"Claude returned error: {data.get('result', 'unknown')}")

        self._session_id = data.get("session_id") or self._session_id
        response = data.get("result", "")
        if not response or not response.strip():
            # Surface what Claude actually returned for debugging
            stderr_hint = result.stderr.strip()[:200] if result.stderr else ""
            keys = list(data.keys())
            raise ProviderError(
                f"Claude returned empty result. "
                f"Keys in response: {keys}. "
                f"is_error={data.get('is_error')}. "
                f"session_id={data.get('session_id', 'none')}. "
                f"stderr: {stderr_hint or 'none'}"
            )
        return response.strip()

    @property
    def supports_streaming(self) -> bool:
        return True

    def generate_stream(
        self,
        agent: AgentConfig,
        transcript: Sequence[Message],
        turn: int,
    ) -> "Iterator[str]":
        """Stream Claude response token by token using stream-json format."""
        from typing import Iterator

        is_first_call = self._session_id is None
        ws_summary = self._workspace_mgr.workspace_summary() if self._workspace_mgr else ""
        prompt = (
            _format_prompt(agent, transcript, turn, workspace_summary=ws_summary)
            if is_first_call
            else _format_continuation(transcript)
        )
        if not prompt:
            prompt = "Continue."

        cmd = [
            "claude", "-p",
            "--model", self._model,
            "--effort", self._effort,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self._session_id:
            cmd += ["--resume", self._session_id]
        cmd += self._permission_flags()

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()

        full_text = ""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                etype = event.get("type", "")

                # Extract session_id from result event
                if etype == "result":
                    self._session_id = event.get("session_id") or self._session_id

                # Stream-json with --verbose uses full message events, not content_block deltas
                # Format: {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
                if etype == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                full_text += text
                                yield text

                # Also handle result text (final fallback)
                if etype == "result" and not full_text:
                    result_text = event.get("result", "")
                    if result_text:
                        yield result_text

                # Forward tool events to callback
                cb = self.on_tool_event
                if cb is not None:
                    if etype == "assistant":
                        msg = event.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "tool_use":
                                cb({
                                    "event": "tool_start",
                                    "tool": block.get("name", "?"),
                                    "id": block.get("id", ""),
                                    "input": json.dumps(block.get("input", {}))[:100],
                                })
                    elif etype == "user":
                        msg = event.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "tool_result":
                                cb({"event": "tool_end"})
                    elif etype == "result":
                        usage = event.get("usage")
                        if usage:
                            cb({"event": "usage", "usage": usage})
                        model_usage = event.get("modelUsage", {})
                        if model_usage:
                            cb({"event": "model_info", "models": model_usage})
        finally:
            proc.wait(timeout=10)


class CliCodexProvider(BaseProvider):
    """Wraps ``codex exec`` with persistent session support."""

    def __init__(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        workspace_path: Path | str | None = None,
        timeout: int | None = None,
    ) -> None:
        self._model = model  # e.g. "gpt-5.4", "o3", "o4-mini"
        self._effort = effort  # e.g. "xhigh", "high", "medium", "low"
        self._workspace_path = Path(workspace_path) if workspace_path else None
        self._timeout = timeout
        self._workspace_mgr = None
        if self._workspace_path:
            from .workspace import WorkspaceManager
            self._workspace_mgr = WorkspaceManager(self._workspace_path)
        self._session_id: str | None = None

    def set_timeout(self, seconds: int | None) -> None:
        self._timeout = seconds

    def set_model(self, model: str) -> None:
        self._model = model

    def set_effort(self, effort: str) -> None:
        self._effort = effort

    @property
    def on_tool_event(self):
        return getattr(self, "_on_tool_event", None)

    @on_tool_event.setter
    def on_tool_event(self, callback):
        self._on_tool_event = callback

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self._session_id = value

    def generate(
        self,
        agent: AgentConfig,
        transcript: Sequence[Message],
        turn: int,
    ) -> str:
        is_first_call = self._session_id is None
        ws_summary = self._workspace_mgr.workspace_summary() if self._workspace_mgr else ""
        prompt = (
            _format_prompt(agent, transcript, turn, workspace_summary=ws_summary)
            if is_first_call
            else _format_continuation(transcript)
        )
        if not prompt:
            prompt = "Continue."

        out_fd, out_path = tempfile.mkstemp(suffix=".md", prefix="relay_codex_")
        os.close(out_fd)
        out_file = Path(out_path)

        if is_first_call:
            cmd = ["codex", "exec", "-", "--skip-git-repo-check", "--full-auto"]
            if self._model:
                cmd += ["-m", self._model]
            if self._effort:
                cmd += ["-c", f"model_reasoning_effort={self._effort}"]
            if self._workspace_path:
                cmd += ["--add-dir", str(self._workspace_path)]
        else:
            cmd = ["codex", "exec", "resume", "--skip-git-repo-check", self._session_id]

        cmd += ["--json", "-o", str(out_file)]

        # Use Popen to stream JSONL events as they arrive
        cb = self.on_tool_event
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                etype = event.get("type", "")

                if etype == "thread.started" and event.get("thread_id"):
                    self._session_id = event["thread_id"]

                # Forward tool events if callback is set
                # Codex format: item.started/item.completed with item.type = "command_execution"
                if cb is not None:
                    item = event.get("item", {})
                    if etype == "item.started" and item.get("type") == "command_execution":
                        cb({
                            "event": "tool_start",
                            "tool": "Bash",
                            "id": item.get("id", ""),
                            "input": item.get("command", "")[:100],
                        })
                    elif etype == "item.completed" and item.get("type") == "command_execution":
                        cb({"event": "tool_end"})
                    elif etype == "turn.completed":
                        usage = event.get("usage")
                        if usage:
                            cb({"event": "usage", "usage": usage})

            proc.wait(timeout=30)
        except Exception as exc:
            if 'proc' in locals():
                proc.kill()
            out_file.unlink(missing_ok=True)
            raise ProviderError(f"Codex failed: {exc}") from exc

        # Read response from output file
        response = ""
        if out_file.exists():
            response = out_file.read_text().strip()
            out_file.unlink(missing_ok=True)
        else:
            out_file.unlink(missing_ok=True)

        if proc.returncode != 0 and not response:
            stderr = proc.stderr.read().strip()[:200] if proc.stderr else "no stderr"
            raise ProviderError(f"Codex exited with code {result.returncode}: {stderr}")

        return response
