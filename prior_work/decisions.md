# Decisions

## Scope

`v1` is an intent-to-action harness for a narrow domain. It is open-ended in reasoning and closed at the mutation boundary.

The kernel is reusable, but the executable action registry is domain-local. New behavior is added by engineering new adapters, not by letting the model invent runtime actions.

## Core Principle

The model proposes. The harness decides.

That means:

1. The model may interpret requests, draft content, and fill structured fields.
2. The harness owns resolution, policy, checks, execution, persistence, commitments, obligations, and future constraint enforcement.
3. If a requested mutation cannot be expressed as a typed action contract, the system must stop, decompose, or escalate.

## Execution Boundary

The executable unit is a typed action adapter with:

- required fields
- entity resolution rules
- preconditions
- cheap checks
- approval policy
- effect emission rules

`SendEmail` is not an adapter. `SendQuoteEmail` is.

## Effect Model

Every successful execution must persist a typed `Effect` made of:

- `mutations`: what changed in the world
- `commitments`: what future actions are now constrained
- `obligations`: what must later be checked or followed up

This split is load-bearing. The system must not collapse commitments and obligations into generic logs.

## Policy Outcomes

The policy engine returns one of:

- `allow`
- `check`
- `clarify`
- `approve`
- `deny`

The decision is based on typed blockers and action metadata, not a scalar uncertainty score.

## Uncertainty Ladder

The harness spends safety budget in tiers:

1. Tier 0: adapter metadata and deterministic policy.
2. Tier 1: missing-field detection, entity resolution, commitment/obligation joins.
3. Tier 2: second interpretation pass for high-cost or schema-competition cases.
4. Tier 3: multi-sample divergence only for dangerous actions with slow or silent feedback.

No higher tier is allowed unless the lower tier leaves the action unresolved.

## Context Projection

The effect store is the source of truth. The model does not carry obligations by memory.

Before any new action executes, the harness queries for open commitments and obligations intersecting:

- target entities
- target resources
- action type
- semantic keys declared by the adapter

Only the intersecting items are projected into context for that action decision.

## First Slice

The first three adapters are:

1. `SendQuoteEmail`
2. `DeleteRows`
3. `ScheduleMeeting`

These were chosen because they cover:

- explicit commitments
- irreversible mutations
- slow feedback
- ambiguity at entity resolution
- obligations with real deadlines

## Immediate Next Build

The next implementation artifact is not more discussion. It is a thin vertical slice with:

1. adapter schemas
2. policy decision tables
3. effect-store schema
4. constraint query API
5. failure-oriented tests
