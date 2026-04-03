"""Live moderator input system.

Provides a thread-safe queue for injecting moderator messages and control
commands into a running relay, plus a background daemon that reads from
stdin or a named FIFO.
"""

from __future__ import annotations

import os
import queue
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, IO


@dataclass(frozen=True, slots=True)
class ModeratorMessage:
    """A text message from the moderator to be injected as an interjection."""
    content: str


@dataclass(slots=True)
class ControlCommand:
    """A control command from the moderator."""
    command: str
    value: int | None = None
    params: dict[str, Any] = field(default_factory=dict)


InputEntry = ModeratorMessage | ControlCommand


def _cmd(command: str, **params: Any) -> ControlCommand:
    """Shorthand for creating a ControlCommand with params."""
    return ControlCommand(command=command, params=params)


def parse_input(line: str) -> InputEntry:
    """Parse a line of moderator input into a queue entry.

    Supports both legacy commands (stop, pause, more 10) and new
    structured commands (deny Claude Write, timeout 120, harness on).
    """
    stripped = line.strip()
    if not stripped:
        return ControlCommand(command="noop")

    lower = stripped.lower()
    parts = stripped.split()

    # --- Legacy commands ---
    if lower == "stop":
        return ControlCommand(command="stop")
    if lower == "pause":
        return ControlCommand(command="pause")
    if lower == "resume":
        return ControlCommand(command="resume")
    if lower == "nolimit":
        return ControlCommand(command="nolimit")
    if lower == "approve":
        return ControlCommand(command="harness_approve")
    if lower == "reject":
        return ControlCommand(command="harness_reject")
    if lower.startswith("more"):
        rest = lower[4:].strip()
        try:
            n = int(rest) if rest else 10
            return ControlCommand(command="more", value=n)
        except ValueError:
            pass

    # --- Permission commands: deny/allow <agent> <tool> ---
    if len(parts) == 3 and lower.startswith(("deny ", "allow ")):
        action = parts[0].lower()
        agent = parts[1]
        tool = parts[2]
        cmd = "deny_tool" if action == "deny" else "allow_tool"
        return _cmd(cmd, agent=agent, tool=tool)

    # --- permission-mode <agent> <mode> ---
    if len(parts) == 3 and lower.startswith("permission-mode "):
        return _cmd("set_permission_mode", agent=parts[1], mode=parts[2])

    # --- skip <agent> ---
    if len(parts) == 2 and lower.startswith("skip "):
        return _cmd("skip", agent=parts[1])

    # --- force <agent> ---
    if len(parts) == 2 and lower.startswith("force "):
        return _cmd("force_next", agent=parts[1])

    # --- instruction <agent> <text...> ---
    if len(parts) >= 3 and lower.startswith("instruction "):
        agent = parts[1]
        instruction = " ".join(parts[2:])
        return _cmd("set_instruction", agent=agent, instruction=instruction)

    # --- timeout [<agent>] <seconds> ---
    if lower.startswith("timeout "):
        timeout_parts = parts[1:]
        if len(timeout_parts) == 1:
            try:
                return _cmd("set_timeout", seconds=int(timeout_parts[0]))
            except ValueError:
                pass
        elif len(timeout_parts) == 2:
            try:
                return _cmd("set_timeout", agent=timeout_parts[0], seconds=int(timeout_parts[1]))
            except ValueError:
                pass

    # --- retry <attempts> <backoff> ---
    if lower.startswith("retry ") and len(parts) == 3:
        try:
            return _cmd("set_retry", attempts=int(parts[1]), backoff=float(parts[2]))
        except ValueError:
            pass

    # --- budget <note> ---
    if lower.startswith("budget "):
        return _cmd("set_budget", note=" ".join(parts[1:]))

    # --- model <agent> <model> ---
    if len(parts) == 3 and lower.startswith("model "):
        return _cmd("set_model", agent=parts[1], model=parts[2])

    # --- effort <agent> <level> ---
    if len(parts) == 3 and lower.startswith("effort "):
        return _cmd("set_effort", agent=parts[1], effort=parts[2])

    # --- harness on/off ---
    if lower in ("harness on", "harness off"):
        return _cmd("harness_toggle", enabled=(lower == "harness on"))

    # --- harness state ---
    if lower == "harness state":
        return ControlCommand(command="harness_state")

    # --- satisfy/breach <obligation_id> ---
    if len(parts) == 2 and lower.startswith("satisfy "):
        return _cmd("obligation_satisfy", obligation_id=parts[1])
    if len(parts) == 2 and lower.startswith("breach "):
        return _cmd("obligation_breach", obligation_id=parts[1])

    # --- Fallback: treat as moderator message ---
    return ModeratorMessage(content=stripped)


def parse_structured_input(body: dict[str, Any]) -> InputEntry:
    """Parse a structured JSON command from the web UI.

    Accepts {command: "...", params: {...}} or {command: "..."} or {message: "..."}.
    """
    if "message" in body and "command" not in body:
        return ModeratorMessage(content=body["message"])
    command = body.get("command", "")
    if not command:
        return ControlCommand(command="noop")
    params = body.get("params", {})
    value = body.get("value")
    return ControlCommand(command=command, value=value, params=params)


class ModeratorInputQueue:
    """Thread-safe queue for moderator input entries."""

    def __init__(self) -> None:
        self._queue: queue.Queue[InputEntry] = queue.Queue()

    def put(self, entry: InputEntry) -> None:
        self._queue.put_nowait(entry)

    def get_nowait(self) -> InputEntry | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> list[InputEntry]:
        """Get all pending entries without blocking."""
        entries: list[InputEntry] = []
        while True:
            entry = self.get_nowait()
            if entry is None:
                break
            entries.append(entry)
        return entries

    @property
    def empty(self) -> bool:
        return self._queue.empty()


class ModeratorDaemon:
    """Background thread that reads moderator input and feeds the queue.

    Supports two input sources:
    - stdin (for interactive terminal use)
    - Named FIFO (for external input via ``relay say``)
    """

    def __init__(
        self,
        input_queue: ModeratorInputQueue,
        fifo_path: Path | None = None,
    ) -> None:
        self._queue = input_queue
        self._fifo_path = fifo_path
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start background input readers."""
        # stdin reader
        stdin_thread = threading.Thread(
            target=self._read_stdin,
            daemon=True,
            name="moderator-stdin",
        )
        stdin_thread.start()
        self._threads.append(stdin_thread)

        # FIFO reader (if path provided)
        if self._fifo_path:
            self._ensure_fifo()
            fifo_thread = threading.Thread(
                target=self._read_fifo,
                daemon=True,
                name="moderator-fifo",
            )
            fifo_thread.start()
            self._threads.append(fifo_thread)

    def stop(self) -> None:
        """Signal readers to stop."""
        self._stop_event.set()

    def _read_stdin(self) -> None:
        """Read lines from stdin until EOF or stop."""
        import sys
        self._read_stream(sys.stdin)

    def _read_fifo(self) -> None:
        """Read from named FIFO in a loop (re-opens after each writer disconnects)."""
        while not self._stop_event.is_set():
            try:
                with open(self._fifo_path, "r") as f:
                    self._read_stream(f)
            except OSError:
                if self._stop_event.is_set():
                    break
                continue

    def _read_stream(self, stream: IO[str]) -> None:
        """Read lines from a stream and put parsed entries into the queue."""
        try:
            for line in stream:
                if self._stop_event.is_set():
                    break
                entry = parse_input(line)
                if isinstance(entry, ControlCommand) and entry.command == "noop":
                    continue
                self._queue.put(entry)
        except (EOFError, ValueError, OSError):
            pass

    def _ensure_fifo(self) -> None:
        """Create the named FIFO if it doesn't exist."""
        if self._fifo_path is None:
            return
        if self._fifo_path.exists():
            if stat.S_ISFIFO(self._fifo_path.stat().st_mode):
                return
            self._fifo_path.unlink()
        self._fifo_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self._fifo_path)
