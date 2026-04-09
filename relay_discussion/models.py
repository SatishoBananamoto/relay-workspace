from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

VALID_PROVIDERS = frozenset({"mock", "openai", "anthropic", "cli-claude", "cli-codex"})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_strict_int(value: object) -> bool:
    return type(value) is int


def is_valid_provider_name(value: object) -> bool:
    return isinstance(value, str) and value in VALID_PROVIDERS


def is_valid_session_snapshot(session: object) -> bool:
    if not isinstance(session, dict):
        return False

    if not isinstance(session.get("moderator"), str):
        return False

    moderator_events = session.get("moderator_events")
    if not isinstance(moderator_events, list):
        return False
    for item in moderator_events:
        if not isinstance(item, dict):
            return False
        turn = item.get("turn")
        if not is_strict_int(turn) or turn < 1:
            return False
        if not isinstance(item.get("content"), str):
            return False
        if not isinstance(item.get("author"), str):
            return False

    for side in ("left_agent", "right_agent"):
        agent = session.get(side)
        if not isinstance(agent, dict):
            return False
        for field in ("name", "model", "instruction"):
            if not isinstance(agent.get(field), str):
                return False
        if not is_valid_provider_name(agent.get("provider")):
            return False

    return True


def is_valid_fault_state_snapshot(fault_state: object) -> bool:
    if not isinstance(fault_state, dict):
        return False

    for side in ("left_agent", "right_agent"):
        script = fault_state.get(side)
        if not isinstance(script, list):
            return False
        if not all(isinstance(item, str) for item in script):
            return False

    return True


def is_valid_policy_state_snapshot(policy_state: object) -> bool:
    if not isinstance(policy_state, dict):
        return False
    if not isinstance(policy_state.get("history", []), list):
        return False
    if not isinstance(policy_state.get("store", {}), dict):
        return False
    return True


def _canonical_json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_resume_state_digest(
    *,
    topic: str,
    session: dict[str, object],
    fault_state: dict[str, list[str]] | None = None,
    policy_state: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {"topic": topic, "session": session}
    if fault_state is not None:
        payload["fault_state"] = fault_state
    if policy_state is not None:
        payload["policy_state"] = policy_state
    return _canonical_json_digest(payload)


@dataclass(slots=True)
class Message:
    seq: int
    timestamp: str
    role: str
    author: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "role": self.role,
            "author": self.author,
            "content": self.content,
            "metadata": self.metadata,
        }


def compute_conversation_digest(messages: Iterable[Message]) -> str:
    return _canonical_json_digest(
        [
            {
                "role": message.role,
                "author": message.author,
                "content": message.content,
                "metadata": message.metadata,
            }
            for message in messages
            if message.role != "system"
        ]
    )


@dataclass(slots=True)
class AgentConfig:
    name: str
    provider: str = "mock"
    model: str = "mirror"
    instruction: str = ""
    fault_script: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ModeratorEvent:
    turn: int
    content: str
    author: str = "Satisho"


@dataclass(slots=True)
class RelayConfig:
    topic: str
    turns: int
    left_agent: AgentConfig
    right_agent: AgentConfig
    moderator: str = "Satisho"
    moderator_events: list[ModeratorEvent] = field(default_factory=list)
    trace_provider_payloads: bool = False
    max_failed_attempts: int = 3
    max_total_appends_without_both: int = 8
    retry_attempts: int = 2
    retry_backoff_seconds: float = 10.0
    operator_tripwire_patterns: list[str] = field(
        default_factory=lambda: [
            r"\bif you (?:guys )?can't implement\b",
            r"\bswitch out of discussion mode\b",
            r"\bthe protocol you set forbids tools\b",
        ]
    )
    use_harness: bool = False
    mode: str = "discuss"
    mount_paths: list[Path] = field(default_factory=list)
    read_only: bool = False


@dataclass(slots=True)
class RelayRunResult:
    messages: list[Message]
    status: str
    pause_reason: str | None = None
