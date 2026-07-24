# Process Cutover v2

- Status: Proposed
- Target milestone: v0.2 Foundation
- Scope: Compatibility mode, legacy-task reconciliation, process activation,
  Docker Compose ordering, upgrade, and rollback
- Extends: [Worker and Task Engine v2 Architecture](worker-task-engine-v2.md)
- Based on:
  [ADR-0001](adr/ADR-0001-single-worker-on-sqlite.md) and
  [ADR-0002](adr/ADR-0002-task-execution-model.md)
- Last updated: 2026-07-24

## Decision summary

MediaSync will use a staged compatibility mode before changing the official
Compose topology. Runtime code first gains mutually exclusive `legacy` and
`process` behavior while `legacy` remains the application default. A later
Compose PR explicitly selects `process` and starts one-shot migration and
reconciliation barriers before one API, one Scheduler, and one Worker.

Legacy and v2 executors MUST never consume the same SQLite queue concurrently.
Persisted legacy execution states are reconciled before the v2 Worker starts,
and rollback after v2 execution is limited to a compatibility-capable release
or a pre-cutover database restore.

This document refines the implementation order in the parent architecture:
the process cutover and its reliability checks complete before Provider SDK v2
begins. It does not change the long-term Provider decomposition decision.

## 1. Background

MediaSync has implemented the Task Engine v2 data model, repository, lease
recovery, Worker runtime, Scan and Transfer handlers, scheduled-scan enqueue
operation, and Scheduler runtime as separately testable foundations.

The default deployment still uses the v0.1 execution topology:

```text
FastAPI
├── API
├── APScheduler subscription jobs
├── FastAPI BackgroundTasks scans
└── APScheduler transfer polling
```

The new commands are present but are not started by Docker Compose:

```text
python -m app.scheduler
python -m app.worker
```

The next change is not merely a Compose edit. A direct switch could run the
legacy executors and Task Engine v2 Worker at the same time, strand legacy
`RUNNING` tasks without leases, advance subscription schedules twice, or leave
manual API requests queued with no active consumer.

This document defines the cutover contract before runtime behavior changes.
Normative terms such as **MUST**, **MUST NOT**, **SHOULD**, and **MAY** describe
implementation requirements.

## 2. Goals and non-goals

### 2.1 Goals

The cutover MUST:

- move background execution out of the API process;
- start exactly one Scheduler and one Worker for the SQLite profile;
- preserve queued work and execution history;
- prevent legacy and v2 executors from overlapping;
- reconcile persisted legacy execution states before the Worker starts;
- keep task creation and schedule advancement durable;
- provide a deterministic upgrade and rollback sequence;
- keep every implementation PR buildable, testable, and releasable.

### 2.2 Non-goals

The cutover does not:

- add a Provider;
- introduce Redis, a message broker, or Kubernetes;
- support multiple Workers or Schedulers on SQLite;
- implement Provider SDK v2;
- implement persistent process heartbeats or the final health API;
- remove all compatibility code immediately;
- redesign the web UI.

## 3. Runtime compatibility mode

The compatibility release MUST introduce one shared setting:

```text
BACKGROUND_EXECUTION_MODE=legacy | process
```

The application default remains `legacy` during the compatibility window.
The official cutover Compose profile explicitly sets `process`.

All backend containers in one deployment MUST receive the same value. Mixing
values against the same SQLite database is unsupported and MUST be called out
as an operator error.

### 3.1 Legacy mode

`legacy` preserves the current deployment while process-mode behavior is being
implemented and verified.

In legacy mode:

- the API starts the in-process APScheduler;
- subscription jobs may execute scans directly;
- manual scans may use the existing compatibility execution path;
- the legacy transfer poller remains active;
- the standalone Scheduler and Worker commands MUST refuse to start.

Refusing standalone commands in legacy mode is a safety fence. It prevents an
operator from accidentally adding a v2 Worker beside the legacy transfer
poller.

### 3.2 Process mode

In process mode:

- the API MUST NOT start APScheduler;
- the API MUST NOT use FastAPI `BackgroundTasks` for synchronization;
- subscription CRUD MUST NOT add, replace, or remove APScheduler jobs;
- manual scans and file retries only enqueue tasks;
- the Scheduler only creates due Scan tasks;
- the Worker is the only scan and transfer executor;
- only the Scheduler advances the periodic `next_scan_at`;
- exactly one Scheduler and one Worker are supported.

### 3.3 No-overlap invariant

The following topology is forbidden:

```text
legacy APScheduler transfer poller
              +
Task Engine v2 Worker
              =
two executors for the same queue
```

The official deployment MUST use one shared mode setting and mutually
exclusive service commands. Upgrade instructions MUST stop the legacy stack
before starting any process-mode service.

The mode setting is a deployment fence, not distributed leader election. It
does not make separately configured containers safe. Supporting independent
orchestrators would require a database-backed runtime-generation or leader
lease and is outside v0.2.

## 4. Process-mode API contract

### 4.1 Manual scan

`POST /subscriptions/{id}/scan` MUST:

1. validate the subscription and cooldown policy;
2. find an existing non-terminal Scan task for the subscription;
3. return that task when one exists;
4. otherwise create a `PENDING` Scan v1 task with:

```json
{
  "force_full": false
}
```

5. set `force_full` from the request;
6. commit before returning HTTP `202`;
7. perform no Provider or scan work in the API process.

The active set is every non-terminal task state, including `RETRY`,
`WAITING_CREDENTIAL`, and `CANCEL_REQUESTED`; it is not limited to
`PENDING` and `RUNNING`.

A completed manual scan does not suppress a later intentional manual scan.
Repeated requests while one scan is active return the same active task.

### 4.2 File retry

`POST /files/{id}/retry` MUST NOT mutate a terminal task back to `PENDING`.

It MUST:

1. return an existing non-terminal Transfer task for the file when present;
2. otherwise create a new successor Transfer v1 task;
3. preserve the terminal predecessor and all of its Task Runs;
4. use a successor idempotency key derived from the file and predecessor task,
   so repeated HTTP requests return or collide with the same successor;
5. clear the file's display error only after the successor is durably created.

An example successor key is:

```text
transfer-retry:{file_id}:{predecessor_task_id}
```

If the successor also reaches a terminal state, a later retry uses that
successor as the next predecessor and therefore receives a new key.

### 4.3 Task mutation

API routers MUST use Task Engine enqueue or command services. They MUST NOT
assign task status strings directly.

Cancellation and credential wake-up remain separate Task Engine commands and
are not added implicitly by the process cutover.

## 5. Schedule ownership

Schedule ownership differs by compatibility mode:

| Mode | Owner of periodic `next_scan_at` |
|---|---|
| `legacy` | The compatibility scan path and APScheduler job management |
| `process` | Scheduler enqueue transaction only |

In process mode, Scan execution MUST NOT move `next_scan_at`. This applies to
scheduled and manual scans:

- a scheduled scan was already accounted for when its task was enqueued;
- a manual scan must not postpone the next periodic scan;
- a failed scan must not create a rapid Scheduler loop.

The Scheduler coalesces downtime by setting the next occurrence from the
current scheduling time, as defined by the scheduled-scan enqueue contract.

The compatibility branch in scan domain code MUST be isolated and covered by
tests. It is temporary and removed after the observation window.

## 6. Legacy task reconciliation

The database may contain tasks created or started by the compatibility
runtime. Reconciliation runs after Alembic migration and before API,
Scheduler, or Worker startup.

The reconciliation command MUST be:

- transactional;
- idempotent;
- safe to rerun after interruption;
- free of Provider calls;
- explicit about every persisted task state;
- recorded through normalized error codes and Task Run history where
  applicable.

### 6.1 State disposition

| Persisted state | Cutover disposition |
|---|---|
| `PENDING` | Preserve; Worker may claim it. |
| `RETRY` | Preserve `next_attempt_at`; Worker claims it when due. |
| `WAITING_CREDENTIAL` | Preserve; no Provider polling occurs. |
| `CANCEL_REQUESTED` without ownership | Preserve; Worker claims and reconciles it. |
| `CANCEL_REQUESTED` with complete v2 ownership and lease | Preserve; normal lease recovery owns it after restart. |
| `SUCCESS` | Preserve as terminal history. |
| `FAILED` | Preserve as terminal history. |
| `CANCELLED` | Preserve as terminal history. |
| `RUNNING` with complete v2 ownership and lease | Preserve; normal lease recovery owns it after restart. |
| `RUNNING` without complete v2 ownership | Reconcile as a legacy orphan. |

### 6.2 Legacy orphan

A `RUNNING` task with no complete `locked_by`, `lock_token`, `locked_at`, and
`lease_until` tuple was not claimed under Task Engine v2.

For each legacy orphan, reconciliation MUST:

1. synchronize the v2 retry counter with the compatibility attempt counter
   without decreasing either history value;
2. append or finalize one synthetic/lost Task Run when no equivalent terminal
   attempt record exists;
3. use error code `LEGACY_CUTOVER_RECOVERY`;
4. clear incomplete ownership fields;
5. move the task to `RETRY` with a bounded near-term `next_attempt_at` when
   retry budget remains;
6. otherwise move it to terminal `FAILED`;
7. preserve subscription, file, messages, timestamps, and prior runs.

Reconciliation MUST NOT claim that the remote operation failed. A legacy
Transfer may have succeeded remotely before the process stopped.

### 6.3 Remote side-effect boundary

Before process cutover is enabled, the Transfer v1 handler MUST have passing
tests for:

- a previously saved file record;
- a target item that already exists after an unknown prior outcome;
- remote success followed by local commit failure;
- a retry that does not create a second remote copy.

If reliable reconciliation is not possible with the current Provider
contract, the cutover MUST stop and Provider reconciliation must be designed
before enabling the Worker by default.

Scan retries may resume from durable file fingerprints and folder checkpoints,
but their task and run history must still record the lost legacy attempt.

## 7. Migration and service barrier

The current backend image runs `alembic upgrade head` inside the API command,
and the API lifespan calls `Base.metadata.create_all()`. This is not safe for
three independently starting processes.

The process-mode Compose topology MUST introduce one-shot barriers:

```text
mediasync-migrate
    alembic upgrade head
            |
            v
mediasync-cutover
    reconcile legacy tasks
            |
            +------------------+------------------+
            |                  |                  |
            v                  v                  v
     mediasync-api      mediasync-scheduler  mediasync-worker
```

API, Scheduler, and Worker MUST start only after migration and reconciliation
complete successfully. A failed one-shot service blocks normal processing.

The exact Scheduler/Worker relative start order is not a correctness
requirement because queued tasks are durable. Starting the Scheduler before
the Worker is acceptable. Both MUST wait for the same migration barrier.

In process mode:

- the API command runs only Uvicorn;
- the Scheduler command is `python -m app.scheduler`;
- the Worker command is `python -m app.worker`;
- all services mount the same SQLite volume;
- all services use `restart: unless-stopped`, except successful one-shot
  services;
- `Base.metadata.create_all()` MUST be removed from production startup;
- Alembic remains the only schema migration authority.

The SQLite database MUST remain on a local filesystem with reliable locking.
The Worker replica count is one and its concurrency is one.

## 8. Upgrade sequence

The supported NAS upgrade sequence is:

1. Stop the existing MediaSync stack completely.
2. Verify no old API or manually started Worker/Scheduler process remains.
3. Create a timestamped SQLite backup and record the current application and
   Alembic revisions.
4. Pull or build the compatibility-capable image.
5. Run `mediasync-migrate` and require exit code `0`.
6. Run `mediasync-cutover` and require exit code `0`.
7. Start API, Scheduler, and exactly one Worker with
   `BACKGROUND_EXECUTION_MODE=process`.
8. Verify API/database health and process startup logs.
9. Trigger one manual scan and confirm it is executed by the Worker.
10. Confirm one due subscription is enqueued by the Scheduler.
11. Confirm no legacy APScheduler or transfer-poller startup log exists.
12. Retain the backup through the observation window.

The upgrade MUST NOT use a rolling restart between legacy and process
topologies. A short full-stack maintenance window is required.

## 9. Rollback contract

Rollback has two distinct boundaries.

### 9.1 Before process execution

If migration or reconciliation fails before API, Scheduler, and Worker start:

- stop the new stack;
- correct the configuration or restore the pre-upgrade backup;
- return to the last compatibility release in `legacy` mode.

No new Provider side effect occurred in this boundary.

### 9.2 After process execution

After the v2 Worker executes any task, rollback MUST NOT return directly to a
v0.1 binary that does not understand v2 states, immutable payloads, Task Runs,
or successor retry semantics.

The supported choices are:

1. stop all process-mode services and run the compatibility-capable version in
   `legacy` mode against a schema it explicitly supports; or
2. stop all services and restore the pre-cutover SQLite backup, accepting loss
   of MediaSync history recorded after the backup.

Restoring the database does not undo remote cloud-drive operations. Operators
must reconcile files saved after the backup before triggering retries.

Every release note MUST state the oldest application version compatible with
its Alembic revision. A database downgrade is not assumed safe merely because
an Alembic downgrade function exists.

## 10. Observability during cutover

The compatibility release MUST log:

- selected background execution mode;
- whether in-process APScheduler is started or suppressed;
- Scheduler and Worker startup and shutdown;
- legacy reconciliation counts by disposition;
- Scheduler enqueue counts;
- Worker task ID, run ID, and ownership loss;
- configuration refusal when a process command is started in legacy mode.

Logs MUST NOT contain refresh tokens, cookies, decrypted credentials, share
passwords, or raw Provider responses.

Persistent process heartbeat and the final composite health response remain a
later v0.2 observability issue. Their absence does not permit silent process
startup failure; Docker restart policy and explicit startup logs are required
for the cutover release.

## 11. Validation matrix

Automated tests MUST cover:

| Scenario | Required result |
|---|---|
| Legacy mode API startup | APScheduler starts; standalone process commands refuse. |
| Process mode API startup | No APScheduler or transfer poller starts. |
| Process mode manual scan | HTTP `202`; durable Scan v1 task; no BackgroundTask. |
| Repeated manual scan | Existing non-terminal task is returned. |
| Retry a terminal transfer | New successor task; predecessor and runs unchanged. |
| Legacy orphan Scan | Lost attempt recorded; task becomes retryable or failed. |
| Legacy orphan Transfer | Lost attempt recorded; retry requires reconciliation. |
| Migration/reconciliation failure | API, Scheduler, and Worker remain stopped. |
| API restart during transfer | Worker continues and completes the task. |
| Scheduler restart | Due scan is eventually enqueued once. |
| Worker restart | Lease recovery preserves fencing and run history. |
| Manual scan plus scheduled tick | At most one active Scan task. |
| Scheduled scan completion | `next_scan_at` is not advanced a second time. |
| NAS full restart | Non-terminal tasks recover without duplicate transfer. |
| Misconfigured extra Worker | Deployment validation rejects the topology. |

The release candidate MUST also pass a real Compose smoke test using a copied
database:

```text
migrate -> reconcile -> api + scheduler + worker
```

The smoke test verifies process lists and logs, not only HTTP health.

## 12. Staged implementation

Implementation follows small, reviewable changes:

### PR A — Compatibility mode and safety fences

- add and validate `BACKGROUND_EXECUTION_MODE`;
- log the selected mode;
- make standalone Scheduler/Worker refuse legacy mode;
- keep the default `legacy`;
- add mode matrix tests;
- do not change official Compose behavior.

### PR B — Process-mode API and schedule ownership

- enqueue manual scans in process mode;
- create successor Transfer retries;
- suppress subscription APScheduler job mutation in process mode;
- make Scheduler the only process-mode owner of `next_scan_at`;
- retain tested legacy branches;
- keep official Compose in legacy mode.

### PR C — Legacy reconciliation and startup barrier

- add the idempotent reconciliation command;
- cover every persisted task state;
- remove `Base.metadata.create_all()` from production startup;
- separate migration, reconciliation, and normal process commands;
- keep official Compose in legacy mode.

### PR D — Official Compose cutover

- add one-shot migrate and reconciliation services;
- add API, Scheduler, and one Worker services;
- set the shared mode to `process`;
- add service dependency and replica constraints;
- run Compose and NAS restart acceptance tests.

### PR E — Observation and compatibility removal

- observe at least one release or an explicitly recorded maintainer soak
  period;
- fix reliability findings without adding Providers;
- remove legacy APScheduler, BackgroundTasks, and transfer-poller code only
  after the rollback window closes.

Provider SDK v2 begins only after the process cutover acceptance matrix passes.

## 13. Exit criteria

The process cutover is complete when:

- the official Compose deployment runs one API, one Scheduler, and one Worker;
- the API contains no active synchronization executor;
- no legacy and v2 executor can start under the same supported configuration;
- legacy task reconciliation is repeatable and fully tested;
- manual scans and retries use durable v2 tasks;
- scheduled scans have one schedule owner;
- API restart does not interrupt execution;
- NAS restart recovers non-terminal work;
- unknown transfer outcomes reconcile without duplicate remote copies;
- backup, upgrade, and rollback instructions have been exercised on a copied
  database.
