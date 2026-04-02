"""End-to-end test: relay session with intent-to-action harness enabled.

Proves the full stack works together — not just unit tests, but
the relay engine running turns through both behavioral rules and the
harness pipeline, with effects persisted and obligations tracked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from relay_discussion.engine import RelayRunner
from relay_discussion.models import AgentConfig, Message, RelayConfig
from relay_discussion.providers import BaseProvider


# ---------------------------------------------------------------------------
# Scripted provider — returns pre-defined responses per turn
# ---------------------------------------------------------------------------

class ScriptedProvider(BaseProvider):
    """Provider that returns exact scripted responses per (agent, turn)."""

    def __init__(self, scripts: dict[str, list[str]]) -> None:
        self._scripts = scripts  # {agent_name: [turn1_response, turn2_response, ...]}
        self._turn_index: dict[str, int] = {}

    def generate(
        self, agent: AgentConfig, transcript: Sequence[Message], turn: int,
    ) -> str:
        idx = self._turn_index.get(agent.name, 0)
        responses = self._scripts.get(agent.name, [])
        if idx >= len(responses):
            return f"{agent.name}: (no more scripted responses)"
        self._turn_index[agent.name] = idx + 1
        return responses[idx]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(
    tmp_path: Path,
    scripts: dict[str, list[str]],
    turns: int = 6,
    use_harness: bool = True,
) -> RelayRunner:
    """Build a RelayRunner with scripted responses and harness enabled."""
    config = RelayConfig(
        topic="Build a shared data model for the relay workspace.",
        turns=turns,
        left_agent=AgentConfig(name="Claude", provider="mock"),
        right_agent=AgentConfig(name="Codex", provider="mock"),
        use_harness=use_harness,
    )
    out_path = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out_path)

    # Inject scripted provider
    provider = ScriptedProvider(scripts)
    runner._providers["left"] = provider
    runner._providers["right"] = provider

    return runner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEndToEndHarness:
    """Full relay sessions with the intent-to-action harness active."""

    def test_artifact_turn_creates_obligation(self, tmp_path):
        """When an agent produces code, the harness creates a review obligation."""
        scripts = {
            "Claude": [
                # Turn 1: produces a Python artifact
                "Here's the data model:\n\n```python\nclass Entity:\n    id: str\n    name: str\n```\n\nThis covers the base case.",
                # Turn 3: discussion (bypasses harness)
                "Good point about validation. The weak assumption is that all IDs are unique.",
                # Turn 5: another artifact
                "Updated model:\n\n```python\nclass Entity:\n    id: str\n    name: str\n    validated: bool = False\n```",
            ],
            "Codex": [
                # Turn 2: analysis (bypasses harness)
                "The findings show three gaps: no validation, no versioning, no audit trail.",
                # Turn 4: fix
                "I'll fix the validation gap. Here's the corrected approach with runtime checks.",
                # Turn 6: discussion
                "Agreed. The build order should be: schema first, then validators, then audit.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=6)
        result = runner.run()

        assert result.status == "completed"

        # Verify harness adapter has tracked effects
        adapter = runner._policy._harness_adapter
        assert adapter is not None

        # Check that effects were persisted in the harness store
        store = adapter.harness.store
        all_effects = store.effects
        # Claude's artifact turns (1, 5) and Codex's discussion/analysis turns
        # only artifact turns create effects through the harness
        artifact_effects = [e for e in all_effects if e.action_type == "produce_artifact"]
        assert len(artifact_effects) >= 1, f"Expected artifact effects, got {[e.action_type for e in all_effects]}"

        # Check obligations were created for artifact review
        obligations = []
        for effect in artifact_effects:
            obligations.extend(effect.obligations)
        assert any(o.kind == "review_artifact" for o in obligations), \
            f"Expected review_artifact obligation, got {[o.kind for o in obligations]}"

    def test_permission_request_blocked_by_harness(self, tmp_path):
        """Permission requests cause a pause — harness routes APPROVE → CLARIFY."""
        scripts = {
            "Claude": [
                # Turn 1: permission request
                "I need write permission to modify the shared config. Please approve the write.",
            ],
            "Codex": [
                "I see the config issue. The build order matters here.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=4)
        result = runner.run()

        # Harness says APPROVE for request_permission → maps to CLARIFY
        # → engine pauses for human input
        assert result.status == "paused"
        assert "clarification" in result.pause_reason.lower()

    def test_permission_request_blocked_without_harness(self, tmp_path):
        """Without harness, permission requests are hardcode-blocked by engine."""
        scripts = {
            "Claude": [
                "I need write permission to modify the shared config. Please approve the write.",
                "Fine. Let me analyze the config instead.",
            ],
            "Codex": [
                "I see the config issue.",
                "Good analysis. The findings confirm the dependency.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=4, use_harness=False)
        result = runner.run()

        assert result.status == "completed"
        # Engine's hardcoded block still works
        system_messages = [m for m in result.messages if m.role == "system"]
        policy_gates = [m for m in system_messages if m.metadata.get("kind") == "policy_gate"]
        assert len(policy_gates) >= 1
        assert policy_gates[0].metadata.get("action_type") == "request_permission"

    def test_mixed_session_behavioral_and_harness(self, tmp_path):
        """Session with mixed action types: some go through harness, some through behavioral rules only."""
        scripts = {
            "Claude": [
                "The weak assumption is the lack of idempotency in the write path.",
                "```python\ndef safe_write(data):\n    if not validate(data): raise ValueError\n    return persist(data)\n```",
                "I'll fix the race condition in safe_write.",
            ],
            "Codex": [
                "Agreed. The findings point to three race conditions in concurrent writes.",
                "Good implementation. But what's actually broken is the lock ordering.",
                "The build order should be: fix locks first, then add idempotency.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=6)
        result = runner.run()

        assert result.status == "completed"

        # Count committed agent messages
        agent_messages = [m for m in result.messages if m.role == "agent"]
        # All 6 should succeed (analysis/discussion bypass harness, artifacts go through)
        assert len(agent_messages) == 6

        # Verify harness tracked the artifact turn
        store = runner._policy._harness_adapter.harness.store
        effects = store.effects
        action_types = [e.action_type for e in effects]
        assert "produce_artifact" in action_types

    def test_harness_disabled_baseline(self, tmp_path):
        """Without harness, the relay still works (backward compatibility)."""
        scripts = {
            "Claude": [
                "```python\nclass Model:\n    pass\n```",
                "Updated the model with validation.",
            ],
            "Codex": [
                "The findings show the model needs more fields.",
                "Good. Build order: fields first, then validation.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=4, use_harness=False)
        result = runner.run()

        assert result.status == "completed"
        assert runner._policy._harness_adapter is None
        agent_messages = [m for m in result.messages if m.role == "agent"]
        assert len(agent_messages) == 4

    def test_obligation_engine_ticks_on_execution(self, tmp_path):
        """The obligation engine's scheduler ticks at natural boundaries (execution)."""
        scripts = {
            "Claude": [
                "```python\nclass Config:\n    version: int = 1\n```\n\nFirst draft of config model.",
            ],
            "Codex": [
                "The findings show version field needs to be immutable.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=2)
        result = runner.run()

        assert result.status == "completed"

        # The harness should have created an obligation from the artifact
        store = runner._policy._harness_adapter.harness.store
        effects = store.effects
        artifact_effects = [e for e in effects if e.action_type == "produce_artifact"]
        assert len(artifact_effects) == 1

        obligations = artifact_effects[0].obligations
        assert len(obligations) >= 1
        # Obligation should still be open (not enough time has passed)
        assert obligations[0].status.value == "open"

    def test_multiple_artifacts_tracked_separately(self, tmp_path):
        """Each artifact production creates its own effect with its own obligation."""
        scripts = {
            "Claude": [
                "```python\nclass ModelA:\n    pass\n```",
                "```python\nclass ModelB:\n    pass\n```",
                "```python\nclass ModelC:\n    pass\n```",
            ],
            "Codex": [
                "Good start on ModelA. The findings are positive.",
                "ModelB looks solid. Build order is correct.",
                "ModelC completes the set. All verified defects addressed.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=6)
        result = runner.run()

        assert result.status == "completed"

        store = runner._policy._harness_adapter.harness.store
        effects = store.effects
        artifact_effects = [e for e in effects if e.action_type == "produce_artifact"]
        # Claude produces artifacts on turns 1, 3, 5
        assert len(artifact_effects) == 3

        # Each should have its own obligation
        all_obligations = []
        for e in artifact_effects:
            all_obligations.extend(e.obligations)
        assert len(all_obligations) == 3
        # All obligation IDs should be unique
        oids = [o.obligation_id for o in all_obligations]
        assert len(set(oids)) == 3

    def test_analysis_tracked_through_harness(self, tmp_path):
        """Analysis turns now go through the harness (low blast, no approval)."""
        scripts = {
            "Claude": [
                "The weak assumption is that we never validate input schemas.",
                "The findings confirm the gap. Build order: validate first.",
            ],
            "Codex": [
                "Agreed. Here's what's actually broken in the validation layer.",
                "Good. The verified defect is in the schema parser.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=4)
        result = runner.run()

        assert result.status == "completed"
        store = runner._policy._harness_adapter.harness.store
        effects = store.effects
        analyze_effects = [e for e in effects if e.action_type == "analyze"]
        assert len(analyze_effects) >= 2, f"Expected analysis effects, got {[e.action_type for e in effects]}"

    def test_escalation_pauses_session(self, tmp_path):
        """Escalation triggers APPROVE → CLARIFY → pause."""
        scripts = {
            "Claude": [
                "I'm blocked and cannot proceed without human guidance on the API design.",
            ],
            "Codex": [
                "I agree, we need human input here.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=4)
        result = runner.run()

        # Escalation (high blast, always approve) → CLARIFY → pause
        assert result.status == "paused"

    def test_config_flag_enables_harness(self, tmp_path):
        """use_harness=True in RelayConfig activates the harness (no monkey-patching)."""
        scripts = {
            "Claude": [
                "```python\nclass Config:\n    pass\n```",
            ],
            "Codex": [
                "Good structure. The findings look clean.",
            ],
        }
        runner = _make_runner(tmp_path, scripts, turns=2, use_harness=True)
        result = runner.run()

        assert result.status == "completed"
        assert runner._policy._harness_adapter is not None
        effects = runner._policy._harness_adapter.harness.store.effects
        assert len(effects) >= 1
