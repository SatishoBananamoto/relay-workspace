"""Action registry with the three MVP adapters."""

from __future__ import annotations

from .types import (
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


def _req_str(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"Missing string arg: {key}")
    return v


def _req_num(args: dict, key: str) -> int | float:
    v = args.get(key)
    if not isinstance(v, (int, float)):
        raise ValueError(f"Missing number arg: {key}")
    return v


# -- SendQuoteEmail adapter --

def _send_quote_effect(
    args: dict, resolution: Resolution, now_iso: str,
) -> tuple[list[Mutation], list[Commitment], list[Obligation]]:
    recipient_id = _req_str(args, "recipientId")
    product_id = _req_str(args, "productId")
    valid_until = _req_str(args, "validUntil")
    return (
        [Mutation(
            resource="email",
            op="send_quote",
            summary=f"Quote sent to {recipient_id}",
        )],
        [Commitment(
            commitment_id=f"commitment:{now_iso}:quote",
            kind="quote",
            entity_ids=resolution.entity_ids,
            resource_keys=resolution.resource_keys,
            semantic_keys=resolution.semantic_keys,
            fields={
                "recipientId": recipient_id,
                "productId": product_id,
                "unitPrice": _req_num(args, "unitPrice"),
                "currency": _req_str(args, "currency"),
                "validUntil": valid_until,
                "termsVersion": _req_str(args, "termsVersion"),
            },
            expires_at=valid_until,
        )],
        [Obligation(
            obligation_id=f"obligation:{now_iso}:quote_ack",
            kind="quote_acknowledgement",
            entity_ids=resolution.entity_ids,
            resource_keys=resolution.resource_keys,
            semantic_keys=resolution.semantic_keys,
            due_at=valid_until,
            verify_with=VerifyWith.HUMAN,
            failure_mode="No acknowledgement or reply before quote expiry",
            status=ObligationStatus.OPEN,
        )],
    )


def _send_quote_preconditions(proposal: Proposal, resolution: Resolution) -> list[Blocker]:
    """Adapter-specific preconditions for SendQuoteEmail."""
    return []


def _send_quote_conflict_detector(
    proposal: Proposal, resolution: Resolution, commitments: list[Commitment],
) -> bool:
    """Detect if a new quote conflicts with existing open commitments."""
    recipient_ids = set(resolution.entity_ids)
    if not recipient_ids:
        recipient = proposal.args.get("recipientId")
        if isinstance(recipient, str):
            recipient_ids.add(recipient)

    product_ids = set(resolution.resource_keys)
    if not product_ids:
        product = proposal.args.get("productId")
        if isinstance(product, str):
            product_ids.add(product)

    proposed_price = proposal.args.get("unitPrice")
    proposed_terms = proposal.args.get("termsVersion")

    for c in commitments:
        if c.kind != "quote":
            continue
        if recipient_ids and not recipient_ids.intersection(c.entity_ids):
            continue
        if product_ids and not product_ids.intersection(c.resource_keys):
            continue
        if c.fields.get("unitPrice") != proposed_price or \
           c.fields.get("termsVersion") != proposed_terms:
            return True
    return False


# -- DeleteRows adapter --

def _delete_rows_effect(
    args: dict, resolution: Resolution, now_iso: str,
) -> tuple[list[Mutation], list[Commitment], list[Obligation]]:
    table = _req_str(args, "table")
    predicate = _req_str(args, "predicate")
    return (
        [Mutation(
            resource=table,
            op="delete_rows",
            summary=f"Delete rows from {table} where {predicate}",
        )],
        [],
        [Obligation(
            obligation_id=f"obligation:{now_iso}:delete_verify",
            kind="delete_verification",
            entity_ids=resolution.entity_ids,
            resource_keys=resolution.resource_keys,
            semantic_keys=resolution.semantic_keys,
            due_at=now_iso,
            verify_with=VerifyWith.QUERY,
            failure_mode="Downstream counts or replication diverged after delete",
            status=ObligationStatus.OPEN,
        )],
    )


def _delete_rows_preconditions(proposal: Proposal, resolution: Resolution) -> list[Blocker]:
    """Deny if predicate is empty."""
    blockers: list[Blocker] = []
    predicate = proposal.args.get("predicate")
    if not predicate or (isinstance(predicate, str) and not predicate.strip()):
        blockers.append(Blocker.BLAST_RADIUS_EXCEEDS_LIMIT)
    return blockers


def _delete_rows_requires_approval(proposal: Proposal, resolution: Resolution) -> bool:
    """Deletes without a backup require explicit approval."""
    backup_ref = proposal.args.get("backupRef")
    return not isinstance(backup_ref, str) or not backup_ref.strip()


# -- ScheduleMeeting adapter --

def _schedule_meeting_effect(
    args: dict, resolution: Resolution, now_iso: str,
) -> tuple[list[Mutation], list[Commitment], list[Obligation]]:
    attendee_ids = resolution.entity_slots.get("attendeeIds", resolution.entity_ids)
    return (
        [Mutation(
            resource="calendar",
            op="create_event",
            summary=f"Meeting scheduled for {_req_str(args, 'startTime')}",
        )],
        [],
        [Obligation(
            obligation_id=f"obligation:{now_iso}:meeting_response",
            kind="meeting_response",
            entity_ids=attendee_ids,
            resource_keys=resolution.resource_keys,
            semantic_keys=resolution.semantic_keys,
            due_at=_req_str(args, "startTime"),
            verify_with=VerifyWith.POLL,
            failure_mode="Required attendees declined or did not respond",
            status=ObligationStatus.OPEN,
        )],
    )


# -- Registry --

REGISTRY: dict[str, ActionSpec] = {
    "SendQuoteEmail": ActionSpec(
        action_type="SendQuoteEmail",
        version="1",
        required_args=[
            "recipientId", "productId", "unitPrice",
            "currency", "validUntil", "termsVersion",
        ],
        blast_radius=BlastRadius.MEDIUM,
        reversible=False,
        feedback_latency=FeedbackLatency.SLOW,
        cheap_checks=[
            CheckSpec(id="pricing_source_lookup", kind=CheckKind.LOOKUP,
                      required_for=[Decision.ALLOW]),
        ],
        approval_policy=ApprovalPolicy.IF_HIGH_RISK,
        effect_template=_send_quote_effect,
        entity_selectors=[SelectorSpec("recipientId", "one")],
        resource_selectors=[SelectorSpec("productId", "one")],
        preconditions=_send_quote_preconditions,
        conflict_detector=_send_quote_conflict_detector,
    ),
    "DeleteRows": ActionSpec(
        action_type="DeleteRows",
        version="1",
        required_args=["connectionId", "table", "predicate", "dryRunCount"],
        blast_radius=BlastRadius.HIGH,
        reversible=False,
        feedback_latency=FeedbackLatency.SLOW,
        cheap_checks=[
            CheckSpec(id="sql_dry_run", kind=CheckKind.DRY_RUN,
                      required_for=[Decision.ALLOW, Decision.APPROVE]),
        ],
        approval_policy=ApprovalPolicy.IF_HIGH_RISK,
        effect_template=_delete_rows_effect,
        preconditions=_delete_rows_preconditions,
        requires_approval=_delete_rows_requires_approval,
    ),
    "ScheduleMeeting": ActionSpec(
        action_type="ScheduleMeeting",
        version="1",
        required_args=["attendeeIds", "startTime", "durationMinutes", "purpose"],
        blast_radius=BlastRadius.LOW,
        reversible=True,
        feedback_latency=FeedbackLatency.SLOW,
        cheap_checks=[
            CheckSpec(id="calendar_lookup", kind=CheckKind.LOOKUP,
                      required_for=[Decision.ALLOW]),
        ],
        approval_policy=ApprovalPolicy.NEVER,
        effect_template=_schedule_meeting_effect,
        entity_selectors=[SelectorSpec("attendeeIds", "many")],
    ),
}


def get_spec(action_type: str) -> ActionSpec | None:
    return REGISTRY.get(action_type)
