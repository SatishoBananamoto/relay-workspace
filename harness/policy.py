"""Deterministic policy engine. Routes proposals to allow/check/clarify/approve/deny."""

from __future__ import annotations

from .store import EffectStore
from .types import (
    ActionSpec,
    ApprovalPolicy,
    BlastRadius,
    Blocker,
    Decision,
    PolicyDecision,
    Proposal,
    Resolution,
)


def evaluate(
    *,
    proposal: Proposal,
    resolution: Resolution,
    spec: ActionSpec,
    store: EffectStore,
    now_iso: str | None = None,
) -> PolicyDecision:
    """
    Evaluate a proposal against the policy engine.

    Order:
    1. Validate required fields.
    2. Validate entity/resource resolution.
    3. Run adapter preconditions.
    4. Query effect store for intersecting commitments/obligations.
    5. Check commitment conflicts.
    6. Route: clarify > deny > approve > check > allow.
    """
    blockers: set[Blocker] = set(proposal.blockers)
    reason_codes: list[str] = []

    # 1. Required fields
    for arg in spec.required_args:
        val = proposal.args.get(arg)
        if val is None or val == "":
            blockers.add(Blocker.MISSING_REQUIRED_ARG)
            reason_codes.append(f"missing:{arg}")

    # 2. Resolution conflicts
    if resolution.conflicts:
        blockers.add(Blocker.ENTITY_RESOLUTION_CONFLICT)
        reason_codes.append("resolution_conflict")

    # 3. Adapter preconditions
    if spec.preconditions:
        for b in spec.preconditions(proposal, resolution):
            blockers.add(b)
            reason_codes.append(f"precondition:{b.value}")

    # 4. Query store for intersecting constraints
    commitments, obligations = store.query_open_intersection(
        action_type=proposal.action_type,
        entity_ids=resolution.entity_ids,
        resource_keys=resolution.resource_keys,
        semantic_keys=resolution.semantic_keys,
        now_iso=now_iso,
    )

    # 5. Commitment conflict detection — adapter-owned
    if spec.conflict_detector and spec.conflict_detector(proposal, resolution, commitments):
        blockers.add(Blocker.COMMITMENT_CONFLICT)
        reason_codes.append("open_commitment_conflict")

    # 6. Decision routing — priority order
    # Schema competition or entity conflicts -> clarify
    if Blocker.SCHEMA_COMPETITION in blockers or \
       Blocker.ENTITY_RESOLUTION_CONFLICT in blockers:
        return PolicyDecision(
            decision=Decision.CLARIFY,
            blockers=sorted(blockers, key=lambda b: b.value),
            required_checks=[],
            reason_codes=reason_codes,
        )

    # Commitment conflict -> deny
    if Blocker.COMMITMENT_CONFLICT in blockers:
        return PolicyDecision(
            decision=Decision.DENY,
            blockers=sorted(blockers, key=lambda b: b.value),
            required_checks=[],
            reason_codes=reason_codes,
        )

    # Blast radius exceeded -> deny
    if Blocker.BLAST_RADIUS_EXCEEDS_LIMIT in blockers:
        return PolicyDecision(
            decision=Decision.DENY,
            blockers=sorted(blockers, key=lambda b: b.value),
            required_checks=[],
            reason_codes=reason_codes,
        )

    # Missing args -> clarify
    if Blocker.MISSING_REQUIRED_ARG in blockers:
        return PolicyDecision(
            decision=Decision.CLARIFY,
            blockers=sorted(blockers, key=lambda b: b.value),
            required_checks=[],
            reason_codes=reason_codes,
        )

    approval_required = _requires_approval(
        spec=spec,
        proposal=proposal,
        resolution=resolution,
    )
    if approval_required:
        check_ids = _required_check_ids(spec, Decision.APPROVE)
        return PolicyDecision(
            decision=Decision.APPROVE,
            blockers=sorted(blockers, key=lambda b: b.value),
            required_checks=check_ids,
            reason_codes=[*reason_codes, "high_risk_irreversible"],
        )

    # Cheap checks exist -> check
    check_ids = _required_check_ids(spec, Decision.ALLOW)
    if check_ids:
        return PolicyDecision(
            decision=Decision.CHECK,
            blockers=sorted(blockers, key=lambda b: b.value),
            required_checks=check_ids,
            reason_codes=[*reason_codes, "cheap_checks_required"],
        )

    # All clear -> allow
    return PolicyDecision(
        decision=Decision.ALLOW,
        blockers=sorted(blockers, key=lambda b: b.value),
        required_checks=[],
        reason_codes=reason_codes,
    )



def _requires_approval(
    *,
    spec: ActionSpec,
    proposal: Proposal,
    resolution: Resolution,
) -> bool:
    """Return True when the action must go through an approval gate."""
    if spec.approval_policy == ApprovalPolicy.NEVER:
        return False
    if spec.approval_policy == ApprovalPolicy.ALWAYS:
        return True
    if spec.requires_approval is not None:
        return spec.requires_approval(proposal, resolution)
    return spec.blast_radius == BlastRadius.HIGH and not spec.reversible


def _required_check_ids(spec: ActionSpec, decision: Decision) -> list[str]:
    """
    Select checks relevant for the current decision.

    Empty required_for means "required whenever this action routes through
    a check gate", which keeps older specs working.
    """
    check_ids: list[str] = []
    for check in spec.cheap_checks:
        if not check.required_for or decision in check.required_for:
            check_ids.append(check.id)
    return check_ids
