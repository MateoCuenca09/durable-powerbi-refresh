from datetime import timedelta

import pytest

from refresh import orchestration
from refresh.powerbi import PowerBIError
from tests.fake_durable import Runtime

SALES = {"workspace_id": "ws-1", "dataset_id": "sales", "name": "Sales"}
FINANCE = {"workspace_id": "ws-1", "dataset_id": "finance", "name": "Finance"}
EXEC = {"workspace_id": "ws-2", "dataset_id": "exec", "name": "Executive"}


def make_runtime(statuses: dict[str, list[str]], trigger_errors: set[str] = frozenset()):
    """statuses: dataset_id -> successive statuses returned by get_refresh_status."""
    queues = {k: list(v) for k, v in statuses.items()}
    summaries = []

    def trigger(ds):
        if ds["dataset_id"] in trigger_errors:
            raise PowerBIError("Refresh not accepted (400)")
        return f"rid-{ds['dataset_id']}"

    def status(ds):
        assert ds["refresh_id"] == f"rid-{ds['dataset_id']}"  # always the refresh we started
        q = queues[ds["dataset_id"]]
        s = q.pop(0) if len(q) > 1 else q[0]
        return {"status": s, "error": "Data source error" if s == "Failed" else None}

    rt = Runtime(
        activities={
            "trigger_refresh": trigger,
            "get_refresh_status": status,
            "send_summary": lambda s: summaries.append(s) or True,
        },
        orchestrators={
            "refresh_all": orchestration.refresh_all,
            "refresh_dataset": orchestration.refresh_dataset,
        },
    )
    rt.summaries = summaries
    return rt


def test_single_dataset_polls_with_backoff_until_completed():
    rt = make_runtime({"sales": ["Unknown", "Unknown", "Completed"]})
    start = rt.clock["now"]
    result = rt.run("refresh_dataset", SALES)

    assert result["status"] == "Succeeded"
    assert result["refresh_id"] == "rid-sales"
    assert result["polls"] == 3
    points = [start, *rt.timers]
    gaps = [(b - a).total_seconds() for a, b in zip(points, points[1:], strict=False)]
    assert gaps == [30, 60, 120]
    assert result["duration_seconds"] == 210


def test_failed_refresh_reports_error():
    rt = make_runtime({"sales": ["Unknown", "Failed"]})
    result = rt.run("refresh_dataset", SALES)
    assert result["status"] == "Failed"
    assert result["error"] == "Data source error"


def test_trigger_failure_is_caught():
    rt = make_runtime({"sales": ["Completed"]}, trigger_errors={"sales"})
    result = rt.run("refresh_dataset", SALES)
    assert result["status"] == "Failed"
    assert "Trigger failed" in result["error"]
    assert result["refresh_id"] is None


def test_timeout_stops_polling():
    rt = make_runtime({"sales": ["Unknown"]})
    result = rt.run("refresh_dataset", {**SALES, "timeout_minutes": 10})
    assert result["status"] == "TimedOut"
    assert rt.timers[-1] - rt.timers[0] <= timedelta(minutes=10)


def test_stages_run_in_order_and_fan_out_within_a_stage():
    rt = make_runtime({"sales": ["Completed"], "finance": ["Completed"], "exec": ["Completed"]})
    summary = rt.run("refresh_all", {"stages": [[SALES, FINANCE], [EXEC]]})

    assert summary["ok"] is True
    assert summary["succeeded"] == 3
    triggered = [c[1]["dataset_id"] for c in rt.calls if c[0] == "trigger_refresh"]
    assert triggered.index("exec") > max(triggered.index("sales"), triggered.index("finance"))
    assert rt.summaries == [summary]


def test_failed_stage_skips_downstream_models():
    rt = make_runtime({"sales": ["Failed"], "finance": ["Completed"], "exec": ["Completed"]})
    summary = rt.run("refresh_all", {"stages": [[SALES, FINANCE], [EXEC]]})

    assert summary["ok"] is False
    assert summary["failed"] == 1
    assert summary["skipped"] == ["Executive"]
    assert all(c[1]["dataset_id"] != "exec" for c in rt.calls if c[0] == "trigger_refresh")


def test_datasets_shorthand_is_one_stage():
    rt = make_runtime({"sales": ["Completed"]})
    assert rt.run("refresh_all", {"datasets": [SALES]})["ok"] is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"stages": []},
        {"stages": [[]]},
        {"datasets": [{"workspace_id": "ws"}]},
        {"datasets": [{"workspace_id": "", "dataset_id": "x"}]},
    ],
)
def test_invalid_payloads_rejected(payload):
    with pytest.raises(ValueError):
        orchestration.validate_payload(payload)
