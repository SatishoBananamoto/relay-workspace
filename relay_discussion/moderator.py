"""Live moderator input system.

Provides a thread-safe queue for injecting moderator messages and control
commands into a running relay, plus a background daemon that reads from
stdin or a named FIFO.
"""

from __future__ import annotations

import os
import queue
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass(frozen=True, slots=True)
class ModeratorMessage:
    """A text message from the moderator to be injected as an interjection."""
    content: str


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """A control command from the moderator."""
    command: str  # stop, pause, resume, nolimit
    value: int | None = None  # for "more N"


InputEntry = ModeratorMessage | ControlCommand


def parse_input(line: str) -> InputEntry:
    """Parse a line of moderator input into a queue entry."""
    stripped = line.strip()
    if not stripped:
        return ControlCommand(command="noop")

    lower = stripped.lower()

    if lower == "stop":
        return ControlCommand(command="stop")
    if lower == "pause":
        return ControlCommand(command="pause")
    if lower == "resume":
        return ControlCommand(command="resume")
    if lower == "nolimit":
        return ControlCommand(command="nolimit")
    if lower.startswith("more"):
        rest = lower[4:].strip()
        try:
            n = int(rest) if rest else 10
            return ControlCommand(command="more", value=n)
        except ValueError:
            pass

    return ModeratorMessage(content=stripped)


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
