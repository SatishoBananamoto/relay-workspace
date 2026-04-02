"""Core harness. Orchestrates the full intent-to-action pipeline.

Pipeline: interpret -> resolve -> policy -> [check] -> execute -> effects -> obligations

Every proposal gets a lifecycle that tracks state transitions with an
append-only audit trail. The check runner executes adapter-owned probes.
The scheduler ticks obligations at natural boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .checks import CheckRunner
from .executor import Executor
from .interpreter import interpret
from .obligations import ObligationEngine
from .policy import evaluate
from .registry import REGISTRY, get_spec
from .scheduler import ObligationScheduler
from .state import LifecycleTracker, ProposalLifecycle, ProposalState
from .store import EffectStore, InMemoryEffectStore
from .types import (
    CheckResult,
    Decision,
    ExecutionResult,
    ExecutionStatus,
    Obligation,
    ObligationStatus,
    PolicyDecision,
    Proposal,
    Resolution,
    SelectorCandidates,
)


@dataclass
class ApprovalRequest:
    """Structured approval request for human review."""
    proposal_id: str
    action_type: str
    reason_codes: list[str]
    risk_summary: str
    required_checks: list[str]
    args_summary: dict[str, Any]


@dataclass
class EvaluationResult:
    """Full result of evaluating an intent through the harness."""
    proposal: Proposal
    resolution: Resolution
    policy: PolicyDecision
    projected_obligations: list[Obligation]
    lifecycle: ProposalLifecycle | None = None


@dataclass
class PipelineResult:
    """Full result of running an intent through the complete pipeline."""
    evaluation: EvaluationResult
    execution: ExecutionResult | None = None
    check_results: list[CheckResult] | None = None
    lifecycle: ProposalLifecycle | None = None


class Harness:
    """
    The intent-to-action harness.

    Pipeline: interpret -> resolve -> policy -> [check] -> execute -> effects -> obligations
    """

    def __init__(self, store: EffectStore | None = None) -> None:
        self.store = store or InMemoryEffectStore()
        self.executor = Executor(self.store)
        self.checks = CheckRunner()
        self.lifecycles = LifecycleTracker()
        self.obligations = ObligationEngine(
            self.store,
            on_status_change=self._handle_obligation_status_change,
        )
        self.scheduler = ObligationScheduler(self.obligations)

    def evaluate(
        self,
        *,
        action_type: str,
        args: dict[str, Any],
        entity_map: dict[str, SelectorCandidates] | None = None,
        resource_map: dict[str, SelectorCandidates] | None = None,
        semantic_keys: list[str] | None = None,
        now_iso: str | None = None,
    ) -> EvaluationResult:
        """
        Evaluate an intent without executing.
        Returns proposal, resolution, policy decision, and projected obligations.
        """
        now = now_iso or datetime.now(timezone.utc).isoformat()
        spec = get_spec(action_type)

        if spec is None:
            # Unregistered action type -> deny
            proposal, resolution = interpret(
                action_type=action_type,
                args=args,
                entity_map=entity_map,
                resource_map=resource_map,
                semantic_keys=semantic_keys,
            )
            lc = self.lifecycles.create(proposal.proposal_id)
            lc.transition(
                ProposalState.DENIED,
                reason="unregistered_action_type",
                timestamp=now,
            )
            return EvaluationResult(
                proposal=proposal,
                resolution=resolution,
                policy=PolicyDecision(
                    decision=Decision.DENY,
                    blockers=[],
                    required_checks=[],
                    reason_codes=["unregistered_action_type"],
                ),
                projected_obligations=[],
                lifecycle=lc,
            )

        proposal, resolution = interpret(
            action_type=action_type,
            args=args,
            entity_map=entity_map,
            resource_map=resource_map,
            semantic_keys=semantic_keys,
        )

        # Create lifecycle tracker for this proposal
        lc = self.lifecycles.create(proposal.proposal_id)

        # Project relevant obligations into context
        projected = self.obligations.project_for_action(
            action_type=action_type,
            entity_ids=resolution.entity_ids,
            resource_keys=resolution.resource_keys,
            semantic_keys=resolution.semantic_keys,
            now_iso=now,
        )

        policy = evaluate(
            proposal=proposal,
            resolution=resolution,
            spec=spec,
            store=self.store,
            now_iso=now,
        )

        # Transition lifecycle based on policy decision
        state_map = {
            Decision.ALLOW: ProposalState.ALLOW,
            Decision.CHECK: ProposalState.CHECK,
            Decision.CLARIFY: ProposalState.CLARIFY,
            Decision.APPROVE: ProposalState.APPROVE,
            Decision.DENY: ProposalState.DENIED,
        }
        target_state = state_map[policy.decision]
        reason = ", ".join(policy.reason_codes) if policy.reason_codes else policy.decision.value
        lc.transition(target_state, reason=reason, timestamp=now)

        return EvaluationResult(
            proposal=proposal,
            resolution=resolution,
            policy=policy,
            projected_obligations=projected,
            lifecycle=lc,
        )

    def execute(
        self,
        evaluation: EvaluationResult,
        *,
        check_results: list[CheckResult] | None = None,
        approved: bool = False,
        now_iso: str | None = None,
    ) -> PipelineResult:
        """
        Execute a previously evaluated proposal.

        For CHECK decisions: pass check_results to verify checks passed,
            or omit check_results to auto-run registered checks.
        For APPROVE decisions: pass approved=True after human sign-off.
            Required checks are still enforced.
        For ALLOW decisions: executes directly.
        """
        now = now_iso or datetime.now(timezone.utc).isoformat()
        policy = evaluation.policy
        spec = get_spec(evaluation.proposal.action_type)
        lc = evaluation.lifecycle

        if spec is None:
            return PipelineResult(
                evaluation=evaluation,
                execution=ExecutionResult(
                    action_id="",
                    status=ExecutionStatus.FAILED,
                    observations=["unregistered_action_type"],
                ),
                lifecycle=lc,
            )

        # Gate on policy decision
        if policy.decision == Decision.DENY:
            return PipelineResult(
                evaluation=evaluation,
                execution=ExecutionResult(
                    action_id="",
                    status=ExecutionStatus.FAILED,
                    observations=[f"Denied: {', '.join(policy.reason_codes)}"],
                ),
                lifecycle=lc,
            )

        if policy.decision == Decision.CLARIFY:
            return PipelineResult(
                evaluation=evaluation,
                execution=ExecutionResult(
                    action_id="",
                    status=ExecutionStatus.FAILED,
                    observations=[f"Clarification needed: {', '.join(policy.reason_codes)}"],
                ),
                lifecycle=lc,
            )

        if policy.decision == Decision.APPROVE and not approved:
            return PipelineResult(
                evaluation=evaluation,
                execution=ExecutionResult(
                    action_id="",
                    status=ExecutionStatus.FAILED,
                    observations=["Awaiting human approval"],
                ),
                lifecycle=lc,
            )

        if policy.decision in (Decision.CHECK, Decision.APPROVE) and policy.required_checks:
            # Auto-run checks if no pre-computed results provided.
            if check_results is None:
                check_results = self.checks.run_checks(
                    spec=spec,
                    proposal=evaluation.proposal,
                    resolution=evaluation.resolution,
                    required_check_ids=policy.required_checks,
                )

            results_by_id = {cr.check_id: cr for cr in check_results}
            missing_checks = [
                check_id
                for check_id in policy.required_checks
                if check_id not in results_by_id
            ]
            failed_checks = [
                check_id
                for check_id in policy.required_checks
                if check_id in results_by_id and not results_by_id[check_id].passed
            ]

            if missing_checks or failed_checks:
                if lc and not lc.is_terminal:
                    lc.transition(
                        ProposalState.DENIED,
                        reason="checks_failed",
                        timestamp=now,
                    )

                details: list[str] = []
                if missing_checks:
                    details.append(f"Missing required checks: {', '.join(missing_checks)}")
                if failed_checks:
                    details.append(f"Failed required checks: {', '.join(failed_checks)}")

                return PipelineResult(
                    evaluation=evaluation,
                    execution=ExecutionResult(
                        action_id="",
                        status=ExecutionStatus.FAILED,
                        observations=details,
                    ),
                    check_results=check_results,
                    lifecycle=lc,
                )

        # Transition to ALLOW if we were in CHECK or APPROVE
        if lc and lc.current_state in (ProposalState.CHECK, ProposalState.APPROVE):
            lc.transition(ProposalState.ALLOW, reason="gates_passed", timestamp=now)

        result = self.executor.execute(
            proposal=evaluation.proposal,
            resolution=evaluation.resolution,
            spec=spec,
            policy=policy,
            now_iso=now,
        )

        # Track execution in lifecycle
        if lc and result.status == ExecutionStatus.EXECUTED:
            lc.transition(
                ProposalState.EXECUTED,
                reason="adapter_success",
                timestamp=now,
            )
            if result.effect:
                lc.transition(
                    ProposalState.EFFECTS_PERSISTED,
                    reason=f"effect:{result.action_id}",
                    timestamp=now,
                )
                if result.effect.obligations:
                    lc.transition(
                        ProposalState.OBLIGATIONS_OPEN,
                        reason=f"{len(result.effect.obligations)} obligations created",
                        timestamp=now,
                    )
        elif lc and result.status == ExecutionStatus.FAILED:
            if not lc.is_terminal:
                lc.transition(
                    ProposalState.FAILED,
                    reason="; ".join(result.observations),
                    timestamp=now,
                )

        # Tick the scheduler at this natural boundary
        self.scheduler.tick(now)

        return PipelineResult(
            evaluation=evaluation,
            execution=result,
            check_results=check_results,
            lifecycle=lc,
        )

    def run(
        self,
        *,
        action_type: str,
        args: dict[str, Any],
        entity_map: dict[str, SelectorCandidates] | None = None,
        resource_map: dict[str, SelectorCandidates] | None = None,
        semantic_keys: list[str] | None = None,
        check_results: list[CheckResult] | None = None,
        approved: bool = False,
        now_iso: str | None = None,
    ) -> PipelineResult:
        """Evaluate and execute in one call. Convenience for tests and simple flows."""
        evaluation = self.evaluate(
            action_type=action_type,
            args=args,
            entity_map=entity_map,
            resource_map=resource_map,
            semantic_keys=semantic_keys,
            now_iso=now_iso,
        )
        return self.execute(
            evaluation,
            check_results=check_results,
            approved=approved,
            now_iso=now_iso,
        )

    def revise(
        self,
        evaluation: EvaluationResult,
        *,
        args: dict[str, Any] | None = None,
        entity_map: dict[str, SelectorCandidates] | None = None,
        resource_map: dict[str, SelectorCandidates] | None = None,
        semantic_keys: list[str] | None = None,
        now_iso: str | None = None,
    ) -> EvaluationResult:
        """
        Revise a proposal that was routed to CLARIFY.

        Creates a new proposal with lineage to the original. The old
        lifecycle transitions to SUPERSEDED. The new proposal gets its
        own lifecycle and goes through the full evaluation pipeline.
        """
        now = now_iso or datetime.now(timezone.utc).isoformat()
        old_lc = evaluation.lifecycle
        if old_lc is None or old_lc.current_state != ProposalState.CLARIFY:
            raise ValueError(
                f"Can only revise proposals in CLARIFY state, "
                f"got {old_lc.current_state.value if old_lc else 'no lifecycle'}"
            )

        # Transition old lifecycle to SUPERSEDED
        old_lc.transition(
            ProposalState.SUPERSEDED,
            reason="revised_by_caller",
            timestamp=now,
        )

        # Merge args: start from original, overlay caller's corrections
        merged_args = dict(evaluation.proposal.args)
        if args:
            merged_args.update(args)

        # Re-evaluate with new args and the supersedes link
        new_eval = self.evaluate(
            action_type=evaluation.proposal.action_type,
            args=merged_args,
            entity_map=entity_map,
            resource_map=resource_map,
            semantic_keys=semantic_keys,
            now_iso=now,
        )
        # Stamp lineage on the new proposal
        new_eval.proposal.supersedes = evaluation.proposal.proposal_id
        return new_eval

    def create_approval_request(
        self,
        evaluation: EvaluationResult,
    ) -> ApprovalRequest:
        """
        Create a structured approval request from an APPROVE evaluation.

        Returns the information a human needs to make an approval decision:
        what action, why it needs approval, what the risk is.
        """
        if evaluation.policy.decision != Decision.APPROVE:
            raise ValueError(
                f"Approval requests are only for APPROVE decisions, "
                f"got {evaluation.policy.decision.value}"
            )

        spec = get_spec(evaluation.proposal.action_type)
        risk_parts = []
        if spec:
            risk_parts.append(f"blast_radius={spec.blast_radius.value}")
            if not spec.reversible:
                risk_parts.append("irreversible")
            risk_parts.append(f"feedback={spec.feedback_latency.value}")

        return ApprovalRequest(
            proposal_id=evaluation.proposal.proposal_id,
            action_type=evaluation.proposal.action_type,
            reason_codes=evaluation.policy.reason_codes,
            risk_summary=", ".join(risk_parts),
            required_checks=evaluation.policy.required_checks,
            args_summary=evaluation.proposal.args,
        )

    def approve(
        self,
        evaluation: EvaluationResult,
        *,
        check_results: list[CheckResult] | None = None,
        now_iso: str | None = None,
    ) -> PipelineResult:
        """Execute a proposal after human approval. Checks are still enforced."""
        if evaluation.policy.decision != Decision.APPROVE:
            raise ValueError(
                f"Can only approve APPROVE decisions, "
                f"got {evaluation.policy.decision.value}"
            )
        return self.execute(
            evaluation,
            approved=True,
            check_results=check_results,
            now_iso=now_iso,
        )

    def reject(
        self,
        evaluation: EvaluationResult,
        *,
        reason: str = "human_rejected",
        now_iso: str | None = None,
    ) -> PipelineResult:
        """Reject a proposal that was awaiting approval."""
        now = now_iso or datetime.now(timezone.utc).isoformat()
        lc = evaluation.lifecycle
        if lc and not lc.is_terminal:
            lc.transition(ProposalState.DENIED, reason=reason, timestamp=now)
        return PipelineResult(
            evaluation=evaluation,
            execution=ExecutionResult(
                action_id="",
                status=ExecutionStatus.FAILED,
                observations=[f"Rejected: {reason}"],
            ),
            lifecycle=lc,
        )

    def _handle_obligation_status_change(
        self,
        obligation: Obligation,
        previous_status: ObligationStatus,
        new_status: ObligationStatus,
        changed_at: str,
    ) -> None:
        proposal_id = obligation.source_proposal_id
        if proposal_id is None:
            return

        lifecycle = self.lifecycles.get(proposal_id)
        if lifecycle is None or lifecycle.current_state != ProposalState.OBLIGATIONS_OPEN:
            return

        linked = self.store.get_obligations_for_proposal(proposal_id)
        if not linked:
            return

        target_state: ProposalState | None = None
        reason: str | None = None
        if any(item.status == ObligationStatus.BREACHED for item in linked):
            target_state = ProposalState.OBLIGATIONS_BREACHED
            reason = f"obligation_breached:{obligation.obligation_id}"
        elif all(item.status == ObligationStatus.SATISFIED for item in linked):
            target_state = ProposalState.OBLIGATIONS_SATISFIED
            reason = f"obligations_satisfied:{obligation.obligation_id}"

        if target_state is None or reason is None:
            return

        lifecycle.transition(
            target_state,
            reason=reason,
            timestamp=changed_at,
            metadata={
                "obligation_id": obligation.obligation_id,
                "previous_status": previous_status.value,
                "new_status": new_status.value,
            },
        )
