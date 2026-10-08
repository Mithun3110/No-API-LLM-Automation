"""One line of runs/<run_id>/log.jsonl. Values are masked before an entry is created."""

from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from .common import ControlState, LogMode, StrictModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LogEntry(StrictModel):
    timestamp: datetime = Field(default_factory=_now)
    run_id: str
    mode: LogMode
    step: int | None = None
    action: str | None = None      # e.g. click, type, control_change, approval
    target: str | None = None      # readable description, e.g. 'button "Search"'
    reason: str | None = None      # why: the LLM's reason, the step description, or the human's note
    outcome: str                   # e.g. ok, failed, blocked_by_policy, drift_warning
    controller: ControlState
    warnings: list[str] = []
    data: dict[str, Any] = {}      # extra structured detail (already masked)
