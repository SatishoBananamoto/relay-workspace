"""Executor. Runs only after policy gates pass."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .store import EffectStore
from .types import (
    ActionSpec,
    Decision,
    Effect,
    ExecutionResult,
    ExecutionStatus,
    Mutation,
    PolicyDecision,
    Proposal,
    Resolution,
)


# Adapter executors: action_type -> callable that performs the action
# Returns (success: bool, observations: list[str])
AdapterExecutor = Callable[[Proposal, Resolution], tuple[bool, list[str]]]


class Executor:
    def __init__(self, store: EffectStore) -> None:
        self._store = store
        self._adapters: dict[str, AdapterExecutor] = {}

    def register_adapter(self, action_type: str, fn: AdapterExecutor) -> None:
        self._adapters[action_type] = fn

    def execute(
        self,
        *,
        proposal: Proposal,
        resolution: Resolution,
        spec: ActionSpec,
        policy: PolicyDecision,
        now_iso: str | None = None,
    ) -> ExecutionResult:
        """
        Execute a proposal that has passed policy gates.

        Only runs if policy decision is ALLOW, or if CHECK/APPROVE
        prerequisites have been satisfied (caller is responsible for
        verifying checks/approval before calling execute).
        """
        if policy.decision not in (Decision.ALLOW, Decision.CHECK, Decision.APPROVE):
            return ExecutionResult(
                action_id="",
                status=ExecutionStatus.FAILED,
                observations=[f"Policy decision was {policy.decision.value}, execution blocked"],
            )

        now = now_iso or datetime.now(timezone.utc).isoformat()
        action_id = f"action:{uuid.uuid4().hex[:12]}"

        # Run the adapter if registered
        adapter = self._adapters.get(proposal.action_type)
        if adapter:
            try:
                success, observations = adapter(proposal, resolution)
            except Exception as exc:
                return ExecutionResult(
                    action_id=action_id,
                    status=ExecutionStatus.FAILED,
                    observations=[f"Adapter error: {exc}"],
                )
            if not success:
                return ExecutionResult(
                    action_id=action_id,
                    status=ExecutionStatus.FAILED,
                    observations=observations,
                )
        else:
            # No adapter = dry run / test mode
            observations = ["no_adapter_registered:dry_run"]

        # Materialize and persist effect
        effect = _materialize_effect(
            action_id=action_id,
            proposal=proposal,
            resolution=resolution,
            spec=spec,
            now_iso=now,
        )
        self._store.append(effect)

        return ExecutionResult(
            action_id=action_id,
            status=ExecutionStatus.EXECUTED,
            observations=observations if adapter else ["dry_run"],
            effect=effect,
        )


def _materialize_effect(
    *,
    action_id: str,
    proposal: Proposal,
    resolution: Resolution,
    spec: ActionSpec,
    now_iso: str,
) -> Effect:
    mutations, commitments, obligations = spec.effect_template(
        proposal.args, resolution, now_iso,
    )
    for obligation in obligations:
        obligation.source_proposal_id = proposal.proposal_id
    return Effect(
        action_id=action_id,
        action_type=proposal.action_type,
        entity_ids=resolution.entity_ids,
        resource_keys=resolution.resource_keys,
        semantic_keys=resolution.semantic_keys,
        mutations=mutations,
        commitments=commitments,
        obligations=obligations,
        observed_at=now_iso,
    )
