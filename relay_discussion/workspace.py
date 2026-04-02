"""Shared workspace manager for build mode.

Manages a workspace directory where both agents can read/write code,
communicate via inbox/outbox files, and leave review artifacts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    size: int
    modified: float


class WorkspaceManager:
    """Manages the shared workspace for build-mode relay sessions."""

    def __init__(self, workspace_path: Path) -> None:
        self._path = workspace_path
        self._last_check_time: float = 0.0

    @property
    def path(self) -> Path:
        return self._path

    def setup(self, left_name: str = "claude", right_name: str = "codex") -> None:
        """Create workspace directory structure."""
        self._path.mkdir(parents=True, exist_ok=True)
        (self._path / "shared").mkdir(exist_ok=True)
        (self._path / "reviews").mkdir(exist_ok=True)

        for agent_name in (left_name.lower(), right_name.lower()):
            agent_dir = self._path / agent_name
            agent_dir.mkdir(exist_ok=True)
            inbox = agent_dir / "inbox.md"
            outbox = agent_dir / "outbox.md"
            if not inbox.exists():
                inbox.write_text("")
            if not outbox.exists():
                outbox.write_text("")

    def workspace_summary(self, max_files: int = 50) -> str:
        """Generate a text summary of workspace contents for agent context."""
        if not self._path.exists():
            return "Workspace is empty."

        lines = ["## Workspace files:"]
        file_count = 0
        for root, _dirs, files in os.walk(self._path):
            # Skip hidden dirs
            rel_root = Path(root).relative_to(self._path)
            if any(part.startswith(".") for part in rel_root.parts):
                continue
            for fname in sorted(files):
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                rel = fpath.relative_to(self._path)
                size = fpath.stat().st_size
                lines.append(f"  {rel} ({size} bytes)")
                file_count += 1
                if file_count >= max_files:
                    lines.append(f"  ... and more files")
                    break
            if file_count >= max_files:
                break

        if file_count == 0:
            return "Workspace is empty — you're starting fresh."

        # Show most recently modified file content
        recent = self._most_recent_file()
        if recent and recent.stat().st_size > 0 and recent.stat().st_size < 10000:
            rel = recent.relative_to(self._path)
            lines.append(f"\n## Most recently modified: {rel}")
            lines.append("```")
            try:
                content = recent.read_text()
                # Limit to 200 lines
                content_lines = content.splitlines()
                if len(content_lines) > 200:
                    lines.extend(content_lines[:200])
                    lines.append(f"... ({len(content_lines)} lines total)")
                else:
                    lines.append(content)
            except (UnicodeDecodeError, OSError):
                lines.append("(binary file)")
            lines.append("```")

        return "\n".join(lines)

    def get_file_changes_since(self, timestamp: float) -> list[FileChange]:
        """Return files modified since the given timestamp."""
        changes: list[FileChange] = []
        if not self._path.exists():
            return changes
        for root, _dirs, files in os.walk(self._path):
            rel_root = Path(root).relative_to(self._path)
            if any(part.startswith(".") for part in rel_root.parts):
                continue
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                try:
                    st = fpath.stat()
                    if st.st_mtime > timestamp:
                        rel = str(fpath.relative_to(self._path))
                        changes.append(FileChange(path=rel, size=st.st_size, modified=st.st_mtime))
                except OSError:
                    continue
        changes.sort(key=lambda c: c.modified, reverse=True)
        return changes

    def mark_checkpoint(self) -> None:
        """Record current time for file change tracking."""
        import time
        self._last_check_time = time.time()

    @property
    def last_check_time(self) -> float:
        return self._last_check_time

    def read_inbox(self, agent_name: str) -> str:
        """Read and clear an agent's inbox."""
        inbox = self._path / agent_name.lower() / "inbox.md"
        if not inbox.exists() or inbox.stat().st_size == 0:
            return ""
        content = inbox.read_text().strip()
        inbox.write_text("")
        return content

    def write_outbox(self, agent_name: str, content: str) -> None:
        """Write to an agent's outbox (read by the relay to forward)."""
        outbox = self._path / agent_name.lower() / "outbox.md"
        outbox.parent.mkdir(parents=True, exist_ok=True)
        outbox.write_text(content)

    def consume_outbox(self, agent_name: str) -> str:
        """Read and clear an agent's outbox."""
        outbox = self._path / agent_name.lower() / "outbox.md"
        if not outbox.exists() or outbox.stat().st_size == 0:
            return ""
        content = outbox.read_text().strip()
        outbox.write_text("")
        return content

    def append_inbox(self, agent_name: str, content: str) -> None:
        """Append a message to an agent's inbox without discarding prior entries."""
        inbox = self._path / agent_name.lower() / "inbox.md"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        existing = inbox.read_text().strip() if inbox.exists() else ""
        if existing:
            inbox.write_text(f"{existing}\n\n{content.strip()}\n")
        else:
            inbox.write_text(f"{content.strip()}\n")

    def forward_outbox(self, source_agent: str, target_agent: str) -> str:
        """Move source outbox content into the target inbox and clear the outbox."""
        content = self.consume_outbox(source_agent)
        if not content:
            return ""
        self.append_inbox(target_agent, f"[{source_agent}]: {content}")
        return content

    def _most_recent_file(self) -> Path | None:
        """Find the most recently modified non-hidden file."""
        best: Path | None = None
        best_mtime = 0.0
        for root, _dirs, files in os.walk(self._path):
            rel_root = Path(root).relative_to(self._path)
            if any(part.startswith(".") for part in rel_root.parts):
                continue
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                try:
                    mtime = fpath.stat().st_mtime
                    if mtime > best_mtime:
                        best_mtime = mtime
                        best = fpath
                except OSError:
                    continue
        return best
