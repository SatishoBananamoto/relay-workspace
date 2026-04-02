"""Tests for the web viewer: EventBus, WebViewer callbacks, HTTP integration."""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from relay_discussion.models import Message
from relay_discussion.moderator import ModeratorInputQueue
from relay_discussion.web import EventBus, WebViewer, run_relay_with_web


# ---------------------------------------------------------------------------
# EventBus tests
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_publish_to_subscriber(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.publish({"type": "test", "data": "hello"})
        event = q.get(timeout=1)
        assert event == {"type": "test", "data": "hello"}

    def test_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish({"type": "msg", "data": "x"})
        assert q1.get(timeout=1)["data"] == "x"
        assert q2.get(timeout=1)["data"] == "x"

    def test_history_replay(self):
        bus = EventBus()
        bus.publish({"type": "a", "data": 1})
        bus.publish({"type": "b", "data": 2})
        # Late subscriber gets history
        q = bus.subscribe()
        assert q.get(timeout=1)["data"] == 1
        assert q.get(timeout=1)["data"] == 2

    def test_unsubscribe(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish({"type": "msg", "data": "x"})
        assert q.empty()

    def test_unsubscribe_nonexistent(self):
        bus = EventBus()
        q = queue.SimpleQueue()
        bus.unsubscribe(q)  # should not raise

    def test_history_count(self):
        bus = EventBus()
        assert bus.history_count == 0
        bus.publish({"type": "a"})
        bus.publish({"type": "b"})
        assert bus.history_count == 2

    def test_subscriber_count(self):
        bus = EventBus()
        assert bus.subscriber_count == 0
        q1 = bus.subscribe()
        assert bus.subscriber_count == 1
        q2 = bus.subscribe()
        assert bus.subscriber_count == 2
        bus.unsubscribe(q1)
        assert bus.subscriber_count == 1

    def test_thread_safety(self):
        """Concurrent publish and subscribe don't crash."""
        bus = EventBus()
        errors = []

        def publisher():
            try:
                for i in range(100):
                    bus.publish({"type": "msg", "data": i})
            except Exception as e:
                errors.append(e)

        def subscriber():
            try:
                q = bus.subscribe()
                count = 0
                while count < 50:
                    try:
                        q.get(timeout=0.1)
                        count += 1
                    except Exception:
                        pass
                bus.unsubscribe(q)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=publisher),
            threading.Thread(target=subscriber),
            threading.Thread(target=subscriber),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors


# ---------------------------------------------------------------------------
# WebViewer callback tests
# ---------------------------------------------------------------------------

def _msg(role="agent", author="Claude", content="hello", metadata=None, seq=1):
    return Message(
        seq=seq, timestamp="2025-01-01T00:00:00Z",
        role=role, author=author, content=content,
        metadata=metadata or {},
    )


class TestWebViewerCallbacks:
    def setup_method(self):
        self.mq = ModeratorInputQueue()
        self.viewer = WebViewer(self.mq, session_id="test-123", topic="Test topic")
        self.q = self.viewer.bus.subscribe()

    def _drain(self) -> list[dict]:
        events = []
        while True:
            try:
                events.append(self.q.get(timeout=0.05))
            except Exception:
                break
        return events

    def test_agent_message_publishes_message_and_status(self):
        msg = _msg(metadata={"turn": 1, "provider": "mock", "model": "mirror"})
        self.viewer.on_commit(msg)
        events = self._drain()
        types = [e["type"] for e in events]
        assert "message" in types
        assert "status" in types
        msg_event = next(e for e in events if e["type"] == "message")
        assert msg_event["data"]["author"] == "Claude"
        assert msg_event["data"]["content"] == "hello"

    def test_moderator_message(self):
        msg = _msg(role="moderator", author="Satisho", content="Push harder.", metadata={"kind": "interjection"})
        self.viewer.on_commit(msg)
        events = self._drain()
        msg_event = next(e for e in events if e["type"] == "message")
        assert msg_event["data"]["role"] == "moderator"

    def test_policy_gate_publishes_policy_event(self):
        msg = _msg(
            role="system", author="relay",
            content="Blocked Claude turn 1: permission denied",
            metadata={
                "kind": "policy_gate",
                "speaker": "Claude",
                "decision": "block",
                "action_type": "request_permission",
                "blockers": ["Permission not allowed"],
                "turn": 1,
            },
        )
        self.viewer.on_commit(msg)
        events = self._drain()
        types = [e["type"] for e in events]
        assert "policy" in types
        policy = next(e for e in events if e["type"] == "policy")
        assert policy["data"]["decision"] == "block"
        assert policy["data"]["action_type"] == "request_permission"

    def test_pause_updates_status(self):
        msg = _msg(
            role="system", author="relay",
            content="Paused",
            metadata={"kind": "pause"},
        )
        self.viewer.on_commit(msg)
        events = self._drain()
        status = next(e for e in events if e["type"] == "status")
        assert status["data"]["status"] == "paused"

    def test_stream_chunks(self):
        self.viewer.on_stream_chunk("Hello ")
        self.viewer.on_stream_chunk("world")
        events = self._drain()
        stream_events = [e for e in events if e["type"] == "stream"]
        assert len(stream_events) == 2
        assert stream_events[0]["data"]["chunk"] == "Hello "
        assert stream_events[1]["data"]["chunk"] == "world"

    def test_stream_end_on_commit(self):
        self.viewer.on_stream_chunk("partial...")
        self.viewer.on_commit(_msg(metadata={"turn": 1}))
        events = self._drain()
        types = [e["type"] for e in events]
        assert "stream_end" in types
        # stream_end should come before the message
        se_idx = types.index("stream_end")
        msg_idx = types.index("message")
        assert se_idx < msg_idx

    def test_get_state(self):
        state = self.viewer.get_state()
        assert state["session_id"] == "test-123"
        assert state["topic"] == "Test topic"
        assert state["status"] == "starting"

    def test_state_updates_after_agent_turn(self):
        self.viewer.on_commit(_msg(metadata={"turn": 3}))
        state = self.viewer.get_state()
        assert state["turn"] == 3
        assert state["agent"] == "Claude"
        assert state["status"] == "running"

    def test_update_status(self):
        self.viewer.update_status("done")
        events = self._drain()
        status = next(e for e in events if e["type"] == "status")
        assert status["data"]["status"] == "done"

    def test_activity_thinking(self):
        self.viewer.on_activity({
            "kind": "thinking",
            "agent": "Codex",
            "turn": 2,
            "provider": "cli-codex",
        })
        events = self._drain()
        activity = next(e for e in events if e["type"] == "activity")
        assert activity["data"]["kind"] == "thinking"
        assert activity["data"]["agent"] == "Codex"
        # State should update
        assert self.viewer.get_state()["agent"] == "Codex"
        assert self.viewer.get_state()["turn"] == 2

    def test_activity_harness_eval(self):
        self.viewer.on_activity({
            "kind": "harness_eval",
            "agent": "Claude",
            "turn": 1,
            "action_type": "produce_artifact",
            "decision": "allow",
            "allowed": True,
            "blockers": [],
        })
        events = self._drain()
        activity = next(e for e in events if e["type"] == "activity")
        assert activity["data"]["action_type"] == "produce_artifact"
        assert activity["data"]["decision"] == "allow"

    def test_activity_turn_committed(self):
        self.viewer.on_activity({
            "kind": "turn_committed",
            "agent": "Claude",
            "turn": 1,
            "action_type": "produce_artifact",
        })
        events = self._drain()
        activity = next(e for e in events if e["type"] == "activity")
        assert activity["data"]["kind"] == "turn_committed"


# ---------------------------------------------------------------------------
# HTTP integration tests
# ---------------------------------------------------------------------------

class TestHTTPIntegration:
    """Start the web viewer server and test HTTP endpoints."""

    @pytest.fixture(autouse=True)
    def _setup_server(self):
        self.mq = ModeratorInputQueue()
        self.viewer = WebViewer(
            self.mq, session_id="http-test", topic="HTTP test",
            host="127.0.0.1", port=0,  # port=0 lets OS pick a free port
        )
        # We need to start the server to get the actual port
        # Use a custom approach: start server in thread, grab port
        self.server_thread = None
        yield
        if self.viewer._server:
            self.viewer.exit()
        if self.server_thread:
            self.server_thread.join(timeout=3)

    def _start_server(self):
        """Start server in background thread and return the actual port."""
        started = threading.Event()
        actual_port = [0]

        def run():
            from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
            import json as _json
            import socket

            viewer = self.viewer

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.end_headers()
                        self.wfile.write(b"<html>ok</html>")
                    elif self.path == "/state":
                        body = _json.dumps(viewer.get_state()).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self.send_error(404)

                def do_POST(self):
                    if self.path == "/control":
                        length = int(self.headers.get("Content-Length", 0))
                        body = _json.loads(self.rfile.read(length)) if length else {}
                        from relay_discussion.moderator import parse_input
                        if "command" in body:
                            viewer._queue.put(parse_input(body["command"]))
                        elif "message" in body:
                            viewer._queue.put(parse_input(body["message"]))
                        resp = _json.dumps({"ok": True}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(resp)
                    else:
                        self.send_error(404)

                def log_message(self, fmt, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            actual_port[0] = server.server_address[1]
            self.viewer._server = server
            started.set()
            server.serve_forever()

        self.server_thread = threading.Thread(target=run, daemon=True)
        self.server_thread.start()
        started.wait(timeout=5)
        return actual_port[0]

    def test_get_index(self):
        port = self._start_server()
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
        assert resp.status == 200
        assert b"html" in resp.read()

    def test_get_state(self):
        port = self._start_server()
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/state")
        data = json.loads(resp.read())
        assert data["session_id"] == "http-test"
        assert data["topic"] == "HTTP test"

    def test_post_control(self):
        port = self._start_server()
        body = json.dumps({"command": "pause"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req)
        assert json.loads(resp.read())["ok"] is True

        # Verify command reached the queue
        entries = self.mq.drain()
        assert len(entries) == 1
        assert entries[0].command == "pause"

    def test_post_message(self):
        port = self._start_server()
        body = json.dumps({"message": "Push harder on the design."}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req)

        entries = self.mq.drain()
        assert len(entries) == 1
        assert entries[0].content == "Push harder on the design."
