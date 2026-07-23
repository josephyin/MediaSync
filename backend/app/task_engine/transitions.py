from collections.abc import Mapping, Set

from app.models.task import TASK_STATUSES

ALLOWED_TASK_TRANSITIONS: Mapping[str, Set[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset(
        {
            "success",
            "failed",
            "retry",
            "waiting_credential",
            "cancel_requested",
        }
    ),
    "retry": frozenset({"running", "cancelled"}),
    "waiting_credential": frozenset({"pending", "cancelled"}),
    "cancel_requested": frozenset({"cancelled", "success"}),
    "success": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
TERMINAL_TASK_STATUSES = frozenset({"success", "failed", "cancelled"})

if set(ALLOWED_TASK_TRANSITIONS) != set(TASK_STATUSES):
    raise RuntimeError("task transition policy must define every persisted task status")


class InvalidTaskTransitionError(ValueError):
    def __init__(self, source_status: str, target_status: str) -> None:
        self.source_status = source_status
        self.target_status = target_status
        super().__init__(f"task transition {source_status!r} -> {target_status!r} is not allowed")


def can_transition(source_status: str, target_status: str) -> bool:
    return target_status in ALLOWED_TASK_TRANSITIONS.get(source_status, ())


def validate_transition(source_status: str, target_status: str) -> None:
    if not can_transition(source_status, target_status):
        raise InvalidTaskTransitionError(source_status, target_status)
