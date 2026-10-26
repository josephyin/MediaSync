from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.v1.system import dashboard_summary
from app.models import Base, CloudAccount, CloudFile, Subscription, Task

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def test_historical_failures_do_not_alert_after_current_work_recovers():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = CloudAccount(provider="pikpak", name="test", refresh_token="encrypted")
        db.add(account)
        db.flush()
        subscription = Subscription(
            cloud_account_id=account.id,
            provider="pikpak",
            name="test",
            share_url="https://mypikpak.com/s/test",
            target_path="/",
        )
        db.add(subscription)
        db.flush()
        file = CloudFile(
            subscription_id=subscription.id,
            remote_file_id="source",
            filename="movie",
            fingerprint="test",
            status="saved",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        db.add(file)
        db.flush()
        for task_type in ("scan", "transfer", "download"):
            for task_status in ("failed", "success"):
                db.add(
                    Task(
                        type=task_type,
                        status=task_status,
                        subscription_id=subscription.id,
                        file_id=file.id if task_type != "scan" else None,
                        payload={"root": "/downloads", "relative_path": "movie"}
                        if task_type == "download"
                        else {},
                    )
                )
                db.flush()
        result = dashboard_summary(db, "admin")
        assert result["failed_tasks"] == 3
        assert result["current_issues"] == 0

        # Another destination's failure is still actionable despite the success above.
        db.add(
            Task(
                type="download",
                status="failed",
                subscription_id=subscription.id,
                file_id=file.id,
                payload={"root": "/other", "relative_path": "movie"},
            )
        )
        db.flush()
        result = dashboard_summary(db, "admin")
        assert result["current_issues"] == result["download_issues"] == 1
        file.status = "failed"
        db.flush()
        result = dashboard_summary(db, "admin")
        assert result["failed_files"] == 1 and result["current_issues"] == 2
    engine.dispose()


def test_disabled_subscription_scan_does_not_alert_but_credential_wait_does():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = CloudAccount(provider="pikpak", name="test", refresh_token="encrypted")
        db.add(account)
        db.flush()
        subscription = Subscription(
            cloud_account_id=account.id,
            provider="pikpak",
            name="test",
            share_url="https://mypikpak.com/s/test",
            target_path="/",
            enabled=False,
        )
        db.add(subscription)
        db.flush()
        db.add(Task(type="scan", status="failed", subscription_id=subscription.id))
        db.flush()
        assert dashboard_summary(db, "admin")["current_issues"] == 0
        subscription.enabled = True
        db.add(Task(type="scan", status="waiting_credential", subscription_id=subscription.id))
        db.flush()
        result = dashboard_summary(db, "admin")
        assert result["scan_issues"] == 0
        assert result["waiting_credentials"] == result["current_issues"] == 1
    engine.dispose()
