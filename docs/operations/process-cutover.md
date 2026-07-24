# Process Cutover Operations

This runbook applies when upgrading the official Docker Compose deployment from
the legacy single-process executor to the v0.2 process topology.

The supported topology is:

```text
mediasync-migrate
        |
        v
mediasync-cutover
        |
        +----------+-------------------+
        |          |                   |
        v          v                   v
mediasync-api  mediasync-scheduler  mediasync-worker
        |
        v
    frontend
```

The SQLite profile supports exactly one Scheduler and one Worker. Do not use
`docker compose up --scale` for either service.

## Upgrade

The cutover requires a maintenance window. Do not perform a rolling restart
between the old and new topologies.

1. Stop the existing stack and verify that no manually started Scheduler or
   Worker remains:

   ```bash
   docker compose stop
   docker compose ps -a
   ```

2. Pull the new source and build the backend image without starting services:

   ```bash
   git pull
   docker compose build mediasync-migrate
   ```

3. Create a timestamped database backup inside the persistent volume:

   ```bash
   docker compose run --rm --no-deps --entrypoint sh mediasync-migrate -c \
     'set -eu; stamp=$(date +%Y%m%d-%H%M%S); mkdir -p /data/backups/$stamp; cp -a /data/mediasync.db* /data/backups/$stamp/'
   ```

   Record the application commit and current Alembic revision with the backup.
   Copy the backup off the NAS volume before continuing when possible.

4. Start the new topology:

   ```bash
   docker compose up -d --build
   ```

   Compose runs `alembic upgrade head`, then `python -m app.reconcile`. API,
   Scheduler, and Worker start only after both one-shot services exit with code
   `0`.

5. Verify the barriers, services, and process-mode logs:

   ```bash
   docker compose ps -a
   docker compose logs mediasync-migrate mediasync-cutover
   docker compose logs mediasync-api mediasync-scheduler mediasync-worker
   ```

   Expected results:

   - `mediasync-migrate` and `mediasync-cutover` exited with code `0`;
   - API, Scheduler, Worker, and frontend are running;
   - API logs report `process=api mode=process`;
   - Scheduler logs report `scheduler_started`;
   - Worker logs report `worker_started`;
   - no in-process APScheduler or legacy transfer-poller startup appears.

6. Trigger one manual scan and confirm that the API returns a durable task,
   then confirm the Worker executes it. Confirm that one due subscription is
   enqueued once by the Scheduler.

Retain the backup throughout the observation window.

## Failure before normal processes start

If migration or reconciliation fails, Compose keeps API, Scheduler, Worker,
and frontend stopped.

```bash
docker compose logs mediasync-migrate mediasync-cutover
docker compose down
```

Correct the configuration or restore the backup before retrying. Do not bypass
the failed one-shot service with a manual process start.

## Rollback

Before the v2 Worker executes any task, stop the stack and restore the
pre-cutover backup, then run the previous compatibility-capable release in
`legacy` mode.

After the v2 Worker executes a task, do not run a v0.1 binary directly against
the v2 database. Either:

1. run a compatibility-capable release in `legacy` mode against a schema that
   release explicitly supports; or
2. stop all services and restore the pre-cutover database backup.

Restoring SQLite does not undo remote cloud-drive operations. Reconcile files
saved after the backup before retrying transfers.
