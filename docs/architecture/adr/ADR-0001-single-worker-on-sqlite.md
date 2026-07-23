# ADR-0001: Single Worker on SQLite

- Status: Accepted
- Date: 2026-07-23
- Owners: MediaSync maintainers
- Related: [Worker and Task Engine v2 Architecture](../worker-task-engine-v2.md)

## Context

MediaSync is primarily deployed by home users on a NAS. Its near-term workload
is tens to hundreds of synchronization operations per day, where reliability,
simple recovery, and low operational overhead matter more than horizontal
throughput.

Supporting concurrent workers on SQLite would add lock contention, deployment
ambiguity, and recovery cases that v0.2 does not need. Adding Redis, a message
queue, or PostgreSQL to the default installation would increase the burden on
the same NAS users MediaSync is intended to serve.

## Decision

The v0.2 default and supported deployment profile is:

```text
SQLite WAL
+ one mediasync-worker process
+ task concurrency 1
```

API and Scheduler processes may use the same SQLite database, but exactly one
Worker process may consume tasks. Operators must not scale the Worker service
horizontally against SQLite.

The Task Engine still uses atomic claims, leases, heartbeats, and lock-token
fencing. The single-Worker limit simplifies the supported topology; it does
not permit unsafe ownership rules.

## Invariants

- A SQLite database has at most one supported Worker process.
- Worker task concurrency is `1`.
- Provider network calls do not hold SQLite write transactions.
- Claim and state-transition transactions remain short.
- Docker Compose declares one Worker replica.
- Multiple-worker behavior is not presented as supported or tested on SQLite.

## Consequences

Benefits:

- the default deployment remains small and NAS-friendly;
- queue durability requires no external service;
- operational diagnosis, backup, and restore remain understandable;
- v0.2 engineering effort stays focused on correctness rather than throughput.

Limitations:

- long-running tasks are processed serially;
- one slow Provider operation can delay later tasks;
- SQLite cannot be used as the supported multi-Worker profile;
- horizontal task throughput requires a future deployment profile.

## Alternatives considered

### Redis, RabbitMQ, or Celery by default

Rejected because they add services, persistence semantics, configuration, and
failure modes without evidence that the NAS workload needs them.

### Multiple Workers on SQLite

Rejected for v0.2 because safe claiming alone does not remove SQLite writer
contention or provide a clear supported operational model.

### PostgreSQL as the default database

Rejected because it raises the minimum deployment and maintenance cost for
users who do not need multi-Worker throughput.

## Future review

A PostgreSQL multi-Worker profile may supersede or complement this decision
only after:

- real workloads demonstrate a throughput requirement;
- claim, lease, recovery, and fencing behavior is tested concurrently;
- migration, backup, and deployment operations are documented;
- a Design PR and new ADR define the supported profile.

SQLite remains the NAS-first default unless a later accepted ADR explicitly
changes it.
