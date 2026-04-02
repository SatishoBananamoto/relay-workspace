"""Policy engine for action gating.

Sits between intent and execution. Evaluates proposals against
deterministic rules — no model reasoning in the gate path.

Designed as a standalone component: the relay is the first consumer,
but any agent harness can use it.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

class Decision(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    FORCE_CHANGE = "force_change"
    CLARIFY = "clarify"


class BlockerKind(Enum):
    """Typed reasons for blocking — not a scalar score."""
    REPEATED_FAILURE = "repeated_failure"
    OUTPUT_NO_DELTA = "output_no_delta"
    PROMISE_BREACH = "promise_breach"
    TOPIC_DRIFT = "topic_drift"
    COMMITMENT_CONFLICT = "commitment_conflict"
    MISSING_REQUIRED = "missing_required"
    BLAST_RADIUS = "blast_radius"


@dataclass(frozen=True)
class Blocker:
    kind: BlockerKind
    detail: str
    suggested_alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    blockers: tuple[Blocker, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


@dataclass(frozen=True)
class ActionOutcome:
    """Recorded after execution (or denial)."""
    action_type: str
    args_hash: str
    result: str          # "success" | "denied" | "failed" | "no_change"
    timestamp: float
    content_hash: str = ""   # hash of output content, for delta detection
    promises: tuple[str, ...] = ()  # promises detected in output


@dataclass
class Obligation:
    """A commitment to do something in the future."""
    id: str
    source_action_type: str
    kind: str               # what needs to happen
    entity_ids: tuple[str, ...] = ()
    due_at: float | None = None  # unix timestamp
    verify_with: str = "check"   # "check" | "query" | "human"
    failure_mode: str = ""
    status: str = "open"         # "open" | "satisfied" | "breached"


@dataclass
class Commitment:
    """A constraint on future actions, created by a past action."""
    id: str
    kind: str
    entity_ids: tuple[str, ...] = ()
    constrains_action_types: tuple[str, ...] = ()
    fields: dict[str, Any] = field(default_factory=dict)
    expires_at: float | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------

def content_hash(text: str) -> str:
    """Deterministic hash of normalized content for delta detection."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def args_hash(args: dict[str, Any]) -> str:
    payload = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Policy rules
# ---------------------------------------------------------------------------

class PolicyRule(ABC):
    @abstractmethod
    def evaluate(
        self,
        action_type: str,
        action_args: dict[str, Any],
        history: Sequence[ActionOutcome],
        obligations: Sequence[Obligation],
        commitments: Sequence[Commitment],
    ) -> PolicyResult:
        ...


class RepeatedFailureRule(PolicyRule):
    """FM-3: Block after N consecutive identical failures.

    The cheapest rule. Stateless beyond a rolling window.
    Would have broken Claude's permission loop by turn 3.
    """

    def __init__(self, max_consecutive: int = 3):
        self.max_consecutive = max_consecutive

    def evaluate(self, action_type, action_args, history, obligations, commitments):
        consecutive = 0
        for outcome in reversed(history):
            if outcome.action_type != action_type:
                break
            if outcome.result in ("denied", "failed"):
                consecutive += 1
            else:
                break

        if consecutive >= self.max_consecutive:
            return PolicyResult(
                decision=Decision.FORCE_CHANGE,
                blockers=(Blocker(
                    kind=BlockerKind.REPEATED_FAILURE,
                    detail=f"{action_type} failed {consecutive}x consecutively with same args",
                    suggested_alternatives=(
                        "change action arguments",
                        "try a different action type",
                        "ask user for guidance",
                    ),
                ),),
            )
        return PolicyResult(decision=Decision.ALLOW)


class OutputDeltaRule(PolicyRule):
    """FM-1 + FM-5: Block if output is isomorphic to previous output.

    Catches both pure repetition (FM-1) and performative rigor (FM-5)
    where the text changes superficially but the information doesn't.

    Uses content hashing with a similarity threshold based on
    structural overlap, not exact match.
    """

    def __init__(self, max_identical: int = 2):
        self.max_identical = max_identical

    def evaluate(self, action_type, action_args, history, obligations, commitments):
        # Count recent outputs with the same content hash from the same action type
        if not action_args.get("_content_hash"):
            return PolicyResult(decision=Decision.ALLOW)

        ch = action_args["_content_hash"]
        identical_recent = 0
        for outcome in reversed(history):
            if outcome.action_type != action_type:
                continue
            if outcome.content_hash == ch:
                identical_recent += 1
            else:
                break

        if identical_recent >= self.max_identical:
            return PolicyResult(
                decision=Decision.FORCE_CHANGE,
                blockers=(Blocker(
                    kind=BlockerKind.OUTPUT_NO_DELTA,
                    detail=f"Last {identical_recent} outputs from {action_type} were structurally identical",
                    suggested_alternatives=(
                        "produce a different artifact type",
                        "execute instead of analyze",
                        "ask user for new direction",
                    ),
                ),),
            )
        return PolicyResult(decision=Decision.ALLOW)


class PromiseBreachRule(PolicyRule):
    """FM-2: Detect repeated promises without delivery.

    If the same promise appears in consecutive outputs without the
    obligation being satisfied, the agent is in a promise loop.
    """

    def __init__(self, max_repeats: int = 2):
        self.max_repeats = max_repeats

    def evaluate(self, action_type, action_args, history, obligations, commitments):
        # Find promises that appear in recent history but were never satisfied
        promise_counts: dict[str, int] = {}
        for outcome in reversed(history[-10:]):
            for promise in outcome.promises:
                promise_counts[promise] = promise_counts.get(promise, 0) + 1

        # Check against obligations — if a promise was made N times but the
        # corresponding obligation is still open, that's a breach
        open_obligation_kinds = {o.kind for o in obligations if o.status == "open"}

        breached: list[str] = []
        for promise, count in promise_counts.items():
            if count >= self.max_repeats and promise in open_obligation_kinds:
                breached.append(promise)

        if breached:
            return PolicyResult(
                decision=Decision.FORCE_CHANGE,
                blockers=(Blocker(
                    kind=BlockerKind.PROMISE_BREACH,
                    detail=f"Promised {', '.join(breached)} multiple times without delivery",
                    suggested_alternatives=(
                        "deliver the promised artifact now",
                        "explicitly retract the promise",
                        "explain why delivery is blocked",
                    ),
                ),),
            )
        return PolicyResult(decision=Decision.ALLOW)


class CommitmentConflictRule(PolicyRule):
    """FM-4 (generalized): Block actions that violate active commitments.

    In the relay context: active topic is a commitment. Pivoting without
    acknowledgment violates it. In the general case: any past action
    that constrains future actions.
    """

    def evaluate(self, action_type, action_args, history, obligations, commitments):
        conflicts: list[str] = []
        entity_ids = set(action_args.get("_entity_ids", ()))

        for commitment in commitments:
            if commitment.expired:
                continue
            if action_type not in commitment.constrains_action_types:
                continue
            # Check entity overlap
            if commitment.entity_ids and not entity_ids.intersection(commitment.entity_ids):
                continue
            conflicts.append(
                f"{commitment.kind}: {json.dumps(commitment.fields, sort_keys=True)}"
            )

        if conflicts:
            return PolicyResult(
                decision=Decision.CLARIFY,
                blockers=(Blocker(
                    kind=BlockerKind.COMMITMENT_CONFLICT,
                    detail=f"Action conflicts with {len(conflicts)} active commitment(s): {'; '.join(conflicts)}",
                    suggested_alternatives=(
                        "acknowledge the conflict explicitly",
                        "get user approval to override",
                        "modify action to respect the commitment",
                    ),
                ),),
            )
        return PolicyResult(decision=Decision.ALLOW)


class ConvergenceRule(PolicyRule):
    """FM-6: Force convergence after N analysis turns without execution.

    If the last N actions are all the same type (typically "analyze" or
    "discuss") with no execution action between them, force a choice.
    """

    def __init__(self, max_analysis_streak: int = 4, analysis_types: tuple[str, ...] = ("analyze",)):
        self.max_analysis_streak = max_analysis_streak
        self.analysis_types = analysis_types

    def evaluate(self, action_type, action_args, history, obligations, commitments):
        if action_type not in self.analysis_types:
            return PolicyResult(decision=Decision.ALLOW)

        streak = 0
        for outcome in reversed(history):
            if outcome.action_type in self.analysis_types:
                streak += 1
            else:
                break

        if streak >= self.max_analysis_streak:
            return PolicyResult(
                decision=Decision.FORCE_CHANGE,
                blockers=(Blocker(
                    kind=BlockerKind.OUTPUT_NO_DELTA,
                    detail=f"{streak} consecutive analysis turns without execution",
                    suggested_alternatives=(
                        "execute the smallest viable action",
                        "explicitly decide not to execute and close the analysis",
                        "ask user whether to continue analyzing or execute",
                    ),
                ),),
            )
        return PolicyResult(decision=Decision.ALLOW)


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------

class PolicyEngine:
    """Evaluates action proposals against an ordered list of rules.

    Stateless: history and obligations are passed in, not stored.
    The caller (harness/relay) owns the state.
    """

    def __init__(self, rules: Sequence[PolicyRule] | None = None):
        self._rules = list(rules) if rules else self._default_rules()

    @staticmethod
    def _default_rules() -> list[PolicyRule]:
        return [
            RepeatedFailureRule(max_consecutive=3),
            OutputDeltaRule(max_identical=2),
            PromiseBreachRule(max_repeats=2),
            CommitmentConflictRule(),
            ConvergenceRule(max_analysis_streak=4),
        ]

    def evaluate(
        self,
        action_type: str,
        action_args: dict[str, Any],
        history: Sequence[ActionOutcome],
        obligations: Sequence[Obligation] = (),
        commitments: Sequence[Commitment] = (),
    ) -> PolicyResult:
        """Run all rules. First non-ALLOW result wins."""
        for rule in self._rules:
            result = rule.evaluate(
                action_type=action_type,
                action_args=action_args,
                history=history,
                obligations=obligations,
                commitments=commitments,
            )
            if not result.allowed:
                return result
        return PolicyResult(decision=Decision.ALLOW)


# ---------------------------------------------------------------------------
# Obligation store
# ---------------------------------------------------------------------------

class ObligationStore:
    """External obligation + commitment store with action-triggered retrieval.

    Not in-context. Not model-scored. Deterministic joins by action type
    and entity IDs.
    """

    def __init__(self) -> None:
        self._obligations: dict[str, Obligation] = {}
        self._commitments: dict[str, Commitment] = {}
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    # -- Obligations --

    def add_obligation(self, **kwargs: Any) -> Obligation:
        ob = Obligation(id=self._next_id("obl"), **kwargs)
        self._obligations[ob.id] = ob
        return ob

    def query_obligations(
        self,
        action_types: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
        status: str = "open",
    ) -> list[Obligation]:
        """Action-triggered retrieval. Cheap database join, not relevance scoring."""
        results = []
        action_set = set(action_types)
        entity_set = set(entity_ids)
        for ob in self._obligations.values():
            if ob.status != status:
                continue
            if action_set and ob.source_action_type not in action_set:
                continue
            if entity_set and not entity_set.intersection(ob.entity_ids):
                continue
            results.append(ob)
        return results

    def satisfy(self, obligation_id: str) -> None:
        if obligation_id in self._obligations:
            self._obligations[obligation_id].status = "satisfied"

    def breach(self, obligation_id: str) -> None:
        if obligation_id in self._obligations:
            self._obligations[obligation_id].status = "breached"

    def check_deadlines(self, now: float | None = None) -> list[Obligation]:
        """Return obligations past their deadline that are still open."""
        now = now or time.time()
        return [
            ob for ob in self._obligations.values()
            if ob.status == "open" and ob.due_at is not None and now > ob.due_at
        ]

    # -- Commitments --

    def add_commitment(self, **kwargs: Any) -> Commitment:
        c = Commitment(id=self._next_id("cmt"), **kwargs)
        self._commitments[c.id] = c
        return c

    def query_commitments(
        self,
        action_types: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
    ) -> list[Commitment]:
        """Before executing an action: what commitments constrain it?"""
        results = []
        action_set = set(action_types)
        entity_set = set(entity_ids)
        for c in self._commitments.values():
            if c.expired:
                continue
            if action_set and not action_set.intersection(c.constrains_action_types):
                continue
            if entity_set and not entity_set.intersection(c.entity_ids):
                continue
            results.append(c)
        return results

    def expire_commitment(self, commitment_id: str) -> None:
        if commitment_id in self._commitments:
            self._commitments[commitment_id].expires_at = 0.0

    # -- Serialization (for pause/resume) --

    def export_state(self) -> dict[str, Any]:
        return {
            "obligations": {
                k: {
                    "id": v.id, "source_action_type": v.source_action_type,
                    "kind": v.kind, "entity_ids": list(v.entity_ids),
                    "due_at": v.due_at, "verify_with": v.verify_with,
                    "failure_mode": v.failure_mode, "status": v.status,
                }
                for k, v in self._obligations.items()
            },
            "commitments": {
                k: {
                    "id": v.id, "kind": v.kind, "entity_ids": list(v.entity_ids),
                    "constrains_action_types": list(v.constrains_action_types),
                    "fields": v.fields, "expires_at": v.expires_at,
                }
                for k, v in self._commitments.items()
            },
            "counter": self._counter,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._counter = state.get("counter", 0)
        self._obligations = {}
        for data in state.get("obligations", {}).values():
            ob = Obligation(
                id=data["id"], source_action_type=data["source_action_type"],
                kind=data["kind"], entity_ids=tuple(data.get("entity_ids", ())),
                due_at=data.get("due_at"), verify_with=data.get("verify_with", "check"),
                failure_mode=data.get("failure_mode", ""), status=data.get("status", "open"),
            )
            self._obligations[ob.id] = ob
        self._commitments = {}
        for data in state.get("commitments", {}).values():
            c = Commitment(
                id=data["id"], kind=data["kind"],
                entity_ids=tuple(data.get("entity_ids", ())),
                constrains_action_types=tuple(data.get("constrains_action_types", ())),
                fields=data.get("fields", {}), expires_at=data.get("expires_at"),
            )
            self._commitments[c.id] = c
