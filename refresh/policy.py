"""Pure decision logic: how to read a refresh status and how long to wait between polls."""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


# Power BI enhanced-refresh statuses. "Unknown" and "NotStarted" mean still in progress.
_SUCCESS = {"completed"}
_FAILURE = {"failed", "cancelled", "disabled"}


def classify(status: str | None) -> Outcome:
    s = (status or "").strip().lower()
    if s in _SUCCESS:
        return Outcome.SUCCEEDED
    if s in _FAILURE:
        return Outcome.FAILED
    return Outcome.RUNNING


def next_poll_delay(attempt: int, base_seconds: int = 30, cap_seconds: int = 300) -> int:
    """Exponential backoff: 30s, 60s, 120s, 240s, then every 5 minutes."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    return min(cap_seconds, base_seconds * 2**attempt)
