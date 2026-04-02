"""Test matrix for the intent-to-action harness.

Maps T1–T20 from prior_work/test_matrix.md, plus adapter-specific
acceptance checks and regression checks. Failure-oriented: a case
only matters if it proves the harness rejects a bad action, routes
uncertainty correctly, or preserves future constraints.
"""

from __future__ import annotations

import pytest

from harness.core import Harness, PipelineResult
from harness.types import (
    Blocker,
    CheckResult,
    Decision,
    ExecutionStatus,
    ObligationStatus,
)


NOW = "2026-04-02T12:00:00Z"
FUTURE = "2026-04-10T12:00:00Z"
PAST = "2026-04-01T12:00:00Z"


# ── Helpers ──────────────────────────────────────────────────────────


def _quote_args(**overrides) -> dict:
    base = {
        "recipientId": "client:123",
        "productId": "product:abc",
        "unitPrice": 100,
        "currency": "USD",
        "validUntil": FUTURE,
        "termsVersion": "v2",
    }
    base.update(overrides)
    return base


def _delete_args(**overrides) -> dict:
    base = {
        "connectionId": "conn:main",
        "table": "orders",
        "predicate": "status = 'cancelled'",
        "dryRunCount": 5,
    }
    base.update(overrides)
    return base


def _meeting_args(**overrides) -> dict:
    base = {
        "attendeeIds": ["user:alice", "user:bob"],
        "startTime": FUTURE,
        "durationMinutes": 30,
        "purpose": "Sprint planning",
    }
    base.update(overrides)
    return base


def _execute_quote(h: Harness, **kwargs) -> PipelineResult:
    """Evaluate + execute a quote through the full pipeline."""
    args = _quote_args(**kwargs.pop("args_overrides", {}))
    ev = h.evaluate(
        action_type="SendQuoteEmail",
        args=args,
        entity_map=kwargs.get("entity_map", {"recipientId": ["client:123"]}),
        resource_map=kwargs.get("resource_map", {"productId": ["product:abc"]}),
        semantic_keys=kwargs.get("semantic_keys", ["quote"]),
        now_iso=kwargs.get("now_iso", NOW),
    )
    return h.execute(
        ev,
        check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
        now_iso=kwargs.get("now_iso", NOW),
    )


# ── T1: Quote missing validUntil → clarify + missing_required_arg ────


class TestT1:
    def test_missing_valid_until_triggers_clarify(self):
        h = Harness()
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(validUntil=""),
            entity_map={"recipientId": ["client:123"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CLARIFY
        assert Blocker.MISSING_REQUIRED_ARG in result.policy.blockers

    def test_missing_currency_also_triggers(self):
        h = Harness()
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(currency=""),
            entity_map={"recipientId": ["client:123"]},
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CLARIFY
        assert any("missing:currency" in rc for rc in result.policy.reason_codes)


# ── T2: Quote with ambiguous client → clarify + entity_resolution_conflict ─


class TestT2:
    def test_ambiguous_client_triggers_clarify(self):
        h = Harness()
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(),
            entity_map={"recipientId": ["client:123", "client:456"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CLARIFY
        assert Blocker.ENTITY_RESOLUTION_CONFLICT in result.policy.blockers


# ── T3: Conflicting quote → deny + commitment_conflict ───────────────


class TestT3:
    def test_conflicting_quote_denied(self):
        h = Harness()
        # Execute first quote at $100
        _execute_quote(h, now_iso=NOW)

        # Propose conflicting quote at $200 on same client/product
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(unitPrice=200),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.DENY
        assert Blocker.COMMITMENT_CONFLICT in result.policy.blockers

    def test_same_terms_no_conflict(self):
        """Identical price+terms should not trigger conflict."""
        h = Harness()
        _execute_quote(h, now_iso=NOW)

        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(),  # same price and terms
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        # No conflict — should proceed to check
        assert result.policy.decision != Decision.DENY


# ── T4: Quote all fields, price source stale → check ─────────────────


class TestT4:
    def test_valid_quote_routes_to_check(self):
        """All fields present, no conflicts → CHECK because cheap checks exist."""
        h = Harness()
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CHECK
        assert "pricing_source_lookup" in result.policy.required_checks


# ── T5: Quote send succeeds → allow, persist commitment + obligation ──


class TestT5:
    def test_quote_execution_persists_effects(self):
        h = Harness()
        pipeline = _execute_quote(h, now_iso=NOW)

        assert pipeline.execution is not None
        assert pipeline.execution.status == ExecutionStatus.EXECUTED

        effect = pipeline.execution.effect
        assert effect is not None
        assert len(effect.commitments) == 1
        assert len(effect.obligations) == 1
        assert effect.commitments[0].kind == "quote"
        assert effect.obligations[0].kind == "quote_acknowledgement"
        assert effect.obligations[0].status == ObligationStatus.OPEN


# ── T6: No reply by deadline → obligation breached ───────────────────


class TestT6:
    def test_obligation_breached_past_due(self):
        h = Harness()
        # Execute quote with validUntil = FUTURE
        _execute_quote(h, now_iso=NOW)

        # Time passes beyond the obligation deadline
        far_future = "2026-04-15T00:00:00Z"
        checks = h.obligations.check_due(far_future)
        assert len(checks) == 1
        assert checks[0].new_status == ObligationStatus.BREACHED

    def test_escalation_on_breach(self):
        h = Harness()
        _execute_quote(h, now_iso=NOW)

        far_future = "2026-04-15T00:00:00Z"
        escalations = h.obligations.get_escalations(far_future)
        assert len(escalations) >= 1
        assert escalations[0].kind == "quote_acknowledgement"
        assert escalations[0].suggested_action != ""


# ── T7: DeleteRows with empty predicate → deny ──────────────────────


class TestT7:
    def test_empty_predicate_denied(self):
        h = Harness()
        result = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(predicate=""),
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.DENY
        assert Blocker.BLAST_RADIUS_EXCEEDS_LIMIT in result.policy.blockers

    def test_whitespace_predicate_denied(self):
        h = Harness()
        result = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(predicate="   "),
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.DENY
        assert Blocker.BLAST_RADIUS_EXCEEDS_LIMIT in result.policy.blockers


# ── T8: DeleteRows large dryRunCount, no backup → approve ────────────


class TestT8:
    def test_high_risk_delete_requires_approval(self):
        h = Harness()
        result = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(dryRunCount=50000),
            now_iso=NOW,
        )
        # HIGH blast radius + irreversible + IF_HIGH_RISK → approve
        assert result.policy.decision == Decision.APPROVE
        assert "high_risk_irreversible" in result.policy.reason_codes

    def test_execution_blocked_without_approval(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(dryRunCount=50000),
            now_iso=NOW,
        )
        pipeline = h.execute(ev, approved=False, now_iso=NOW)
        assert pipeline.execution.status == ExecutionStatus.FAILED
        assert "Awaiting human approval" in pipeline.execution.observations[0]

    def test_approved_delete_still_requires_passing_checks(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(dryRunCount=50000),
            now_iso=NOW,
        )
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=False)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.FAILED
        assert "Failed required checks: sql_dry_run" in pipeline.execution.observations[0]


# ── T9: DeleteRows safe predicate + backup → executes after approval + check ─


class TestT9:
    def test_delete_with_backup_routes_to_check(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(backupRef="backup:20260402"),
            now_iso=NOW,
        )
        assert ev.policy.decision == Decision.CHECK

    def test_delete_executes_with_backup_and_checks(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(backupRef="backup:20260402"),
            now_iso=NOW,
        )

        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.EXECUTED
        assert pipeline.execution.effect is not None

    def test_delete_emits_verification_obligation(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(),
            now_iso=NOW,
        )
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )
        effect = pipeline.execution.effect
        assert effect is not None
        assert len(effect.obligations) == 1
        assert effect.obligations[0].kind == "delete_verification"


# ── T10: DeleteRows succeeds but downstream count mismatch → breached ─


class TestT10:
    def test_obligation_breached_after_successful_delete(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(),
            now_iso=NOW,
        )
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )

        # Delete succeeded
        assert pipeline.execution.status == ExecutionStatus.EXECUTED

        # Obligation is open — simulate breach (downstream count mismatch)
        obligation_id = pipeline.execution.effect.obligations[0].obligation_id
        h.obligations.breach(obligation_id)

        # Verify escalation
        escalations = h.obligations.get_escalations(NOW)
        assert len(escalations) >= 1
        assert any(e.kind == "delete_verification" for e in escalations)


# ── T11: ScheduleMeeting ambiguous attendee → clarify ─────────────────


class TestT11:
    def test_ambiguous_attendee_triggers_clarify(self):
        h = Harness()
        result = h.evaluate(
            action_type="ScheduleMeeting",
            args=_meeting_args(),
            entity_map={"attendeeIds": [["user:alice", "user:alice2"]]},
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CLARIFY
        assert Blocker.ENTITY_RESOLUTION_CONFLICT in result.policy.blockers

    def test_one_ambiguous_attendee_among_many_triggers_clarify(self):
        h = Harness()
        result = h.evaluate(
            action_type="ScheduleMeeting",
            args=_meeting_args(),
            entity_map={"attendeeIds": [["user:alice"], ["user:bob", "user:robert"]]},
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CLARIFY
        assert Blocker.ENTITY_RESOLUTION_CONFLICT in result.policy.blockers
        assert result.resolution.entity_slots["attendeeIds"] == ["user:alice"]
        assert any(
            conflict.startswith("ambiguous:attendeeIds[1]:")
            for conflict in result.resolution.conflicts
        )


# ── T12: ScheduleMeeting hard conflict, no fallback → deny or clarify ─


class TestT12:
    def test_missing_required_field_blocks(self):
        """Missing startTime should clarify, not silently proceed."""
        h = Harness()
        result = h.evaluate(
            action_type="ScheduleMeeting",
            args=_meeting_args(startTime=""),
            entity_map={"attendeeIds": ["user:alice"]},
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CLARIFY
        assert Blocker.MISSING_REQUIRED_ARG in result.policy.blockers


# ── T13: ScheduleMeeting clear attendees, available slot → allow + obligation ─


class TestT13:
    def test_meeting_executes_with_checks(self):
        h = Harness()
        ev = h.evaluate(
            action_type="ScheduleMeeting",
            args=_meeting_args(),
            entity_map={"attendeeIds": [["user:alice"], ["user:bob"]]},
            semantic_keys=["meeting"],
            now_iso=NOW,
        )
        # Low blast + reversible + never approval → CHECK (cheap checks exist)
        assert ev.policy.decision == Decision.CHECK
        assert ev.resolution.entity_slots["attendeeIds"] == ["user:alice", "user:bob"]

        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="calendar_lookup", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.EXECUTED
        assert len(pipeline.execution.effect.obligations) == 1
        assert pipeline.execution.effect.obligations[0].kind == "meeting_response"
        assert pipeline.execution.effect.obligations[0].entity_ids == ["user:alice", "user:bob"]


# ── T14: Two incompatible schemas for high-cost → check or clarify ────


class TestT14:
    def test_schema_competition_blocks_allow(self):
        h = Harness()
        # Simulate schema competition via interpreter blockers
        from harness.types import Proposal, Resolution
        from harness.policy import evaluate as policy_evaluate
        from harness.registry import get_spec

        proposal = Proposal(
            proposal_id="test:schema_comp",
            action_type="SendQuoteEmail",
            args=_quote_args(),
            blockers=[Blocker.SCHEMA_COMPETITION],
        )
        resolution = Resolution(
            entity_ids=["client:123"],
            resource_keys=["product:abc"],
            semantic_keys=["quote"],
        )
        spec = get_spec("SendQuoteEmail")
        decision = policy_evaluate(
            proposal=proposal,
            resolution=resolution,
            spec=spec,
            store=h.store,
            now_iso=NOW,
        )
        assert decision.decision == Decision.CLARIFY
        assert Blocker.SCHEMA_COMPETITION in decision.blockers


# ── T15: Multiple variants with safe first step → check on intersection ─


class TestT15:
    def test_safe_intersection_proceeds_to_check(self):
        """When schema competition is NOT present, cheap checks gate execution."""
        h = Harness()
        # If the interpreter resolves to a single schema, policy falls through
        # to CHECK (because cheap checks exist)
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.CHECK


# ── T16: Unregistered action type → deny ─────────────────────────────


class TestT16:
    def test_unregistered_action_denied(self):
        h = Harness()
        result = h.evaluate(
            action_type="LaunchMissiles",
            args={"target": "moon"},
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.DENY
        assert "unregistered_action_type" in result.policy.reason_codes

    def test_unregistered_action_cannot_execute(self):
        h = Harness()
        ev = h.evaluate(action_type="LaunchMissiles", args={}, now_iso=NOW)
        pipeline = h.execute(ev, now_iso=NOW)
        assert pipeline.execution.status == ExecutionStatus.FAILED


# ── T17: Freeform info request → no execution proposal ───────────────


class TestT17:
    def test_info_request_produces_no_mutation(self):
        """
        An unregistered action type that's really just an info request
        should be denied, preventing the system from forcing a mutation.
        """
        h = Harness()
        result = h.evaluate(
            action_type="LookupClientHistory",
            args={"clientId": "client:123"},
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.DENY
        pipeline = h.execute(result, now_iso=NOW)
        assert pipeline.execution.effect is None


# ── T18: Old obligation intersects new action on same entity → projected ─


class TestT18:
    def test_old_obligation_projected_into_context(self):
        h = Harness()
        # Execute a quote → creates obligation on client:123
        _execute_quote(h, now_iso=NOW)

        # New action on same entity should see the obligation
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(),  # same terms, no conflict
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert len(result.projected_obligations) >= 1
        assert result.projected_obligations[0].kind == "quote_acknowledgement"


# ── T19: Unrelated obligation on another entity → no projection ───────


class TestT19:
    def test_unrelated_obligation_not_projected(self):
        h = Harness()
        # Execute a quote on client:123
        _execute_quote(h, now_iso=NOW)

        # New action on a DIFFERENT entity
        result = h.evaluate(
            action_type="ScheduleMeeting",
            args=_meeting_args(),
            entity_map={"attendeeIds": ["user:bob"]},
            semantic_keys=["meeting"],
            now_iso=NOW,
        )
        # Should NOT see the quote obligation (different entity, different semantic key)
        assert len(result.projected_obligations) == 0


# ── T20: High-cost action with fast feedback → no tier-3 sampling ─────


class TestT20:
    def test_fast_feedback_uses_standard_checks(self):
        """
        The safety budget is spent where feedback is poor, not everywhere.
        A high-risk action with fast feedback should still only go through
        the standard policy flow (approve → check), not extra sampling.
        """
        h = Harness()
        # DeleteRows is high-cost. Policy routes to APPROVE.
        # The harness does NOT escalate to tier-3 sampling — that's
        # reserved for slow/silent feedback actions.
        result = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(),
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.APPROVE
        # Verify only standard checks are required, not multi-sample
        assert result.policy.required_checks == ["sql_dry_run"]


# ═══════════════════════════════════════════════════════════════════════
# Adapter-Specific Acceptance Checks
# ═══════════════════════════════════════════════════════════════════════


class TestSendQuoteEmailAcceptance:
    def test_commitment_fields_complete(self):
        h = Harness()
        pipeline = _execute_quote(h, now_iso=NOW)
        commitment = pipeline.execution.effect.commitments[0]
        assert commitment.entity_ids == ["client:123"]
        assert commitment.resource_keys == ["product:abc"]
        assert commitment.fields["recipientId"] == "client:123"
        assert commitment.fields["productId"] == "product:abc"
        assert commitment.fields["unitPrice"] == 100
        assert commitment.fields["currency"] == "USD"
        assert commitment.fields["validUntil"] == FUTURE
        assert commitment.fields["termsVersion"] == "v2"

    def test_obligation_due_date_deterministic(self):
        h = Harness()
        pipeline = _execute_quote(h, now_iso=NOW)
        obligation = pipeline.execution.effect.obligations[0]
        # Due date derived from validUntil, not arbitrary
        assert obligation.due_at == FUTURE

    def test_conflicting_quote_blocked_until_expiry(self):
        """Commitment blocks new conflicting quote until it expires."""
        h = Harness()
        _execute_quote(h, now_iso=NOW)

        # Before expiry: conflict
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(unitPrice=200),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert result.policy.decision == Decision.DENY

        # After expiry: no conflict (commitment expired)
        after_expiry = "2026-04-11T00:00:00Z"
        result2 = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(unitPrice=200),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=after_expiry,
        )
        assert result2.policy.decision != Decision.DENY

    def test_quote_conflict_is_scoped_to_same_recipient_and_product(self):
        h = Harness()
        _execute_quote(h, now_iso=NOW)

        other_client = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(recipientId="client:999", unitPrice=200),
            entity_map={"recipientId": ["client:999"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert other_client.policy.decision != Decision.DENY

        other_product = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(productId="product:xyz", unitPrice=200),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:xyz"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert other_product.policy.decision != Decision.DENY


class TestDeleteRowsAcceptance:
    def test_mutation_records_table_and_predicate(self):
        h = Harness()
        ev = h.evaluate(action_type="DeleteRows", args=_delete_args(), now_iso=NOW)
        pipeline = h.execute(
            ev, approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )
        mutation = pipeline.execution.effect.mutations[0]
        assert mutation.resource == "orders"
        assert "Delete rows from orders where" in mutation.summary
        assert "status = 'cancelled'" in mutation.summary

    def test_missing_backup_enforced_by_policy(self):
        """Policy enforces approval regardless of operator memory."""
        h = Harness()
        ev = h.evaluate(action_type="DeleteRows", args=_delete_args(), now_iso=NOW)
        assert ev.policy.decision == Decision.APPROVE

    def test_backup_allows_cheaper_check_path(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(backupRef="backup:20260402"),
            now_iso=NOW,
        )
        assert ev.policy.decision == Decision.CHECK

    def test_downstream_verification_obligation(self):
        """Breach is separate from execution success."""
        h = Harness()
        ev = h.evaluate(action_type="DeleteRows", args=_delete_args(), now_iso=NOW)
        pipeline = h.execute(
            ev, approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.EXECUTED
        obligation = pipeline.execution.effect.obligations[0]
        assert obligation.verify_with.value == "query"


class TestScheduleMeetingAcceptance:
    def test_calendar_check_is_policy_input(self):
        """Calendar check is a policy input, not after-the-fact audit."""
        h = Harness()
        ev = h.evaluate(
            action_type="ScheduleMeeting",
            args=_meeting_args(),
            entity_map={"attendeeIds": ["user:alice"]},
            now_iso=NOW,
        )
        assert ev.policy.decision == Decision.CHECK
        assert "calendar_lookup" in ev.policy.required_checks

    def test_decline_monitoring_obligation(self):
        h = Harness()
        ev = h.evaluate(
            action_type="ScheduleMeeting",
            args=_meeting_args(),
            entity_map={"attendeeIds": [["user:alice"], ["user:bob"]]},
            now_iso=NOW,
        )
        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="calendar_lookup", passed=True)],
            now_iso=NOW,
        )
        obligation = pipeline.execution.effect.obligations[0]
        assert obligation.kind == "meeting_response"
        assert obligation.entity_ids == ["user:alice", "user:bob"]
        assert obligation.verify_with.value == "poll"


# ═══════════════════════════════════════════════════════════════════════
# Regression Checks
# ═══════════════════════════════════════════════════════════════════════


class TestRegressions:
    def test_new_adapter_cannot_skip_effect_emission(self):
        """Any executed action must produce an effect."""
        h = Harness()
        for action_type in ["SendQuoteEmail", "DeleteRows", "ScheduleMeeting"]:
            ev = h.evaluate(
                action_type=action_type,
                args=(
                    _quote_args() if action_type == "SendQuoteEmail"
                    else _delete_args() if action_type == "DeleteRows"
                    else _meeting_args()
                ),
                entity_map=(
                    {"recipientId": ["client:123"]} if action_type == "SendQuoteEmail"
                    else {"attendeeIds": ["user:alice"]} if action_type == "ScheduleMeeting"
                    else None
                ),
                resource_map=(
                    {"productId": ["product:abc"]} if action_type == "SendQuoteEmail"
                    else None
                ),
                now_iso=NOW,
            )
            pipeline = h.execute(
                ev,
                approved=True,
                check_results=[
                    CheckResult(check_id=ev.policy.required_checks[0], passed=True)
                ] if ev.policy.required_checks else None,
                now_iso=NOW,
            )
            assert pipeline.execution.effect is not None, (
                f"{action_type} executed without emitting an effect"
            )

    def test_adapter_without_policy_cannot_execute(self):
        """Unregistered action types cannot bypass policy."""
        h = Harness()
        ev = h.evaluate(action_type="UnknownAction", args={}, now_iso=NOW)
        assert ev.policy.decision == Decision.DENY
        pipeline = h.execute(ev, now_iso=NOW)
        assert pipeline.execution.status == ExecutionStatus.FAILED

    def test_passed_check_cannot_override_hard_deny(self):
        """A passed check does not override a DENY decision."""
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args=_delete_args(predicate=""),  # triggers deny
            now_iso=NOW,
        )
        assert ev.policy.decision == Decision.DENY

        # Even with checks + approval, deny holds
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.FAILED

    def test_missing_required_check_result_blocks_execution(self):
        h = Harness()
        ev = h.evaluate(action_type="DeleteRows", args=_delete_args(), now_iso=NOW)
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="other_check", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.FAILED
        assert "Missing required checks: sql_dry_run" in pipeline.execution.observations[0]

    def test_expired_commitment_no_longer_blocks(self):
        """Expired commitments don't block, but remain in effect history."""
        h = Harness()
        _execute_quote(h, now_iso=NOW)

        # After expiry, conflicting quote is allowed
        after_expiry = "2026-04-11T00:00:00Z"
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(unitPrice=200),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=after_expiry,
        )
        assert result.policy.decision != Decision.DENY

        # But the old effect is still in the store (auditable)
        assert len(h.store.effects) >= 1

    def test_expired_commitment_no_longer_blocks_without_explicit_now(self):
        """
        Harness.evaluate() must use its computed clock for policy/store queries,
        otherwise expired commitments keep blocking in normal runtime.
        """
        h = Harness()
        expired_at = "2020-01-01T00:00:00+00:00"
        created_at = "2019-12-01T00:00:00+00:00"

        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(validUntil=expired_at),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=created_at,
        )
        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
            now_iso=created_at,
        )
        assert pipeline.execution.status == ExecutionStatus.EXECUTED

        # No explicit now_iso here: the harness should still treat the old quote
        # as expired relative to the real current time.
        result = h.evaluate(
            action_type="SendQuoteEmail",
            args=_quote_args(unitPrice=200, validUntil="2030-01-01T00:00:00+00:00"),
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
        )
        assert result.policy.decision != Decision.DENY
