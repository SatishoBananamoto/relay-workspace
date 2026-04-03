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
        return response.strip() if response else ""

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

                # Stream content deltas
                if etype == "content_block_delta":
                    delta = event.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        full_text += text
                        yield text

                # Also handle result text (final)
                if etype == "result" and not full_text:
                    result_text = event.get("result", "")
                    if result_text:
                        yield result_text

                # Forward tool events to callback
                cb = self.on_tool_event
                if cb is not None:
                    if etype == "content_block_start":
                        ctype = event.get("content_block", {}).get("type", "")
                        if ctype == "tool_use":
                            cb({
                                "event": "tool_start",
                                "tool": event["content_block"].get("name", "?"),
                                "id": event["content_block"].get("id", ""),
                            })
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "input_json_delta":
                            cb({
                                "event": "tool_input",
                                "partial": delta.get("partial_json", ""),
                            })
                    elif etype == "content_block_stop":
                        cb({"event": "tool_end"})
                    elif etype == "result":
                        # Include cost/usage info if present
                        usage = event.get("usage") or event.get("total_usage")
                        if usage:
                            cb({"event": "usage", "usage": usage})
        finally:
            proc.wait(timeout=10)


class CliCodexProvider(BaseProvider):
    """Wraps ``codex exec`` with persistent session support."""

    def __init__(
        self,
        *,
        workspace_path: Path | str | None = None,
        timeout: int | None = None,
    ) -> None:
        self._workspace_path = Path(workspace_path) if workspace_path else None
        self._timeout = timeout
        self._workspace_mgr = None
        if self._workspace_path:
            from .workspace import WorkspaceManager
            self._workspace_mgr = WorkspaceManager(self._workspace_path)
        self._session_id: str | None = None

    def set_timeout(self, seconds: int | None) -> None:
        self._timeout = seconds

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
            if self._workspace_path:
                cmd += ["--add-dir", str(self._workspace_path)]
        else:
            cmd = ["codex", "exec", "resume", "--skip-git-repo-check", self._session_id]

        cmd += ["--json", "-o", str(out_file)]

        try:
            result = subprocess.run(
                cmd,
                input=prompt if is_first_call or not self._session_id else prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            out_file.unlink(missing_ok=True)
            raise ProviderError(f"Codex timed out after {self._timeout}s") from exc

        # Parse JSONL for thread_id
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    self._session_id = event["thread_id"]

        # Read response from output file
        response = ""
        if out_file.exists():
            response = out_file.read_text().strip()
            out_file.unlink(missing_ok=True)
        else:
            out_file.unlink(missing_ok=True)

        if result.returncode != 0 and not response:
            stderr = result.stderr.strip()[:200] if result.stderr else "no stderr"
            raise ProviderError(f"Codex exited with code {result.returncode}: {stderr}")

        return response
