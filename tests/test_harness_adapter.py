"""Tests for the relay-harness integration adapter."""

from __future__ import annotations

import pytest

from relay_discussion.policy import Decision as RelayDecision
from relay_discussion.policy_relay import RelayPolicyHarness
from relay_discussion.harness_adapter import HarnessAdapter, RELAY_ADAPTERS
from relay_discussion.models import Message
from harness.registry import REGISTRY, get_spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(author: str, content: str, seq: int = 1) -> Message:
    return Message(
        seq=seq, timestamp="2026-04-02T12:00:00Z",
        role="agent", author=author, content=content,
    )


# Code block triggers produce_artifact classification
ARTIFACT_CONTENT = """\
Here's the implementation:

```python
def hello():
    return "world"
```
"""

DISCUSS_CONTENT = "I think we should refactor the module to separate concerns."

PERMISSION_CONTENT = "I need write permission to modify the database schema."


# ---------------------------------------------------------------------------
# Adapter standalone tests
# ---------------------------------------------------------------------------

class TestHarnessAdapter:

    def setup_method(self):
        # Register relay adapters fresh for each test
        for action_type, spec in RELAY_ADAPTERS.items():
            REGISTRY[action_type] = spec
        self.adapter = HarnessAdapter()

    def test_artifact_production_allowed(self):
        result = self.adapter.evaluate_turn("claude", ARTIFACT_CONTENT, [])
        assert result.allowed

    def test_discuss_bypasses_harness(self):
        """Unregistered action types (discuss) bypass the harness entirely."""
        result = self.adapter.evaluate_turn("claude", DISCUSS_CONTENT, [])
        assert result.allowed  # ALLOW — not in harness registry

    def test_permission_request_blocked(self):
        """request_permission is registered with approval_policy=ALWAYS."""
        result = self.adapter.evaluate_turn("claude", PERMISSION_CONTENT, [])
        # Should route to CLARIFY (maps to APPROVE in harness → CLARIFY in relay)
        assert result.decision == RelayDecision.CLARIFY

    def test_record_outcome_persists_effects(self):
        """Successful artifact production persists effects in the harness."""
        self.adapter.evaluate_turn("claude", ARTIFACT_CONTENT, [])
        self.adapter.record_outcome("claude", ARTIFACT_CONTENT, "success", "produce_artifact")
        # Effect should be in the store
        effects = self.adapter.harness.store.effects
        assert len(effects) == 1
        assert effects[0].action_type == "produce_artifact"

    def test_denied_outcome_no_effects(self):
        """Denied turns should not persist effects."""
        self.adapter.evaluate_turn("claude", ARTIFACT_CONTENT, [])
        self.adapter.record_outcome("claude", ARTIFACT_CONTENT, "denied", "produce_artifact")
        assert len(self.adapter.harness.store.effects) == 0

    def test_obligation_created_on_artifact(self):
        """Artifact production creates a review obligation."""
        self.adapter.evaluate_turn("claude", ARTIFACT_CONTENT, [])
        self.adapter.record_outcome("claude", ARTIFACT_CONTENT, "success", "produce_artifact")
        effects = self.adapter.harness.store.effects
        assert len(effects[0].obligations) == 1
        assert effects[0].obligations[0].kind == "review_artifact"


# ---------------------------------------------------------------------------
# Integrated relay policy harness tests
# ---------------------------------------------------------------------------

class TestRelayPolicyHarnessWithHarness:

    def setup_method(self):
        for action_type, spec in RELAY_ADAPTERS.items():
            REGISTRY[action_type] = spec
        self.harness_policy = RelayPolicyHarness(use_harness=True)

    def test_discuss_allowed_by_both_layers(self):
        result = self.harness_policy.evaluate_turn(
            "claude", DISCUSS_CONTENT, [],
        )
        assert result.allowed

    def test_artifact_allowed_by_both_layers(self):
        result = self.harness_policy.evaluate_turn(
            "claude", ARTIFACT_CONTENT, [],
        )
        assert result.allowed

    def test_permission_request_blocked_by_harness(self):
        result = self.harness_policy.evaluate_turn(
            "claude", PERMISSION_CONTENT, [],
        )
        assert not result.allowed
        assert result.decision == RelayDecision.CLARIFY

    def test_behavioral_rules_still_apply(self):
        """Relay behavioral rules (repeated failure etc) still work alongside harness."""
        # Record 3 consecutive failures → should trigger FORCE_CHANGE
        for _ in range(3):
            self.harness_policy.record_outcome("claude", DISCUSS_CONTENT, "denied", "discuss")

        result = self.harness_policy.evaluate_turn(
            "claude", DISCUSS_CONTENT, [],
        )
        # Behavioral rule (RepeatedFailureRule) should block
        assert not result.allowed

    def test_most_restrictive_wins(self):
        """If behavioral allows but harness blocks, block wins."""
        # Permission request: behavioral rules allow (no history of failure),
        # but harness blocks (needs approval)
        result = self.harness_policy.evaluate_turn(
            "claude", PERMISSION_CONTENT, [],
        )
        assert not result.allowed

    def test_without_harness_flag_no_harness_evaluation(self):
        """use_harness=False should not evaluate through the harness."""
        policy = RelayPolicyHarness(use_harness=False)
        # Permission requests are allowed by behavioral rules alone
        result = policy.evaluate_turn("claude", PERMISSION_CONTENT, [])
        # Without harness, behavioral rules allow this (no failure/delta/promise history)
        assert result.allowed

    def test_record_outcome_flows_to_harness(self):
        """Recording a successful outcome should persist effects in the harness."""
        self.harness_policy.evaluate_turn("claude", ARTIFACT_CONTENT, [])
        self.harness_policy.record_outcome("claude", ARTIFACT_CONTENT, "success")
        # Check the harness adapter's store
        adapter = self.harness_policy._harness_adapter
        assert adapter is not None
        effects = adapter.harness.store.effects
        assert len(effects) == 1
