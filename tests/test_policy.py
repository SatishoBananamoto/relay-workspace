"""Test matrix: each test reproduces a failure mode from the relay session
and proves the corresponding policy rule catches it.

FM-1: Repetition without delta        → OutputDeltaRule
FM-2: Promise without delivery         → PromiseBreachRule
FM-3: Action insensitivity to feedback → RepeatedFailureRule
FM-4: Topic abandonment                → CommitmentConflictRule
FM-5: Performative rigor (no delta)    → OutputDeltaRule
FM-6: Scope creep in analysis          → ConvergenceRule
FM-7: Workspace without use            → (structural, not a rule — tested via integration)
"""

from __future__ import annotations

import time

import pytest

from relay_discussion.policy import (
    ActionOutcome,
    Commitment,
    CommitmentConflictRule,
    ConvergenceRule,
    Decision,
    Obligation,
    ObligationStore,
    OutputDeltaRule,
    PolicyEngine,
    PolicyResult,
    PromiseBreachRule,
    RepeatedFailureRule,
    content_hash,
)
from relay_discussion.policy_relay import (
    RelayPolicyHarness,
    classify_relay_action,
    detect_promises,
)


# ---------------------------------------------------------------------------
# FM-3: RepeatedFailureRule
# ---------------------------------------------------------------------------

class TestRepeatedFailureRule:
    """Reproduces Claude's permission loop: same action, same args, denied 15x."""

    def test_allows_first_two_failures(self):
        rule = RepeatedFailureRule(max_consecutive=3)
        history = [
            ActionOutcome("request_permission", "abc", "denied", time.time()),
            ActionOutcome("request_permission", "abc", "denied", time.time()),
        ]
        result = rule.evaluate("request_permission", {}, history, [], [])
        assert result.allowed

    def test_blocks_after_three_consecutive_failures(self):
        rule = RepeatedFailureRule(max_consecutive=3)
        history = [
            ActionOutcome("request_permission", "abc", "denied", time.time()),
            ActionOutcome("request_permission", "abc", "denied", time.time()),
            ActionOutcome("request_permission", "abc", "denied", time.time()),
        ]
        result = rule.evaluate("request_permission", {}, history, [], [])
        assert result.decision == Decision.FORCE_CHANGE
        assert result.blockers[0].detail.startswith("request_permission failed 3x")

    def test_resets_on_success(self):
        rule = RepeatedFailureRule(max_consecutive=3)
        history = [
            ActionOutcome("request_permission", "abc", "denied", time.time()),
            ActionOutcome("request_permission", "abc", "denied", time.time()),
            ActionOutcome("request_permission", "abc", "success", time.time()),
            ActionOutcome("request_permission", "abc", "denied", time.time()),
        ]
        result = rule.evaluate("request_permission", {}, history, [], [])
        assert result.allowed

    def test_different_action_type_doesnt_count(self):
        rule = RepeatedFailureRule(max_consecutive=3)
        history = [
            ActionOutcome("request_permission", "abc", "denied", time.time()),
            ActionOutcome("analyze", "xyz", "success", time.time()),
            ActionOutcome("request_permission", "abc", "denied", time.time()),
        ]
        result = rule.evaluate("request_permission", {}, history, [], [])
        assert result.allowed


# ---------------------------------------------------------------------------
# FM-1 + FM-5: OutputDeltaRule
# ---------------------------------------------------------------------------

class TestOutputDeltaRule:
    """Reproduces Codex's repetition: same findings block repeated 15x."""

    def test_allows_first_output(self):
        rule = OutputDeltaRule(max_identical=2)
        history = []
        result = rule.evaluate("analyze", {"_content_hash": "aaa"}, history, [], [])
        assert result.allowed

    def test_allows_different_content(self):
        rule = OutputDeltaRule(max_identical=2)
        history = [
            ActionOutcome("analyze", "h", "success", time.time(), content_hash="aaa"),
            ActionOutcome("analyze", "h", "success", time.time(), content_hash="bbb"),
        ]
        result = rule.evaluate("analyze", {"_content_hash": "ccc"}, history, [], [])
        assert result.allowed

    def test_blocks_after_two_identical_outputs(self):
        rule = OutputDeltaRule(max_identical=2)
        ch = "same_hash"
        history = [
            ActionOutcome("analyze", "h", "success", time.time(), content_hash=ch),
            ActionOutcome("analyze", "h", "success", time.time(), content_hash=ch),
        ]
        result = rule.evaluate("analyze", {"_content_hash": ch}, history, [], [])
        assert result.decision == Decision.FORCE_CHANGE

    def test_no_content_hash_is_allowed(self):
        """If content hash isn't provided, rule is skipped (graceful degradation)."""
        rule = OutputDeltaRule(max_identical=2)
        history = [
            ActionOutcome("analyze", "h", "success", time.time(), content_hash="aaa"),
            ActionOutcome("analyze", "h", "success", time.time(), content_hash="aaa"),
        ]
        result = rule.evaluate("analyze", {}, history, [], [])
        assert result.allowed


# ---------------------------------------------------------------------------
# FM-2: PromiseBreachRule
# ---------------------------------------------------------------------------

class TestPromiseBreachRule:
    """Reproduces Codex's "I'll produce a patch" repeated 10x without delivery."""

    def test_allows_first_promise(self):
        rule = PromiseBreachRule(max_repeats=2)
        history = [
            ActionOutcome("analyze", "h", "success", time.time(), promises=("produce_artifact",)),
        ]
        obligations = [Obligation(id="o1", source_action_type="analyze", kind="produce_artifact", status="open")]
        result = rule.evaluate("analyze", {}, history, obligations, [])
        assert result.allowed

    def test_blocks_repeated_promise_without_delivery(self):
        rule = PromiseBreachRule(max_repeats=2)
        history = [
            ActionOutcome("analyze", "h", "success", time.time(), promises=("produce_artifact",)),
            ActionOutcome("analyze", "h", "success", time.time(), promises=("produce_artifact",)),
        ]
        obligations = [Obligation(id="o1", source_action_type="analyze", kind="produce_artifact", status="open")]
        result = rule.evaluate("analyze", {}, history, obligations, [])
        assert result.decision == Decision.FORCE_CHANGE
        assert "produce_artifact" in result.blockers[0].detail

    def test_allows_if_obligation_satisfied(self):
        rule = PromiseBreachRule(max_repeats=2)
        history = [
            ActionOutcome("analyze", "h", "success", time.time(), promises=("produce_artifact",)),
            ActionOutcome("analyze", "h", "success", time.time(), promises=("produce_artifact",)),
        ]
        obligations = [Obligation(id="o1", source_action_type="analyze", kind="produce_artifact", status="satisfied")]
        result = rule.evaluate("analyze", {}, history, obligations, [])
        assert result.allowed


# ---------------------------------------------------------------------------
# FM-4: CommitmentConflictRule
# ---------------------------------------------------------------------------

class TestCommitmentConflictRule:
    """Reproduces topic abandonment: session committed to architecture,
    agents pivoted to relay bugs without acknowledgment."""

    def test_no_conflict_when_no_commitments(self):
        rule = CommitmentConflictRule()
        result = rule.evaluate("discuss", {"_entity_ids": ("session",)}, [], [], [])
        assert result.allowed

    def test_detects_topic_conflict(self):
        rule = CommitmentConflictRule()
        topic_commitment = Commitment(
            id="c1",
            kind="active_topic",
            entity_ids=("session",),
            constrains_action_types=("discuss", "analyze"),
            fields={"topic": "intent-to-action architecture"},
        )
        result = rule.evaluate(
            "discuss",
            {"_entity_ids": ("session",)},
            [], [],
            [topic_commitment],
        )
        assert result.decision == Decision.CLARIFY

    def test_ignores_expired_commitment(self):
        rule = CommitmentConflictRule()
        expired = Commitment(
            id="c1",
            kind="active_topic",
            entity_ids=("session",),
            constrains_action_types=("discuss",),
            fields={"topic": "old topic"},
            expires_at=0.0,  # expired
        )
        result = rule.evaluate("discuss", {"_entity_ids": ("session",)}, [], [], [expired])
        assert result.allowed

    def test_ignores_unrelated_action_type(self):
        rule = CommitmentConflictRule()
        commitment = Commitment(
            id="c1",
            kind="pricing",
            entity_ids=("client-1",),
            constrains_action_types=("send_quote",),
            fields={"price": 50},
        )
        # "discuss" is not in constrains_action_types
        result = rule.evaluate("discuss", {"_entity_ids": ("client-1",)}, [], [], [commitment])
        assert result.allowed


# ---------------------------------------------------------------------------
# FM-6: ConvergenceRule
# ---------------------------------------------------------------------------

class TestConvergenceRule:
    """Reproduces scope creep: 15+ analysis turns without any execution."""

    def test_allows_initial_analysis(self):
        rule = ConvergenceRule(max_analysis_streak=4)
        history = [
            ActionOutcome("analyze", "h", "success", time.time()),
            ActionOutcome("analyze", "h", "success", time.time()),
        ]
        result = rule.evaluate("analyze", {}, history, [], [])
        assert result.allowed

    def test_blocks_after_streak(self):
        rule = ConvergenceRule(max_analysis_streak=4)
        history = [
            ActionOutcome("analyze", "h", "success", time.time()),
            ActionOutcome("analyze", "h", "success", time.time()),
            ActionOutcome("analyze", "h", "success", time.time()),
            ActionOutcome("analyze", "h", "success", time.time()),
        ]
        result = rule.evaluate("analyze", {}, history, [], [])
        assert result.decision == Decision.FORCE_CHANGE

    def test_resets_on_execution(self):
        rule = ConvergenceRule(max_analysis_streak=4)
        history = [
            ActionOutcome("analyze", "h", "success", time.time()),
            ActionOutcome("analyze", "h", "success", time.time()),
            ActionOutcome("analyze", "h", "success", time.time()),
            ActionOutcome("produce_artifact", "h", "success", time.time()),
            ActionOutcome("analyze", "h", "success", time.time()),
        ]
        result = rule.evaluate("analyze", {}, history, [], [])
        assert result.allowed

    def test_ignores_non_analysis_actions(self):
        rule = ConvergenceRule(max_analysis_streak=4)
        history = [
            ActionOutcome("analyze", "h", "success", time.time()),
        ] * 10
        # Proposing a produce action, not analysis — should pass
        result = rule.evaluate("produce_artifact", {}, history, [], [])
        assert result.allowed


# ---------------------------------------------------------------------------
# ObligationStore
# ---------------------------------------------------------------------------

class TestObligationStore:

    def test_add_and_query(self):
        store = ObligationStore()
        store.add_obligation(
            source_action_type="analyze",
            kind="produce_artifact",
            entity_ids=("Claude",),
        )
        results = store.query_obligations(action_types=("analyze",))
        assert len(results) == 1
        assert results[0].kind == "produce_artifact"

    def test_query_filters_by_status(self):
        store = ObligationStore()
        ob = store.add_obligation(
            source_action_type="analyze",
            kind="produce_artifact",
        )
        store.satisfy(ob.id)
        results = store.query_obligations(action_types=("analyze",), status="open")
        assert len(results) == 0

    def test_check_deadlines(self):
        store = ObligationStore()
        store.add_obligation(
            source_action_type="analyze",
            kind="produce_artifact",
            due_at=time.time() - 100,  # past deadline
        )
        store.add_obligation(
            source_action_type="analyze",
            kind="something_else",
            due_at=time.time() + 1000,  # future deadline
        )
        breached = store.check_deadlines()
        assert len(breached) == 1
        assert breached[0].kind == "produce_artifact"

    def test_export_restore_roundtrip(self):
        store = ObligationStore()
        store.add_obligation(
            source_action_type="analyze",
            kind="produce_artifact",
            entity_ids=("Claude",),
        )
        store.add_commitment(
            kind="active_topic",
            entity_ids=("session",),
            constrains_action_types=("discuss",),
            fields={"topic": "architecture"},
        )

        state = store.export_state()
        new_store = ObligationStore()
        new_store.restore_state(state)

        obs = new_store.query_obligations()
        assert len(obs) == 1
        cmts = new_store.query_commitments()
        assert len(cmts) == 1
        assert cmts[0].fields["topic"] == "architecture"


# ---------------------------------------------------------------------------
# PolicyEngine (integration)
# ---------------------------------------------------------------------------

class TestPolicyEngine:
    """Full engine with default rules — reproduces the relay session cascade."""

    def test_allows_normal_action(self):
        engine = PolicyEngine()
        result = engine.evaluate("discuss", {}, [])
        assert result.allowed

    def test_catches_permission_loop(self):
        """Reproduces the exact failure: 3 denied permission requests."""
        engine = PolicyEngine()
        history = [
            ActionOutcome("request_permission", "same", "denied", time.time()),
            ActionOutcome("request_permission", "same", "denied", time.time()),
            ActionOutcome("request_permission", "same", "denied", time.time()),
        ]
        result = engine.evaluate("request_permission", {}, history)
        assert not result.allowed
        assert result.decision == Decision.FORCE_CHANGE

    def test_catches_analysis_loop(self):
        """Reproduces scope creep: 4+ analysis turns without execution."""
        engine = PolicyEngine()
        history = [ActionOutcome("analyze", "h", "success", time.time()) for _ in range(4)]
        result = engine.evaluate("analyze", {}, history)
        assert result.decision == Decision.FORCE_CHANGE


# ---------------------------------------------------------------------------
# Relay integration helpers
# ---------------------------------------------------------------------------

class TestClassifyRelayAction:

    def test_permission_request(self):
        from relay_discussion.models import Message
        msg = Message(seq=1, timestamp="", role="agent", author="Claude",
                      content="I need write permission to relay_discussion/engine.py")
        assert classify_relay_action(msg, []) == "request_permission"

    def test_artifact_production(self):
        from relay_discussion.models import Message
        msg = Message(seq=1, timestamp="", role="agent", author="Codex",
                      content="Here is the fix:\n```python\ndef _get_provider(self):\n    pass\n```")
        assert classify_relay_action(msg, []) == "produce_artifact"

    def test_analysis(self):
        from relay_discussion.models import Message
        msg = Message(seq=1, timestamp="", role="agent", author="Codex",
                      content="The weak assumption to kill is that provider caching makes sessions persistent. Findings show...")
        assert classify_relay_action(msg, []) == "analyze"


class TestDetectPromises:

    def test_detects_produce_artifact(self):
        text = "I can turn this into an exact patch plan against engine.py"
        promises = detect_promises(text)
        assert "produce_artifact" in promises

    def test_detects_permission_request(self):
        text = "I need write permission to the relay workspace"
        promises = detect_promises(text)
        assert "request_permission" in promises

    def test_no_promises_in_neutral_text(self):
        text = "The architecture has three components."
        promises = detect_promises(text)
        assert promises == []


# ---------------------------------------------------------------------------
# RelayPolicyHarness (end-to-end)
# ---------------------------------------------------------------------------

class TestRelayPolicyHarness:
    """End-to-end: simulate the relay session and prove the harness catches failures."""

    def test_catches_permission_loop_end_to_end(self):
        """Simulate Claude asking for write permission 5 times."""
        harness = RelayPolicyHarness()

        permission_msg = "I need write permission to relay_discussion/engine.py. Please approve."

        # First 3 attempts: record as denied
        for _ in range(3):
            harness.record_outcome("Claude", permission_msg, "denied", "request_permission")

        # 4th attempt: policy blocks it
        from relay_discussion.models import Message
        result = harness.evaluate_turn("Claude", permission_msg, [])
        assert not result.allowed
        assert result.decision == Decision.FORCE_CHANGE

    def test_catches_repetition_end_to_end(self):
        """Simulate Codex producing identical analysis 3 times."""
        harness = RelayPolicyHarness()

        findings = (
            "**Findings**\n"
            "- Same-process provider continuity is broken in engine.py\n"
            "- Cross-process resume is still false\n"
            "- Non-TUI relay resume is broken\n"
            "**Build Order**\n1. Fix runtime truth\n2. Make resume real"
        )

        harness.record_outcome("Codex", findings, "success")
        harness.record_outcome("Codex", findings, "success")

        result = harness.evaluate_turn("Codex", findings, [])
        assert not result.allowed

    def test_allows_genuine_progress(self):
        """Different content each turn should be fine."""
        harness = RelayPolicyHarness()

        harness.record_outcome("Claude", "Analysis of bug #1: provider caching is broken", "success")
        harness.record_outcome("Claude", "Here is the fix:\n```python\ndef cached(): pass\n```", "success")

        result = harness.evaluate_turn(
            "Claude",
            "Now testing the fix with pytest. Results: all passing.",
            [],
        )
        assert result.allowed
