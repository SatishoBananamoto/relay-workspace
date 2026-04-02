"""Tests for the obligation scheduler."""

from __future__ import annotations

import pytest

from harness.core import Harness
from harness.obligations import Escalation
from harness.scheduler import ObligationScheduler
from harness.types import CheckResult, ObligationStatus


NOW = "2026-04-02T12:00:00Z"
FUTURE = "2026-04-10T12:00:00Z"
FAR_FUTURE = "2026-04-15T00:00:00Z"


def _setup_with_quote() -> Harness:
    """Create a harness with one executed quote (obligation due at FUTURE)."""
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
    h.execute(
        ev,
        check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
        now_iso=NOW,
    )
    return h


class TestObligationScheduler:

    def test_due_exactly_now_stays_open_until_time_advances(self):
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
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso=NOW,
        )

        obligation = pipeline.execution.effect.obligations[0]
        assert obligation.status == ObligationStatus.OPEN
        assert h.scheduler.history[-1].checked == []

        result = h.scheduler.tick("2026-04-02T12:00:01Z")
        assert len(result.checked) == 1
        assert result.checked[0].obligation_id == obligation.obligation_id
        assert result.checked[0].new_status == ObligationStatus.BREACHED

    def test_tick_before_deadline_no_breach(self):
        h = _setup_with_quote()
        result = h.scheduler.tick(NOW)
        assert len(result.checked) == 0
        assert len(result.escalations) == 0

    def test_tick_after_deadline_triggers_breach(self):
        h = _setup_with_quote()
        result = h.scheduler.tick(FAR_FUTURE)
        assert len(result.checked) == 1
        assert result.checked[0].new_status == ObligationStatus.BREACHED
        assert len(result.escalations) >= 1

    def test_escalation_handler_fires(self):
        h = _setup_with_quote()
        fired: list[Escalation] = []
        h.scheduler.on_escalation(lambda e: fired.append(e))

        h.scheduler.tick(FAR_FUTURE)
        assert len(fired) >= 1
        assert fired[0].kind == "quote_acknowledgement"

    def test_double_tick_no_duplicate_escalation(self):
        """Already-breached obligations should not re-escalate."""
        h = _setup_with_quote()
        fired: list[Escalation] = []
        h.scheduler.on_escalation(lambda e: fired.append(e))

        h.scheduler.tick(FAR_FUTURE)
        first_count = len(fired)

        h.scheduler.tick(FAR_FUTURE)
        assert len(fired) == first_count  # no new escalations

    def test_tick_history_recorded(self):
        h = _setup_with_quote()
        # Note: execute() also ticks the scheduler, so 1 tick already exists
        baseline = len(h.scheduler.history)
        h.scheduler.tick(NOW)
        h.scheduler.tick(FAR_FUTURE)
        assert len(h.scheduler.history) == baseline + 2
        assert h.scheduler.history[-2].timestamp == NOW
        assert h.scheduler.history[-1].timestamp == FAR_FUTURE

    def test_due_comparison_normalizes_iso_offsets(self):
        h = Harness()
        ev = h.evaluate(
            action_type="DeleteRows",
            args={
                "connectionId": "conn:main",
                "table": "orders",
                "predicate": "status = 'done'",
                "dryRunCount": 50000,
            },
            now_iso="2026-04-02T12:00:00Z",
        )
        pipeline = h.execute(
            ev,
            approved=True,
            check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
            now_iso="2026-04-02T12:00:00Z",
        )

        obligation = pipeline.execution.effect.obligations[0]
        assert obligation.status == ObligationStatus.OPEN

        result = h.scheduler.tick("2026-04-02T12:00:01+00:00")
        assert len(result.checked) == 1
        assert result.checked[0].obligation_id == obligation.obligation_id
