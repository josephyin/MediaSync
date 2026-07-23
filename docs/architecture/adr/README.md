# Architecture Decision Records

Architecture Decision Records (ADRs) explain why MediaSync chose a durable
architectural direction and what consequences follow from it.

An ADR records a long-lived decision. A Design Spec describes the detailed
contract and implementation boundaries for a concrete change. ADRs should link
to Design Specs instead of duplicating their protocols.

## When an ADR is required

Create or supersede an ADR when a change:

- introduces or changes a core architectural invariant;
- changes a supported deployment profile;
- changes ownership, persistence, or failure-recovery semantics;
- adopts or removes a foundational dependency;
- reverses a decision that contributors might otherwise reintroduce.

Routine fixes that preserve accepted decisions do not need a new ADR.

## Naming and status

Files use a sequential number and a durable decision name:

```text
ADR-0001-single-worker-on-sqlite.md
ADR-0002-task-execution-model.md
```

Supported statuses:

- `Proposed`
- `Accepted`
- `Superseded by ADR-NNNN`
- `Deprecated`
- `Rejected`

Numbers are never reused. Accepted ADRs are historical records and MUST NOT be
rewritten to reflect a new decision. Correct small factual or formatting
errors in place; use a new ADR to supersede a decision. Status and
`Superseded by` metadata may be updated to link the historical record to its
replacement, but the original context and decision remain unchanged.

## Process

1. Define the relevant invariants in a Design PR.
2. Add the next sequential ADR when the decision has long-term architectural
   consequences.
3. Describe context, decision, consequences, alternatives, and future
   conditions.
4. Review architecture separately from Runtime code.
5. Merge the Design PR before opening the implementation Issue or Runtime PR.
6. Link subsequent Issues and Runtime PRs to the accepted ADR and Design Spec.

An ADR marked `Accepted` becomes binding when its Design PR is merged.

## ADR template

```markdown
# ADR-NNNN: Decision title

- Status: Proposed
- Date: YYYY-MM-DD
- Decision Makers: MediaSync maintainers
- Supersedes: None
- Superseded by: None
- Related: links to Design Specs, Issues, or PRs

## Context

What forces are acting on the decision?

## Decision

What is the durable choice?

## Invariants

What must remain true?

## Consequences

What becomes easier, harder, supported, or unsupported?

## Alternatives considered

Which realistic alternatives were rejected, and why?

## Future review

What evidence would justify superseding this decision?
```

## Index

| ADR | Decision | Status |
|---|---|---|
| [ADR-0001](ADR-0001-single-worker-on-sqlite.md) | Single Worker on SQLite | Accepted |
| [ADR-0002](ADR-0002-task-execution-model.md) | Task Execution Model | Accepted |
