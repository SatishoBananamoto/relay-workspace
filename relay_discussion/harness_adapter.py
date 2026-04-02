"""Adapter between the relay discussion system and the intent-to-action harness.

Routes structured agent actions (artifact production, file writes, permission
requests) through the full harness pipeline. Unstructured actions (discuss,
analyze) bypass the harness and use the relay's existing behavioral rules.

Both the relay policy and the harness evaluate every turn. The most restrictive
decision wins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from .models import Message
from .policy import Decision as RelayDecision, PolicyResult as RelayPolicyResult, Blocker as RelayBlocker, BlockerKind
from .policy_relay import classify_relay_action, detect_promises

from harness.core import Harness, EvaluationResult
from harness.intent import IntentClassifier
from harness.store import InMemoryEffectStore
from harness.sdk import action, EffectBuilder, ActionHandle
from harness.types import (
    ActionSpec,
    Decision as HarnessDecision,
)


# ---------------------------------------------------------------------------
# Relay-specific action adapters (SDK-registered)
# ---------------------------------------------------------------------------

# Isolated registry so relay adapters don't pollute the base harness registry
# at import time. HarnessAdapter.__init__ merges them in.
RELAY_ADAPTERS: dict[str, ActionSpec] = {}


@action(
    "produce_artifact",
    blast_radius="medium",
    reversible=True,
    approval="never",
    entities={"_agent": "one"},
    intent=[
        r"```(?:python|typescript|ts|json|diff)",
        r"\bdef \w+\(",
        r"\bclass \w+[:\(]",
        r"\btype \w+\s*=",
    ],
    registry=RELAY_ADAPTERS,
)
def produce_artifact(args, resolution, now_iso, fx: EffectBuilder):
    agent = args.get("_agent", "unknown")
    kind = args.get("_artifact_kind", "artifact")
    fx.mutate("workspace", "produce_artifact", f"{agent} produced {kind}")
    fx.obligate(
        "review_artifact",
        due_minutes=10,
        verify="poll",
        failure_mode=f"Produced {kind} was not reviewed by the other agent",
    )


@action(
    "request_permission",
    blast_radius="high",
    reversible=False,
    approval="always",
    entities={"_agent": "one"},
    intent=[
        r"\bwrite permission\b",
        r"\bapprove the (?:edit|write)\b",
        r"\bgrant write access\b",
        r"\bpermission prompt\b",
        r"\bI need (?:write )?permission\b",
    ],
    registry=RELAY_ADAPTERS,
)
def request_permission(args, resolution, now_iso, fx: EffectBuilder):
    agent = args.get("_agent", "unknown")
    fx.mutate("system", "request_permission", f"{agent} requested elevated access")


@action(
    "fix_issue",
    blast_radius="medium",
    reversible=True,
    approval="never",
    entities={"_agent": "one"},
    intent=[
        r"\bI'?ll fix\b",
        r"\bLet me fix\b",
        r"\bfix (?:the |this )?(?:bug|issue|race|error)\b",
    ],
    registry=RELAY_ADAPTERS,
)
def fix_issue(args, resolution, now_iso, fx: EffectBuilder):
    agent = args.get("_agent", "unknown")
    fx.mutate("workspace", "fix_issue", f"{agent} proposed a fix")
    fx.obligate(
        "verify_fix",
        due_minutes=10,
        verify="query",
        failure_mode="Fix was not verified",
    )

@action(
    "analyze",
    blast_radius="low",
    reversible=True,
    approval="never",
    entities={"_agent": "one"},
    intent=[
        r"\bweak assumption\b",
        r"\bfindings\b",
        r"\bwhat's actually broken\b",
        r"\bthe hidden assumption\b",
        r"\bverified defect\b",
        r"\bbuild order\b",
    ],
    registry=RELAY_ADAPTERS,
)
def analyze(args, resolution, now_iso, fx: EffectBuilder):
    agent = args.get("_agent", "unknown")
    fx.mutate("workspace", "analyze", f"{agent} produced analysis")


@action(
    "escalate",
    blast_radius="high",
    reversible=False,
    approval="always",
    entities={"_agent": "one"},
    intent=[
        r"\bescalate\b",
        r"\bneed human (?:input|decision|review)\b",
        r"\bblocked\b.*\bneed (?:help|guidance)\b",
        r"\bcannot proceed without\b",
    ],
    registry=RELAY_ADAPTERS,
)
def escalate(args, resolution, now_iso, fx: EffectBuilder):
    agent = args.get("_agent", "unknown")
    fx.mutate("system", "escalate", f"{agent} escalated to human")
    fx.obligate(
        "human_response",
        due_minutes=30,
        verify="human",
        failure_mode="Escalation not addressed by human",
    )


@produce_artifact.extract_args
def _extract_artifact_args(text: str) -> dict[str, Any]:
    return {"_artifact_kind": _detect_artifact_kind(text)}


# "discuss" is not registered — it bypasses the harness (no safety-relevant effects).


# ---------------------------------------------------------------------------
# Decision mapping
# ---------------------------------------------------------------------------

_HARNESS_TO_RELAY: dict[HarnessDecision, RelayDecision] = {
    HarnessDecision.ALLOW: RelayDecision.ALLOW,
    HarnessDecision.CHECK: RelayDecision.ALLOW,  # checks already ran in harness
    HarnessDecision.CLARIFY: RelayDecision.CLARIFY,
    HarnessDecision.APPROVE: RelayDecision.CLARIFY,  # needs human input
    HarnessDecision.DENY: RelayDecision.BLOCK,
}

_HARNESS_BLOCKER_TO_RELAY: dict[str, BlockerKind] = {
    "missing_required_arg": BlockerKind.MISSING_REQUIRED,
    "entity_resolution_conflict": BlockerKind.MISSING_REQUIRED,
    "schema_competition": BlockerKind.MISSING_REQUIRED,
    "commitment_conflict": BlockerKind.COMMITMENT_CONFLICT,
    "blast_radius_exceeds_limit": BlockerKind.BLAST_RADIUS,
}


def _map_harness_result(eval_result: EvaluationResult) -> RelayPolicyResult:
    """Map a harness evaluation result to a relay PolicyResult."""
    relay_decision = _HARNESS_TO_RELAY.get(
        eval_result.policy.decision, RelayDecision.ALLOW,
    )
    relay_blockers = []
    for hb in eval_result.policy.blockers:
        relay_kind = _HARNESS_BLOCKER_TO_RELAY.get(hb.value, BlockerKind.MISSING_REQUIRED)
        relay_blockers.append(RelayBlocker(
            kind=relay_kind,
            detail=f"harness:{hb.value}",
        ))
    if eval_result.policy.reason_codes:
        for code in eval_result.policy.reason_codes:
            if not any(b.detail == f"harness:{code}" for b in relay_blockers):
                relay_blockers.append(RelayBlocker(
                    kind=BlockerKind.MISSING_REQUIRED,
                    detail=f"harness_reason:{code}",
                ))

    return RelayPolicyResult(
        decision=relay_decision,
        blockers=tuple(relay_blockers) if relay_blockers else (),
    )


# ---------------------------------------------------------------------------
# Adapter class
# ---------------------------------------------------------------------------

class HarnessAdapter:
    """Bridges the relay turn loop to the intent-to-action harness.

    Uses IntentClassifier (driven by SDK intent patterns) to route
    agent text to action types. Registered types go through the full
    harness pipeline. Unregistered types (discuss, analyze) return
    ALLOW and let the relay's behavioral rules handle them.
    """

    def __init__(self, harness: Harness | None = None) -> None:
        if harness is None:
            from harness.registry import REGISTRY
            harness = Harness()
            # Register relay-specific adapters alongside the existing ones
            for action_type, spec in RELAY_ADAPTERS.items():
                REGISTRY[action_type] = spec
        self._harness = harness
        self._classifier = IntentClassifier(RELAY_ADAPTERS)
        self._eval_cache: dict[str, EvaluationResult] = {}

    @property
    def harness(self) -> Harness:
        return self._harness

    @property
    def classifier(self) -> IntentClassifier:
        return self._classifier

    def evaluate_turn(
        self,
        agent_name: str,
        proposed_content: str,
        transcript: Sequence[Message],
    ) -> RelayPolicyResult:
        """Evaluate a proposed agent turn through the harness.

        Uses IntentClassifier to identify the action type from free text.
        Unregistered action types return ALLOW (handled by relay behavioral rules).
        """
        intent = self._classifier.classify(proposed_content)

        # Fallback (no pattern match) → bypass harness
        if intent.confidence == 0.0:
            return RelayPolicyResult(decision=RelayDecision.ALLOW)

        # Unregistered in the harness → bypass
        from harness.registry import get_spec
        if get_spec(intent.action_type) is None:
            return RelayPolicyResult(decision=RelayDecision.ALLOW)

        now = datetime.now(timezone.utc).isoformat()
        promises = detect_promises(proposed_content)

        # Merge classifier-extracted args with standard context
        args = {
            "_agent": agent_name,
            "_content": proposed_content,
            "_artifact_kind": intent.args.get("_artifact_kind", _detect_artifact_kind(proposed_content)),
            "_promises": promises,
            **intent.args,
        }

        eval_result = self._harness.evaluate(
            action_type=intent.action_type,
            args=args,
            entity_map={"_agent": [agent_name]},
            semantic_keys=[intent.action_type],
            now_iso=now,
        )

        # Cache for potential execution
        self._eval_cache[agent_name] = eval_result

        return _map_harness_result(eval_result)

    def record_outcome(
        self,
        agent_name: str,
        content: str,
        result: str,
        action_type: str,
    ) -> None:
        """Record the outcome of a turn.

        On success, executes the cached evaluation through the harness
        to persist effects and create obligations.
        """
        if result != "success":
            self._eval_cache.pop(agent_name, None)
            return

        eval_result = self._eval_cache.pop(agent_name, None)
        if eval_result is None:
            return

        # Only execute if the harness allowed it
        if eval_result.policy.decision in (
            HarnessDecision.ALLOW, HarnessDecision.CHECK,
        ):
            now = datetime.now(timezone.utc).isoformat()
            self._harness.execute(eval_result, now_iso=now)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_artifact_kind(content: str) -> str:
    """Classify what kind of artifact the agent produced."""
    lower = content.lower()
    if "```python" in lower or "def " in lower or "class " in lower:
        return "python_code"
    if "```typescript" in lower or "```ts" in lower:
        return "typescript_code"
    if "```diff" in lower:
        return "diff"
    if "```json" in lower:
        return "json_config"
    return "text"


