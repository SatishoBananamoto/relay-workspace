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
    ) -> None:
        self._queue = moderator_queue
        self._bus = EventBus()
        self._session_id = session_id
        self._topic = topic
        self._host = host
        self._port = port
        self._status = "starting"
        self._current_turn = 0
        self._current_agent = "---"
        self._streaming = False
        self._server: ThreadingHTTPServer | None = None

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

        # Update thinking state
        kind = activity.get("kind")
        if kind == "thinking":
            self._current_agent = activity.get("agent", self._current_agent)
            self._current_turn = activity.get("turn", self._current_turn)

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
            **self._status_dict(),
        }

    def _status_dict(self) -> dict:
        return {
            "status": self._status,
            "turn": self._current_turn,
            "agent": self._current_agent,
        }

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
) -> object:
    """Run the relay engine in a background thread with web viewer in foreground.

    Mirrors run_relay_with_tui: engine in thread, viewer blocks main thread.
    """
    viewer = WebViewer(
        moderator_queue=moderator_queue,
        session_id=session_id,
        topic=topic,
        host=host,
        port=port,
    )

    result_holder: list = []

    def engine_thread():
        try:
            runner = runner_factory(
                moderator_queue=moderator_queue,
                on_commit=viewer.on_commit,
                on_stream_chunk=viewer.on_stream_chunk,
                on_activity=viewer.on_activity,
            )
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

  /* Thinking indicator */
  #thinking-bar {
    display: none;
    padding: 8px 16px;
    background: #161b22;
    border-bottom: 1px solid #30363d;
    font-size: 12px;
    color: #8b949e;
    flex-shrink: 0;
  }
  #thinking-bar.active { display: flex; align-items: center; gap: 8px; }
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

<div id="toast-container"></div>

<div id="thinking-bar">
  <span class="thinking-dot"></span>
  <span class="thinking-dot"></span>
  <span class="thinking-dot"></span>
  <span id="thinking-text">---</span>
  <span id="thinking-elapsed"></span>
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

      <h3>Steering</h3>
      <div class="btn-row" id="steering-btns"></div>
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <input type="number" id="timeout-input" value="600" min="30" max="3600" style="width:70px;padding:4px 6px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;font-family:inherit">
        <button class="btn" onclick="setTimeoutVal()">Set Timeout</button>
      </div>

      <h3>Inject Message</h3>
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
    header = '<span class="turn">Turn ' + turn + '</span> <span class="author">' + escapeHtml(msg.author) + '</span>';
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

// --- Thinking indicator ---
const thinkingBar = document.getElementById('thinking-bar');
const thinkingText = document.getElementById('thinking-text');
const thinkingElapsed = document.getElementById('thinking-elapsed');
let thinkingTimer = null;
let thinkingStart = 0;

function showThinking(agent, turn, provider) {
  thinkingBar.className = 'active';
  thinkingText.textContent = agent + ' is thinking... (T' + turn + ', ' + provider + ')';
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
  if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
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
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  await fetch('/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: text}),
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
  }
}

// Initialize with agent names from state
fetch('/state').then(r => r.json()).then(data => {
  document.getElementById('s-session').textContent = data.session_id || '---';
  updateStatus(data);
  // We don't know agent names from state alone — they'll come from messages
});

// Track agents from messages
const knownAgents = new Set();
const origAppendMessage = appendMessage;
appendMessage = function(msg) {
  if (msg.role === 'agent' && !knownAgents.has(msg.author)) {
    knownAgents.add(msg.author);
    if (knownAgents.size <= 2) {
      buildSteeringBtns([...knownAgents]);
      buildPermPanels([...knownAgents]);
    }
  }
  origAppendMessage(msg);
};
</script>
</body>
</html>
"""
