"""Tests for the adapter authoring SDK."""

from __future__ import annotations

import pytest
from harness.sdk import action, ActionHandle, EffectBuilder
from harness.types import (
    ActionSpec,
    ApprovalPolicy,
    BlastRadius,
    Blocker,
    CheckKind,
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
from harness.core import Harness
from harness.store import InMemoryEffectStore


# ---------------------------------------------------------------------------
# EffectBuilder tests
# ---------------------------------------------------------------------------

class TestEffectBuilder:
    def test_mutate(self):
        fx = EffectBuilder(Resolution(), "2025-01-01T00:00:00+00:00")
        fx.mutate("workspace", "write", "wrote a file")
        assert len(fx.mutations) == 1
        assert fx.mutations[0].resource == "workspace"
        assert fx.mutations[0].op == "write"

    def test_commit(self):
        res = Resolution(entity_ids=["user:1"], resource_keys=["prod:A"])
        fx = EffectBuilder(res, "2025-01-01T00:00:00+00:00")
        fx.commit("quote", {"price": 100}, expires_at="2025-02-01T00:00:00+00:00")
        assert len(fx.commitments) == 1
        assert fx.commitments[0].kind == "quote"
        assert fx.commitments[0].entity_ids == ["user:1"]
        assert fx.commitments[0].fields["price"] == 100

    def test_obligate_relative(self):
        fx = EffectBuilder(Resolution(), "2025-01-01T00:00:00+00:00")
        fx.obligate("review", due_minutes=30, verify="poll", failure_mode="Not reviewed")
        assert len(fx.obligations) == 1
        assert "2025-01-01T00:30" in fx.obligations[0].due_at
        assert fx.obligations[0].verify_with == VerifyWith.POLL

    def test_obligate_absolute(self):
        fx = EffectBuilder(Resolution(), "2025-01-01T00:00:00+00:00")
        fx.obligate("review", due_at="2025-06-01T00:00:00+00:00", verify=VerifyWith.HUMAN)
        assert fx.obligations[0].due_at == "2025-06-01T00:00:00+00:00"
        assert fx.obligations[0].verify_with == VerifyWith.HUMAN

    def test_build_returns_tuple(self):
        fx = EffectBuilder(Resolution(), "2025-01-01T00:00:00+00:00")
        fx.mutate("a", "b", "c")
        fx.commit("k", {"x": 1})
        fx.obligate("o", due_minutes=5, verify="query")
        m, c, o = fx.build()
        assert len(m) == 1
        assert len(c) == 1
        assert len(o) == 1

    def test_resolution_properties(self):
        res = Resolution(
            entity_ids=["e1"], resource_keys=["r1"], semantic_keys=["s1"],
        )
        fx = EffectBuilder(res, "2025-01-01T00:00:00+00:00")
        assert fx.entity_ids == ["e1"]
        assert fx.resource_keys == ["r1"]
        assert fx.semantic_keys == ["s1"]
        assert fx.now == "2025-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# @action decorator tests
# ---------------------------------------------------------------------------

class TestActionDecorator:
    def test_basic_registration(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_basic", blast_radius="low", registry=reg)
        def test_basic(args, resolution, now_iso, fx):
            fx.mutate("ws", "op", "summary")

        assert "test_basic" in reg
        assert reg["test_basic"].action_type == "test_basic"
        assert reg["test_basic"].blast_radius == BlastRadius.LOW

    def test_returns_action_handle(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_handle", registry=reg)
        def test_handle(args, resolution, now_iso, fx):
            pass

        assert isinstance(test_handle, ActionHandle)
        assert test_handle.action_type == "test_handle"

    def test_effect_template_works(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_effect", blast_radius="medium", registry=reg)
        def test_effect(args, resolution, now_iso, fx):
            fx.mutate("workspace", "write", f"wrote {args['file']}")
            fx.obligate("review", due_minutes=5, verify="poll", failure_mode="Not reviewed")

        spec = reg["test_effect"]
        m, c, o = spec.effect_template(
            {"file": "main.py"},
            Resolution(),
            "2025-01-01T00:00:00+00:00",
        )
        assert len(m) == 1
        assert m[0].summary == "wrote main.py"
        assert len(o) == 1
        assert o[0].kind == "review"

    def test_defaults_inferred(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_defaults", blast_radius="high", registry=reg)
        def test_defaults(args, resolution, now_iso, fx):
            pass

        spec = reg["test_defaults"]
        assert spec.feedback_latency == FeedbackLatency.SLOW  # inferred from high
        assert spec.version == "1"
        assert spec.required_args == []
        assert spec.approval_policy == ApprovalPolicy.IF_HIGH_RISK

    def test_feedback_latency_override(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_latency", blast_radius="high", feedback_latency="fast", registry=reg)
        def test_latency(args, resolution, now_iso, fx):
            pass

        assert reg["test_latency"].feedback_latency == FeedbackLatency.FAST

    def test_entity_selectors(self):
        reg: dict[str, ActionSpec] = {}

        @action(
            "test_selectors",
            entities={"userId": "one", "teamIds": "many"},
            resources={"fileId": "one"},
            registry=reg,
        )
        def test_selectors(args, resolution, now_iso, fx):
            pass

        spec = reg["test_selectors"]
        assert SelectorSpec("userId", "one") in spec.entity_selectors
        assert SelectorSpec("teamIds", "many") in spec.entity_selectors
        assert SelectorSpec("fileId", "one") in spec.resource_selectors

    def test_checks_from_dicts(self):
        reg: dict[str, ActionSpec] = {}

        @action(
            "test_checks",
            checks=[
                {"id": "dry_run", "kind": "dry_run", "required_for": ["allow", "approve"]},
                {"id": "ext_review", "kind": "lookup", "mode": "external"},
            ],
            registry=reg,
        )
        def test_checks(args, resolution, now_iso, fx):
            pass

        spec = reg["test_checks"]
        assert len(spec.cheap_checks) == 2
        assert spec.cheap_checks[0].id == "dry_run"
        assert spec.cheap_checks[0].kind == CheckKind.DRY_RUN
        assert Decision.APPROVE in spec.cheap_checks[0].required_for
        assert spec.cheap_checks[1].mode.value == "external"

    def test_approval_never(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_approval", approval="never", registry=reg)
        def test_approval(args, resolution, now_iso, fx):
            pass

        assert reg["test_approval"].approval_policy == ApprovalPolicy.NEVER

    def test_required_args(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_req", required_args=["name", "email"], registry=reg)
        def test_req(args, resolution, now_iso, fx):
            pass

        assert reg["test_req"].required_args == ["name", "email"]


# ---------------------------------------------------------------------------
# Hook decorator tests
# ---------------------------------------------------------------------------

class TestHookDecorators:
    def test_precondition(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_pre", registry=reg)
        def test_pre(args, resolution, now_iso, fx):
            pass

        @test_pre.precondition
        def check_it(proposal, resolution):
            if not proposal.args.get("required_field"):
                return [Blocker.MISSING_REQUIRED_ARG]
            return []

        spec = reg["test_pre"]
        assert spec.preconditions is not None
        blockers = spec.preconditions(
            Proposal(proposal_id="p1", action_type="test_pre", args={}),
            Resolution(),
        )
        assert Blocker.MISSING_REQUIRED_ARG in blockers

        no_blockers = spec.preconditions(
            Proposal(proposal_id="p2", action_type="test_pre", args={"required_field": "yes"}),
            Resolution(),
        )
        assert no_blockers == []

    def test_approval_gate(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_gate", blast_radius="medium", approval="if_high_risk", registry=reg)
        def test_gate(args, resolution, now_iso, fx):
            pass

        @test_gate.approval_gate
        def needs_approval(proposal, resolution):
            return proposal.args.get("dangerous", False)

        spec = reg["test_gate"]
        assert spec.requires_approval is not None
        assert spec.requires_approval(
            Proposal(proposal_id="p1", action_type="test_gate", args={"dangerous": True}),
            Resolution(),
        ) is True
        assert spec.requires_approval(
            Proposal(proposal_id="p2", action_type="test_gate", args={}),
            Resolution(),
        ) is False

    def test_conflict_check(self):
        reg: dict[str, ActionSpec] = {}

        @action("test_conflict", registry=reg)
        def test_conflict(args, resolution, now_iso, fx):
            pass

        @test_conflict.conflict_check
        def detect_conflict(proposal, resolution, commitments):
            return any(c.kind == "quote" for c in commitments)

        spec = reg["test_conflict"]
        assert spec.conflict_detector is not None
        assert spec.conflict_detector(
            Proposal(proposal_id="p1", action_type="test_conflict", args={}),
            Resolution(),
            [Commitment(
                commitment_id="c1", kind="quote",
                entity_ids=[], resource_keys=[], semantic_keys=[],
                fields={},
            )],
        ) is True


# ---------------------------------------------------------------------------
# Integration: SDK-registered action through full harness pipeline
# ---------------------------------------------------------------------------

class TestSDKIntegration:
    def test_sdk_action_through_harness(self):
        """An SDK-registered action runs through the full harness pipeline."""
        reg: dict[str, ActionSpec] = {}

        @action(
            "sdk_test_write",
            blast_radius="low",
            reversible=True,
            approval="never",
            entities={"author": "one"},
            registry=reg,
        )
        def sdk_test_write(args, resolution, now_iso, fx):
            fx.mutate("workspace", "write", f"{args['author']} wrote {args['file']}")
            fx.obligate("review", due_minutes=5, verify="poll", failure_mode="Not reviewed")

        # Inject into harness registry
        from harness.registry import REGISTRY
        REGISTRY["sdk_test_write"] = reg["sdk_test_write"]

        try:
            harness = Harness()
            result = harness.run(
                action_type="sdk_test_write",
                args={"author": "agent_a", "file": "main.py"},
                entity_map={"author": ["agent_a"]},
                semantic_keys=["write"],
                now_iso="2025-01-01T00:00:00+00:00",
            )

            assert result.execution is not None
            assert result.execution.status.value == "executed"
            assert result.execution.effect is not None
            assert len(result.execution.effect.mutations) == 1
            assert result.execution.effect.mutations[0].summary == "agent_a wrote main.py"
            assert len(result.execution.effect.obligations) == 1
            assert result.execution.effect.obligations[0].kind == "review"
        finally:
            REGISTRY.pop("sdk_test_write", None)

    def test_sdk_action_with_precondition_blocks(self):
        """SDK action with precondition blocker gets denied."""
        reg: dict[str, ActionSpec] = {}

        @action(
            "sdk_test_guarded",
            blast_radius="medium",
            approval="never",
            registry=reg,
        )
        def sdk_test_guarded(args, resolution, now_iso, fx):
            fx.mutate("workspace", "delete", "deleted something")

        @sdk_test_guarded.precondition
        def guard(proposal, resolution):
            if not proposal.args.get("confirm"):
                return [Blocker.BLAST_RADIUS_EXCEEDS_LIMIT]
            return []

        from harness.registry import REGISTRY
        REGISTRY["sdk_test_guarded"] = reg["sdk_test_guarded"]

        try:
            harness = Harness()

            # Without confirm → denied
            result = harness.run(
                action_type="sdk_test_guarded",
                args={},
                now_iso="2025-01-01T00:00:00+00:00",
            )
            assert result.evaluation.policy.decision == Decision.DENY

            # With confirm → allowed
            result = harness.run(
                action_type="sdk_test_guarded",
                args={"confirm": True},
                now_iso="2025-01-01T00:00:00+00:00",
            )
            assert result.evaluation.policy.decision == Decision.ALLOW
        finally:
            REGISTRY.pop("sdk_test_guarded", None)

    def test_sdk_action_with_approval_gate(self):
        """SDK action with custom approval gate routes to APPROVE when triggered."""
        reg: dict[str, ActionSpec] = {}

        @action(
            "sdk_test_gated",
            blast_radius="high",
            reversible=False,
            approval="if_high_risk",
            registry=reg,
        )
        def sdk_test_gated(args, resolution, now_iso, fx):
            fx.mutate("db", "drop", "dropped table")

        @sdk_test_gated.approval_gate
        def gate(proposal, resolution):
            return not proposal.args.get("backup_ref")

        from harness.registry import REGISTRY
        REGISTRY["sdk_test_gated"] = reg["sdk_test_gated"]

        try:
            harness = Harness()

            # No backup → approve required
            result = harness.run(
                action_type="sdk_test_gated",
                args={},
                now_iso="2025-01-01T00:00:00+00:00",
            )
            assert result.evaluation.policy.decision == Decision.APPROVE

            # With backup → gate says no approval, no checks defined → ALLOW
            result = harness.run(
                action_type="sdk_test_gated",
                args={"backup_ref": "s3://backups/123"},
                now_iso="2025-01-01T00:00:00+00:00",
            )
            assert result.evaluation.policy.decision == Decision.ALLOW
        finally:
            REGISTRY.pop("sdk_test_gated", None)

    def test_callable_directly(self):
        """ActionHandle is callable — the underlying function runs."""
        reg: dict[str, ActionSpec] = {}

        @action("sdk_callable", registry=reg)
        def sdk_callable(args, resolution, now_iso, fx):
            fx.mutate("test", "op", "ran")

        fx = EffectBuilder(Resolution(), "2025-01-01T00:00:00+00:00")
        sdk_callable({}, Resolution(), "2025-01-01T00:00:00+00:00", fx)
        assert len(fx.mutations) == 1
