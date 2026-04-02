"""CLI demo. Walks through the three adapter scenarios showing the full pipeline.

Usage: python3 -m harness.cli
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from .checks import CheckRunner
from .core import Harness
from .obligations import Escalation
from .types import CheckResult, Decision, ExecutionStatus, Proposal, Resolution


NOW = "2026-04-02T12:00:00Z"
FUTURE = "2026-04-10T12:00:00Z"


def _header(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def _subheader(text: str) -> None:
    print(f"\n  --- {text} ---")


def _result(label: str, value: str) -> None:
    print(f"  {label:.<40} {value}")


def _escalation_handler(escalation: Escalation) -> None:
    print(f"  !! ESCALATION: {escalation.kind}")
    print(f"     Failure: {escalation.failure_mode}")
    print(f"     Suggested: {escalation.suggested_action}")


def demo_send_quote(h: Harness) -> None:
    _header("Scenario 1: SendQuoteEmail — Happy Path")

    ev = h.evaluate(
        action_type="SendQuoteEmail",
        args={
            "recipientId": "client:acme",
            "productId": "product:widget",
            "unitPrice": 49.99,
            "currency": "USD",
            "validUntil": FUTURE,
            "termsVersion": "v2",
        },
        entity_map={"recipientId": ["client:acme"]},
        resource_map={"productId": ["product:widget"]},
        semantic_keys=["quote"],
        now_iso=NOW,
    )

    _result("Policy decision", ev.policy.decision.value)
    _result("Required checks", ", ".join(ev.policy.required_checks) or "none")
    _result("Projected obligations", str(len(ev.projected_obligations)))

    pipeline = h.execute(
        ev,
        check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
        now_iso=NOW,
    )
    _result("Execution status", pipeline.execution.status.value)
    if pipeline.execution.effect:
        _result("Commitments emitted", str(len(pipeline.execution.effect.commitments)))
        _result("Obligations emitted", str(len(pipeline.execution.effect.obligations)))

    if pipeline.lifecycle:
        _subheader("Lifecycle audit trail")
        for t in pipeline.lifecycle.audit_trail:
            print(f"    {t['from']} -> {t['to']}  ({t['reason']})")


def demo_conflicting_quote(h: Harness) -> None:
    _header("Scenario 2: SendQuoteEmail — Commitment Conflict")

    ev = h.evaluate(
        action_type="SendQuoteEmail",
        args={
            "recipientId": "client:acme",
            "productId": "product:widget",
            "unitPrice": 79.99,  # different price!
            "currency": "USD",
            "validUntil": FUTURE,
            "termsVersion": "v2",
        },
        entity_map={"recipientId": ["client:acme"]},
        resource_map={"productId": ["product:widget"]},
        semantic_keys=["quote"],
        now_iso=NOW,
    )

    _result("Policy decision", ev.policy.decision.value)
    _result("Blockers", ", ".join(b.value for b in ev.policy.blockers))
    _result("Reason codes", ", ".join(ev.policy.reason_codes))


def demo_delete_rows(h: Harness) -> None:
    _header("Scenario 3: DeleteRows — High Risk, Requires Approval")

    ev = h.evaluate(
        action_type="DeleteRows",
        args={
            "connectionId": "conn:prod",
            "table": "orders",
            "predicate": "status = 'cancelled' AND created_at < '2025-01-01'",
            "dryRunCount": 12000,
        },
        now_iso=NOW,
    )

    _result("Policy decision", ev.policy.decision.value)
    _result("Required checks", ", ".join(ev.policy.required_checks))
    _result("Reason codes", ", ".join(ev.policy.reason_codes))

    _subheader("Attempt without approval")
    pipeline = h.execute(ev, approved=False, now_iso=NOW)
    _result("Execution status", pipeline.execution.status.value)
    _result("Observation", pipeline.execution.observations[0])

    _subheader("Attempt with approval + checks")
    pipeline = h.execute(
        ev,
        approved=True,
        check_results=[CheckResult(check_id="sql_dry_run", passed=True)],
        now_iso=NOW,
    )
    _result("Execution status", pipeline.execution.status.value)
    if pipeline.execution.effect:
        _result("Obligations", str(len(pipeline.execution.effect.obligations)))

    if pipeline.lifecycle:
        _subheader("Lifecycle audit trail")
        for t in pipeline.lifecycle.audit_trail:
            print(f"    {t['from']} -> {t['to']}  ({t['reason']})")


def demo_empty_predicate(h: Harness) -> None:
    _header("Scenario 4: DeleteRows — Empty Predicate (Denied)")

    ev = h.evaluate(
        action_type="DeleteRows",
        args={
            "connectionId": "conn:prod",
            "table": "orders",
            "predicate": "",
            "dryRunCount": 0,
        },
        now_iso=NOW,
    )

    _result("Policy decision", ev.policy.decision.value)
    _result("Blockers", ", ".join(b.value for b in ev.policy.blockers))


def demo_schedule_meeting(h: Harness) -> None:
    _header("Scenario 5: ScheduleMeeting — With Auto-Check")

    # Register a check implementation for calendar lookup
    def calendar_check(proposal: Proposal, resolution: Resolution) -> tuple[bool, str]:
        return True, "All attendees available"

    h.checks.register("calendar_lookup", calendar_check)

    ev = h.evaluate(
        action_type="ScheduleMeeting",
        args={
            "attendeeIds": ["user:alice", "user:bob"],
            "startTime": FUTURE,
            "durationMinutes": 30,
            "purpose": "Sprint planning",
        },
        entity_map={"attendeeIds": [["user:alice"], ["user:bob"]]},
        semantic_keys=["meeting"],
        now_iso=NOW,
    )

    _result("Policy decision", ev.policy.decision.value)
    _result("Required checks", ", ".join(ev.policy.required_checks))

    # Execute without passing check_results — auto-runs registered checks
    pipeline = h.execute(ev, now_iso=NOW)
    _result("Execution status", pipeline.execution.status.value)
    if pipeline.check_results:
        for cr in pipeline.check_results:
            _result(f"  Check '{cr.check_id}'", f"{'PASS' if cr.passed else 'FAIL'}: {cr.detail}")

    if pipeline.lifecycle:
        _subheader("Lifecycle audit trail")
        for t in pipeline.lifecycle.audit_trail:
            print(f"    {t['from']} -> {t['to']}  ({t['reason']})")


def demo_unregistered(h: Harness) -> None:
    _header("Scenario 6: Unregistered Action — Closed Mutation Boundary")

    ev = h.evaluate(
        action_type="LaunchMissiles",
        args={"target": "moon"},
        now_iso=NOW,
    )

    _result("Policy decision", ev.policy.decision.value)
    _result("Reason codes", ", ".join(ev.policy.reason_codes))


def demo_obligation_breach(h: Harness) -> None:
    _header("Scenario 7: Obligation Breach — Time Passes")

    print("  (Using effects from scenario 1...)")
    print(f"  Advancing time past quote expiry: {FUTURE}")

    far_future = "2026-04-15T00:00:00Z"
    tick = h.scheduler.tick(far_future)

    _result("Obligations checked", str(len(tick.checked)))
    _result("Escalations fired", str(len(tick.escalations)))
    for e in tick.escalations:
        print(f"    -> {e.kind}: {e.failure_mode}")
        print(f"       Suggested: {e.suggested_action}")


def main() -> None:
    print("\n  Intent-to-Action Harness — CLI Demo")
    print("  The model proposes. The harness decides.\n")

    h = Harness()
    h.scheduler.on_escalation(_escalation_handler)

    demo_send_quote(h)
    demo_conflicting_quote(h)
    demo_delete_rows(h)
    demo_empty_predicate(h)
    demo_schedule_meeting(h)
    demo_unregistered(h)
    demo_obligation_breach(h)

    _header("Summary")
    _result("Total effects in store", str(len(h.store.effects)))
    _result("Active lifecycles", str(len(h.lifecycles.all_active())))
    _result("Terminal lifecycles", str(len(h.lifecycles.all_terminal())))

    print()


if __name__ == "__main__":
    main()
