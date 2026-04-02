"""Terminal UI for the relay system.

Split-pane layout using prompt_toolkit:
- Top: scrolling conversation output (read-only)
- Status bar: session info, turn, agent, elapsed time
- Bottom: persistent input prompt for moderator
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Callable

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.widgets import TextArea

from .models import Message
from .moderator import ModeratorInputQueue, parse_input, ControlCommand


def _notify(title: str, body: str) -> None:
    """Send desktop notification (best-effort)."""
    try:
        subprocess.Popen(
            ["notify-send", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def format_message(msg: Message) -> str:
    """Format a message for display in the output pane."""
    if msg.role == "system":
        kind = msg.metadata.get("kind", "system")
        if kind == "pause":
            return f"\n  [PAUSED] {msg.content}\n"
        if kind == "attempt_failed":
            speaker = msg.metadata.get("speaker", "?")
            return f"  [{speaker} FAILED] {msg.content}\n"
        return ""

    if msg.role == "moderator":
        kind = msg.metadata.get("kind", "")
        if kind == "topic":
            return f"\n  TOPIC: {msg.content}\n"
        return f"\n  [{msg.author}] {msg.content}\n"

    # Agent message
    turn = msg.metadata.get("turn", "?")
    header = f"\n--- Turn {turn} [{msg.author}] ---\n"
    return header + msg.content + "\n"


class RelayTUI:
    """Split-pane terminal UI for relay conversations."""

    def __init__(
        self,
        moderator_queue: ModeratorInputQueue,
        session_id: str = "",
        topic: str = "",
    ) -> None:
        self._queue = moderator_queue
        self._session_id = session_id[:8] if session_id else "---"
        self._topic = topic[:60]

        # State
        self._current_turn = 0
        self._current_agent = "---"
        self._status = "starting"
        self._start_time = time.time()
        self._output_text = ""

        # Output pane — read-only TextArea (scrollable, focusable for scroll keys)
        self._output_area = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            focusable=True,
            wrap_lines=True,
        )

        # Input area — editable TextArea with accept handler
        self._input_area = TextArea(
            height=3,
            prompt="Satisho > ",
            multiline=False,
            wrap_lines=True,
            accept_handler=self._on_accept,
        )

        # Key bindings
        self._kb = KeyBindings()

        @self._kb.add("c-c")
        def _on_ctrl_c(event):
            self._queue.put(ControlCommand(command="stop"))
            self._append_output("\n  [Ctrl+C — stopping]\n")
            event.app.exit()

        @self._kb.add("tab")
        def _on_tab(event):
            """Toggle focus between output (scroll) and input (type)."""
            app = event.app
            if app.layout.has_focus(self._input_area):
                app.layout.focus(self._output_area)
            else:
                app.layout.focus(self._input_area)

        @self._kb.add("c-d")
        def _on_ctrl_d(event):
            event.app.exit()

        # Layout
        self._app = self._build_app()

    def _on_accept(self, buff: Buffer) -> None:
        """Called when Enter is pressed in the input area."""
        text = buff.text.strip()
        if text:
            entry = parse_input(text)
            self._queue.put(entry)
            if isinstance(entry, ControlCommand):
                self._append_output(f"  > [{entry.command}]\n")
            else:
                self._append_output(f"  > [Satisho] {text}\n")

    def _build_app(self) -> Application:
        status_bar = Window(
            content=FormattedTextControl(self._get_status_text),
            height=1,
            style="reverse",
        )

        body = HSplit([
            self._output_area,
            status_bar,
            self._input_area,
        ])

        return Application(
            layout=Layout(body, focused_element=self._input_area),
            key_bindings=self._kb,
            full_screen=True,
            mouse_support=True,
        )

    def _get_status_text(self) -> list[tuple[str, str]]:
        elapsed = int(time.time() - self._start_time)
        minutes, seconds = divmod(elapsed, 60)
        return [
            ("", f" [{self._session_id}] "),
            ("bold", f" {self._status.upper()} "),
            ("", f" | Turn {self._current_turn} "),
            ("", f" | {self._current_agent} "),
            ("", f" | {minutes:02d}:{seconds:02d} "),
            ("", f" | {self._topic} "),
        ]

    def _append_output(self, text: str) -> None:
        """Append text to the output pane (thread-safe via invalidate)."""
        self._output_text += text
        self._output_area.text = self._output_text
        # Auto-scroll to bottom
        buf = self._output_area.buffer
        buf.cursor_position = len(self._output_text)

    def on_commit(self, message: Message) -> None:
        """Callback for RelayRunner._commit() — called from engine thread."""
        formatted = format_message(message)
        if formatted:
            self._append_output(formatted)
            # Update state from message metadata
            if message.role == "agent":
                turn = message.metadata.get("turn", self._current_turn)
                self._current_turn = turn
                self._current_agent = message.author
                self._status = "running"
                _notify("Relay", f"Turn {turn} — {message.author}")
            elif message.role == "system" and message.metadata.get("kind") == "pause":
                self._status = "paused"
                _notify("Relay", "PAUSED")

            # Schedule UI refresh from engine thread
            try:
                self._app.invalidate()
            except Exception:
                pass

    def on_stream_chunk(self, chunk: str) -> None:
        """Callback for streaming — append partial text."""
        self._append_output(chunk)
        try:
            self._app.invalidate()
        except Exception:
            pass

    def update_status(self, status: str) -> None:
        self._status = status
        try:
            self._app.invalidate()
        except Exception:
            pass

    def run(self) -> None:
        """Run the TUI event loop (blocks until exit)."""
        self._status = "running"
        self._app.run()

    def exit(self) -> None:
        """Exit the TUI from another thread."""
        try:
            self._app.exit()
        except Exception:
            pass


def run_relay_with_tui(
    runner_factory: Callable[..., object],
    moderator_queue: ModeratorInputQueue,
    session_id: str = "",
    topic: str = "",
    resume: bool = False,
) -> object:
    """Run the relay engine in a background thread with TUI in the foreground.

    Args:
        runner_factory: Callable that returns a RelayRunner when called with
                       moderator_queue and on_commit kwargs.
        moderator_queue: The shared input queue.
        session_id: Session ID for display.
        topic: Topic for display.
        resume: Whether to resume.

    Returns:
        The RelayRunResult from the engine.
    """
    tui = RelayTUI(
        moderator_queue=moderator_queue,
        session_id=session_id,
        topic=topic,
    )

    result_holder: list = []

    def engine_thread():
        try:
            runner = runner_factory(
                moderator_queue=moderator_queue,
                on_commit=tui.on_commit,
            )
            result = runner.run(resume=resume)
            result_holder.append(result)
        except Exception as exc:
            result_holder.append(exc)
        finally:
            tui.update_status("done")
            # Give TUI a moment to display final state, then exit
            time.sleep(1)
            tui.exit()

    thread = threading.Thread(target=engine_thread, daemon=True, name="relay-engine")
    thread.start()

    # TUI runs in main thread (prompt_toolkit requires this)
    tui.run()

    thread.join(timeout=5)

    if result_holder and isinstance(result_holder[0], Exception):
        raise result_holder[0]
    return result_holder[0] if result_holder else None
