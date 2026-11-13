"""Minimal Power BI REST client for enhanced (asynchronous) dataset refreshes.

Docs: https://learn.microsoft.com/power-bi/connect-data/asynchronous-refresh
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, Protocol

import requests

API_URL = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"


class PowerBIError(RuntimeError):
    pass


class RefreshClient(Protocol):
    def trigger_refresh(self, workspace_id: str, dataset_id: str) -> str: ...

    def get_refresh(self, workspace_id: str, dataset_id: str, refresh_id: str) -> dict: ...


class PowerBIClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        session: requests.Session | None = None,
        base_url: str = API_URL,
        max_throttle_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._token = token_provider
        self._session = session or requests.Session()
        self._base = base_url.rstrip("/")
        self._max_retries = max_throttle_retries
        self._sleep = sleep

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(self._max_retries + 1):
            resp = self._session.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self._token()}"},
                timeout=30,
                **kwargs,
            )
            if resp.status_code != 429 or attempt == self._max_retries:
                return resp
            self._sleep(float(resp.headers.get("Retry-After", 2**attempt)))
        raise AssertionError("unreachable")

    def _refreshes_url(self, workspace_id: str, dataset_id: str) -> str:
        return f"{self._base}/groups/{workspace_id}/datasets/{dataset_id}/refreshes"

    def trigger_refresh(self, workspace_id: str, dataset_id: str) -> str:
        """Start an enhanced refresh and return its refresh id (from the Location header)."""
        resp = self._request(
            "POST",
            self._refreshes_url(workspace_id, dataset_id),
            json={"type": "full", "commitMode": "transactional", "retryCount": 1},
        )
        if resp.status_code != 202:
            raise PowerBIError(f"Refresh not accepted ({resp.status_code}): {resp.text[:300]}")
        refresh_id = resp.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
        if not refresh_id or refresh_id == "refreshes":
            raise PowerBIError("Power BI accepted the refresh but returned no refresh id.")
        return refresh_id

    def get_refresh(self, workspace_id: str, dataset_id: str, refresh_id: str) -> dict:
        """Status of *this* refresh. Never "the latest one", which may be an older run."""
        resp = self._request("GET", f"{self._refreshes_url(workspace_id, dataset_id)}/{refresh_id}")
        if resp.status_code != 200:
            raise PowerBIError(f"Could not read refresh ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        errors = [
            m.get("message", "") for m in data.get("messages", []) if m.get("type") == "Error"
        ]
        return {
            "status": data.get("status"),
            "extended_status": data.get("extendedStatus"),
            "error": errors[0] if errors else None,
        }


class FakePowerBIClient:
    """Stateless stand-in for local runs: a refresh "completes" after ``duration`` seconds.

    Datasets whose id contains ``fail`` end in ``Failed`` so you can see the error path.
    """

    def __init__(self, duration_seconds: float = 20, clock: Callable[[], float] = time.time):
        self._duration = duration_seconds
        self._clock = clock

    def trigger_refresh(self, workspace_id: str, dataset_id: str) -> str:
        return f"fake-{int(self._clock())}"

    def get_refresh(self, workspace_id: str, dataset_id: str, refresh_id: str) -> dict:
        started = int(refresh_id.removeprefix("fake-"))
        if self._clock() - started < self._duration:
            return {"status": "Unknown", "extended_status": "InProgress", "error": None}
        if "fail" in dataset_id:
            return {"status": "Failed", "extended_status": "Failed", "error": "Simulated failure"}
        return {"status": "Completed", "extended_status": "Completed", "error": None}


def build_client() -> RefreshClient:
    """Fake client when POWERBI_FAKE=1, otherwise the real API with Managed Identity / az login."""
    if os.environ.get("POWERBI_FAKE") == "1":
        return FakePowerBIClient(float(os.environ.get("POWERBI_FAKE_DURATION", "20")))

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    return PowerBIClient(lambda: credential.get_token(SCOPE).token)
