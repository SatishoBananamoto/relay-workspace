"""Obligation engine. Schedules checks, detects breaches, raises escalations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .store import EffectStore
from .types import Obligation, ObligationStatus, VerifyWith


@dataclass
class ObligationCheck:
    """Result of checking a single obligation."""
    obligation_id: str
    previous_status: ObligationStatus
    new_status: ObligationStatus
    detail: str = ""


@dataclass
class Escalation:
    """Raised when an obligation breaches."""
    obligation_id: str
    kind: str
    failure_mode: str
    entity_ids: list[str]
    suggested_action: str = ""


StatusChangeHandler = Callable[[Obligation, ObligationStatus, ObligationStatus, str], None]


class ObligationEngine:
    def __init__(
        self,
        store: EffectStore,
        on_status_change: StatusChangeHandler | None = None,
    ) -> None:
        self._store = store
        self._status_handlers: list[StatusChangeHandler] = []
        if on_status_change is not None:
            self._status_handlers.append(on_status_change)

    def on_status_change(self, handler: StatusChangeHandler) -> None:
        self._status_handlers.append(handler)

    def check_due(self, now_iso: str) -> list[ObligationCheck]:
        """
        Check all open obligations against the current time.
        Obligations strictly past their due date are marked breached.
        """
        results: list[ObligationCheck] = []
        for obligation in self._store.get_all_open_obligations():
            if _is_past(obligation.due_at, now_iso):
                old_status = obligation.status
                updated = self._update_status(
                    obligation.obligation_id,
                    ObligationStatus.BREACHED,
                    now_iso=now_iso,
                )
                if updated is not None:
                    results.append(ObligationCheck(
                        obligation_id=obligation.obligation_id,
                        previous_status=old_status,
                        new_status=updated.status,
                        detail=f"Past due: {obligation.due_at}",
                    ))
        return results

    def satisfy(self, obligation_id: str, now_iso: str | None = None) -> bool:
        """Mark an obligation as satisfied. Returns True if found."""
        return self._update_status(
            obligation_id, ObligationStatus.SATISFIED, now_iso=now_iso,
        ) is not None

    def breach(self, obligation_id: str, now_iso: str | None = None) -> bool:
        """Mark an obligation as breached. Returns True if found."""
        return self._update_status(
            obligation_id, ObligationStatus.BREACHED, now_iso=now_iso,
        ) is not None

    def get_escalations(self, now_iso: str) -> list[Escalation]:
        """
        Return escalations for all breached obligations.
        Checks due obligations first, then collects breached ones.
        """
        self.check_due(now_iso)
        escalations: list[Escalation] = []

        for effect in self._store.effects:
            for o in effect.obligations:
                if o.status == ObligationStatus.BREACHED:
                    escalations.append(Escalation(
                        obligation_id=o.obligation_id,
                        kind=o.kind,
                        failure_mode=o.failure_mode,
                        entity_ids=o.entity_ids,
                        suggested_action=_suggest_followup(o),
                    ))
        return escalations

    def project_for_action(
        self,
        *,
        action_type: str,
        entity_ids: list[str],
        resource_keys: list[str],
        semantic_keys: list[str],
        now_iso: str | None = None,
    ) -> list[Obligation]:
        """
        Return only the open obligations that intersect with a candidate action.
        This is what gets projected into model context — not the full ledger.
        """
        _, obligations = self._store.query_open_intersection(
            action_type=action_type,
            entity_ids=entity_ids,
            resource_keys=resource_keys,
            semantic_keys=semantic_keys,
            now_iso=now_iso,
        )
        return obligations

    def _update_status(
        self,
        obligation_id: str,
        new_status: ObligationStatus,
        now_iso: str | None = None,
    ) -> Obligation | None:
        obligation = self._store.get_obligation(obligation_id)
        if obligation is None:
            return None
        previous_status = obligation.status
        if previous_status == new_status:
            return obligation
        marked = self._store.mark_obligation(obligation_id, new_status)
        if not marked:
            return None
        updated = self._store.get_obligation(obligation_id)
        if updated is None:
            return None
        changed_at = now_iso or datetime.now(timezone.utc).isoformat()
        self._notify_status_change(updated, previous_status, updated.status, changed_at)
        return updated

    def _notify_status_change(
        self,
        obligation: Obligation,
        previous_status: ObligationStatus,
        new_status: ObligationStatus,
        changed_at: str,
    ) -> None:
        for handler in self._status_handlers:
            handler(obligation, previous_status, new_status, changed_at)


def _suggest_followup(obligation: Obligation) -> str:
    """Generate a suggested follow-up action for a breached obligation."""
    suggestions = {
        "quote_acknowledgement": "Send follow-up email or escalate to account manager",
        "delete_verification": "Run downstream count verification query",
        "meeting_response": "Send reminder or find alternative time",
    }
    return suggestions.get(obligation.kind, "Review and resolve manually")


def _is_past(timestamp: str, now_iso: str) -> bool:
    return _parse_iso8601(timestamp) < _parse_iso8601(now_iso)


def _parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
