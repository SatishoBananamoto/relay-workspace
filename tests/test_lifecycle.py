"""Tests for lifecycle tracking through the full pipeline."""

from __future__ import annotations

import pytest

from harness.core import ApprovalRequest, Harness
from harness.sqlite_store import SqliteEffectStore
from harness.state import ProposalState
from harness.types import CheckResult, Decision, ExecutionStatus


NOW = "2026-04-02T12:00:00Z"
FUTURE = "2026-04-10T12:00:00Z"


class TestLifecycleIntegration:

    def test_happy_path_lifecycle(self):
        """Full lifecycle: proposed -> check -> allow -> executed -> effects -> obligations"""
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": FUTURE,
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        assert ev.lifecycle is not None
        assert ev.lifecycle.current_state == ProposalState.CHECK

        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
            now_iso=NOW,
        )
        lc = pipeline.lifecycle
        assert lc.current_state == ProposalState.OBLIGATIONS_OPEN

        trail = lc.audit_trail
        states = [t["to"] for t in trail]
        assert states == [
            "check", "allow", "executed", "effects_persisted", "obligations_open",
        ]

    def test_deny_lifecycle(self):
        h = Harness()
        ev = h.evaluate(
            action_type="LaunchMissiles",
            args={},
            now_iso=NOW,
        )
        assert ev.lifecycle.current_state == ProposalState.DENIED
        assert ev.lifecycle.is_terminal

    def test_clarify_lifecycle(self):
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": "",  # missing
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            now_iso=NOW,
        )
        assert ev.lifecycle.current_state == ProposalState.CLARIFY

    def test_approve_lifecycle(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args={
                "connectionId": "conn:main",
                "table": "orders",
                "predicate": "status = 'done'",
                "dryRunCount": 50000,
            },
            now_iso=NOW,
        )
        assert ev.lifecycle.current_state == ProposalState.APPROVE

        # Execute with approval
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.lifecycle.current_state == ProposalState.OBLIGATIONS_OPEN

    def test_check_failure_lifecycle(self):
        """Failed checks should transition to denied."""
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": FUTURE,
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="pricing_source_lookup", passed=False)],
            now_iso=NOW,
        )
        assert pipeline.lifecycle.current_state == ProposalState.DENIED
        assert pipeline.execution.status == ExecutionStatus.FAILED

    def test_auto_check_integration(self):
        """Registered checks run automatically when no check_results provided."""
        h = Harness()
        h.checks.register(
            "calendar_lookup",
            lambda p, r: (True, "Slot available"),
        )

        ev = h.evaluate(
            action_type="ScheduleMeeting",
            args={
                "attendeeIds": ["user:alice"],
                "startTime": FUTURE,
                "durationMinutes": 30,
                "purpose": "standup",
            },
            entity_map={"attendeeIds": ["user:alice"]},
            semantic_keys=["meeting"],
            now_iso=NOW,
        )
        assert ev.policy.decision == Decision.CHECK

        # Don't pass check_results — auto-run
        pipeline = h.execute(ev, now_iso=NOW)
        assert pipeline.execution.status == ExecutionStatus.EXECUTED
        assert pipeline.check_results is not None
        assert pipeline.check_results[0].passed is True

    def test_tracker_counts_match(self):
        """Lifecycle tracker accurately tracks active vs terminal."""
        h = Harness()

        # One deny
        h.evaluate(action_type="Unknown", args={}, now_iso=NOW)

        # One full execution
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "c:1", "productId": "p:1",
                "unitPrice": 50, "currency": "USD",
                "validUntil": FUTURE, "termsVersion": "v1",
            },
            entity_map={"recipientId": ["c:1"]},
            resource_map={"productId": ["p:1"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        h.execute(
            ev,
            check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
            now_iso=NOW,
        )

        assert len(h.lifecycles.all_terminal()) == 1  # denied
        assert len(h.lifecycles.all_active()) == 1    # obligations_open

    def test_satisfy_updates_lifecycle_to_obligations_satisfied(self):
        satisfied_at = "2026-04-03T09:00:00Z"
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": FUTURE,
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
            now_iso=NOW,
        )
        obligation_id = pipeline.execution.effect.obligations[0].obligation_id

        assert h.obligations.satisfy(obligation_id, now_iso=satisfied_at) is True
        assert pipeline.lifecycle.current_state == ProposalState.OBLIGATIONS_SATISFIED
        assert pipeline.lifecycle.audit_trail[-1]["timestamp"] == satisfied_at

    def test_scheduler_tick_updates_lifecycle_to_obligations_breached(self):
        breached_at = "2026-04-15T00:00:00Z"
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": FUTURE,
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        pipeline = h.execute(
            ev,
            check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
            now_iso=NOW,
        )

        h.scheduler.tick(breached_at)
        assert pipeline.lifecycle.current_state == ProposalState.OBLIGATIONS_BREACHED
        assert pipeline.lifecycle.audit_trail[-1]["timestamp"] == breached_at

    def test_sqlite_store_updates_lifecycle_end_to_end(self):
        store = SqliteEffectStore()
        try:
            h = Harness(store=store)
            ev = h.evaluate(
                action_type="SendQuoteEmail",
                args={
                    "recipientId": "client:123",
                    "productId": "product:abc",
                    "unitPrice": 100,
                    "currency": "USD",
                    "validUntil": FUTURE,
                    "termsVersion": "v2",
                },
                entity_map={"recipientId": ["client:123"]},
                resource_map={"productId": ["product:abc"]},
                semantic_keys=["quote"],
                now_iso=NOW,
            )
            pipeline = h.execute(
                ev,
                check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
                now_iso=NOW,
            )

            obligation_id = pipeline.execution.effect.obligations[0].obligation_id
            assert store.get_obligations_for_proposal(ev.proposal.proposal_id)[0].obligation_id == obligation_id

            assert h.obligations.satisfy(obligation_id, now_iso="2026-04-03T09:00:00Z") is True
            assert pipeline.lifecycle.current_state == ProposalState.OBLIGATIONS_SATISFIED
        finally:
            store.close()


class TestRevise:

    def test_revise_creates_new_proposal_with_lineage(self):
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": "",  # missing → CLARIFY
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            now_iso=NOW,
        )
        assert ev.lifecycle.current_state == ProposalState.CLARIFY

        revised = h.revise(
            ev,
            args={"validUntil": FUTURE},
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )

        # Old lifecycle is SUPERSEDED
        assert ev.lifecycle.current_state == ProposalState.SUPERSEDED
        assert ev.lifecycle.is_terminal

        # New proposal has lineage
        assert revised.proposal.supersedes == ev.proposal.proposal_id
        assert revised.proposal.proposal_id != ev.proposal.proposal_id

        # New proposal merges args and evaluates fresh
        assert revised.proposal.args["validUntil"] == FUTURE
        assert revised.policy.decision == Decision.CHECK

    def test_revise_non_clarify_raises(self):
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": FUTURE,
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            now_iso=NOW,
        )
        assert ev.policy.decision == Decision.CHECK

        with pytest.raises(ValueError, match="CLARIFY"):
            h.revise(ev, args={"unitPrice": 200}, now_iso=NOW)

    def test_revised_proposal_can_execute(self):
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": "",
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            now_iso=NOW,
        )
        revised = h.revise(
            ev,
            args={"validUntil": FUTURE},
            entity_map={"recipientId": ["client:123"]},
            resource_map={"productId": ["product:abc"]},
            semantic_keys=["quote"],
            now_iso=NOW,
        )
        pipeline = h.execute(
            revised,
            check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.EXECUTED
        assert pipeline.lifecycle.current_state == ProposalState.OBLIGATIONS_OPEN


class TestApprovalWorkflow:

    def _evaluate_delete(self, h: Harness) -> "EvaluationResult":
        return h.evaluate(
            action_type="DeleteRows",
            args={
                "connectionId": "conn:main",
                "table": "orders",
                "predicate": "status = 'done'",
                "dryRunCount": 50000,
            },
            now_iso=NOW,
        )

    def test_create_approval_request(self):
        h = Harness()
        ev = self._evaluate_delete(h)
        assert ev.policy.decision == Decision.APPROVE

        req = h.create_approval_request(ev)
        assert isinstance(req, ApprovalRequest)
        assert req.proposal_id == ev.proposal.proposal_id
        assert req.action_type == "DeleteRows"
        assert "high" in req.risk_summary
        assert "irreversible" in req.risk_summary
        assert "sql_dry_run" in req.required_checks

    def test_create_approval_request_rejects_non_approve(self):
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": FUTURE,
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            now_iso=NOW,
        )
        assert ev.policy.decision == Decision.CHECK
        with pytest.raises(ValueError, match="APPROVE"):
            h.create_approval_request(ev)

    def test_approve_executes_with_checks(self):
        h = Harness()
        ev = self._evaluate_delete(h)
        pipeline = h.approve(
            ev,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.EXECUTED
        assert pipeline.lifecycle.current_state == ProposalState.OBLIGATIONS_OPEN

    def test_approve_still_enforces_failing_checks(self):
        h = Harness()
        ev = self._evaluate_delete(h)
        pipeline = h.approve(
            ev,
            check_results=[CheckResult(check_id="sql_dry_run", passed=False)],
            now_iso=NOW,
        )
        assert pipeline.execution.status == ExecutionStatus.FAILED

    def test_reject_denies_proposal(self):
        h = Harness()
        ev = self._evaluate_delete(h)
        pipeline = h.reject(ev, reason="too risky", now_iso=NOW)
        assert pipeline.execution.status == ExecutionStatus.FAILED
        assert pipeline.lifecycle.current_state == ProposalState.DENIED
        assert pipeline.lifecycle.is_terminal
        assert "too risky" in pipeline.execution.observations[0]

    def test_approve_rejects_non_approve_decision(self):
        h = Harness()
        ev = h.evaluate(
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": FUTURE,
                "termsVersion": "v2",
            },
            entity_map={"recipientId": ["client:123"]},
            now_iso=NOW,
        )
        with pytest.raises(ValueError, match="APPROVE"):
            h.approve(ev, now_iso=NOW)
