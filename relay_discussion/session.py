"""Session management for relay conversations.

Each session lives in its own directory under ~/.relay/sessions/<id>/
with a meta.json file and a transcript.jsonl file.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import utc_now

DEFAULT_RELAY_DIR = Path.home() / ".relay"


@dataclass(slots=True)
class SessionMeta:
    id: str
    topic: str
    left_agent_name: str
    right_agent_name: str
    moderator: str
    status: str  # new, running, paused, completed, archived
    created: str
    updated: str
    turns_completed: int = 0
    name: str = ""
    mode: str = "freeform"
    left_provider: str = "mock"
    right_provider: str = "mock"
    left_model: str = "mirror"
    right_model: str = "mirror"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMeta:
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        # Backward compat: old sessions have build_mode but no mode
        if "mode" not in filtered and data.get("build_mode"):
            filtered["mode"] = "build"
        return cls(**filtered)


class SessionManager:
    """Manages relay sessions under a root directory."""

    def __init__(self, relay_dir: Path | None = None) -> None:
        self._relay_dir = relay_dir or DEFAULT_RELAY_DIR
        self._sessions_dir = self._relay_dir / "sessions"
        self._archive_dir = self._relay_dir / "archive"

    @property
    def relay_dir(self) -> Path:
        return self._relay_dir

    def _ensure_dirs(self) -> None:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        *,
        topic: str,
        left_agent_name: str,
        right_agent_name: str,
        moderator: str = "Satisho",
        name: str = "",
        mode: str = "freeform",
        left_provider: str = "mock",
        right_provider: str = "mock",
        left_model: str = "mirror",
        right_model: str = "mirror",
    ) -> SessionMeta:
        self._ensure_dirs()
        session_id = str(uuid.uuid4())
        now = utc_now()

        meta = SessionMeta(
            id=session_id,
            topic=topic,
            left_agent_name=left_agent_name,
            right_agent_name=right_agent_name,
            moderator=moderator,
            status="new",
            created=now,
            updated=now,
            name=name,
            mode=mode,
            left_provider=left_provider,
            right_provider=right_provider,
            left_model=left_model,
            right_model=right_model,
        )

        session_dir = self._sessions_dir / session_id
        session_dir.mkdir(parents=True)

        from .modes import get_mode
        mode_spec = get_mode(mode)
        if mode_spec.workspace_required:
            ws = session_dir / "workspace"
            ws.mkdir()
            (ws / "shared").mkdir()
            for agent_name in (left_agent_name.lower(), right_agent_name.lower()):
                agent_dir = ws / agent_name
                agent_dir.mkdir()
                (agent_dir / "inbox.md").write_text("")
                (agent_dir / "outbox.md").write_text("")
            (ws / "reviews").mkdir()

        self._write_meta(session_id, meta)
        return meta

    def get_session(self, session_id: str) -> SessionMeta:
        meta_path = self._sessions_dir / session_id / "meta.json"
        if not meta_path.exists():
            raise ValueError(f"Session not found: {session_id}")
        data = json.loads(meta_path.read_text())
        return SessionMeta.from_dict(data)

    def get_session_by_name(self, name: str) -> SessionMeta | None:
        """Find a session by its human-readable name."""
        self._ensure_dirs()
        for meta_path in self._sessions_dir.glob("*/meta.json"):
            try:
                data = json.loads(meta_path.read_text())
                if data.get("name") == name:
                    return SessionMeta.from_dict(data)
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return None

    def list_sessions(self, status_filter: str | None = None) -> list[SessionMeta]:
        self._ensure_dirs()
        sessions: list[SessionMeta] = []
        for meta_path in self._sessions_dir.glob("*/meta.json"):
            try:
                data = json.loads(meta_path.read_text())
                meta = SessionMeta.from_dict(data)
                if status_filter and meta.status != status_filter:
                    continue
                sessions.append(meta)
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        sessions.sort(key=lambda s: s.updated, reverse=True)
        return sessions

    def update_status(
        self,
        session_id: str,
        status: str,
        turns_completed: int | None = None,
    ) -> SessionMeta:
        meta = self.get_session(session_id)
        meta.status = status
        meta.updated = utc_now()
        if turns_completed is not None:
            meta.turns_completed = turns_completed
        self._write_meta(session_id, meta)
        return meta

    def archive_session(self, session_id: str) -> None:
        self._ensure_dirs()
        src = self._sessions_dir / session_id
        if not src.exists():
            raise ValueError(f"Session not found: {session_id}")
        meta = self.get_session(session_id)
        meta.status = "archived"
        meta.updated = utc_now()
        self._write_meta(session_id, meta)
        dst = self._archive_dir / session_id
        shutil.move(str(src), str(dst))

    def delete_session(self, session_id: str) -> None:
        """Permanently delete a session directory."""
        session_dir = self._sessions_dir / session_id
        if not session_dir.exists():
            raise ValueError(f"Session not found: {session_id}")
        shutil.rmtree(session_dir)

    def write_pid(self, session_id: str) -> None:
        """Write current PID to session directory."""
        pid_path = self._sessions_dir / session_id / "engine.pid"
        pid_path.write_text(str(os.getpid()))

    def read_pid(self, session_id: str) -> int | None:
        """Read stored PID, or None if no PID file."""
        pid_path = self._sessions_dir / session_id / "engine.pid"
        if not pid_path.exists():
            return None
        try:
            return int(pid_path.read_text().strip())
        except (ValueError, OSError):
            return None

    def is_engine_alive(self, session_id: str) -> bool:
        """Check if the engine process for this session is still running."""
        pid = self.read_pid(session_id)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)  # signal 0 = check if process exists
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but we can't signal it

    def clear_pid(self, session_id: str) -> None:
        pid_path = self._sessions_dir / session_id / "engine.pid"
        pid_path.unlink(missing_ok=True)

    def get_transcript_path(self, session_id: str) -> Path:
        return self._sessions_dir / session_id / "transcript.jsonl"

    def get_workspace_path(self, session_id: str) -> Path:
        return self._sessions_dir / session_id / "workspace"

    def get_session_dir(self, session_id: str) -> Path:
        return self._sessions_dir / session_id

    def _write_meta(self, session_id: str, meta: SessionMeta) -> None:
        meta_path = self._sessions_dir / session_id / "meta.json"
        meta_path.write_text(json.dumps(meta.to_dict(), indent=2) + "\n")
