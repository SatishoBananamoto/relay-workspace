export type Blocker =
  | "missing_required_arg"
  | "entity_resolution_conflict"
  | "schema_competition"
  | "commitment_conflict"
  | "blast_radius_exceeds_limit";

export type PolicyDecisionKind =
  | "allow"
  | "check"
  | "clarify"
  | "approve"
  | "deny";

export type Proposal = {
  proposalId: string;
  actionType: string;
  args: Record<string, unknown>;
  evidenceRefs: string[];
  blockers: Blocker[];
};

export type Resolution = {
  entityIds: string[];
  resourceKeys: string[];
  semanticKeys: string[];
  conflicts: string[];
};

export type CheckSpec = {
  id: string;
  kind: "query" | "dry_run" | "lookup" | "simulation";
  requiredFor: ("allow" | "approve")[];
};

export type ApprovalPolicy = "never" | "if_high_risk" | "always";

export type ActionSpec = {
  actionType: string;
  version: string;
  requiredArgs: string[];
  blastRadius: "low" | "medium" | "high";
  reversible: boolean;
  feedbackLatency: "fast" | "slow" | "silent";
  cheapChecks: CheckSpec[];
  approvalPolicy: ApprovalPolicy;
  effectTemplate: EffectTemplate;
};

export type PolicyDecision = {
  decision: PolicyDecisionKind;
  blockers: Blocker[];
  requiredChecks: string[];
  reasonCodes: string[];
};

export type Mutation = {
  resource: string;
  op: string;
  summary: string;
};

export type Commitment = {
  commitmentId: string;
  kind: string;
  entityIds: string[];
  resourceKeys: string[];
  semanticKeys: string[];
  fields: Record<string, string | number | boolean>;
  expiresAt?: string;
  supersededBy?: string;
};

export type Obligation = {
  obligationId: string;
  kind: string;
  entityIds: string[];
  resourceKeys: string[];
  semanticKeys: string[];
  dueAt: string;
  verifyWith: "poll" | "query" | "human";
  failureMode: string;
  status: "open" | "satisfied" | "breached";
};

export type Effect = {
  actionId: string;
  actionType: string;
  entityIds: string[];
  resourceKeys: string[];
  semanticKeys: string[];
  mutations: Mutation[];
  commitments: Commitment[];
  obligations: Obligation[];
  observedAt: string;
};

export type EffectTemplate = (
  args: Record<string, unknown>,
  resolution: Resolution,
  nowIso: string,
) => Omit<Effect, "actionId" | "actionType" | "observedAt">;

export class InMemoryEffectStore {
  private readonly effects: Effect[] = [];

  append(effect: Effect): void {
    this.effects.push(effect);
  }

  queryOpenIntersection(input: {
    actionType: string;
    entityIds: string[];
    resourceKeys: string[];
    semanticKeys: string[];
  }): { commitments: Commitment[]; obligations: Obligation[] } {
    const entitySet = new Set(input.entityIds);
    const resourceSet = new Set(input.resourceKeys);
    const semanticSet = new Set(input.semanticKeys);

    const commitments: Commitment[] = [];
    const obligations: Obligation[] = [];

    for (const effect of this.effects) {
      for (const commitment of effect.commitments) {
        if (commitment.supersededBy) {
          continue;
        }
        if (commitment.expiresAt && commitment.expiresAt < new Date().toISOString()) {
          continue;
        }
        if (
          intersects(entitySet, commitment.entityIds) ||
          intersects(resourceSet, commitment.resourceKeys) ||
          intersects(semanticSet, commitment.semanticKeys)
        ) {
          commitments.push(commitment);
        }
      }

      for (const obligation of effect.obligations) {
        if (obligation.status !== "open") {
          continue;
        }
        if (
          intersects(entitySet, obligation.entityIds) ||
          intersects(resourceSet, obligation.resourceKeys) ||
          intersects(semanticSet, obligation.semanticKeys)
        ) {
          obligations.push(obligation);
        }
      }
    }

    return { commitments, obligations };
  }
}

export function evaluateProposal(input: {
  proposal: Proposal;
  resolution: Resolution;
  spec: ActionSpec;
  store: InMemoryEffectStore;
}): PolicyDecision {
  const { proposal, resolution, spec, store } = input;
  const blockers = new Set<Blocker>(proposal.blockers);
  const reasonCodes: string[] = [];

  for (const requiredArg of spec.requiredArgs) {
    const value = proposal.args[requiredArg];
    if (value === undefined || value === null || value === "") {
      blockers.add("missing_required_arg");
      reasonCodes.push(`missing:${requiredArg}`);
    }
  }

  if (resolution.conflicts.length > 0) {
    blockers.add("entity_resolution_conflict");
    reasonCodes.push("resolution_conflict");
  }

  const intersecting = store.queryOpenIntersection({
    actionType: proposal.actionType,
    entityIds: resolution.entityIds,
    resourceKeys: resolution.resourceKeys,
    semanticKeys: resolution.semanticKeys,
  });

  if (hasCommitmentConflict(proposal, intersecting.commitments)) {
    blockers.add("commitment_conflict");
    reasonCodes.push("open_commitment_conflict");
  }

  if (blockers.has("schema_competition") || blockers.has("entity_resolution_conflict")) {
    return {
      decision: "clarify",
      blockers: [...blockers],
      requiredChecks: [],
      reasonCodes,
    };
  }

  if (blockers.has("commitment_conflict")) {
    return {
      decision: "deny",
      blockers: [...blockers],
      requiredChecks: [],
      reasonCodes,
    };
  }

  if (blockers.has("missing_required_arg")) {
    return {
      decision: "clarify",
      blockers: [...blockers],
      requiredChecks: [],
      reasonCodes,
    };
  }

  if (spec.blastRadius === "high" && !spec.reversible && spec.approvalPolicy !== "never") {
    return {
      decision: "approve",
      blockers: [...blockers],
      requiredChecks: spec.cheapChecks.map((check) => check.id),
      reasonCodes: [...reasonCodes, "high_risk_irreversible"],
    };
  }

  if (spec.cheapChecks.length > 0) {
    return {
      decision: "check",
      blockers: [...blockers],
      requiredChecks: spec.cheapChecks.map((check) => check.id),
      reasonCodes: [...reasonCodes, "cheap_checks_required"],
    };
  }

  return {
    decision: "allow",
    blockers: [...blockers],
    requiredChecks: [],
    reasonCodes,
  };
}

export function materializeEffect(input: {
  actionId: string;
  proposal: Proposal;
  resolution: Resolution;
  spec: ActionSpec;
  nowIso?: string;
}): Effect {
  const nowIso = input.nowIso ?? new Date().toISOString();
  const partial = input.spec.effectTemplate(input.proposal.args, input.resolution, nowIso);

  return {
    actionId: input.actionId,
    actionType: input.proposal.actionType,
    entityIds: input.resolution.entityIds,
    resourceKeys: input.resolution.resourceKeys,
    semanticKeys: input.resolution.semanticKeys,
    mutations: partial.mutations,
    commitments: partial.commitments,
    obligations: partial.obligations,
    observedAt: nowIso,
  };
}

function intersects(index: Set<string>, values: string[]): boolean {
  for (const value of values) {
    if (index.has(value)) {
      return true;
    }
  }
  return false;
}

function hasCommitmentConflict(proposal: Proposal, commitments: Commitment[]): boolean {
  if (proposal.actionType !== "SendQuoteEmail") {
    return false;
  }

  const proposedPrice = proposal.args.unitPrice;
  const proposedTerms = proposal.args.termsVersion;

  for (const commitment of commitments) {
    if (commitment.kind !== "quote") {
      continue;
    }
    if (
      commitment.fields.unitPrice !== proposedPrice ||
      commitment.fields.termsVersion !== proposedTerms
    ) {
      return true;
    }
  }

  return false;
}

function requireString(args: Record<string, unknown>, key: string): string {
  const value = args[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Missing string arg: ${key}`);
  }
  return value;
}

function requireNumber(args: Record<string, unknown>, key: string): number {
  const value = args[key];
  if (typeof value !== "number") {
    throw new Error(`Missing number arg: ${key}`);
  }
  return value;
}

export const registry: Record<string, ActionSpec> = {
  SendQuoteEmail: {
    actionType: "SendQuoteEmail",
    version: "1",
    requiredArgs: [
      "recipientId",
      "productId",
      "unitPrice",
      "currency",
      "validUntil",
      "termsVersion",
    ],
    blastRadius: "medium",
    reversible: false,
    feedbackLatency: "slow",
    approvalPolicy: "if_high_risk",
    cheapChecks: [
      { id: "pricing_source_lookup", kind: "lookup", requiredFor: ["allow"] },
    ],
    effectTemplate: (args, resolution, nowIso) => ({
      mutations: [
        {
          resource: "email",
          op: "send_quote",
          summary: `Quote sent to ${requireString(args, "recipientId")}`,
        },
      ],
      commitments: [
        {
          commitmentId: `commitment:${nowIso}:quote`,
          kind: "quote",
          entityIds: resolution.entityIds,
          resourceKeys: resolution.resourceKeys,
          semanticKeys: resolution.semanticKeys,
          fields: {
            unitPrice: requireNumber(args, "unitPrice"),
            currency: requireString(args, "currency"),
            termsVersion: requireString(args, "termsVersion"),
          },
          expiresAt: requireString(args, "validUntil"),
        },
      ],
      obligations: [
        {
          obligationId: `obligation:${nowIso}:quote_ack`,
          kind: "quote_acknowledgement",
          entityIds: resolution.entityIds,
          resourceKeys: resolution.resourceKeys,
          semanticKeys: resolution.semanticKeys,
          dueAt: requireString(args, "validUntil"),
          verifyWith: "human",
          failureMode: "No acknowledgement or reply before quote expiry",
          status: "open",
        },
      ],
    }),
  },
  DeleteRows: {
    actionType: "DeleteRows",
    version: "1",
    requiredArgs: ["connectionId", "table", "predicate", "dryRunCount"],
    blastRadius: "high",
    reversible: false,
    feedbackLatency: "slow",
    approvalPolicy: "if_high_risk",
    cheapChecks: [{ id: "sql_dry_run", kind: "dry_run", requiredFor: ["approve"] }],
    effectTemplate: (args, resolution, nowIso) => ({
      mutations: [
        {
          resource: requireString(args, "table"),
          op: "delete_rows",
          summary: `Delete rows where ${requireString(args, "predicate")}`,
        },
      ],
      commitments: [],
      obligations: [
        {
          obligationId: `obligation:${nowIso}:delete_verify`,
          kind: "delete_verification",
          entityIds: resolution.entityIds,
          resourceKeys: resolution.resourceKeys,
          semanticKeys: resolution.semanticKeys,
          dueAt: nowIso,
          verifyWith: "query",
          failureMode: "Downstream counts or replication diverged after delete",
          status: "open",
        },
      ],
    }),
  },
  ScheduleMeeting: {
    actionType: "ScheduleMeeting",
    version: "1",
    requiredArgs: ["attendeeIds", "startTime", "durationMinutes", "purpose"],
    blastRadius: "low",
    reversible: true,
    feedbackLatency: "slow",
    approvalPolicy: "never",
    cheapChecks: [{ id: "calendar_lookup", kind: "lookup", requiredFor: ["allow"] }],
    effectTemplate: (args, resolution, nowIso) => ({
      mutations: [
        {
          resource: "calendar",
          op: "create_event",
          summary: `Meeting scheduled for ${requireString(args, "startTime")}`,
        },
      ],
      commitments: [],
      obligations: [
        {
          obligationId: `obligation:${nowIso}:meeting_response`,
          kind: "meeting_response",
          entityIds: resolution.entityIds,
          resourceKeys: resolution.resourceKeys,
          semanticKeys: resolution.semanticKeys,
          dueAt: requireString(args, "startTime"),
          verifyWith: "poll",
          failureMode: "Required attendees declined or did not respond",
          status: "open",
        },
      ],
    }),
  },
};
