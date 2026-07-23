# Task Execution Data Model v2 Migration

This migration upgrades a v0.1 SQLite database from
`0005_folder_checkpoints` to `0006_task_execution_data_model_v2`.

## Before upgrading

1. Stop MediaSync so no scheduler or transfer work is writing to SQLite.
2. Copy the SQLite database and its `-wal` and `-shm` files as one consistent
   backup, or use the SQLite backup command.
3. Record the current application image and Alembic revision.
4. Run:

```text
alembic upgrade head
```

## Preserved data

The migration preserves existing accounts, subscriptions, files, and tasks.
It keeps the v0.1 Task compatibility columns so the current single-process
runtime continues to operate.

Existing Tasks receive:

- `payload_version = 1`;
- an empty JSON payload;
- deterministic retry and completion compatibility values;
- an account reference derived from the related subscription when available.

A Task with evidence of an existing execution receives one synthetic Task Run
that records the latest v0.1 outcome. v0.1 did not retain every individual
attempt, so the migration cannot reconstruct attempts that were previously
overwritten.

SQLite data guards make Task payload/version, assigned idempotency keys, and
Task Run identity immutable. Terminal Task Runs cannot be updated, and Task
Run history cannot be deleted through normal database operations.

## Rollback and restore

`alembic downgrade 0005_folder_checkpoints` removes `task_runs` and all Task
Engine v2 fields. Any Task Run history created after the upgrade is lost by
that downgrade.

For a production rollback, restoring the pre-migration SQLite backup together
with the previous application image is the supported recovery path. Do not run
an older application image against a database that remains at revision
`0006_task_execution_data_model_v2`.

## Scope

This migration stores the fields required by later Task Repository work. It
does not implement claiming, heartbeat, lease expiry, recovery, Worker
processes, or Scheduler changes.
