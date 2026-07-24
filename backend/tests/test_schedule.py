import pytest

from app.scheduler.schedule import parse_schedule


def test_parse_minutes() -> None:
    assert parse_schedule("interval:30m").delta.total_seconds() == 1800


def test_parse_hours() -> None:
    assert parse_schedule("interval:2h").delta.total_seconds() == 7200


@pytest.mark.parametrize(
    "value", ["cron:* * * * *", "interval:0m", "interval:4m", "interval:14m", "30m"]
)
def test_reject_invalid_schedule(value: str) -> None:
    with pytest.raises(ValueError):
        parse_schedule(value)
