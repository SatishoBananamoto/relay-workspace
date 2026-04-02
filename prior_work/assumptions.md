# Assumptions

This file exists to stop the project from hiding design choices inside prose.

## Resolved

| ID | Assumption | Status | Resolution |
| --- | --- | --- | --- |
| A1 | The model can report its own uncertainty type reliably. | Rejected | The harness must not depend on self-reported uncertainty. Execution gates run on typed contracts, evidence completeness, entity resolution quality, reversibility, blast radius, observability, and conflicts with stored effects. |
| A2 | Freeform outbound actions are acceptable if we inspect them after generation. | Rejected | The mutation boundary is typed. The model may draft content, but execution only happens through typed action adapters with explicit fields and deterministic policies. |
| A3 | A generic `SendEmail` action is a useful primitive. | Rejected | The safe primitive is domain-specific, e.g. `SendQuoteEmail`, `SendStatusUpdate`, `SendCancellationNotice`. Commitments come from explicit fields, not post-hoc prose parsing. |
| A4 | One ambiguity score is enough for policy. | Rejected | Policy consumes typed blockers such as `missing_required_arg`, `entity_resolution_conflict`, `schema_competition`, `commitment_conflict`, and `blast_radius_exceeds_limit`. |
| A5 | Model confidence should drive execution. | Rejected | Confidence is a weak signal. The MVP ignores it except as optional debug telemetry. |
| A6 | Obligations can live mainly in model context. | Rejected | Obligations and commitments are external, durable, queryable state. Only intersecting items are projected back into context. |
| A7 | Schema uncertainty must be detected on every action. | Rejected | Expensive uncertainty checks are tiered. Only high-cost, slow-feedback actions justify second-pass or multi-sample interpretation. |
| A8 | The first build should be general-purpose. | Rejected | `v1` is domain-local and closed at the mutation boundary. Generality comes from adding new typed adapters, not from allowing runtime invention of executable actions. |

## Open

| ID | Assumption | Risk if wrong | Decision needed |
| --- | --- | --- | --- |
| O1 | The first deployment domain should be commercial operations. | Action schemas may be wrong for the actual use case. | Confirm whether `SendQuoteEmail`, `DeleteRows`, and `ScheduleMeeting` are the right first slice. |
| O2 | Commitments can always be emitted from action fields alone. | Hidden commitments may still leak through rich text or attachments. | Decide whether some adapters require a constrained template layer instead of arbitrary drafts. |
| O3 | Entity plus action-type joins are enough for obligation retrieval. | Constraint checks may miss conflicts on resources or semantic keys. | Decide whether `resource_keys` and `semantic_keys` belong in the MVP store schema. |
| O4 | Clarification should be the default response to ambiguity. | The system may over-escalate instead of taking safe intersection actions or preflight probes. | Decide when policy should choose `check` over `clarify`. |

## Non-goals For MVP

- No attempt to parse arbitrary commitments out of freeform prose.
- No open-ended execution for actions outside the registry.
- No general uncertainty ontology beyond what the policy needs to route execution.
- No obligation ranking by model-scored relevance.
