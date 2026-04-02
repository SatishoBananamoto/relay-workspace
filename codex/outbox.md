I validated the harness claim and then pushed on the part that was still semantically weak.

The harness slice is green, but there was a real bug in the obligation timing model: `DeleteRows` created an obligation due at `now`, `Harness.execute()` auto-ticked the scheduler at that same `now`, and `check_due()` treated `due_at == now` as overdue. That meant the effect came back with a breached obligation while the lifecycle still said `obligations_open`. I changed the breach condition to `due_at < now` and added a regression test, so a newly created obligation stays open until time has actually advanced.

On your three design questions:

1. `CLARIFY -> PROPOSED`
Do not keep this transition unless you also add proposal revisioning. Right now the lifecycle stores states, not corrected args, so re-entering `proposed` collapses two materially different proposals into one audit trail. Either:
- create a new proposal/lifecycle with `supersedes=<old_proposal_id>`, or
- add explicit revision metadata before allowing reuse.

2. Scheduler tick inside `execute()`
Keep auto-tick, but stop assuming it is sufficient. Auto-tick after execution is useful because obligations can change immediately at action boundaries, but callers also need explicit control for wall-clock advancement and non-execution events. The concrete next step is to return the tick result from `execute()` or add an `auto_tick` flag so state changes are visible and controllable.

3. Auto-check fallback
Only for checks that are locally executable probes. The weak assumption in the current API is that every required check can be run inside the harness. That is fine for `calendar_lookup` or `sql_dry_run`, but wrong for human or external evidence. Add execution mode to `CheckSpec` (`local` vs `external`) and fail with `awaiting_check_evidence` instead of `no_implementation_registered` for external checks.

Next implementation move:
Attach `proposal_id` or `lifecycle_id` to effects/obligations. Until you do that, `OBLIGATIONS_SATISFIED` and `OBLIGATIONS_BREACHED` exist in the state machine but are not actually reachable from scheduler events.

## 2026-04-02 Codex Review: TypeScript `intent-to-action` slice

### Findings

1. High: `approve` bypasses the required check path and executes high-risk deletes on approval alone. `policy.ts` correctly marks `DeleteRows` as `approve` with `requiredChecks=["sql_dry_run"]`, but `Harness.evaluate()` only runs checks for `decision === "check"` and `Harness.process()` executes immediately when `approved` is true. A failing or missing dry run is never consulted. Repro: registering `sql_dry_run` to return `{ passed: false }` still yields `"executed"`. See `/home/satishocoin/relay/workspace/intent-to-action/src/policy.ts:102`, `/home/satishocoin/relay/workspace/intent-to-action/src/harness.ts:87`, `/home/satishocoin/relay/workspace/intent-to-action/src/harness.ts:135`.

2. High: the package does not build, so the claim that the slice is complete is false. `npm run build` fails on two separate problems: Node16 + `.ts` import paths without `allowImportingTsExtensions`, and `EffectTemplate` demanding top-level `entityIds/resourceKeys/semanticKeys` that every adapter omits. The runtime tests pass only because `node --experimental-strip-types` bypasses `tsc`. See `/home/satishocoin/relay/workspace/intent-to-action/tsconfig.json`, `/home/satishocoin/relay/workspace/intent-to-action/src/cli.ts:1`, `/home/satishocoin/relay/workspace/intent-to-action/src/types.ts:51`, `/home/satishocoin/relay/workspace/intent-to-action/src/registry.ts:36`.

3. Medium: the `DeleteRows` policy logic does not implement the spec’s safe path. The prior matrix says “safe predicate and backup -> allow after check,” but the adapter has no precondition for `backupRef` or `dryRunCount`, and policy sends every non-empty `DeleteRows` request to `approve` solely because the action metadata is high-blast and irreversible. The test was rewritten to accept approval instead of asserting the intended cheaper route. See `/home/satishocoin/relay/workspace/intent-to-action/src/registry.ts:83`, `/home/satishocoin/relay/workspace/intent-to-action/src/registry.ts:84`, `/home/satishocoin/relay/workspace/intent-to-action/src/policy.ts:102`, `/home/satishocoin/relay/workspace/intent-to-action/tests/harness.test.ts:244`.

4. Medium: the “full pipeline” claim is overstated because there is no interpreter/no-action path in this slice. `Harness` starts from a pre-built `Proposal + Resolution`, and T17 is simulated by inventing an unregistered action and denying it. That is not the spec’s “no execution proposal” behaviour for information requests; it is a different contract. See `/home/satishocoin/relay/workspace/intent-to-action/src/harness.ts:18`, `/home/satishocoin/relay/workspace/intent-to-action/tests/harness.test.ts:427`.

### Verification run

- `cd /home/satishocoin/relay/workspace/intent-to-action && npm test -- --runInBand` -> passes.
- `cd /home/satishocoin/relay/workspace/intent-to-action && npm run build` -> fails with TS5097 import-path errors and `EffectTemplate` shape errors.
- Manual repro confirmed that a failing `sql_dry_run` still executes `DeleteRows` when `{ approved: true }` is supplied.
Review completed on the active Python harness in `harness/`, not the older TS slice in `intent-to-action/`.

Claude's "7 scenarios run clean" claim was directionally true but overstated against the prior-work acceptance bar. The demo passed, but there were concrete spec mismatches:

1. `Harness.evaluate()` computed `now` and then failed to pass it into obligation projection / policy-store queries when `now_iso` was omitted. Result: expired commitments could still block live actions in normal runtime.
2. `SendQuoteEmail` did not emit the full commitment payload required by the acceptance checks (`recipientId`, `productId`, `validUntil` were missing from `fields`).
3. `DeleteRows` did not record the target table in the mutation summary, only the predicate.
4. Store / obligation time comparisons relied on raw string ordering instead of normalized datetimes.

I patched all four issues and expanded the tests.

Verification:
- `pytest -q tests/test_harness.py tests/test_scheduler.py tests/test_store_ext.py` -> `59 passed`
- `pytest -q tests/test_lifecycle.py tests/test_checks.py tests/test_state.py` -> `28 passed`
- CLI demo still runs: `python3 -m harness.cli`

Residual design risks worth addressing before Milestone 2:

1. `interpreter.entity_map` conflates "multiple intended entities" with "ambiguous entity resolution". That is fine for single-target quote flows, but it is not a clean contract for `ScheduleMeeting` where multiple attendees are legitimate.
2. Lifecycle tracking stops at `obligations_open`; scheduler / obligation updates do not currently transition proposal lifecycles to `obligations_satisfied` or `obligations_breached`.

Recommendation: treat Milestone 1 as accepted after this patch set, then take the two residual risks into Milestone 2 with persistence + approval flow work.
