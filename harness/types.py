"""Typed contracts for the intent-to-action harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, TypeAlias


# -- Blockers --

class Blocker(str, Enum):
    MISSING_REQUIRED_ARG = "missing_required_arg"
    ENTITY_RESOLUTION_CONFLICT = "entity_resolution_conflict"
    SCHEMA_COMPETITION = "schema_competition"
    COMMITMENT_CONFLICT = "commitment_conflict"
    BLAST_RADIUS_EXCEEDS_LIMIT = "blast_radius_exceeds_limit"


# -- Policy decisions --

class Decision(str, Enum):
    ALLOW = "allow"
    CHECK = "check"
    CLARIFY = "clarify"
    APPROVE = "approve"
    DENY = "deny"


# -- Blast radius / feedback / approval --

class BlastRadius(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FeedbackLatency(str, Enum):
    FAST = "fast"
    SLOW = "slow"
    SILENT = "silent"


class ApprovalPolicy(str, Enum):
    NEVER = "never"
    IF_HIGH_RISK = "if_high_risk"
    ALWAYS = "always"


class CheckKind(str, Enum):
    QUERY = "query"
    DRY_RUN = "dry_run"
    LOOKUP = "lookup"
    SIMULATION = "simulation"


class CheckMode(str, Enum):
    LOCAL = "local"       # Executable inside the harness (probe, dry run)
    EXTERNAL = "external" # Requires evidence from outside (human, external system)


class VerifyWith(str, Enum):
    POLL = "poll"
    QUERY = "query"
    HUMAN = "human"


class ObligationStatus(str, Enum):
    OPEN = "open"
    SATISFIED = "satisfied"
    BREACHED = "breached"


class ExecutionStatus(str, Enum):
    EXECUTED = "executed"
    FAILED = "failed"


# -- Core data structures --

SelectorCardinality: TypeAlias = Literal["one", "many"]
SelectorCandidates: TypeAlias = list[str | list[str]]

@dataclass
class Proposal:
    proposal_id: str
    action_type: str
    args: dict[str, Any]
    evidence_refs: list[str] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    supersedes: str | None = None


@dataclass(frozen=True)
class SelectorSpec:
    name: str
    cardinality: SelectorCardinality = "one"


@dataclass
class Resolution:
    entity_ids: list[str] = field(default_factory=list)
    resource_keys: list[str] = field(default_factory=list)
    semantic_keys: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    entity_slots: dict[str, list[str]] = field(default_factory=dict)
    resource_slots: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class CheckSpec:
    id: str
    kind: CheckKind
    required_for: list[Decision] = field(default_factory=list)
    mode: CheckMode = CheckMode.LOCAL


@dataclass
class CheckResult:
    check_id: str
    passed: bool
    detail: str = ""


@dataclass
class PolicyDecision:
    decision: Decision
    blockers: list[Blocker] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class Mutation:
    resource: str
    op: str
    summary: str


@dataclass
class Commitment:
    commitment_id: str
    kind: str
    entity_ids: list[str]
    resource_keys: list[str]
    semantic_keys: list[str]
    fields: dict[str, str | int | float | bool]
    expires_at: str | None = None
    superseded_by: str | None = None


@dataclass
class Obligation:
    obligation_id: str
    kind: str
    entity_ids: list[str]
    resource_keys: list[str]
    semantic_keys: list[str]
    due_at: str
    verify_with: VerifyWith
    failure_mode: str
    status: ObligationStatus = ObligationStatus.OPEN
    source_proposal_id: str | None = None


@dataclass
class Effect:
    action_id: str
    action_type: str
    entity_ids: list[str]
    resource_keys: list[str]
    semantic_keys: list[str]
    mutations: list[Mutation]
    commitments: list[Commitment]
    obligations: list[Obligation]
    observed_at: str


# Effect template: callable that produces partial effect from args + resolution
EffectTemplate = Callable[
    [dict[str, Any], Resolution, str],
    tuple[list[Mutation], list[Commitment], list[Obligation]],
]


@dataclass
class ActionSpec:
    action_type: str
    version: str
    required_args: list[str]
    blast_radius: BlastRadius
    reversible: bool
    feedback_latency: FeedbackLatency
    cheap_checks: list[CheckSpec]
    approval_policy: ApprovalPolicy
    effect_template: EffectTemplate
    entity_selectors: list[SelectorSpec] = field(default_factory=list)
    resource_selectors: list[SelectorSpec] = field(default_factory=list)
    # Adapter-specific precondition callable (proposal, resolution) -> list[Blocker]
    preconditions: Callable[[Proposal, Resolution], list[Blocker]] | None = None
    # Adapter-specific approval gate for cases where generic risk metadata
    # is not enough to choose between CHECK and APPROVE.
    requires_approval: Callable[[Proposal, Resolution], bool] | None = None
    # Adapter-owned commitment conflict detection. Receives the proposal,
    # resolution, and all intersecting open commitments. Returns True if
    # the proposal conflicts with any existing commitment.
    conflict_detector: Callable[[Proposal, Resolution, list[Commitment]], bool] | None = None
    # Intent classification: regex patterns that identify this action in free text.
    # Used by IntentClassifier to route unstructured input to the right action.
    intent_patterns: list[str] = field(default_factory=list)
    # Arg extractor: callable that pulls structured args from free text.
    # Receives (text: str) -> dict[str, Any]. Used after pattern match.
    arg_extractor: Callable[[str], dict[str, Any]] | None = None


@dataclass
class ExecutionResult:
    action_id: str
    status: ExecutionStatus
    observations: list[str] = field(default_factory=list)
    effect: Effect | None = None
