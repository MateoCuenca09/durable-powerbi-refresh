"""Orchestrator logic as plain generators.

Kept free of Azure imports so it can be unit-tested by driving the generators with a
fake context. ``function_app.py`` registers them with the Durable Functions runtime.

Rules for orchestrator code (Durable Functions replays it): no I/O, no ``datetime.now()``,
no randomness. Time comes from ``context.current_utc_datetime`` and waiting is done with
durable timers, which cost nothing while they sleep.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .policy import Outcome, classify, next_poll_delay

DEFAULT_TIMEOUT_MINUTES = 120


def validate_payload(payload: Any) -> list[list[dict]]:
    """Accept ``{"datasets": [...]}`` (one stage) or ``{"stages": [[...], [...]]}``."""
    if not isinstance(payload, dict):
        raise ValueError("Body must be a JSON object.")
    if "stages" in payload:
        stages = payload["stages"]
    elif "datasets" in payload:
        stages = [payload["datasets"]]
    else:
        raise ValueError("Provide 'datasets' or 'stages'.")
    if not isinstance(stages, list) or not stages:
        raise ValueError("'stages' must be a non-empty list of lists.")
    for stage in stages:
        if not isinstance(stage, list) or not stage:
            raise ValueError("Each stage must be a non-empty list of datasets.")
        for ds in stage:
            if not isinstance(ds, dict) or not all(
                isinstance(ds.get(k), str) and ds.get(k) for k in ("workspace_id", "dataset_id")
            ):
                raise ValueError("Each dataset needs string 'workspace_id' and 'dataset_id'.")
    return stages


def refresh_all(context):
    """Run stages in order; datasets inside a stage refresh in parallel (fan-out/fan-in).

    A failed stage stops the run: models downstream of a broken one are not refreshed.
    """
    stages = validate_payload(context.get_input())
    results: list[dict] = []
    skipped: list[dict] = []

    for i, stage in enumerate(stages):
        tasks = [context.call_sub_orchestrator("refresh_dataset", ds) for ds in stage]
        stage_results = yield context.task_all(tasks)
        results.extend(stage_results)
        if any(r["status"] != Outcome.SUCCEEDED for r in stage_results):
            skipped = [ds for later in stages[i + 1 :] for ds in later]
            break

    summary = summarize(results, skipped)
    yield context.call_activity("send_summary", summary)
    return summary


def refresh_dataset(context):
    """Trigger one refresh and follow *that* refresh id until it finishes or times out."""
    ds = context.get_input()
    name = ds.get("name", ds["dataset_id"])
    started = context.current_utc_datetime
    deadline = started + timedelta(minutes=ds.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES))

    def result(status: str, refresh_id: str | None, polls: int, error: str | None = None):
        return {
            "name": name,
            "dataset_id": ds["dataset_id"],
            "refresh_id": refresh_id,
            "status": status,
            "error": error,
            "polls": polls,
            "duration_seconds": int((context.current_utc_datetime - started).total_seconds()),
        }

    try:
        refresh_id = yield context.call_activity("trigger_refresh", ds)
    except Exception as e:  # activity failures surface here as exceptions
        return result(Outcome.FAILED, None, 0, f"Trigger failed: {e}")

    polls = 0
    while True:
        fire_at = context.current_utc_datetime + timedelta(seconds=next_poll_delay(polls))
        if fire_at > deadline:
            return result("TimedOut", refresh_id, polls, "Refresh did not finish in time.")
        yield context.create_timer(fire_at)

        try:
            status = yield context.call_activity(
                "get_refresh_status", {**ds, "refresh_id": refresh_id}
            )
        except Exception as e:
            return result(Outcome.FAILED, refresh_id, polls + 1, f"Status check failed: {e}")
        polls += 1

        outcome = classify(status.get("status"))
        if outcome is not Outcome.RUNNING:
            return result(outcome, refresh_id, polls, status.get("error"))


def summarize(results: list[dict], skipped: list[dict] | None = None) -> dict:
    skipped = skipped or []
    failed = [r for r in results if r["status"] != Outcome.SUCCEEDED]
    return {
        "ok": not failed and not skipped,
        "succeeded": len(results) - len(failed),
        "failed": len(failed),
        "skipped": [ds.get("name", ds["dataset_id"]) for ds in skipped],
        "results": results,
    }
