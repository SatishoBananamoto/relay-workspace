"""Live web viewer for relay discussions.

Opens a browser tab that shows the conversation streaming in real-time,
harness activity (policy decisions, obligations), and moderator controls.

Zero external dependencies — uses stdlib http.server + SSE.

Usage:
    relay new --topic "..." --web              # default port 8411
    relay new --topic "..." --web --port 9000  # custom port
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .models import Message
from .moderator import ModeratorInputQueue, parse_input


# ---------------------------------------------------------------------------
# EventBus — thread-safe fanout from engine callbacks to SSE clients
# ---------------------------------------------------------------------------

# Events worth replaying for late-connecting clients
_REPLAY_TYPES = {"message", "status", "policy"}


class EventBus:
    """Thread-safe fanout with history replay for late-connecting clients."""

    def __init__(self) -> None:
        self._subscribers: list[queue.SimpleQueue] = []
        self._lock = threading.Lock()
        self._history: list[dict] = []

    def subscribe(self) -> queue.SimpleQueue:
        """Subscribe and receive all past events immediately."""
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            for event in self._history:
                q.put(event)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.SimpleQueue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: dict) -> None:
        with self._lock:
            # Only store replayable events in history
            if event.get("type") in _REPLAY_TYPES:
                self._history.append(event)
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    @property
    def history_count(self) -> int:
        with self._lock:
            return len(self._history)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# ---------------------------------------------------------------------------
# WebViewer — mirrors TUI callback interface
# ---------------------------------------------------------------------------

class WebViewer:
    """Bridges engine callbacks to SSE events via EventBus.

    Same callback interface as RelayTUI: on_commit, on_stream_chunk.
    """

    def __init__(
        self,
        moderator_queue: ModeratorInputQueue,
        session_id: str = "",
        topic: str = "",
        host: str = "127.0.0.1",
        port: int = 8411,
        agents: list[dict] | None = None,
    ) -> None:
        self._queue = moderator_queue
        self._bus = EventBus()
        self._session_id = session_id
        self._topic = topic
        self._agents = agents or []  # [{name, provider, model}]
        self._host = host
        self._port = port
        self._status = "starting"
        self._current_turn = 0
        self._current_agent = "---"
        self._streaming = False
        self._server: ThreadingHTTPServer | None = None
        self._start_event = threading.Event()
        self._config_overrides: dict = {}  # settings changed in lobby

    @property
    def bus(self) -> EventBus:
        return self._bus

    def on_commit(self, message: Message) -> None:
        """Callback for RelayRunner._commit() — called from engine thread."""
        # Finalize any in-progress stream
        if self._streaming:
            self._bus.publish({"type": "stream_end", "data": {}})
            self._streaming = False

        # Publish the message
        msg_dict = {
            "seq": message.seq,
            "timestamp": message.timestamp,
            "role": message.role,
            "author": message.author,
            "content": message.content,
            "metadata": message.metadata,
        }
        self._bus.publish({"type": "message", "data": msg_dict})

        # Track state
        if message.role == "agent":
            self._current_turn = message.metadata.get("turn", self._current_turn)
            self._current_agent = message.author
            self._status = "running"

        elif message.role == "system":
            kind = message.metadata.get("kind")
            if kind == "pause":
                self._status = "paused"
            elif kind == "policy_gate":
                self._bus.publish({
                    "type": "policy",
                    "data": {
                        "speaker": message.metadata.get("speaker", ""),
                        "decision": message.metadata.get("decision", ""),
                        "action_type": message.metadata.get("action_type", ""),
                        "blockers": message.metadata.get("blockers", []),
                        "turn": message.metadata.get("turn", 0),
                    },
                })

        # Always publish status update
        self._bus.publish({
            "type": "status",
            "data": self._status_dict(),
        })

    def on_stream_chunk(self, chunk: str) -> None:
        """Callback for streaming partial agent responses."""
        self._streaming = True
        self._bus.publish({"type": "stream", "data": {"chunk": chunk}})

    def on_activity(self, activity: dict) -> None:
        """Callback for engine activity events — thinking, harness eval, turn committed."""
        self._bus.publish({"type": "activity", "data": activity})

        # Update thinking state and push status update so status bar reflects current agent
        kind = activity.get("kind")
        if kind == "thinking":
            self._current_agent = activity.get("agent", self._current_agent)
            self._current_turn = activity.get("turn", self._current_turn)
            self._bus.publish({
                "type": "status",
                "data": self._status_dict(),
            })

    def update_status(self, status: str) -> None:
        self._status = status
        self._bus.publish({
            "type": "status",
            "data": self._status_dict(),
        })

    def get_state(self) -> dict:
        """Current state snapshot for GET /state."""
        return {
            "session_id": self._session_id,
            "topic": self._topic,
            "agents": self._agents,
            **self._status_dict(),
        }

    def _status_dict(self) -> dict:
        return {
            "status": self._status,
            "turn": self._current_turn,
            "agent": self._current_agent,
        }

    def wait_for_start(self) -> dict:
        """Block until the moderator clicks Start or auto-start fires."""
        self._start_event.wait()
        return self._config_overrides

    def trigger_start(self, overrides: dict | None = None) -> None:
        """Called from POST /start — unblocks the engine thread."""
        if overrides:
            self._config_overrides = overrides
        self._start_event.set()
        self._bus.publish({"type": "status", "data": {"status": "starting"}})

    def run(self) -> None:
        """Start HTTP server (blocks until exit)."""
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    self._serve_html()
                elif self.path == "/events":
                    self._serve_sse()
                elif self.path == "/state":
                    self._serve_state()
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path == "/control":
                    self._handle_control()
                elif self.path == "/start":
                    self._handle_start()
                else:
                    self.send_error(404)

            def _serve_html(self):
                body = _INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_state(self):
                body = json.dumps(viewer.get_state()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                # Disable Nagle for low latency
                try:
                    self.connection.setsockopt(
                        socket.IPPROTO_TCP, socket.TCP_NODELAY, 1,
                    )
                except Exception:
                    pass

                q = viewer._bus.subscribe()
                try:
                    while True:
                        try:
                            event = q.get(timeout=15)
                            event_type = event.get("type", "message")
                            event_data = json.dumps(event.get("data", {}))
                            self.wfile.write(
                                f"event: {event_type}\ndata: {event_data}\n\n".encode()
                            )
                            self.wfile.flush()
                        except queue.Empty:
                            # Keepalive
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    viewer._bus.unsubscribe(q)

            def _handle_start(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                except (json.JSONDecodeError, ValueError):
                    body = {}
                viewer.trigger_start(body)
                resp = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def _handle_control(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                except (json.JSONDecodeError, ValueError):
                    self.send_error(400, "Invalid JSON")
                    return

                from .moderator import parse_structured_input
                entry = parse_structured_input(body)

                viewer._queue.put(entry)

                resp = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, format, *args):
                # Suppress default access logs
                pass

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        print(f"  Web viewer: http://{self._host}:{self._port}")
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass

    def exit(self) -> None:
        if self._server:
            self._server.shutdown()


# ---------------------------------------------------------------------------
# Top-level wiring — mirrors run_relay_with_tui
# ---------------------------------------------------------------------------

def run_relay_with_web(
    runner_factory: Callable[..., object],
    moderator_queue: ModeratorInputQueue,
    session_id: str = "",
    topic: str = "",
    resume: bool = False,
    host: str = "127.0.0.1",
    port: int = 8411,
    agents: list[dict] | None = None,
) -> object:
    """Run the relay engine in a background thread with web viewer in foreground.

    Mirrors run_relay_with_tui: engine in thread, viewer blocks main thread.
    """
    viewer = WebViewer(
        moderator_queue=moderator_queue,
        agents=agents or [],
        session_id=session_id,
        topic=topic,
        host=host,
        port=port,
    )

    result_holder: list = []

    def engine_thread():
        try:
            # Wait for Start from web UI (or auto-start after 10s)
            if not resume:
                viewer.update_status("lobby")
                overrides = viewer.wait_for_start()
            else:
                overrides = {}

            viewer.update_status("starting")
            runner = runner_factory(
                moderator_queue=moderator_queue,
                on_commit=viewer.on_commit,
                on_stream_chunk=viewer.on_stream_chunk,
                on_activity=viewer.on_activity,
                config_overrides=overrides,
            )
            # Queue effort overrides — they'll be applied when providers are created
            # (providers are created lazily on first use during run())
            if overrides.get("left_effort") or overrides.get("right_effort"):
                from .moderator import ControlCommand
                if overrides.get("left_effort"):
                    moderator_queue.put(ControlCommand(
                        command="set_effort",
                        params={"agent": runner.config.left_agent.name, "effort": overrides["left_effort"]},
                    ))
                if overrides.get("right_effort"):
                    moderator_queue.put(ControlCommand(
                        command="set_effort",
                        params={"agent": runner.config.right_agent.name, "effort": overrides["right_effort"]},
                    ))
            result = runner.run(resume=resume)
            result_holder.append(result)
        except Exception as exc:
            result_holder.append(exc)
        finally:
            viewer.update_status("done")
            time.sleep(2)
            viewer.exit()

    thread = threading.Thread(target=engine_thread, daemon=True, name="relay-engine")
    thread.start()

    viewer.run()

    thread.join(timeout=5)

    if result_holder and isinstance(result_holder[0], Exception):
        raise result_holder[0]
    return result_holder[0] if result_holder else None


# ---------------------------------------------------------------------------
# HTML page — single inline constant
# ---------------------------------------------------------------------------

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relay Viewer</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Consolas', 'Menlo', monospace;
    background: #0d1117;
    color: #c9d1d9;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* Status bar */
  #status-bar {
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 8px 16px;
    display: flex;
    align-items: center;
    gap: 16px;
    font-size: 13px;
    flex-shrink: 0;
  }
  #status-bar .label { color: #8b949e; }
  #status-bar .session-id { color: #58a6ff; }
  .status-badge {
    padding: 2px 8px;
    border-radius: 3px;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
  }
  .status-running { background: #1f6feb33; color: #58a6ff; }
  .status-paused { background: #d2992233; color: #d29922; }
  .status-done { background: #23863633; color: #3fb950; }
  .status-starting { background: #30363d; color: #8b949e; }
  .status-lobby { background: #1f6feb33; color: #58a6ff; }

  /* Main layout */
  #main {
    flex: 1;
    display: grid;
    grid-template-columns: 1fr 320px;
    overflow: hidden;
  }
  @media (max-width: 768px) {
    #main { grid-template-columns: 1fr; }
    #sidebar { display: none; }
  }

  /* Conversation pane */
  #conversation {
    overflow-y: auto;
    padding: 16px;
    scroll-behavior: smooth;
  }

  .msg { margin-bottom: 16px; }
  .msg-header {
    font-size: 12px;
    color: #8b949e;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .msg-header .turn { color: #58a6ff; }
  .msg-header .author { font-weight: 600; }
  .msg-body {
    padding: 8px 12px;
    border-radius: 6px;
    background: #161b22;
    border-left: 3px solid #30363d;
    white-space: pre-wrap;
    word-wrap: break-word;
    font-size: 13px;
    line-height: 1.5;
  }

  .msg-agent-Claude .msg-body { border-left-color: #58a6ff; }
  .msg-agent-Codex .msg-body { border-left-color: #3fb950; }
  .msg-moderator .msg-body {
    border-left-color: #d29922;
    background: #1c1d21;
  }
  .msg-system .msg-body {
    border-left-color: #f85149;
    background: #1c1d21;
    font-size: 12px;
    color: #8b949e;
  }
  .msg-topic .msg-body {
    border-left-color: #bc8cff;
    background: #1c1d21;
    font-weight: 600;
    font-size: 14px;
  }

  #stream-buffer {
    padding: 8px 12px;
    border-radius: 6px;
    background: #161b22;
    border-left: 3px solid #58a6ff;
    white-space: pre-wrap;
    word-wrap: break-word;
    font-size: 13px;
    line-height: 1.5;
    display: none;
  }
  #stream-buffer.active { display: block; }
  #stream-cursor {
    display: inline-block;
    width: 8px;
    height: 14px;
    background: #58a6ff;
    animation: blink 1s step-end infinite;
    vertical-align: text-bottom;
  }
  @keyframes blink { 50% { opacity: 0; } }

  /* Sidebar */
  #sidebar {
    border-left: 1px solid #30363d;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .sidebar-section {
    padding: 12px;
    border-bottom: 1px solid #30363d;
  }
  .sidebar-section h3 {
    font-size: 11px;
    text-transform: uppercase;
    color: #8b949e;
    margin-bottom: 8px;
    letter-spacing: 0.5px;
  }

  /* Harness activity */
  #harness-feed {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
  }
  #harness-feed h3 {
    font-size: 11px;
    text-transform: uppercase;
    color: #8b949e;
    margin-bottom: 8px;
    letter-spacing: 0.5px;
  }
  .harness-event {
    font-size: 12px;
    padding: 6px 8px;
    margin-bottom: 6px;
    border-radius: 4px;
    background: #161b22;
  }
  .decision-badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    margin-right: 4px;
  }
  .decision-allow { background: #23863633; color: #3fb950; }
  .decision-block { background: #f8514933; color: #f85149; }
  .decision-clarify { background: #d2992233; color: #d29922; }
  .decision-force_change { background: #d2992233; color: #d29922; }

  /* Controls */
  #controls { padding: 12px; }
  #controls h3 {
    font-size: 11px;
    text-transform: uppercase;
    color: #8b949e;
    margin-bottom: 8px;
    letter-spacing: 0.5px;
  }
  .btn-row {
    display: flex;
    gap: 6px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .btn {
    padding: 6px 12px;
    border: 1px solid #30363d;
    border-radius: 4px;
    background: #21262d;
    color: #c9d1d9;
    font-size: 12px;
    font-family: inherit;
    cursor: pointer;
    transition: background 0.15s;
  }
  .btn:hover { background: #30363d; }
  .btn-danger { border-color: #f85149; color: #f85149; }
  .btn-danger:hover { background: #f8514922; }
  .btn-warn { border-color: #d29922; color: #d29922; }
  .btn-warn:hover { background: #d2992222; }

  #msg-input {
    display: flex;
    gap: 6px;
  }
  #msg-input input {
    flex: 1;
    padding: 6px 10px;
    border: 1px solid #30363d;
    border-radius: 4px;
    background: #0d1117;
    color: #c9d1d9;
    font-size: 12px;
    font-family: inherit;
    outline: none;
  }
  #msg-input input:focus { border-color: #58a6ff; }

  /* Thinking / tool activity bar */
  #thinking-bar {
    display: none;
    padding: 8px 16px;
    background: #161b22;
    border-bottom: 1px solid #30363d;
    font-size: 12px;
    color: #8b949e;
    flex-shrink: 0;
    flex-direction: column;
    gap: 4px;
  }
  #thinking-bar.active { display: flex; }
  .thinking-row { display: flex; align-items: center; gap: 8px; }
  .thinking-dot {
    width: 6px; height: 6px;
    background: #58a6ff;
    border-radius: 50%;
    animation: pulse 1.5s ease-in-out infinite;
  }
  .thinking-dot:nth-child(2) { animation-delay: 0.3s; }
  .thinking-dot:nth-child(3) { animation-delay: 0.6s; }
  @keyframes pulse { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }
  #thinking-text { color: #c9d1d9; }
  #thinking-elapsed { color: #484f58; margin-left: auto; }
  #tool-activity {
    font-size: 11px;
    color: #8b949e;
    max-height: 60px;
    overflow-y: auto;
  }
  .tool-line {
    padding: 1px 0;
    color: #58a6ff;
  }
  .tool-line .tool-name { color: #bc8cff; font-weight: 600; }
  .tool-line .tool-input { color: #484f58; }

  /* Tabs */
  .tab-btn {
    flex: 1;
    padding: 8px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: #8b949e;
    font-size: 11px;
    font-family: inherit;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    cursor: pointer;
  }
  .tab-btn:hover { color: #c9d1d9; }
  .tab-btn.active { color: #58a6ff; border-bottom-color: #58a6ff; }

  /* Tool toggles */
  .tool-toggle {
    display: inline-block;
    padding: 3px 8px;
    margin: 2px;
    border-radius: 3px;
    font-size: 11px;
    font-family: inherit;
    cursor: pointer;
    border: 1px solid #30363d;
    background: #21262d;
    color: #c9d1d9;
    transition: all 0.15s;
  }
  .tool-toggle.allowed { border-color: #3fb950; color: #3fb950; }
  .tool-toggle.denied { border-color: #f85149; color: #f85149; background: #f8514915; }

  /* Toast */
  #toast-container {
    position: fixed;
    top: 48px;
    right: 16px;
    z-index: 100;
  }
  .toast {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 8px 14px;
    font-size: 12px;
    margin-bottom: 6px;
    animation: fadeIn 0.2s ease, fadeOut 0.3s ease 2.7s forwards;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; } }
  @keyframes fadeOut { to { opacity: 0; transform: translateY(-8px); } }

  /* Obligation item */
  .obl-item {
    padding: 6px 8px;
    margin-bottom: 4px;
    border-radius: 4px;
    background: #161b22;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .obl-item .obl-status {
    font-size: 10px;
    font-weight: 600;
    padding: 1px 5px;
    border-radius: 3px;
  }
  .obl-open { background: #1f6feb33; color: #58a6ff; }
  .obl-satisfied { background: #23863633; color: #3fb950; }
  .obl-breached { background: #f8514933; color: #f85149; }

  /* Empty state */
  .empty-state {
    color: #484f58;
    font-size: 12px;
    font-style: italic;
  }
</style>
</head>
<body>

<div id="status-bar">
  <span class="label">Relay</span>
  <span class="session-id" id="s-session">---</span>
  <span class="status-badge status-starting" id="s-badge">STARTING</span>
  <span class="label">Turn</span>
  <span id="s-turn">0</span>
  <span class="label">Agent</span>
  <span id="s-agent">---</span>
</div>

<!-- Lobby overlay -->
<div id="lobby" style="position:fixed;inset:0;background:#0d1117ee;z-index:200;display:flex;align-items:center;justify-content:center">
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:24px;width:520px;max-width:90vw">
    <h2 style="color:#c9d1d9;font-size:16px;margin-bottom:16px">Session Settings</h2>
    <div style="margin-bottom:12px">
      <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px">Topic</label>
      <div id="lobby-topic" style="font-size:13px;color:#c9d1d9;padding:8px;background:#0d1117;border-radius:4px;border:1px solid #30363d;max-height:60px;overflow:auto"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">
      <div>
        <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px">Turns</label>
        <input type="number" id="lobby-turns" value="4" min="1" style="width:100%;padding:6px 10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:13px;font-family:inherit">
      </div>
      <div>
        <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px" id="lobby-left-label">Agent 1 Model</label>
        <select id="lobby-left-model" style="width:100%;padding:6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
          <option value="opus" selected>opus</option>
          <option value="sonnet">sonnet</option>
          <option value="haiku">haiku</option>
        </select>
      </div>
      <div>
        <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px" id="lobby-right-label">Agent 2 Model</label>
        <select id="lobby-right-model" style="width:100%;padding:6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
          <option value="opus">opus</option>
          <option value="sonnet" selected>sonnet</option>
          <option value="haiku">haiku</option>
        </select>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">
      <div>
        <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px">Auto-start</label>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="lobby-countdown" style="font-size:24px;color:#58a6ff;font-weight:600">30</span>
          <span style="font-size:12px;color:#8b949e">sec</span>
        </div>
      </div>
      <div>
        <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px" id="lobby-left-effort-label">Agent 1 Effort</label>
        <select id="lobby-left-effort" style="width:100%;padding:6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
          <option value="max" selected>max</option>
          <option value="high">high</option>
          <option value="medium">medium</option>
          <option value="low">low</option>
        </select>
      </div>
      <div>
        <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px" id="lobby-right-effort-label">Agent 2 Effort</label>
        <select id="lobby-right-effort" style="width:100%;padding:6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
          <option value="max" selected>max</option>
          <option value="high">high</option>
          <option value="medium">medium</option>
          <option value="low">low</option>
        </select>
      </div>
    </div>
    <div style="margin-bottom:12px">
      <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px" id="lobby-left-instr-label">Agent 1 instruction</label>
      <input type="text" id="lobby-left-instr" placeholder="(optional)" style="width:100%;padding:6px 10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
    </div>
    <div style="margin-bottom:16px">
      <label style="font-size:11px;color:#8b949e;text-transform:uppercase;display:block;margin-bottom:4px" id="lobby-right-instr-label">Agent 2 instruction</label>
      <input type="text" id="lobby-right-instr" placeholder="(optional)" style="width:100%;padding:6px 10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
    </div>
    <div style="display:flex;gap:8px">
      <button onclick="lobbyStart()" style="flex:1;padding:10px;background:#1f6feb;border:none;border-radius:4px;color:#fff;font-size:14px;font-weight:600;font-family:inherit;cursor:pointer">Start Now</button>
      <button onclick="lobbyCancelAuto()" style="padding:10px 16px;background:#21262d;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit;cursor:pointer">Cancel Auto</button>
    </div>
  </div>
</div>

<div id="toast-container"></div>

<div id="thinking-bar">
  <div class="thinking-row">
    <span class="thinking-dot"></span>
    <span class="thinking-dot"></span>
    <span class="thinking-dot"></span>
    <span id="thinking-text">---</span>
    <span id="thinking-elapsed"></span>
  </div>
  <div id="tool-activity"></div>
</div>

<div id="main">
  <div id="conversation">
    <div class="empty-state" id="empty-msg">Waiting for first message...</div>
  </div>

  <div id="sidebar">
    <!-- Tabs -->
    <div style="display:flex;border-bottom:1px solid #30363d;flex-shrink:0">
      <button class="tab-btn active" onclick="showTab('controls')">Controls</button>
      <button class="tab-btn" onclick="showTab('permissions')">Permissions</button>
      <button class="tab-btn" onclick="showTab('harness')">Harness</button>
    </div>

    <!-- Controls tab -->
    <div id="tab-controls" class="tab-content active" style="overflow-y:auto;padding:12px">
      <h3>Session</h3>
      <div class="btn-row">
        <button class="btn btn-warn" onclick="sendControl('pause')">Pause</button>
        <button class="btn btn-danger" onclick="sendControl('stop')">Stop</button>
        <button class="btn" onclick="sendControl('more 10')">More +10</button>
        <button class="btn" onclick="sendControl('nolimit')">No Limit</button>
      </div>

      <h3>Model / Effort</h3>
      <div id="model-controls"></div>

      <h3>Steering</h3>
      <div class="btn-row" id="steering-btns"></div>
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <input type="number" id="timeout-input" placeholder="none" min="0" max="3600" style="width:70px;padding:4px 6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
        <button class="btn" onclick="setTimeoutVal()">Set Timeout</button>
      </div>

      <h3>Inject Message</h3>
      <div style="display:flex;gap:6px;margin-bottom:4px">
        <select id="msg-target" style="padding:4px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px">
          <option value="both">To: Both</option>
        </select>
      </div>
      <div id="msg-input">
        <input type="text" id="inject-text" placeholder="Message to agents..." onkeydown="if(event.key==='Enter')sendMessage()">
        <button class="btn" onclick="sendMessage()">Send</button>
      </div>

      <h3>Instruction</h3>
      <div style="display:flex;gap:6px;margin-bottom:4px">
        <select id="instr-agent" style="padding:4px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px"></select>
      </div>
      <div style="display:flex;gap:6px">
        <input type="text" id="instr-text" placeholder="New instruction..." style="flex:1;padding:6px 10px;border:1px solid #30363d;border-radius:4px;background:#0d1117;color:#c9d1d9;font-size:12px;font-family:inherit">
        <button class="btn" onclick="setInstruction()">Set</button>
      </div>
    </div>

    <!-- Permissions tab -->
    <div id="tab-permissions" class="tab-content" style="overflow-y:auto;padding:12px;display:none">
      <div id="perm-panels"></div>
    </div>

    <!-- Harness tab -->
    <div id="tab-harness" class="tab-content" style="overflow-y:auto;padding:12px;display:none">
      <h3>Harness</h3>
      <div class="btn-row" style="margin-bottom:12px">
        <button class="btn" id="harness-toggle-btn" onclick="toggleHarness()">Toggle ON/OFF</button>
        <button class="btn" onclick="sendCmd('harness_state',{})">Refresh State</button>
      </div>

      <div id="approval-panel" style="display:none;background:#1c1d21;border:1px solid #d29922;border-radius:6px;padding:10px;margin-bottom:12px">
        <div style="font-size:11px;color:#d29922;text-transform:uppercase;margin-bottom:6px">Approval Required</div>
        <div id="approval-detail" style="font-size:12px;margin-bottom:8px"></div>
        <div class="btn-row">
          <button class="btn" style="border-color:#3fb950;color:#3fb950" onclick="sendCmd('harness_approve',{})">Approve</button>
          <button class="btn btn-danger" onclick="sendCmd('harness_reject',{})">Reject</button>
        </div>
      </div>

      <h3>Obligations</h3>
      <div id="obligations-list"><span class="empty-state">None yet</span></div>

      <h3>Activity Feed</h3>
      <div id="harness-feed">
        <div class="empty-state" id="harness-empty">No events yet</div>
      </div>
    </div>
  </div>
</div>

<script>
const conv = document.getElementById('conversation');
const emptyMsg = document.getElementById('empty-msg');
const harnessEmpty = document.getElementById('harness-empty');
const harnessFeed = document.getElementById('harness-feed');

let autoScroll = true;
let streamBuf = null;

// Track scroll position — stop auto-scroll if user scrolls up
conv.addEventListener('scroll', () => {
  const atBottom = conv.scrollHeight - conv.scrollTop - conv.clientHeight < 40;
  autoScroll = atBottom;
});

function scrollToBottom() {
  if (autoScroll) conv.scrollTop = conv.scrollHeight;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

function renderContent(text) {
  // Simple code block detection
  return escapeHtml(text).replace(
    /```(\\w*)\\n([\\s\\S]*?)```/g,
    '<code style="display:block;background:#0d1117;padding:8px;border-radius:4px;margin:4px 0;overflow-x:auto">$2</code>'
  );
}

function appendMessage(msg) {
  emptyMsg.style.display = 'none';

  const div = document.createElement('div');
  const role = msg.role;
  const kind = (msg.metadata && msg.metadata.kind) || '';

  let cls = 'msg';
  if (role === 'agent') cls += ' msg-agent-' + msg.author;
  else if (role === 'moderator') cls += kind === 'topic' ? ' msg-topic' : ' msg-moderator';
  else cls += ' msg-system';
  div.className = cls;

  let header = '';
  if (role === 'agent') {
    const turn = msg.metadata && msg.metadata.turn;
    const model = msg.metadata && msg.metadata.model || '';
    const provider = msg.metadata && msg.metadata.provider || '';
    const modelTag = model ? ' <span style="color:#484f58;font-size:11px">[' + escapeHtml(model) + ']</span>' : '';
    header = '<span class="turn">Turn ' + turn + '</span> <span class="author">' + escapeHtml(msg.author) + '</span>' + modelTag;
  } else if (role === 'moderator') {
    header = '<span class="author">' + escapeHtml(msg.author) + '</span>' + (kind === 'topic' ? ' <span style="color:#bc8cff">TOPIC</span>' : '');
  } else {
    const sKind = kind || 'system';
    header = '<span style="color:#f85149">' + sKind.toUpperCase() + '</span>';
  }

  div.innerHTML = '<div class="msg-header">' + header + '</div><div class="msg-body">' + renderContent(msg.content) + '</div>';
  conv.appendChild(div);
  scrollToBottom();
}

function startStream() {
  if (streamBuf) return;
  streamBuf = document.createElement('div');
  streamBuf.id = 'stream-buffer';
  streamBuf.className = 'active';
  streamBuf.innerHTML = '<span id="stream-cursor"></span>';
  conv.appendChild(streamBuf);
}

function appendToStream(chunk) {
  if (!streamBuf) startStream();
  const cursor = streamBuf.querySelector('#stream-cursor');
  streamBuf.insertBefore(document.createTextNode(chunk), cursor);
  scrollToBottom();
}

function finalizeStream() {
  if (streamBuf) {
    streamBuf.remove();
    streamBuf = null;
  }
}

function appendHarnessEvent(data) {
  harnessEmpty.style.display = 'none';
  const div = document.createElement('div');
  div.className = 'harness-event';

  const decClass = 'decision-' + (data.decision || 'allow');
  const blockers = (data.blockers || []).join(', ') || 'none';
  div.innerHTML =
    '<span class="decision-badge ' + decClass + '">' + escapeHtml(data.decision || '?') + '</span>' +
    '<strong>' + escapeHtml(data.action_type || '?') + '</strong>' +
    ' <span style="color:#8b949e">T' + (data.turn || '?') + ' ' + escapeHtml(data.speaker || data.agent || '') + '</span>' +
    (data.decision !== 'allow' ? '<div style="font-size:11px;color:#8b949e;margin-top:2px">' + escapeHtml(blockers) + '</div>' : '');
  harnessFeed.appendChild(div);
}

// --- Thinking indicator + tool activity ---
const thinkingBar = document.getElementById('thinking-bar');
const thinkingText = document.getElementById('thinking-text');
const thinkingElapsed = document.getElementById('thinking-elapsed');
const toolActivity = document.getElementById('tool-activity');
let thinkingTimer = null;
let thinkingStart = 0;
let currentToolName = '';

function showThinking(agent, turn, provider) {
  thinkingBar.className = 'active';
  thinkingText.textContent = agent + ' is thinking... (T' + turn + ', ' + provider + ')';
  toolActivity.innerHTML = '';
  currentToolName = '';
  thinkingStart = Date.now();
  if (thinkingTimer) clearInterval(thinkingTimer);
  thinkingTimer = setInterval(() => {
    const secs = Math.floor((Date.now() - thinkingStart) / 1000);
    thinkingElapsed.textContent = secs + 's';
  }, 1000);
  thinkingElapsed.textContent = '0s';
}

function hideThinking() {
  thinkingBar.className = '';
  toolActivity.innerHTML = '';
  currentToolName = '';
  if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
}

function handleToolEvent(data) {
  if (!thinkingBar.classList.contains('active')) {
    thinkingBar.className = 'active';
  }
  const evt = data.event;
  if (evt === 'tool_start') {
    currentToolName = data.tool || '?';
    const inputPreview = data.input ? ' ' + data.input.slice(0, 80) : '';
    const line = document.createElement('div');
    line.className = 'tool-line';
    line.innerHTML = '→ <span class="tool-name">' + escapeHtml(currentToolName) + '</span>' +
      (inputPreview ? '<span class="tool-input"> ' + escapeHtml(inputPreview) + '</span>' : '');
    line.id = 'tool-current';
    toolActivity.appendChild(line);
    toolActivity.scrollTop = toolActivity.scrollHeight;
    thinkingText.textContent = (data.agent || '') + ' using ' + currentToolName;
  } else if (evt === 'tool_end') {
    const cur = document.getElementById('tool-current');
    if (cur) cur.removeAttribute('id');
    currentToolName = '';
  } else if (evt === 'reasoning') {
    const line = document.createElement('div');
    line.className = 'tool-line';
    line.style.color = '#d29922';
    line.innerHTML = '💭 <span style="color:#8b949e">' + escapeHtml(data.text || '') + '</span>';
    toolActivity.appendChild(line);
    toolActivity.scrollTop = toolActivity.scrollHeight;
    thinkingText.textContent = (data.agent || '') + ' reasoning...';
  } else if (evt === 'usage') {
    const line = document.createElement('div');
    line.className = 'tool-line';
    line.style.color = '#8b949e';
    const u = data.usage || {};
    line.textContent = 'usage: ' + (u.input_tokens || '?') + ' in / ' + (u.output_tokens || '?') + ' out';
    toolActivity.appendChild(line);
  }
}

function updateStatus(data) {
  document.getElementById('s-turn').textContent = data.turn || 0;
  document.getElementById('s-agent').textContent = data.agent || '---';
  const badge = document.getElementById('s-badge');
  badge.textContent = (data.status || 'starting').toUpperCase();
  badge.className = 'status-badge status-' + (data.status || 'starting');
}

// SSE connection
const evtSource = new EventSource('/events');

evtSource.addEventListener('message', (e) => {
  hideThinking();
  appendMessage(JSON.parse(e.data));
});

evtSource.addEventListener('stream', (e) => {
  hideThinking();
  appendToStream(JSON.parse(e.data).chunk);
});

evtSource.addEventListener('stream_end', () => {
  finalizeStream();
});

evtSource.addEventListener('status', (e) => {
  updateStatus(JSON.parse(e.data));
});

evtSource.addEventListener('policy', (e) => {
  appendHarnessEvent(JSON.parse(e.data));
});

evtSource.addEventListener('activity', (e) => {
  handleActivity(JSON.parse(e.data));
});

evtSource.onerror = () => {
  const badge = document.getElementById('s-badge');
  badge.textContent = 'DISCONNECTED';
  badge.className = 'status-badge status-starting';
};

// --- Tabs ---
function showTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.style.display = 'none');
  event.target.classList.add('active');
  document.getElementById('tab-' + name).style.display = 'block';
}

// --- Toast ---
function showToast(msg) {
  const c = document.getElementById('toast-container');
  const d = document.createElement('div');
  d.className = 'toast';
  d.textContent = msg;
  c.appendChild(d);
  setTimeout(() => d.remove(), 3000);
}

// --- Controls ---
async function sendControl(cmd) {
  await fetch('/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command: cmd}),
  });
}

async function sendCmd(command, params) {
  await fetch('/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command, params}),
  });
}

async function sendMessage() {
  const input = document.getElementById('inject-text');
  const target = document.getElementById('msg-target').value;
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const prefix = target !== 'both' ? '[To ' + target + '] ' : '';
  await fetch('/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: prefix + text}),
  });
}

function setTimeoutVal() {
  const secs = parseInt(document.getElementById('timeout-input').value);
  if (secs > 0) sendCmd('set_timeout', {seconds: secs});
}

function setInstruction() {
  const agent = document.getElementById('instr-agent').value;
  const text = document.getElementById('instr-text').value.trim();
  if (agent && text) sendCmd('set_instruction', {agent, instruction: text});
}

function toggleHarness() {
  // Toggle — we track current state loosely
  const btn = document.getElementById('harness-toggle-btn');
  const enabling = btn.textContent.includes('ON');
  sendCmd('harness_toggle', {enabled: enabling});
  btn.textContent = enabling ? 'Turn OFF' : 'Turn ON';
}

// --- Permissions ---
const TOOLS = ['Bash', 'Edit', 'Write', 'Read', 'Glob', 'Grep'];
const toolState = {};  // {agent: {tool: true/false}}

function buildPermPanels(agents) {
  const container = document.getElementById('perm-panels');
  container.innerHTML = '';
  agents.forEach(agent => {
    if (!toolState[agent]) {
      toolState[agent] = {};
      TOOLS.forEach(t => toolState[agent][t] = true);
    }
    const div = document.createElement('div');
    div.style.marginBottom = '16px';
    div.innerHTML = '<h3>' + escapeHtml(agent) + '</h3>';
    const row = document.createElement('div');
    TOOLS.forEach(tool => {
      const btn = document.createElement('button');
      btn.className = 'tool-toggle ' + (toolState[agent][tool] ? 'allowed' : 'denied');
      btn.textContent = tool;
      btn.onclick = () => {
        const allowed = toolState[agent][tool];
        toolState[agent][tool] = !allowed;
        btn.className = 'tool-toggle ' + (!allowed ? 'allowed' : 'denied');
        sendCmd(allowed ? 'deny_tool' : 'allow_tool', {agent, tool});
      };
      row.appendChild(btn);
    });
    div.appendChild(row);

    // Permission mode
    const modeDiv = document.createElement('div');
    modeDiv.style.marginTop = '8px';
    modeDiv.innerHTML = '<span style="font-size:11px;color:#8b949e">Mode: </span>';
    ['auto', 'dangerously-skip-permissions'].forEach(mode => {
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.style.fontSize = '10px';
      btn.style.padding = '2px 6px';
      btn.textContent = mode === 'auto' ? 'Auto' : 'Skip Perms';
      btn.onclick = () => sendCmd('set_permission_mode', {agent, mode});
      modeDiv.appendChild(btn);
    });
    div.appendChild(modeDiv);
    container.appendChild(div);
  });
}

// --- Steering buttons ---
function buildSteeringBtns(agents) {
  const container = document.getElementById('steering-btns');
  agents.forEach(agent => {
    const skip = document.createElement('button');
    skip.className = 'btn';
    skip.textContent = 'Skip ' + agent;
    skip.onclick = () => sendCmd('skip', {agent});
    container.appendChild(skip);
    const force = document.createElement('button');
    force.className = 'btn';
    force.textContent = 'Force ' + agent;
    force.onclick = () => sendCmd('force_next', {agent});
    container.appendChild(force);
  });

  // Instruction agent selector
  const sel = document.getElementById('instr-agent');
  agents.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a;
    opt.textContent = a;
    sel.appendChild(opt);
  });
}

// --- Obligations ---
function updateObligations(obligations) {
  const list = document.getElementById('obligations-list');
  if (!obligations || obligations.length === 0) {
    list.innerHTML = '<span class="empty-state">None</span>';
    return;
  }
  list.innerHTML = '';
  obligations.forEach(ob => {
    const div = document.createElement('div');
    div.className = 'obl-item';
    div.innerHTML =
      '<span class="obl-status obl-' + ob.status + '">' + ob.status.toUpperCase() + '</span>' +
      '<span style="flex:1">' + escapeHtml(ob.kind) + '</span>';
    if (ob.status === 'open') {
      const sBtn = document.createElement('button');
      sBtn.className = 'btn';
      sBtn.style.cssText = 'font-size:10px;padding:1px 5px;border-color:#3fb950;color:#3fb950';
      sBtn.textContent = 'OK';
      sBtn.title = 'Mark satisfied';
      sBtn.onclick = () => sendCmd('obligation_satisfy', {obligation_id: ob.obligation_id});
      div.appendChild(sBtn);
      const bBtn = document.createElement('button');
      bBtn.className = 'btn';
      bBtn.style.cssText = 'font-size:10px;padding:1px 5px;border-color:#f85149;color:#f85149';
      bBtn.textContent = 'X';
      bBtn.title = 'Mark breached';
      bBtn.onclick = () => sendCmd('obligation_breach', {obligation_id: ob.obligation_id});
      div.appendChild(bBtn);
    }
    list.appendChild(div);
  });
}

// --- Approval panel ---
function showApproval(data) {
  const panel = document.getElementById('approval-panel');
  const detail = document.getElementById('approval-detail');
  panel.style.display = 'block';
  detail.innerHTML =
    '<strong>' + escapeHtml(data.agent) + '</strong> T' + data.turn +
    ' (' + escapeHtml(data.action_type || '?') + ')<br>' +
    '<pre style="margin-top:6px;font-size:11px;color:#8b949e;max-height:120px;overflow:auto">' +
    escapeHtml(data.response_preview || '') + '</pre>';
  // Auto-switch to harness tab
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.style.display = 'none');
  document.querySelectorAll('.tab-btn')[2].classList.add('active');
  document.getElementById('tab-harness').style.display = 'block';
}

function hideApproval() {
  document.getElementById('approval-panel').style.display = 'none';
}

// --- Activity handler (extended) ---
function handleActivity(data) {
  const kind = data.kind;
  if (kind === 'thinking') {
    showThinking(data.agent, data.turn, data.provider);
  } else if (kind === 'harness_eval') {
    hideThinking();
    appendHarnessEvent(data);
  } else if (kind === 'turn_committed') {
    hideThinking();
    harnessEmpty.style.display = 'none';
    const div = document.createElement('div');
    div.className = 'harness-event';
    div.innerHTML =
      '<span class="decision-badge decision-allow">OK</span>' +
      '<strong>' + escapeHtml(data.action_type || '?') + '</strong>' +
      ' <span style="color:#8b949e">T' + (data.turn || '?') + ' ' + escapeHtml(data.agent || '') + '</span>';
    harnessFeed.appendChild(div);
  } else if (kind === 'command_processed') {
    showToast(data.command + ' applied');
  } else if (kind === 'approval_needed') {
    showApproval(data);
  } else if (kind === 'approval_accepted' || kind === 'approval_rejected') {
    hideApproval();
    showToast(kind === 'approval_accepted' ? 'Turn approved' : 'Turn rejected');
  } else if (kind === 'turn_skipped') {
    showToast(data.agent + ' skipped on T' + data.turn);
  } else if (kind === 'harness_state') {
    if (data.data && data.data.obligations) {
      updateObligations(data.data.obligations);
    }
  } else if (kind === 'tool_event') {
    handleToolEvent(data);
  } else if (kind === 'warning') {
    showToast('WARNING: ' + (data.message || 'unknown'));
  } else if (kind === 'agent_failed') {
    hideThinking();
    showToast(data.agent + ' failed: ' + (data.failure_reason || data.failure_type || 'unknown'));
    // Add to harness feed for visibility
    harnessEmpty.style.display = 'none';
    const div = document.createElement('div');
    div.className = 'harness-event';
    div.innerHTML =
      '<span class="decision-badge decision-block">FAIL</span>' +
      '<strong>' + escapeHtml(data.agent || '?') + '</strong> T' + (data.turn || '?') +
      '<div style="font-size:11px;color:#f85149;margin-top:2px">' +
      escapeHtml(data.failure_type || '') + ': ' + escapeHtml(data.failure_reason || '') + '</div>';
    harnessFeed.appendChild(div);
  }
}

// Initialize — build controls immediately from state
fetch('/state').then(r => r.json()).then(data => {
  document.getElementById('s-session').textContent = data.session_id || '---';
  updateStatus(data);
  // Populate lobby
  document.getElementById('lobby-topic').textContent = data.topic || '';
  if (data.status !== 'lobby') {
    document.getElementById('lobby').style.display = 'none';
  }
  // Build controls from agent info
  if (data.agents && data.agents.length) {
    agentInfos = data.agents;
    data.agents.forEach(a => registerAgent(a.name));
    buildModelControls(data.agents);

    // Configure lobby for each agent
    function setupLobbyAgent(agent, side) {
      const label = document.getElementById('lobby-' + side + '-label');
      const mSel = document.getElementById('lobby-' + side + '-model');
      const eLabel = document.getElementById('lobby-' + side + '-effort-label');
      const eSel = document.getElementById('lobby-' + side + '-effort');

      const iLabel = document.getElementById('lobby-' + side + '-instr-label');
      if (label) label.textContent = agent.name + ' Model';
      if (eLabel) eLabel.textContent = agent.name + ' Effort';
      if (iLabel) iLabel.textContent = agent.name + ' Instruction';

      if (agent.provider === 'cli-claude') {
        mSel.innerHTML = CLAUDE_MODELS.map(m => '<option value="' + m + '">' + m + '</option>').join('');
        eSel.innerHTML = CLAUDE_EFFORTS.map(e => '<option value="' + e + '">' + e + '</option>').join('');
      } else if (agent.provider === 'cli-codex') {
        mSel.innerHTML = CODEX_MODELS.map(m => '<option value="' + m + '">' + m + '</option>').join('');
        eSel.innerHTML = CODEX_EFFORTS.map(e => '<option value="' + e + '">' + e + '</option>').join('');
      } else {
        mSel.innerHTML = '<option value="mirror">mirror</option>';
        eSel.style.display = 'none';
      }
      // Select current model if known
      if (agent.model) {
        for (const opt of mSel.options) {
          if (opt.value === agent.model) { opt.selected = true; break; }
        }
      }
    }
    if (data.agents[0]) setupLobbyAgent(data.agents[0], 'left');
    if (data.agents[1]) setupLobbyAgent(data.agents[1], 'right');
  }
});

// --- Lobby ---
let lobbyTimer = null;
let lobbyCountdown = 30;

function startLobbyCountdown() {
  lobbyCountdown = 30;
  document.getElementById('lobby-countdown').textContent = lobbyCountdown;
  lobbyTimer = setInterval(() => {
    lobbyCountdown--;
    document.getElementById('lobby-countdown').textContent = lobbyCountdown;
    if (lobbyCountdown <= 0) {
      clearInterval(lobbyTimer);
      lobbyStart();
    }
  }, 1000);
}

function lobbyCancelAuto() {
  if (lobbyTimer) { clearInterval(lobbyTimer); lobbyTimer = null; }
  document.getElementById('lobby-countdown').textContent = '--';
}

async function lobbyStart() {
  if (lobbyTimer) { clearInterval(lobbyTimer); lobbyTimer = null; }
  document.getElementById('lobby').style.display = 'none';
  const overrides = {};
  const turns = document.getElementById('lobby-turns').value;
  if (turns) overrides.turns = parseInt(turns);
  const leftInstr = document.getElementById('lobby-left-instr').value.trim();
  if (leftInstr) overrides.left_instruction = leftInstr;
  const rightInstr = document.getElementById('lobby-right-instr').value.trim();
  if (rightInstr) overrides.right_instruction = rightInstr;
  overrides.left_model = document.getElementById('lobby-left-model').value;
  overrides.right_model = document.getElementById('lobby-right-model').value;
  overrides.left_effort = document.getElementById('lobby-left-effort').value;
  overrides.right_effort = document.getElementById('lobby-right-effort').value;
  await fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(overrides),
  });
}

// Start countdown when lobby is visible
if (document.getElementById('lobby').style.display !== 'none') {
  startLobbyCountdown();
}

// Hide lobby when status changes from lobby
evtSource.addEventListener('status', (e) => {
  const data = JSON.parse(e.data);
  if (data.status !== 'lobby') {
    document.getElementById('lobby').style.display = 'none';
    if (lobbyTimer) { clearInterval(lobbyTimer); lobbyTimer = null; }
  }
});

// Track agents from messages AND activity events
const knownAgents = new Set();
let controlsBuilt = false;
let agentInfos = [];

function registerAgent(name) {
  if (!name || knownAgents.has(name)) return;
  knownAgents.add(name);
  // Add to message target dropdown
  const msgTarget = document.getElementById('msg-target');
  const opt = document.createElement('option');
  opt.value = name;
  opt.textContent = 'To: ' + name;
  msgTarget.appendChild(opt);
  if (knownAgents.size >= 2 && !controlsBuilt) {
    controlsBuilt = true;
    buildSteeringBtns([...knownAgents]);
    buildPermPanels([...knownAgents]);
    if (!agentInfos.length) {
      // Fallback: no provider info, show generic controls
      buildModelControls([...knownAgents].map(n => ({name: n, provider: 'unknown'})));
    }
  }
}

const CLAUDE_MODELS = ['opus', 'sonnet', 'haiku'];
const CODEX_MODELS = ['gpt-5.4', 'gpt-5.4-mini', 'gpt-5.3-codex', 'gpt-5.2-codex', 'gpt-5.2', 'gpt-5.1-codex-max', 'gpt-5.1-codex-mini'];
const CLAUDE_EFFORTS = ['max', 'high', 'medium', 'low'];
const CODEX_EFFORTS = ['xhigh', 'high', 'medium', 'low', 'minimal', 'none'];

function buildModelControls(agentInfos) {
  const container = document.getElementById('model-controls');
  container.innerHTML = '';
  agentInfos.forEach(info => {
    const isClaude = info.provider === 'cli-claude';
    const isCodex = info.provider === 'cli-codex';
    const models = isClaude ? CLAUDE_MODELS : isCodex ? CODEX_MODELS : [];
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px;align-items:center;margin-bottom:6px;font-size:12px';
    row.innerHTML = '<span style="width:50px;color:#8b949e">' + escapeHtml(info.name) + '</span>';
    if (models.length) {
      const mSel = document.createElement('select');
      mSel.style.cssText = 'padding:3px 6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:11px';
      models.forEach(m => {
        const o = document.createElement('option');
        o.value = m; o.textContent = m;
        if (m === info.model) o.selected = true;
        mSel.appendChild(o);
      });
      mSel.onchange = () => sendCmd('set_model', {agent: info.name, model: mSel.value});
      row.appendChild(mSel);
    }
    const efforts = isClaude ? CLAUDE_EFFORTS : isCodex ? CODEX_EFFORTS : [];
    if (efforts.length) {
      const eSel = document.createElement('select');
      eSel.style.cssText = 'padding:3px 6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:11px';
      efforts.forEach(e => {
        const o = document.createElement('option');
        o.value = e; o.textContent = e;
        eSel.appendChild(o);
      });
      eSel.onchange = () => sendCmd('set_effort', {agent: info.name, effort: eSel.value});
      row.appendChild(eSel);
    }
    if (!isClaude && !isCodex) {
      row.innerHTML += '<span style="color:#484f58;font-size:11px">(mock)</span>';
    }
    container.appendChild(row);
  });
}

const origAppendMessage = appendMessage;
appendMessage = function(msg) {
  if (msg.role === 'agent') registerAgent(msg.author);
  origAppendMessage(msg);
};

const origHandleActivity = handleActivity;
handleActivity = function(data) {
  if (data.agent) registerAgent(data.agent);
  origHandleActivity(data);
};
</script>
</body>
</html>
"""
