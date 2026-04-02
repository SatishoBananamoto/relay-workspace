"""Proposal lifecycle state machine with transition tracking.

State machine from spec:
  proposed -> clarify -> check -> approve -> allow
    -> executed -> effects_persisted
    -> obligations_open -> obligations_satisfied | obligations_breached

Every transition is recorded. The audit trail is append-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProposalState(str, Enum):
    PROPOSED = "proposed"
    CLARIFY = "clarify"
    CHECK = "check"
    APPROVE = "approve"
    ALLOW = "allow"
    EXECUTED = "executed"
    EFFECTS_PERSISTED = "effects_persisted"
    OBLIGATIONS_OPEN = "obligations_open"
    OBLIGATIONS_SATISFIED = "obligations_satisfied"
    OBLIGATIONS_BREACHED = "obligations_breached"
    DENIED = "denied"
    FAILED = "failed"
    SUPERSEDED = "superseded"


# Valid transitions. Key = from state, value = set of allowed to states.
TRANSITIONS: dict[ProposalState, set[ProposalState]] = {
    ProposalState.PROPOSED: {
        ProposalState.CLARIFY,
        ProposalState.CHECK,
        ProposalState.APPROVE,
        ProposalState.ALLOW,
        ProposalState.DENIED,
    },
    ProposalState.CLARIFY: {
        ProposalState.SUPERSEDED,  # replaced by revised proposal
        ProposalState.DENIED,
    },
    ProposalState.CHECK: {
        ProposalState.ALLOW,
        ProposalState.APPROVE,
        ProposalState.DENIED,
        ProposalState.CLARIFY,
    },
    ProposalState.APPROVE: {
        ProposalState.ALLOW,
        ProposalState.DENIED,
    },
    ProposalState.ALLOW: {
        ProposalState.EXECUTED,
        ProposalState.FAILED,
    },
    ProposalState.EXECUTED: {
        ProposalState.EFFECTS_PERSISTED,
        ProposalState.FAILED,
    },
    ProposalState.EFFECTS_PERSISTED: {
        ProposalState.OBLIGATIONS_OPEN,
    },
    ProposalState.OBLIGATIONS_OPEN: {
        ProposalState.OBLIGATIONS_SATISFIED,
        ProposalState.OBLIGATIONS_BREACHED,
    },
    ProposalState.OBLIGATIONS_SATISFIED: set(),
    ProposalState.OBLIGATIONS_BREACHED: set(),
    ProposalState.DENIED: set(),
    ProposalState.FAILED: set(),
    ProposalState.SUPERSEDED: set(),
}


@dataclass
class Transition:
    """A single state transition in the proposal lifecycle."""
    from_state: ProposalState
    to_state: ProposalState
    reason: str
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProposalLifecycle:
    """Tracks the full lifecycle of a single proposal."""
    proposal_id: str
    current_state: ProposalState = ProposalState.PROPOSED
    transitions: list[Transition] = field(default_factory=list)

    def transition(
        self,
        to_state: ProposalState,
        *,
        reason: str,
        timestamp: str,
        metadata: dict[str, Any] | None = None,
    ) -> Transition:
        """
        Transition to a new state. Raises ValueError on invalid transitions.
        Returns the recorded transition.
        """
        allowed = TRANSITIONS.get(self.current_state, set())
        if to_state not in allowed:
            raise ValueError(
                f"Invalid transition: {self.current_state.value} -> {to_state.value}. "
                f"Allowed: {', '.join(s.value for s in sorted(allowed, key=lambda s: s.value))}"
            )

        t = Transition(
            from_state=self.current_state,
            to_state=to_state,
            reason=reason,
            timestamp=timestamp,
            metadata=metadata or {},
        )
        self.transitions.append(t)
        self.current_state = to_state
        return t

    @property
    def is_terminal(self) -> bool:
        return not TRANSITIONS.get(self.current_state, set())

    @property
    def audit_trail(self) -> list[dict[str, Any]]:
        """Export the full transition history as dicts."""
        return [
            {
                "from": t.from_state.value,
                "to": t.to_state.value,
                "reason": t.reason,
                "timestamp": t.timestamp,
                **({k: v for k, v in t.metadata.items()} if t.metadata else {}),
            }
            for t in self.transitions
        ]


class LifecycleTracker:
    """Tracks lifecycles for all proposals in a session."""

    def __init__(self) -> None:
        self._lifecycles: dict[str, ProposalLifecycle] = {}

    def create(self, proposal_id: str) -> ProposalLifecycle:
        if proposal_id in self._lifecycles:
            raise ValueError(f"Lifecycle already exists: {proposal_id}")
        lc = ProposalLifecycle(proposal_id=proposal_id)
        self._lifecycles[proposal_id] = lc
        return lc

    def get(self, proposal_id: str) -> ProposalLifecycle | None:
        return self._lifecycles.get(proposal_id)

    def all_active(self) -> list[ProposalLifecycle]:
        return [lc for lc in self._lifecycles.values() if not lc.is_terminal]

    def all_terminal(self) -> list[ProposalLifecycle]:
        return [lc for lc in self._lifecycles.values() if lc.is_terminal]

    @property
    def lifecycles(self) -> dict[str, ProposalLifecycle]:
        return dict(self._lifecycles)
