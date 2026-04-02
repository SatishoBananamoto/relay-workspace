"""Tests for workspace manager."""

from __future__ import annotations

import time
from pathlib import Path

from relay_discussion.workspace import WorkspaceManager


def test_setup_creates_directory_structure(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()

    assert (tmp_path / "workspace" / "shared").is_dir()
    assert (tmp_path / "workspace" / "reviews").is_dir()
    assert (tmp_path / "workspace" / "claude" / "inbox.md").exists()
    assert (tmp_path / "workspace" / "claude" / "outbox.md").exists()
    assert (tmp_path / "workspace" / "codex" / "inbox.md").exists()
    assert (tmp_path / "workspace" / "codex" / "outbox.md").exists()


def test_setup_custom_agent_names(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup(left_name="Alpha", right_name="Beta")

    assert (tmp_path / "workspace" / "alpha" / "inbox.md").exists()
    assert (tmp_path / "workspace" / "beta" / "inbox.md").exists()


def test_setup_idempotent(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    # Write something to inbox
    (tmp_path / "workspace" / "claude" / "inbox.md").write_text("hello")
    # Setup again — should not overwrite existing files
    ws.setup()
    assert (tmp_path / "workspace" / "claude" / "inbox.md").read_text() == "hello"


def test_workspace_summary_empty(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    summary = ws.workspace_summary()
    # Only empty inbox/outbox files, so should list them
    assert "inbox.md" in summary or "empty" in summary.lower()


def test_workspace_summary_with_files(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    (tmp_path / "workspace" / "shared" / "main.py").write_text("print('hello')")
    summary = ws.workspace_summary()
    assert "main.py" in summary


def test_workspace_summary_shows_recent_file_content(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    (tmp_path / "workspace" / "shared" / "app.py").write_text("def run():\n    return 42\n")
    summary = ws.workspace_summary()
    assert "def run" in summary


def test_get_file_changes_since(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()

    before = time.time()
    time.sleep(0.05)
    (tmp_path / "workspace" / "shared" / "new.py").write_text("new file")

    changes = ws.get_file_changes_since(before)
    paths = [c.path for c in changes]
    assert any("new.py" in p for p in paths)


def test_get_file_changes_empty_when_no_changes(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()

    future_time = time.time() + 1000
    changes = ws.get_file_changes_since(future_time)
    assert len(changes) == 0


def test_read_inbox_returns_and_clears(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    (tmp_path / "workspace" / "claude" / "inbox.md").write_text("Review this code")

    content = ws.read_inbox("Claude")
    assert content == "Review this code"

    # Second read should be empty
    assert ws.read_inbox("Claude") == ""


def test_read_inbox_empty(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    assert ws.read_inbox("Claude") == ""


def test_write_outbox(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    ws.write_outbox("Claude", "Here are my changes")

    content = (tmp_path / "workspace" / "claude" / "outbox.md").read_text()
    assert content == "Here are my changes"


def test_consume_outbox_returns_and_clears(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    ws.write_outbox("Claude", "Ship the parser change")

    content = ws.consume_outbox("Claude")

    assert content == "Ship the parser change"
    assert (tmp_path / "workspace" / "claude" / "outbox.md").read_text() == ""


def test_append_inbox_preserves_existing_messages(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    ws.append_inbox("Codex", "[Claude]: First note")
    ws.append_inbox("Codex", "[Claude]: Second note")

    content = (tmp_path / "workspace" / "codex" / "inbox.md").read_text()

    assert "[Claude]: First note" in content
    assert "[Claude]: Second note" in content


def test_forward_outbox_moves_message_to_peer_inbox(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.setup()
    ws.write_outbox("Claude", "Please review shared/main.py")

    forwarded = ws.forward_outbox("Claude", "Codex")

    assert forwarded == "Please review shared/main.py"
    assert (tmp_path / "workspace" / "claude" / "outbox.md").read_text() == ""
    inbox = (tmp_path / "workspace" / "codex" / "inbox.md").read_text()
    assert "[Claude]: Please review shared/main.py" in inbox


def test_mark_checkpoint(tmp_path: Path):
    ws = WorkspaceManager(tmp_path / "workspace")
    assert ws.last_check_time == 0.0
    ws.mark_checkpoint()
    assert ws.last_check_time > 0.0


def test_workspace_summary_nonexistent_dir():
    ws = WorkspaceManager(Path("/nonexistent/workspace"))
    assert "empty" in ws.workspace_summary().lower()
