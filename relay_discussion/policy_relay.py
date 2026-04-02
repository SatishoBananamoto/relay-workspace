"""Relay-specific integration for the policy engine.

Maps the relay's turn loop concepts (agent responses, transcript messages)
to the policy engine's action/obligation/commitment model.

This is the adapter layer. The policy engine is generic; this file
knows about relay specifics.
"""

from __future__ import annotations

import re
import time
from typing import Sequence

from .models import Message
from .policy import (
    ActionOutcome,
    Commitment,
    Decision,
    Obligation,
    ObligationStore,
    PolicyEngine,
    PolicyResult,
    content_hash,
)

# ---------------------------------------------------------------------------
# Promise detection (cheap heuristics, not model calls)
# ---------------------------------------------------------------------------

_PROMISE_PATTERNS: list[tuple[str, str]] = [
    # "I can turn this into X" / "I'll produce X"
    (r"I (?:can|will|'ll|could) (?:turn this into|produce|create|write|generate)\s+(.+?)(?:\.|$)", "produce_artifact"),
    # "next step is X" / "the right next artifact is X"
    (r"(?:next step|next artifact|next move)\s+(?:is|should be)\s+(.+?)(?:\.|$)", "produce_artifact"),
    # "I'll fix X" / "Let me fix X"
    (r"(?:I'?ll|Let me)\s+fix\s+(.+?)(?:\.|$)", "fix_issue"),
    # "I need write permission"
    (r"I need (?:write )?permission", "request_permission"),
]


def detect_promises(text: str) -> list[str]:
    """Extract promise-like statements from agent output. Cheap regex, not inference."""
    found: list[str] = []
    for pattern, kind in _PROMISE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(kind)
    return list(set(found))


# ---------------------------------------------------------------------------
# Relay action classification
# ---------------------------------------------------------------------------

def classify_relay_action(message: Message, transcript: Sequence[Message]) -> str:
    """Classify a relay agent message into an action type.

    Not trying to be precise — just enough to feed the policy engine.
    """
    text = message.content.lower()

    # Did the agent try to write/edit files?
    if any(phrase in text for phrase in [
        "write permission", "approve the edit", "approve the write",
        "grant write access", "permission prompt",
    ]):
        return "request_permission"

    # Did the agent produce concrete artifacts (code, diffs, specs)?
    if any(phrase in text for phrase in [
        "```python", "```typescript", "```ts", "```json",
        "```diff", "def ", "class ", "type ",
    ]):
        return "produce_artifact"

    # Is this analysis / discussion?
    if any(phrase in text for phrase in [
        "weak assumption", "build order", "findings", "what's actually broken",
        "the hidden assumption", "verified defect",
    ]):
        return "analyze"

    return "discuss"


# ---------------------------------------------------------------------------
# Relay policy harness
# ---------------------------------------------------------------------------

class RelayPolicyHarness:
    """Wraps the policy engine for relay-specific use.

    Sits between the relay engine's turn loop and agent execution.
    Tracks action history, obligations, and commitments across turns.

    When use_harness=True, structured actions (produce_artifact,
    request_permission, fix_issue) are also routed through the
    intent-to-action harness for richer safety evaluation. The most
    restrictive decision between the behavioral rules and the harness wins.
    """

    def __init__(
        self,
        engine: PolicyEngine | None = None,
        use_harness: bool = False,
    ):
        self.engine = engine or PolicyEngine()
        self.store = ObligationStore()
        self._history: list[ActionOutcome] = []
        self._harness_adapter = None
        if use_harness:
            from .harness_adapter import HarnessAdapter
            self._harness_adapter = HarnessAdapter()

    def evaluate_turn(
        self,
        agent_name: str,
        proposed_content: str,
        transcript: Sequence[Message],
    ) -> PolicyResult:
        """Evaluate a proposed agent turn before committing it.

        Returns PolicyResult. If not allowed, the relay should:
        - BLOCK: skip the turn, log the reason
        - FORCE_CHANGE: inject a system message telling the agent to change strategy
        - CLARIFY: ask the user to resolve the conflict
        """
        # Build a synthetic Message for classification
        synthetic = Message(
            seq=0, timestamp="", role="agent",
            author=agent_name, content=proposed_content,
        )
        action_type = classify_relay_action(synthetic, transcript)

        # Build action args with content hash for delta detection
        ch = content_hash(proposed_content)
        promises = detect_promises(proposed_content)

        action_args = {
            "_content_hash": ch,
            "_entity_ids": (),
            "_agent": agent_name,
        }

        # Query relevant obligations and commitments
        obligations = self.store.query_obligations(
            action_types=(action_type,),
        )
        commitments = self.store.query_commitments(
            action_types=(action_type,),
        )

        behavioral_result = self.engine.evaluate(
            action_type=action_type,
            action_args=action_args,
            history=self._agent_history(agent_name),
            obligations=obligations,
            commitments=commitments,
        )

        # If harness is active, also evaluate through it. Most restrictive wins.
        if self._harness_adapter is not None:
            harness_result = self._harness_adapter.evaluate_turn(
                agent_name, proposed_content, transcript,
            )
            if not harness_result.allowed and behavioral_result.allowed:
                return harness_result

        return behavioral_result

    def record_outcome(
        self,
        agent_name: str,
        content: str,
        result: str,
        action_type: str | None = None,
    ) -> None:
        """Record the outcome of a turn after execution."""
        if action_type is None:
            synthetic = Message(
                seq=0, timestamp="", role="agent",
                author=agent_name, content=content,
            )
            action_type = classify_relay_action(synthetic, [])

        promises = detect_promises(content)
        ch = content_hash(content)

        outcome = ActionOutcome(
            action_type=action_type,
            args_hash=f"{agent_name}:{action_type}",
            result=result,
            timestamp=time.time(),
            content_hash=ch,
            promises=tuple(promises),
        )
        self._history.append(outcome)

        # Record in harness adapter too
        if self._harness_adapter is not None:
            self._harness_adapter.record_outcome(
                agent_name, content, result, action_type,
            )

        # Auto-create obligations from promises
        if result != "success":
            return

        for promise in promises:
            existing = self.store.query_obligations(
                action_types=(action_type,),
            )
            already_tracked = any(o.kind == promise and o.status == "open" for o in existing)
            if not already_tracked:
                self.store.add_obligation(
                    source_action_type=action_type,
                    kind=promise,
                    entity_ids=(agent_name,),
                    due_at=time.time() + 300,  # 5 minute default deadline
                    verify_with="check",
                    failure_mode=f"Agent promised '{promise}' but hasn't delivered",
                )

    def record_topic_commitment(self, topic: str) -> Commitment:
        """At session start, register the topic as a commitment."""
        return self.store.add_commitment(
            kind="active_topic",
            entity_ids=("session",),
            constrains_action_types=("discuss", "analyze"),
            fields={"topic": topic},
        )

    def check_breached_obligations(self) -> list[Obligation]:
        """Check for obligations past their deadline."""
        return self.store.check_deadlines()

    def _agent_history(self, agent_name: str) -> list[ActionOutcome]:
        """Filter history to actions from this agent."""
        return [
            o for o in self._history
            if o.args_hash.startswith(f"{agent_name}:")
        ]

    def export_state(self) -> dict:
        return {
            "history": [
                {
                    "action_type": o.action_type,
                    "args_hash": o.args_hash,
                    "result": o.result,
                    "timestamp": o.timestamp,
                    "content_hash": o.content_hash,
                    "promises": list(o.promises),
                }
                for o in self._history
            ],
            "store": self.store.export_state(),
        }

    def restore_state(self, state: dict) -> None:
        self._history = [
            ActionOutcome(
                action_type=h["action_type"],
                args_hash=h["args_hash"],
                result=h["result"],
                timestamp=h["timestamp"],
                content_hash=h.get("content_hash", ""),
                promises=tuple(h.get("promises", ())),
            )
            for h in state.get("history", [])
        ]
        self.store.restore_state(state.get("store", {}))
