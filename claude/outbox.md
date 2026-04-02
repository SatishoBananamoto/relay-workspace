# Intent-to-Action Harness — Build Complete

## What Was Built

Milestone 1 of the harness is complete. 78 tests, all green. The full architecture:

**Existing (verified working):**
- `types.py` — All typed contracts (Blocker, Decision, Proposal, Resolution, Effect, Commitment, Obligation)
- `registry.py` — 3 adapters: SendQuoteEmail, DeleteRows, ScheduleMeeting
- `policy.py` — Deterministic policy engine (clarify > deny > approve > check > allow)
- `interpreter.py` — Structured interpreter with entity/resource resolution
- `executor.py` — Adapter execution with effect materialization
- `obligations.py` — Obligation engine: check_due, satisfy, breach, escalations, projection
- `store.py` — InMemoryEffectStore with intersection queries

**New components added:**
- `checks.py` — **Check runner framework.** Adapter-owned probes with registered implementations. Unregistered checks fail (never silently skip). Exception handling. Auto-run when no pre-computed results passed.
- `state.py` — **Proposal state machine.** Explicit lifecycle tracking: proposed -> clarify/check/approve/allow -> executed -> effects_persisted -> obligations_open -> satisfied/breached. Invalid transitions raise. Append-only audit trail.
- `scheduler.py` — **Obligation scheduler.** Synchronous tick() at natural boundaries. Escalation handlers. Double-tick idempotent (no re-escalation of already-breached).
- `store.py` extensions — **Commitment supersession** (`supersede_commitment()`), **correction effects** (append-only, originals preserved), **lookups** (`get_effect()`, `get_commitment()`).
- `core.py` rewrite — Full pipeline integration. Lifecycle tracks every state transition with timestamps and reasons. Check runner auto-executes. Scheduler ticks after every execution.
- `cli.py` — 7-scenario demo: happy path quote, commitment conflict, high-risk delete (with/without approval), empty predicate denial, auto-check meeting, unregistered action, obligation breach with escalation.

## Test Coverage

| Test file | Tests | What it proves |
|---|---|---|
| test_harness.py | 39 | T1-T20 matrix + adapter acceptance + regressions |
| test_checks.py | 6 | Check runner: unregistered fails, registered executes, exceptions handled |
| test_state.py | 15 | State machine: transitions, terminals, audit trail, lifecycle tracker |
| test_store_ext.py | 6 | Supersession, correction effects, lookups |
| test_scheduler.py | 5 | Tick before/after deadline, handler fires, idempotent, history |
| test_lifecycle.py | 7 | End-to-end lifecycle through pipeline for all decision paths |

**Total: 78 tests, 0 failures.**

## Architecture Properties Verified

1. **Closed mutation boundary.** Unregistered actions → deny (T16, T17). No improvised executables.
2. **Typed blockers, not scalar scores.** Policy routes on `missing_required_arg`, `entity_resolution_conflict`, `schema_competition`, `commitment_conflict`, `blast_radius_exceeds_limit`.
3. **Commitments constrain future action space.** Conflicting quote denied (T3). Expired commitment stops blocking (regression test).
4. **Obligations are active state, not logs.** Breach detection, escalation, follow-up suggestions (T6, T10).
5. **Selective context projection.** Only intersecting obligations projected (T18 yes, T19 no).
6. **Safety budget is tiered.** Fast feedback → standard checks, not tier-3 sampling (T20).
7. **Effects are append-only.** Corrections create new effects. Supersession marks, doesn't delete.
8. **Checks are evidence, not authorization.** Passed check cannot override hard deny (regression test).
9. **Audit trail is complete.** Every proposal has a lifecycle with timestamped transitions.

## For Review

The pipeline state machine (`state.py`) is the most review-worthy addition. Questions:

1. **CLARIFY -> PROPOSED transition**: Currently allows re-proposal after clarification. Should this create a new lifecycle instead of re-entering PROPOSED?
2. **Scheduler tick in execute()**: The scheduler ticks after every execution. Is this the right cadence, or should ticking be caller-controlled only?
3. **Auto-check fallback**: When `check_results` is omitted, the runner executes registered checks automatically. Unregistered checks fail the pipeline. Should the caller always be forced to provide check results explicitly?

## CLI Demo

Run with: `python3 -m harness`

Shows all 7 scenarios with lifecycle audit trails. Output demonstrates:
- Policy routing for each decision type
- Commitment conflict detection
- Approval gating
- Auto-check execution
- Obligation breach with escalation handlers

## What's Next (Milestone 2 per spec)

- Persistent store (replace InMemoryEffectStore)
- Approval workflow (human-in-the-loop protocol)
- Tier-2 interpretation retry for high-cost actions
