def start_scheduler() -> None:
    from app.scheduler.manager import start_scheduler as start

    start()


def stop_scheduler() -> None:
    from app.scheduler.manager import stop_scheduler as stop

    stop()

__all__ = ["start_scheduler", "stop_scheduler"]
