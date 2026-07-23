# ADR-0002: Task Execution Model

- Status: Accepted
- Date: 2026-07-23
- Owners: MediaSync maintainers
- Related: [Worker and Task Engine v2 Architecture](../worker-task-engine-v2.md)

## Context

MediaSync v0.1 combines HTTP handling, scheduling, scan execution, and transfer
execution inside the FastAPI process. That topology validated the MVP, but it
couples durable synchronization work to an API lifecycle and overloads one
task record with queue state, execution history, and logs.

Cloud-drive work also crosses a non-transactional boundary: a remote operation
may succeed before MediaSync commits its local result. Process restarts,
network failures, expired credentials, and stale workers are normal runtime
conditions, not exceptional edge cases.

## Decision

MediaSync uses a durable Task Engine with two related concepts:

- A **Task** represents one durable business intent and owns queue state,
  retry policy, payload version, idempotency, lease, and current ownership.
- A **Task Run** represents one execution attempt and preserves its worker,
  fencing token, timing, outcome, error, and metrics.

The execution boundary is:

```text
API       → enqueue and query
Scheduler → enqueue due scan tasks
Worker    → claim, execute, heartbeat, reconcile, and finalize
```

Only the Worker executes background synchronization. The Scheduler never scans
or transfers files.

Each claim creates a fresh `lock_token` and Task Run. State changes from an
active owner must match the current token. A lease permits recovery after a
Worker disappears, while fencing prevents a stale Worker from committing a
terminal result after ownership changes.

Provider-side success and SQLite commit are not one transaction. Transfer
execution therefore uses durable idempotency plus remote reconciliation rather
than claiming exactly-once delivery.

## Invariants

### Data invariants

- Every Task payload has an explicit `payload_version`.
- Task idempotency keys are unique for their business operation.
- `(task_id, run_number)` is unique, and a run number is never reused.
- Task Run records are not deleted during normal operation.
- A terminal Task Run's core execution outcome is immutable.

### Engine behavior invariants

- A Task has at most one active lease owner.
- Every claim generates a new, unpredictable `lock_token`.
- Heartbeat and finalization match Task ID, owner, state, and `lock_token`.
- A stale Worker cannot finalize a Task after losing ownership.
- A retry creates a new Task Run and preserves prior attempts.
- A running cancellation becomes `CANCEL_REQUESTED`; the owner reconciles
  remote state before choosing `SUCCESS` or `CANCELLED`.
- A non-refreshable credential moves work to `WAITING_CREDENTIAL`, not
  `FAILED`, and does not consume retry budget.

### Task Run lifecycle

An active Task Run may update operational fields while moving from `RUNNING`
to one terminal outcome such as `SUCCESS`, `FAILED`, `CANCELLED`, `BLOCKED`, or
`LOST`.

After terminalization:

- its run number is never reused;
- its row is not deleted as part of normal processing;
- its core outcome, timestamps, result, and normalized error are not changed;
- a later attempt appends a new Task Run.

### Runtime invariants

- Restart recovery preserves the interrupted Run and creates a new attempt.
- Retry uses bounded backoff and never becomes an executor-local sleep loop.
- A remote success followed by a local failure is reconciled before another
  copy is attempted.
- Credentials and raw Provider responses never enter task history or logs.

## Consequences

Benefits:

- execution survives API and frontend restarts;
- every attempt is traceable without overloading the Task row;
- ownership and stale writes have explicit database conditions;
- retry, cancellation, credential blocking, and recovery have durable states;
- future Providers share the same reliability contract.

Costs:

- the Task and Task Run schemas are more explicit than the v0.1 model;
- handlers must return normalized outcomes instead of assigning state directly;
- reconciliation requires Provider-specific capabilities;
- migrations and state-machine tests become release-critical.

## Alternatives considered

### Keep FastAPI background execution

Rejected because API lifecycle and deployment changes would continue to
interrupt durable work.

### Store queue and complete history in one Task row

Rejected because retries overwrite evidence and mix business intent with
individual attempts.

### Rely only on `locked_by` and lease expiry

Rejected because a paused Worker can resume after lease expiry and overwrite a
new owner's result. A per-claim fencing token is required.

### Claim exactly-once transfer semantics

Rejected because SQLite and a cloud Provider cannot share one atomic
transaction. MediaSync instead provides at-least-once execution with
effectively-once side effects through idempotency and reconciliation.

## Future review

This decision may be superseded only by a Design PR that preserves or replaces
its reliability guarantees with testable equivalents. A different queue,
database, workflow engine, or multi-Worker profile does not by itself justify
weakening Task/Run history, lease ownership, fencing, or reconciliation.
