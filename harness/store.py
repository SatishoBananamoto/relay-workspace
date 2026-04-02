"""Effect-store contracts and in-memory implementation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from .types import Commitment, Effect, Obligation, ObligationStatus


class EffectStore(Protocol):
    """Minimal store contract used by the harness runtime."""

    def append(self, effect: Effect) -> None: ...

    @property
    def effects(self) -> list[Effect]: ...

    def query_open_intersection(
        self,
        *,
        action_type: str,
        entity_ids: list[str],
        resource_keys: list[str],
        semantic_keys: list[str],
        now_iso: str | None = None,
    ) -> tuple[list[Commitment], list[Obligation]]: ...

    def get_all_open_obligations(self) -> list[Obligation]: ...

    def mark_obligation(
        self, obligation_id: str, status: ObligationStatus,
    ) -> bool: ...

    def supersede_commitment(
        self, commitment_id: str, superseded_by: str,
    ) -> bool: ...

    def append_correction(self, original_action_id: str, correction: Effect) -> None: ...

    def get_effect(self, action_id: str) -> Effect | None: ...

    def get_commitment(self, commitment_id: str) -> Commitment | None: ...

    def get_obligation(self, obligation_id: str) -> Obligation | None: ...

    def get_obligations_for_proposal(self, proposal_id: str) -> list[Obligation]: ...


class InMemoryEffectStore:
    def __init__(self) -> None:
        self._effects: list[Effect] = []

    def append(self, effect: Effect) -> None:
        self._effects.append(effect)

    @property
    def effects(self) -> list[Effect]:
        return list(self._effects)

    def query_open_intersection(
        self,
        *,
        action_type: str,
        entity_ids: list[str],
        resource_keys: list[str],
        semantic_keys: list[str],
        now_iso: str | None = None,
    ) -> tuple[list[Commitment], list[Obligation]]:
        """Return open commitments and obligations that intersect the given keys."""
        now = now_iso or datetime.now(timezone.utc).isoformat()
        entity_set = set(entity_ids)
        resource_set = set(resource_keys)
        semantic_set = set(semantic_keys)

        commitments: list[Commitment] = []
        obligations: list[Obligation] = []

        for effect in self._effects:
            for c in effect.commitments:
                if c.superseded_by:
                    continue
                if c.expires_at and _is_past(c.expires_at, now):
                    continue
                if _intersects(entity_set, c.entity_ids) or \
                   _intersects(resource_set, c.resource_keys) or \
                   _intersects(semantic_set, c.semantic_keys):
                    commitments.append(c)

            for o in effect.obligations:
                if o.status != ObligationStatus.OPEN:
                    continue
                if _intersects(entity_set, o.entity_ids) or \
                   _intersects(resource_set, o.resource_keys) or \
                   _intersects(semantic_set, o.semantic_keys):
                    obligations.append(o)

        return commitments, obligations

    def get_all_open_obligations(self) -> list[Obligation]:
        """Return all obligations with status OPEN across all effects."""
        result: list[Obligation] = []
        for effect in self._effects:
            for o in effect.obligations:
                if o.status == ObligationStatus.OPEN:
                    result.append(o)
        return result

    def mark_obligation(
        self, obligation_id: str, status: ObligationStatus,
    ) -> bool:
        """Update an obligation's status. Returns True if found."""
        for effect in self._effects:
            for o in effect.obligations:
                if o.obligation_id == obligation_id:
                    o.status = status
                    return True
        return False

    def supersede_commitment(
        self, commitment_id: str, superseded_by: str,
    ) -> bool:
        """
        Mark a commitment as superseded by a newer commitment.
        The old commitment remains in history but stops constraining future actions.
        Returns True if found.
        """
        for effect in self._effects:
            for c in effect.commitments:
                if c.commitment_id == commitment_id:
                    c.superseded_by = superseded_by
                    return True
        return False

    def append_correction(self, original_action_id: str, correction: Effect) -> None:
        """
        Append a correction effect. Corrections are new effects, not mutations
        of history. The original effect remains in the store for audit.
        """
        self._effects.append(correction)

    def get_effect(self, action_id: str) -> Effect | None:
        """Look up a specific effect by action ID."""
        for effect in self._effects:
            if effect.action_id == action_id:
                return effect
        return None

    def get_commitment(self, commitment_id: str) -> Commitment | None:
        """Look up a specific commitment by ID."""
        for effect in self._effects:
            for c in effect.commitments:
                if c.commitment_id == commitment_id:
                    return c
        return None

    def get_obligation(self, obligation_id: str) -> Obligation | None:
        """Look up a specific obligation by ID."""
        for effect in self._effects:
            for o in effect.obligations:
                if o.obligation_id == obligation_id:
                    return o
        return None

    def get_obligations_for_proposal(self, proposal_id: str) -> list[Obligation]:
        """Return all obligations emitted by a specific proposal."""
        obligations: list[Obligation] = []
        for effect in self._effects:
            for obligation in effect.obligations:
                if obligation.source_proposal_id == proposal_id:
                    obligations.append(obligation)
        return obligations


def _intersects(index: set[str], values: list[str]) -> bool:
    return any(v in index for v in values)


def _is_past(timestamp: str, now_iso: str) -> bool:
    return _parse_iso8601(timestamp) < _parse_iso8601(now_iso)


def _parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
