# MVP Spec

## Goal

Build a thin harness that turns model proposals into typed, enforceable actions with durable commitments and obligations.

## Non-goals

- General autonomous execution outside a typed registry
- Parsing commitments from arbitrary prose
- Rich planning UI
- Model-native memory as the primary store of obligations

## Architecture

```text
User Request
  -> Interpreter
  -> Resolver
  -> Policy Engine
  -> Checks
  -> Executor
  -> Effect Store
  -> Obligation Engine
```

## Components

### 1. Interpreter

Input:

- user request
- relevant transcript slice
- projected open commitments and obligations

Output:

```ts
type ActionProposal = {
  proposalId: string
  actionType: string
  args: Record<string, unknown>
  evidenceRefs: string[]
  blockers: Blocker[]
}

type Blocker =
  | "missing_required_arg"
  | "entity_resolution_conflict"
  | "schema_competition"
  | "commitment_conflict"
  | "blast_radius_exceeds_limit"
```

Rules:

- The interpreter may return `0..k` proposals.
- Proposals are inert until they map to a registered adapter.
- If no typed action fits, the interpreter must return blockers, not improvise an executable primitive.

### 2. Resolver

Responsibility:

- map human references to canonical IDs
- normalize times, prices, tables, products, and recipients
- attach resolution evidence

Output:

```ts
type Resolution = {
  entityIds: string[]
  resourceKeys: string[]
  semanticKeys: string[]
  conflicts: string[]
}
```

Rules:

- Resolution conflicts become blockers.
- Policy must never silently choose one entity when multiple plausible matches remain.

### 3. Action Registry

```ts
type ActionSpec = {
  actionType: string
  version: string
  requiredArgs: string[]
  entitySelectors: string[]
  resourceSelectors: string[]
  semanticKeySelectors: string[]
  blastRadius: "low" | "medium" | "high"
  reversible: boolean
  feedbackLatency: "fast" | "slow" | "silent"
  preconditions: PolicyRule[]
  cheapChecks: CheckSpec[]
  approvalPolicy: ApprovalPolicy
  effectTemplate: EffectTemplate
}
```

Adapters must be code, not prompts.

### 4. Policy Engine

```ts
type PolicyDecision = {
  decision: "allow" | "check" | "clarify" | "approve" | "deny"
  blockers: Blocker[]
  requiredChecks: string[]
  reasonCodes: string[]
}
```

Policy order:

1. Validate required fields.
2. Validate entity/resource resolution.
3. Query effect store for intersecting commitments and obligations.
4. Evaluate preconditions and cheap checks.
5. Choose the cheapest action that preserves safety:
   - `allow` when all deterministic conditions pass
   - `check` when a cheap probe can reduce uncertainty
   - `clarify` when the user must choose among incompatible interpretations
   - `approve` when risk is real but bounded and human sign-off is the right control
   - `deny` when the action violates hard policy

### 5. Checks

Checks are orthogonal probes owned by the adapter.

```ts
type CheckSpec = {
  id: string
  kind: "query" | "dry_run" | "lookup" | "simulation"
  requiredFor: ("allow" | "approve")[]
}
```

Rules:

- Cheap checks should survive operational pressure.
- Expensive checks are only allowed for high-cost, slow-feedback actions.
- A check result is evidence, not automatic authorization.

### 6. Executor

The executor runs only after policy returns `allow` or after the required `check` / `approve` path succeeds.

```ts
type ExecutionResult = {
  actionId: string
  status: "executed" | "failed"
  observations: string[]
  effect?: Effect
}
```

### 7. Effect Store

```ts
type Effect = {
  actionId: string
  actionType: string
  entityIds: string[]
  resourceKeys: string[]
  semanticKeys: string[]
  mutations: Mutation[]
  commitments: Commitment[]
  obligations: Obligation[]
  observedAt: string
}

type Mutation = {
  resource: string
  op: string
  summary: string
}

type Commitment = {
  commitmentId: string
  kind: string
  entityIds: string[]
  resourceKeys: string[]
  semanticKeys: string[]
  fields: Record<string, string | number | boolean>
  expiresAt?: string
}

type Obligation = {
  obligationId: string
  kind: string
  entityIds: string[]
  resourceKeys: string[]
  semanticKeys: string[]
  dueAt: string
  verifyWith: "poll" | "query" | "human"
  failureMode: string
  status: "open" | "satisfied" | "breached"
}
```

Rules:

- Commitments constrain future action space.
- Obligations drive follow-up checks and escalation.
- Effects are append-only. Corrections create new effects rather than mutating history.

### 8. Obligation Engine

Responsibilities:

- schedule checks for open obligations
- mark obligations `satisfied` or `breached`
- raise new actions or escalations when a breach occurs

Projection rule:

- only obligations intersecting the candidate action are injected into model context

## First Three Adapters

### `SendQuoteEmail`

Input schema:

```ts
type SendQuoteEmailArgs = {
  recipientId: string
  productId: string
  unitPrice: number
  currency: string
  validUntil: string
  termsVersion: string
}
```

Deterministic rules:

- deny if recipient or product is unresolved
- clarify if any commercial field is missing
- deny if an open conflicting quote exists for the same recipient and product
- check current pricing source before send

Effects:

- mutation: outbound quote sent
- commitment: quoted price and terms valid until `validUntil`
- obligation: verify acknowledgement or reply by deadline

### `DeleteRows`

Input schema:

```ts
type DeleteRowsArgs = {
  connectionId: string
  table: string
  predicate: string
  backupRef?: string
  dryRunCount: number
}
```

Deterministic rules:

- deny if predicate is empty
- deny if table is unresolved
- require `approve` if irreversible and no backup exists
- require `check` when `dryRunCount` exceeds configured threshold

Effects:

- mutation: rows deleted
- obligation: verify downstream counts and replication state

### `ScheduleMeeting`

Input schema:

```ts
type ScheduleMeetingArgs = {
  attendeeIds: string[]
  startTime: string
  durationMinutes: number
  purpose: string
}
```

Deterministic rules:

- clarify on attendee ambiguity
- check calendars before scheduling
- deny if required attendees have hard conflicts and no fallback was chosen

Effects:

- mutation: meeting created
- obligation: monitor declines or non-response before meeting start

## API Sketch

### Evaluate

```http
POST /v1/actions/evaluate
```

Response:

```json
{
  "proposal": {},
  "resolution": {},
  "policyDecision": {}
}
```

### Execute

```http
POST /v1/actions/{proposalId}/execute
```

### Query constraints

```http
POST /v1/effects/query
```

Request:

```json
{
  "actionType": "SendQuoteEmail",
  "entityIds": ["client:123"],
  "resourceKeys": ["product:abc"],
  "semanticKeys": ["quote"]
}
```

## State Machine

```text
proposed
  -> clarify
  -> check
  -> approve
  -> allow
  -> executed
  -> effects_persisted
  -> obligations_open
  -> obligations_satisfied | obligations_breached
```

## Thin Vertical Slice

Milestone 1:

- hard-code three adapters
- in-memory effect store
- deterministic policy engine
- one scheduler loop for obligations
- CLI or HTTP demo

Milestone 2:

- persistent store
- approval workflow
- tier-2 interpretation retry for high-cost actions

Milestone 3:

- adapter authoring SDK
- more domains
