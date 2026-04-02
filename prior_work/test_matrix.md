# Test Matrix

The matrix is failure-oriented. A case only matters if it proves the harness rejects a bad action, routes uncertainty correctly, or preserves future constraints.

| ID | Scenario | Expected policy | Why it matters |
| --- | --- | --- | --- |
| T1 | Quote request with missing `validUntil` | `clarify` with `missing_required_arg` | Prevents silent execution on incomplete commercial commitments. |
| T2 | Quote request names a client with two matches | `clarify` with `entity_resolution_conflict` | Ensures entity ambiguity does not collapse into a guess. |
| T3 | New quote conflicts with open commitment on same client and product | `deny` with `commitment_conflict` | Proves commitments constrain future action space. |
| T4 | Quote request has all fields but price source is stale | `check` | Shows cheap preflight checks can block high-confidence silent failures. |
| T5 | Quote send succeeds | `allow`, then persist commitment and obligation | Verifies effect emission on success. |
| T6 | No reply arrives by quote obligation deadline | obligation marked `breached`, follow-up action suggested | Proves obligations are active state, not logs. |
| T7 | DeleteRows with empty predicate | `deny` | Guards against catastrophic broad deletes. |
| T8 | DeleteRows with large `dryRunCount` and no backup | `approve` or `deny` per policy | Exercises irreversibility and blast radius gating. |
| T9 | DeleteRows with safe predicate and backup | `allow` after `check` | Confirms the happy path still honors cheap safety checks. |
| T10 | DeleteRows succeeds but downstream count mismatch appears | obligation `breached` | Confirms postconditions and obligation checks are separate from execution success. |
| T11 | ScheduleMeeting with ambiguous attendee name | `clarify` | Ensures human entity selection when multiple matches exist. |
| T12 | ScheduleMeeting with hard conflict but no fallback | `deny` or `clarify` | Prevents polite failure from becoming silent double-booking. |
| T13 | ScheduleMeeting with clear attendees and available slot | `allow` then obligation open | Verifies obligation emission for response monitoring. |
| T14 | Interpreter returns two incompatible schemas for a high-cost action | `check` or `clarify`, not `allow` | Confirms schema competition is not collapsed into a guess. |
| T15 | Interpreter returns multiple variants that share a safe first step | `check` on the intersection action | Proves the harness can commit to safe overlap rather than escalating immediately. |
| T16 | Unregistered action type proposed | `deny` or decompose | Preserves the closed mutation boundary. |
| T17 | Freeform request produces no typed action but clear information request | no execution proposal | Prevents the system from forcing a mutation when none is intended. |
| T18 | Old open obligation intersects a new action on the same entity | obligation projected into context before evaluation | Proves selective context projection works. |
| T19 | Unrelated obligation exists on another entity | no projection | Prevents context pollution from the entire ledger. |
| T20 | High-cost action with fast feedback | no tier-3 sampling by default | Confirms safety budget is spent where feedback is poor, not everywhere. |

## Adapter-specific acceptance checks

### SendQuoteEmail

- emitted commitment contains `recipientId`, `productId`, `unitPrice`, `currency`, `validUntil`, `termsVersion`
- emitted obligation due date is derived deterministically from adapter policy
- future conflicting quote is blocked until commitment expires or is superseded

### DeleteRows

- executor records exact target table and predicate in the mutation summary
- missing backup route is enforced by policy, not by operator memory
- downstream verification can breach even if the SQL delete itself succeeded

### ScheduleMeeting

- calendar check is a policy input, not an after-the-fact audit
- attendee decline / no-response monitoring is persisted as an obligation

## Regression checks

- Adding a new adapter cannot bypass effect emission.
- An adapter cannot execute if it lacks a policy definition.
- A passed check cannot override a hard deny.
- Expired commitments no longer block, but remain auditable in effect history.
