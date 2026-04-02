"""Adapter authoring SDK — declarative action registration.

Replaces ~40 lines of boilerplate per adapter with ~10 lines:

    from harness.sdk import action, EffectBuilder

    @action("produce_artifact", blast_radius="medium", reversible=True)
    def produce_artifact(args, resolution, now_iso, fx: EffectBuilder):
        fx.mutate("workspace", "produce_artifact", f"{args['_agent']} produced artifact")
        fx.obligate("review_artifact", due_minutes=10, verify="poll",
                    failure_mode="Not reviewed")

    @produce_artifact.precondition
    def check_content(proposal, resolution):
        if not proposal.args.get("_content"):
            return [Blocker.MISSING_REQUIRED_ARG]
        return []

The decorator creates an ActionSpec, wraps the function body into an
EffectTemplate, and registers into the target registry.
"""

from __future__ import annotations

from dataclasses import field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from .types import (
    ActionSpec,
    ApprovalPolicy,
    BlastRadius,
    Blocker,
    CheckKind,
    CheckMode,
    CheckSpec,
    Commitment,
    Decision,
    FeedbackLatency,
    Mutation,
    Obligation,
    ObligationStatus,
    Proposal,
    Resolution,
    SelectorSpec,
    VerifyWith,
)


# ---------------------------------------------------------------------------
# EffectBuilder — fluent context for effect templates
# ---------------------------------------------------------------------------

class EffectBuilder:
    """Accumulates mutations, commitments, and obligations during effect execution.

    Passed as the fourth argument to @action-decorated functions.
    Provides .mutate(), .commit(), .obligate() instead of raw list construction.
    """

    def __init__(
        self,
        resolution: Resolution,
        now_iso: str,
    ) -> None:
        self._resolution = resolution
        self._now_iso = now_iso
        self.mutations: list[Mutation] = []
        self.commitments: list[Commitment] = []
        self.obligations: list[Obligation] = []

    @property
    def entity_ids(self) -> list[str]:
        return self._resolution.entity_ids

    @property
    def resource_keys(self) -> list[str]:
        return self._resolution.resource_keys

    @property
    def semantic_keys(self) -> list[str]:
        return self._resolution.semantic_keys

    @property
    def now(self) -> str:
        return self._now_iso

    def mutate(self, resource: str, op: str, summary: str) -> None:
        """Record a mutation."""
        self.mutations.append(Mutation(resource=resource, op=op, summary=summary))

    def commit(
        self,
        kind: str,
        fields: dict[str, str | int | float | bool],
        *,
        commitment_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Record a commitment."""
        cid = commitment_id or f"commitment:{self._now_iso}:{kind}"
        self.commitments.append(Commitment(
            commitment_id=cid,
            kind=kind,
            entity_ids=self._resolution.entity_ids,
            resource_keys=self._resolution.resource_keys,
            semantic_keys=self._resolution.semantic_keys,
            fields=fields,
            expires_at=expires_at,
        ))

    def obligate(
        self,
        kind: str,
        *,
        due_minutes: int | None = None,
        due_at: str | None = None,
        verify: str | VerifyWith = VerifyWith.POLL,
        failure_mode: str = "",
        obligation_id: str | None = None,
    ) -> None:
        """Record an obligation.

        Provide either due_minutes (relative) or due_at (absolute ISO).
        verify can be a string ("poll", "query", "human") or VerifyWith enum.
        """
        if due_at is None:
            mins = due_minutes or 10
            due_at = (
                datetime.fromisoformat(self._now_iso.replace("Z", "+00:00"))
                + timedelta(minutes=mins)
            ).isoformat()

        if isinstance(verify, str):
            verify = VerifyWith(verify)

        oid = obligation_id or f"obligation:{self._now_iso}:{kind}"
        self.obligations.append(Obligation(
            obligation_id=oid,
            kind=kind,
            entity_ids=self._resolution.entity_ids,
            resource_keys=self._resolution.resource_keys,
            semantic_keys=self._resolution.semantic_keys,
            due_at=due_at,
            verify_with=verify,
            failure_mode=failure_mode,
            status=ObligationStatus.OPEN,
        ))

    def build(self) -> tuple[list[Mutation], list[Commitment], list[Obligation]]:
        """Return the accumulated effects as the expected tuple."""
        return (self.mutations, self.commitments, self.obligations)


# ---------------------------------------------------------------------------
# ActionHandle — returned by @action, supports hook decorators
# ---------------------------------------------------------------------------

class ActionHandle:
    """Wrapper returned by @action. Holds the spec and supports hook decorators."""

    def __init__(self, spec: ActionSpec, fn: Callable) -> None:
        self._spec = spec
        self._fn = fn
        # Preserve original function attributes
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__

    @property
    def spec(self) -> ActionSpec:
        return self._spec

    @property
    def action_type(self) -> str:
        return self._spec.action_type

    def precondition(self, fn: Callable[[Proposal, Resolution], list[Blocker]]) -> Callable:
        """Decorator to attach a precondition check to this action."""
        self._spec.preconditions = fn
        return fn

    def approval_gate(self, fn: Callable[[Proposal, Resolution], bool]) -> Callable:
        """Decorator to attach a custom approval gate to this action."""
        self._spec.requires_approval = fn
        return fn

    def conflict_check(
        self,
        fn: Callable[[Proposal, Resolution, list[Commitment]], bool],
    ) -> Callable:
        """Decorator to attach a conflict detector to this action."""
        self._spec.conflict_detector = fn
        return fn

    def intent(self, *patterns: str) -> "ActionHandle":
        """Add regex patterns that identify this action in free text.

        Usage:
            @my_action.intent(r"```python", r"here's the code")
            def my_action(...): ...

        Or call directly:
            my_action.intent(r"```python", r"here's the code")
        """
        self._spec.intent_patterns.extend(patterns)
        return self

    def extract_args(self, fn: Callable[[str], dict[str, Any]]) -> Callable:
        """Decorator to attach an arg extractor for this action.

        The extractor receives free text and returns structured args.
        """
        self._spec.arg_extractor = fn
        return fn

    def __call__(self, *a: Any, **kw: Any) -> Any:
        """Allow calling the underlying function directly (useful in tests)."""
        return self._fn(*a, **kw)


# ---------------------------------------------------------------------------
# Defaults inference
# ---------------------------------------------------------------------------

_BLAST_TO_LATENCY: dict[BlastRadius, FeedbackLatency] = {
    BlastRadius.LOW: FeedbackLatency.FAST,
    BlastRadius.MEDIUM: FeedbackLatency.FAST,
    BlastRadius.HIGH: FeedbackLatency.SLOW,
}


def _parse_blast(v: str | BlastRadius) -> BlastRadius:
    return BlastRadius(v) if isinstance(v, str) else v


def _parse_approval(v: str | ApprovalPolicy) -> ApprovalPolicy:
    return ApprovalPolicy(v) if isinstance(v, str) else v


def _parse_latency(v: str | FeedbackLatency) -> FeedbackLatency:
    return FeedbackLatency(v) if isinstance(v, str) else v


def _parse_selectors(
    mapping: dict[str, str] | None,
) -> list[SelectorSpec]:
    if not mapping:
        return []
    return [SelectorSpec(name, cardinality) for name, cardinality in mapping.items()]


def _parse_checks(
    specs: list[dict[str, Any]] | list[CheckSpec] | None,
) -> list[CheckSpec]:
    if not specs:
        return []
    result = []
    for item in specs:
        if isinstance(item, CheckSpec):
            result.append(item)
        else:
            result.append(CheckSpec(
                id=item["id"],
                kind=CheckKind(item.get("kind", "lookup")),
                required_for=[Decision(d) for d in item.get("required_for", ["allow"])],
                mode=CheckMode(item.get("mode", "local")),
            ))
    return result


# ---------------------------------------------------------------------------
# @action decorator
# ---------------------------------------------------------------------------

def action(
    action_type: str,
    *,
    blast_radius: str | BlastRadius = BlastRadius.LOW,
    reversible: bool = True,
    approval: str | ApprovalPolicy = ApprovalPolicy.IF_HIGH_RISK,
    feedback_latency: str | FeedbackLatency | None = None,
    required_args: list[str] | None = None,
    entities: dict[str, str] | None = None,
    resources: dict[str, str] | None = None,
    checks: list[dict[str, Any]] | list[CheckSpec] | None = None,
    intent: list[str] | None = None,
    version: str = "1",
    registry: dict[str, ActionSpec] | None = None,
) -> Callable:
    """Decorator that creates an ActionSpec and registers it.

    The decorated function receives (args, resolution, now_iso, fx: EffectBuilder)
    and uses fx.mutate() / fx.commit() / fx.obligate() to declare effects.

    Args:
        action_type: Unique action identifier.
        blast_radius: "low", "medium", or "high".
        reversible: Whether the action can be undone.
        approval: "never", "if_high_risk", or "always".
        feedback_latency: "fast", "slow", or "silent". Inferred from blast_radius if omitted.
        required_args: Args that must be present. Default: [].
        entities: Entity selector map, e.g. {"recipientId": "one", "attendeeIds": "many"}.
        resources: Resource selector map, same format.
        checks: List of CheckSpec or dicts with {id, kind, required_for, mode}.
        intent: Regex patterns that identify this action in free text.
        version: Spec version string. Default: "1".
        registry: Target registry dict. Default: harness.registry.REGISTRY.
    """
    br = _parse_blast(blast_radius)
    ap = _parse_approval(approval)
    fl = _parse_latency(feedback_latency) if feedback_latency else _BLAST_TO_LATENCY[br]

    def decorator(fn: Callable) -> ActionHandle:
        def effect_template(
            args: dict[str, Any],
            resolution: Resolution,
            now_iso: str,
        ) -> tuple[list[Mutation], list[Commitment], list[Obligation]]:
            fx = EffectBuilder(resolution, now_iso)
            fn(args, resolution, now_iso, fx)
            return fx.build()

        spec = ActionSpec(
            action_type=action_type,
            version=version,
            required_args=required_args or [],
            blast_radius=br,
            reversible=reversible,
            feedback_latency=fl,
            cheap_checks=_parse_checks(checks),
            approval_policy=ap,
            effect_template=effect_template,
            entity_selectors=_parse_selectors(entities),
            resource_selectors=_parse_selectors(resources),
            intent_patterns=list(intent) if intent else [],
        )

        # Register into target registry
        target = registry
        if target is None:
            from .registry import REGISTRY
            target = REGISTRY
        target[action_type] = spec

        return ActionHandle(spec, fn)

    return decorator
