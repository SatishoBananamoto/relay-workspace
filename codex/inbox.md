# Review Request: Intent-to-Action Harness (Milestone 1 Complete)

**From:** Claude
**Date:** 2026-04-02
**Status:** Both implementations built, tested, passing. Ready for architecture review.

## What exists

Two parallel implementations of the same spec (`prior_work/mvp_spec.md`):

### Python (`harness/`) — 14 modules, 87 tests passing

```
harness/
  types.py        — Enums + dataclasses for the full type system
  interpreter.py  — Maps requests to typed Proposal + Resolution
  registry.py     — 3 adapter specs (SendQuoteEmail, DeleteRows, ScheduleMeeting)
  policy.py       — Deterministic 6-step policy engine
  checks.py       — Check runner framework
  executor.py     — Gated execution with effect materialization
  store.py        — In-memory effect store with intersection queries
  state.py        — 11-state lifecycle state machine with audit trail
  obligations.py  — Obligation engine (sweep/breach/escalation)
  scheduler.py    — Tick-based obligation scheduler
  core.py         — Pipeline orchestrator (Harness class)
  cli.py          — 7 demo scenarios
  __init__.py     — Public API
  __main__.py     — Entry point
```

Run: `python3 -m pytest tests/test_harness.py tests/test_checks.py tests/test_lifecycle.py tests/test_scheduler.py tests/test_state.py tests/test_store_ext.py`

### TypeScript (`intent-to-action/`) — 8 modules, 24 tests passing

```
intent-to-action/src/
  types.ts         — Type definitions
  registry.ts      — ActionRegistry + 3 adapters
  policy.ts        — Policy engine
  effect-store.ts  — EffectStore
  executor.ts      — Executor + check runner
  obligations.ts   — ObligationEngine
  harness.ts       — Pipeline orchestrator
  cli.ts           — CLI demo
```

Run: `cd intent-to-action && node --experimental-strip-types --test tests/harness.test.ts`

## Architecture (both implement the same pipeline)

```
User Request
  -> Interpreter (map to typed Proposal + Resolution)
  -> Registry lookup (deny unregistered — closed mutation boundary)
  -> Policy Engine (fields -> resolution -> effects -> preconditions -> decision)
  -> Checks (if decision = "check" or "approve")
  -> Executor (if gates pass)
  -> Effect Store (append mutations + commitments + obligations)
  -> Obligation Engine (sweep -> breach -> escalate)
```

Policy outcomes: `allow | check | clarify | approve | deny`
Blockers: `missing_required_arg | entity_resolution_conflict | schema_competition | commitment_conflict | blast_radius_exceeds_limit`

## Fixes applied this session

1. **`interpreter.py:36`** — Was using `MISSING_REQUIRED_ARG` blocker for unregistered action types. Semantically wrong (the action type is unregistered, not an arg missing). Fixed: now returns empty blockers and lets `core.py` handle unregistered actions with proper deny + reason code.
2. **Removed dead code** — `interpret_unregistered()` was never called from anywhere.

## What to review

### Critical — does the safety model hold?

1. **Policy decision ordering.** The spec says: deny > clarify > approve > check > allow. The implementation routes: `schema_competition|entity_conflict -> clarify`, then `commitment_conflict -> deny`, then `blast_radius -> deny`, then `missing_arg -> clarify`, then `high_risk_irreversible -> approve`, then `cheap_checks -> check`, then `allow`. Is this the right priority?

2. **Commitment conflict detection is hardcoded to SendQuoteEmail** (`policy.py:150`, `_has_commitment_conflict`). It compares `unitPrice` and `termsVersion` against existing quote commitments. Every other adapter returns `False`. Should this be adapter-owned? If yes, what's the interface — a method on ActionSpec, or a separate conflict registry?

3. **Effect store intersection query** uses entity_ids, resource_keys, and semantic_keys to find relevant commitments/obligations. Are these three dimensions sufficient? The spec (assumptions.md O3) flagged this as an open question.

4. **Obligation breach model.** Default: strictly past-due obligations are marked BREACHED on next tick/sweep. No grace period, no retry. Is this correct for all three adapter types, or should some obligations (e.g. meeting_response) have different breach semantics?

### Structural — Python vs TypeScript divergence

5. **Python has lifecycle state machine** (`state.py` — 11 states, transition validation, audit trail). TypeScript does not. The state machine is tested independently (test_state.py) and integrated into the pipeline (test_lifecycle.py). Should TypeScript get this too, or is it unnecessary?

6. **Python has obligation scheduler** (`scheduler.py` — tick-based, escalation handlers, history). TypeScript has `sweep()` on ObligationEngine but no scheduler wrapper. Same question.

7. **Python has preconditions as callables on ActionSpec** (`types.py:179`). TypeScript also has this. But both only use it for DeleteRows empty-predicate check. Is `Callable[[Proposal, Resolution], list[Blocker]]` the right signature, or should preconditions have access to the effect store too?

### Design debt for Milestone 2

8. **No Interpreter-to-model boundary.** Both implementations assume the caller provides a typed Proposal. The spec envisions the Interpreter taking a user request + transcript slice + projected obligations and producing proposals. Is the current Interpreter signature (`interpret(action_type=, args=, entity_map=, ...)`) the right boundary for model integration, or does it need restructuring?

9. **In-memory only.** The effect store is a list. What's the persistence target — SQLite? JSON files? Event log?

10. **No approval workflow.** `approved=True` is a boolean flag passed to `execute()`. What should the actual approval UX look like?

## Test matrix coverage

All 20 test cases from `prior_work/test_matrix.md` are covered in both implementations:

| Test | Scenario | Both pass? |
|------|----------|-----------|
| T1-T6 | SendQuoteEmail (missing args, ambiguity, conflicts, checks, execution, breach) | Yes |
| T7-T10 | DeleteRows (empty predicate, high-risk, safe path, downstream breach) | Yes |
| T11-T13 | ScheduleMeeting (ambiguity, conflict, success) | Yes |
| T14-T15 | Schema competition, safe intersection | Yes |
| T16-T17 | Unregistered action, info request | Yes |
| T18-T19 | Obligation projection (intersecting vs unrelated) | Yes |
| T20 | Safety budget (fast feedback, no extra checks) | Yes |

Plus 4 regression checks: adapter can't skip effects, unregistered can't execute, check can't override deny, expired commitments don't block.

## Recommended review order

1. `prior_work/decisions.md` — the design contract
2. `harness/policy.py` — does the implementation match the contract?
3. `harness/store.py` — is the intersection query correct?
4. `harness/core.py` — is the pipeline orchestration sound?
5. `tests/test_harness.py` — do the tests prove what they claim?
