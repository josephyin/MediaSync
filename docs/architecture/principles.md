# Architecture Principles

These principles govern changes to MediaSync's core architecture. They apply
to maintainers and contributors, and take precedence over implementation
convenience.

Normative terms such as **MUST**, **MUST NOT**, **SHOULD**, and **MAY** describe
requirements that contributions are expected to follow.

## 1. Invariant First

Core changes MUST define their invariants before implementation:

- valid states and transitions;
- ownership and uniqueness constraints;
- lifecycle and terminal behavior;
- failure, recovery, and reconciliation rules;
- compatibility and migration boundaries.

Code is an implementation of those invariants, not their source of truth.

Invariants belong to distinct verification layers:

1. **Data invariants** are enforced by schema, constraints, migrations, and
   model tests.
2. **Engine behavior invariants** are enforced by repositories, state
   transitions, leases, fencing, and integration tests.
3. **Production runtime invariants** are verified through restart, network,
   credential, partial-success, and fault-injection tests.

A PR MUST NOT pull behavior from a later layer into its scope merely to make a
feature appear complete.

### Task Run lifecycle

A Task Run is mutable only while its execution is active. During that period,
its status and operational result fields may advance from `RUNNING` to a
terminal outcome.

After a Task Run reaches a terminal state:

- its run number MUST NOT be reused;
- the record MUST NOT be deleted as part of normal operation;
- its core execution outcome MUST NOT be changed;
- later attempts MUST create a new Task Run.

## 2. Design Before Runtime

A change that introduces or modifies core invariants in the Task Engine,
Provider, Credential, Scheduler, or Storage modules MUST have an approved
Design PR before its Runtime PR.

The expected sequence is:

```text
Design PR → Review → Architecture Merge → Issue → Runtime PR
```

Small fixes that preserve existing invariants do not require a new Design PR,
but the Runtime PR MUST identify the invariant it preserves.

## 3. Database Is a Contract

A migration is not only a table edit. It changes the persistent contract
between releases.

Every database change MUST consider:

- existing and partially populated data;
- deterministic backfill behavior;
- application compatibility during upgrade;
- backup, rollback, or restore limitations;
- indexes, uniqueness, and referential integrity;
- preservation of execution and audit history.

Alembic is the production migration authority. Runtime startup code MUST NOT
silently reinterpret or rewrite an existing schema.

## 4. Runtime Failure Is Normal

Designs MUST assume that the following events will occur:

- a NAS or container restarts during work;
- a Provider request times out or is rate-limited;
- a credential expires or is revoked;
- a remote operation succeeds before the local commit;
- a worker pauses, crashes, or resumes after losing ownership.

Success-path behavior is incomplete without bounded retry, recovery,
reconciliation, ownership fencing, and observable history.

## 5. NAS First

MediaSync prioritizes a reliable, understandable self-hosted deployment:

```text
SQLite
+ one worker
+ controlled concurrency
```

The project MUST NOT add distributed infrastructure for hypothetical scale at
the expense of the default NAS experience. PostgreSQL, multiple workers, or an
external queue require demonstrated need, a Design PR, and an explicit
deployment profile.
