"""Obligation scheduler. Polls open obligations and raises escalations.

Lightweight synchronous scheduler — no async, no threads. Designed for
the harness to call tick() at natural boundaries (after each action,
on user input, etc). Not a background daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .obligations import Escalation, ObligationCheck, ObligationEngine
from .types import Obligation, ObligationStatus


@dataclass
class TickResult:
    """Result of a single scheduler tick."""
    checked: list[ObligationCheck]
    escalations: list[Escalation]
    timestamp: str


# Callback for when an escalation fires
EscalationHandler = Callable[[Escalation], None]


class ObligationScheduler:
    """
    Synchronous obligation scheduler.

    Call tick() at natural boundaries. It checks all due obligations,
    marks them breached, and fires escalation handlers.
    """

    def __init__(self, engine: ObligationEngine) -> None:
        self._engine = engine
        self._handlers: list[EscalationHandler] = []
        self._tick_history: list[TickResult] = []

    def on_escalation(self, handler: EscalationHandler) -> None:
        """Register a handler that fires when any obligation breaches."""
        self._handlers.append(handler)

    def tick(self, now_iso: str | None = None) -> TickResult:
        """
        Check all due obligations and fire escalation handlers.

        Returns what happened this tick. Safe to call frequently —
        already-breached obligations won't re-escalate.
        """
        now = now_iso or datetime.now(timezone.utc).isoformat()

        checked = self._engine.check_due(now)
        escalations = self._engine.get_escalations(now)

        # Only fire handlers for newly breached (this tick)
        newly_breached_ids = {
            c.obligation_id
            for c in checked
            if c.new_status == ObligationStatus.BREACHED
        }
        new_escalations = [
            e for e in escalations
            if e.obligation_id in newly_breached_ids
        ]

        for escalation in new_escalations:
            for handler in self._handlers:
                handler(escalation)

        result = TickResult(
            checked=checked,
            escalations=new_escalations,
            timestamp=now,
        )
        self._tick_history.append(result)
        return result

    @property
    def history(self) -> list[TickResult]:
        return list(self._tick_history)
