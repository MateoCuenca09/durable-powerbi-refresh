"""Send the run summary to a webhook (Teams workflow, Slack, or anything that takes JSON)."""

from __future__ import annotations

import logging

import requests


def format_summary(summary: dict) -> str:
    icon = "✅" if summary["ok"] else "❌"
    lines = [
        f"{icon} Power BI refresh: {summary['succeeded']} succeeded, {summary['failed']} failed"
    ]
    for r in summary["results"]:
        detail = f" ({r['error']})" if r.get("error") else ""
        lines.append(f"- {r['name']}: {r['status']} in {r['duration_seconds']}s{detail}")
    if summary["skipped"]:
        lines.append(f"Skipped (upstream failure): {', '.join(summary['skipped'])}")
    return "\n".join(lines)


def send_summary(summary: dict, webhook_url: str | None, session=requests) -> bool:
    text = format_summary(summary)
    logging.info(text)
    if not webhook_url:
        return False
    resp = session.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()
    return True
