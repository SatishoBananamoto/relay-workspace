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
    mount_specs: list[dict] = field(default_factory=list)
    read_only: bool = False

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
        mount_specs: list | None = None,
        read_only: bool = False,
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
            read_only=read_only,
        )

        session_dir = self._sessions_dir / session_id
        session_dir.mkdir(parents=True)

        ws_dir = session_dir / "workspace"
        if mode == "build":
            ws_dir.mkdir()
            (ws_dir / "shared").mkdir()
            for agent_name in (left_agent_name.lower(), right_agent_name.lower()):
                agent_dir = ws_dir / agent_name
                agent_dir.mkdir()
                (agent_dir / "inbox.md").write_text("")
                (agent_dir / "outbox.md").write_text("")
            (ws_dir / "reviews").mkdir()

        # Apply mount specs (sandbox copies / direct mounts)
        if mount_specs:
            from .mount import MountSpec, mount as mount_dispatch
            ws_dir.mkdir(exist_ok=True)
            mount_points = []
            for spec in mount_specs:
                if isinstance(spec, MountSpec):
                    point = mount_dispatch(spec, ws_dir)
                else:
                    # Accept dict form too
                    point = mount_dispatch(
                        MountSpec(
                            source=Path(spec["source"]).expanduser().resolve(),
                            mount_mode=spec.get("mount_mode", "sandbox"),
                            read_only=bool(spec.get("read_only", False)),
                        ),
                        ws_dir,
                    )
                mount_points.append(point.to_dict())
            meta.mount_specs = mount_points

        self._write_meta(session_id, meta)
        return meta

    def get_session(self, session_id: str) -> SessionMeta:
        full_id = self._resolve_id(session_id)
        meta_path = self._sessions_dir / full_id / "meta.json"
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
        """Permanently delete a session directory.

        Cleans up mount points (git worktrees, sandbox copies) before
        removing the session directory. Direct mounts are never touched.
        """
        full_id = self._resolve_id(session_id)
        session_dir = self._sessions_dir / full_id
        if not session_dir.exists():
            raise ValueError(f"Session not found: {session_id}")

        # Clean up mounts before removing the session dir
        try:
            meta = self.get_session(full_id)
            if meta.mount_specs:
                from .mount import MountPoint, cleanup_mount
                for spec_dict in meta.mount_specs:
                    try:
                        point = MountPoint.from_dict(spec_dict)
                        cleanup_mount(point)
                    except Exception:
                        # Best-effort cleanup; don't block session deletion
                        pass
        except (ValueError, FileNotFoundError):
            pass  # No meta or unreadable — proceed with rmtree

        shutil.rmtree(session_dir)

    def get_mount_points(self, session_id: str) -> list:
        """Return list of MountPoint objects for a session."""
        from .mount import MountPoint
        meta = self.get_session(session_id)
        return [MountPoint.from_dict(spec) for spec in meta.mount_specs]

    def add_mount(self, session_id: str, mount_point_dict: dict) -> None:
        """Append a mount point to an existing session's metadata."""
        full_id = self._resolve_id(session_id)
        meta = self.get_session(full_id)
        meta.mount_specs.append(mount_point_dict)
        self._write_meta(full_id, meta)

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

    def _resolve_id(self, session_id: str) -> str:
        """Resolve a short ID prefix to a full session ID."""
        full_path = self._sessions_dir / session_id
        if full_path.exists():
            return session_id
        self._ensure_dirs()
        matches = [d.name for d in self._sessions_dir.iterdir()
                   if d.is_dir() and d.name.startswith(session_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous session ID prefix: {session_id}")
        raise ValueError(f"Session not found: {session_id}")

    def get_transcript_path(self, session_id: str) -> Path:
        return self._sessions_dir / self._resolve_id(session_id) / "transcript.jsonl"

    def get_workspace_path(self, session_id: str) -> Path:
        return self._sessions_dir / self._resolve_id(session_id) / "workspace"

    def get_session_dir(self, session_id: str) -> Path:
        return self._sessions_dir / self._resolve_id(session_id)

    def _write_meta(self, session_id: str, meta: SessionMeta) -> None:
        meta_path = self._sessions_dir / session_id / "meta.json"
        meta_path.write_text(json.dumps(meta.to_dict(), indent=2) + "\n")
