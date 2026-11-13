"""Azure Functions entry point (Python v2 programming model)."""

from __future__ import annotations

import json
import logging
import os

import azure.durable_functions as df
import azure.functions as func

from refresh import orchestration
from refresh.notify import send_summary as notify
from refresh.powerbi import build_client

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


# ---------- Starters ----------


@app.route(route="refresh", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_refresh(req: func.HttpRequest, client) -> func.HttpResponse:
    """POST /api/refresh with {"datasets": [...]} or {"stages": [[...], [...]]}."""
    try:
        payload = req.get_json()
        orchestration.validate_payload(payload)
    except ValueError as e:
        return func.HttpResponse(str(e) or "Invalid JSON body.", status_code=400)
    instance_id = await client.start_new("refresh_all", client_input=payload)
    logging.info("Started refresh run %s", instance_id)
    return client.create_check_status_response(req, instance_id)


@app.schedule(schedule="%REFRESH_SCHEDULE%", arg_name="timer", run_on_startup=False)
@app.durable_client_input(client_name="client")
async def scheduled_refresh(timer: func.TimerRequest, client) -> None:
    """Run the plan in REFRESH_PLAN (JSON) on a CRON schedule, e.g. after the nightly ETL."""
    plan = os.environ.get("REFRESH_PLAN")
    if not plan:
        logging.warning("REFRESH_PLAN is empty; nothing to refresh.")
        return
    payload = json.loads(plan)
    orchestration.validate_payload(payload)
    instance_id = await client.start_new("refresh_all", client_input=payload)
    logging.info("Started scheduled refresh run %s", instance_id)


# ---------- Orchestrators ----------


@app.orchestration_trigger(context_name="context")
def refresh_all(context: df.DurableOrchestrationContext):
    return (yield from orchestration.refresh_all(context))


@app.orchestration_trigger(context_name="context")
def refresh_dataset(context: df.DurableOrchestrationContext):
    return (yield from orchestration.refresh_dataset(context))


# ---------- Activities ----------


@app.activity_trigger(input_name="dataset")
def trigger_refresh(dataset: dict) -> str:
    return build_client().trigger_refresh(dataset["workspace_id"], dataset["dataset_id"])


@app.activity_trigger(input_name="dataset")
def get_refresh_status(dataset: dict) -> dict:
    return build_client().get_refresh(
        dataset["workspace_id"], dataset["dataset_id"], dataset["refresh_id"]
    )


@app.activity_trigger(input_name="summary")
def send_summary(summary: dict) -> bool:
    return notify(summary, os.environ.get("NOTIFY_WEBHOOK_URL"))
