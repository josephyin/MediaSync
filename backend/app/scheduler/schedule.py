import re
from dataclasses import dataclass
from datetime import timedelta

INTERVAL_PATTERN = re.compile(r"^interval:(\d+)([mh])$")


@dataclass(frozen=True, slots=True)
class IntervalSchedule:
    delta: timedelta


def parse_schedule(value: str) -> IntervalSchedule:
    match = INTERVAL_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("Schedule must use interval:<number>m or interval:<number>h")
    amount = int(match.group(1))
    if amount < 1:
        raise ValueError("Schedule interval must be positive")
    unit = match.group(2)
    delta = timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount)
    if delta < timedelta(minutes=15):
        raise ValueError("Schedule interval must be at least 15 minutes")
    return IntervalSchedule(delta=delta)
