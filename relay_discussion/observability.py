"""Observability layer for relay sessions.

Tracks per-turn timing, generates session summaries, and writes structured logs.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TurnRecord:
    turn: int
    agent: str
    success: bool
    duration_ms: int
    failure_type: str | None = None


@dataclass(slots=True)
class SessionSummary:
    total_turns: int
    total_duration_ms: int
    messages_per_agent: dict[str, int]
    failures_per_agent: dict[str, int]
    avg_turn_duration_ms: int
    longest_turn_ms: int
    longest_turn_agent: str
    status: str

    def to_text(self) -> str:
        """Human-readable summary string."""
        total_s = self.total_duration_ms / 1000
        minutes, secs = divmod(int(total_s), 60)
        avg_s = self.avg_turn_duration_ms / 1000

        lines = [f"{self.status.upper()}: {self.total_turns} turns in {minutes}m{secs:02d}s"]
        for agent, count in sorted(self.messages_per_agent.items()):
            failures = self.failures_per_agent.get(agent, 0)
            fail_str = f", {failures} failed" if failures else ""
            lines.append(f"  {agent}: {count} messages, avg {avg_s:.1f}s{fail_str}")
        if self.longest_turn_ms > 0:
            lines.append(f"  Longest turn: {self.longest_turn_ms / 1000:.1f}s ({self.longest_turn_agent})")
        return "\n".join(lines)


class SessionObserver:
    """Observes relay engine events and produces timing data."""

    def __init__(self) -> None:
        self._turns: list[TurnRecord] = []
        self._session_start: float = 0.0
        self._session_end: float = 0.0
        self._current_turn_start: float = 0.0
        self._current_turn: int = 0
        self._current_agent: str = ""
        self._final_status: str = "unknown"

    def on_session_start(self) -> None:
        self._session_start = time.time()

    def on_turn_start(self, turn: int, agent_name: str) -> None:
        self._current_turn_start = time.time()
        self._current_turn = turn
        self._current_agent = agent_name

    def on_turn_end(
        self,
        turn: int,
        agent_name: str,
        success: bool,
        failure_type: str | None = None,
    ) -> None:
        duration_ms = int((time.time() - self._current_turn_start) * 1000)
        self._turns.append(TurnRecord(
            turn=turn,
            agent=agent_name,
            success=success,
            duration_ms=duration_ms,
            failure_type=failure_type,
        ))

    def on_session_end(self, status: str) -> None:
        self._session_end = time.time()
        self._final_status = status

    def summary(self) -> SessionSummary:
        total_ms = int((self._session_end - self._session_start) * 1000) if self._session_end else 0
        messages_per_agent: dict[str, int] = {}
        failures_per_agent: dict[str, int] = {}
        longest_ms = 0
        longest_agent = ""

        for t in self._turns:
            if t.success:
                messages_per_agent[t.agent] = messages_per_agent.get(t.agent, 0) + 1
            else:
                failures_per_agent[t.agent] = failures_per_agent.get(t.agent, 0) + 1
            if t.duration_ms > longest_ms:
                longest_ms = t.duration_ms
                longest_agent = t.agent

        total_turns = len(self._turns)
        avg_ms = total_ms // total_turns if total_turns > 0 else 0

        return SessionSummary(
            total_turns=total_turns,
            total_duration_ms=total_ms,
            messages_per_agent=messages_per_agent,
            failures_per_agent=failures_per_agent,
            avg_turn_duration_ms=avg_ms,
            longest_turn_ms=longest_ms,
            longest_turn_agent=longest_agent,
            status=self._final_status,
        )

    def write_log(self, log_path: Path) -> None:
        """Write structured JSONL log of all turn records."""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as f:
            # Session metadata
            f.write(json.dumps({
                "type": "session_start",
                "timestamp": self._session_start,
            }) + "\n")

            for record in self._turns:
                f.write(json.dumps({
                    "type": "turn",
                    "turn": record.turn,
                    "agent": record.agent,
                    "success": record.success,
                    "duration_ms": record.duration_ms,
                    "failure_type": record.failure_type,
                }) + "\n")

            summary = self.summary()
            f.write(json.dumps({
                "type": "session_end",
                "timestamp": self._session_end,
                "summary": asdict(summary),
            }) + "\n")

    @property
    def turns(self) -> list[TurnRecord]:
        return list(self._turns)
