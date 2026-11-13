import pytest

from refresh.notify import format_summary, send_summary
from refresh.policy import Outcome, classify, next_poll_delay
from refresh.powerbi import FakePowerBIClient, PowerBIClient, PowerBIError

BASE = "https://api.powerbi.com/v1.0/myorg/groups/ws/datasets/ds/refreshes"


class Resp:
    def __init__(self, status_code, headers=None, json_data=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_data
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def client(responses, sleeps=None):
    session = FakeSession(responses)
    c = PowerBIClient(
        lambda: "tok", session=session, sleep=(sleeps if sleeps is not None else []).append
    )
    return c, session


def test_trigger_returns_refresh_id_from_location():
    c, s = client([Resp(202, {"Location": f"{BASE}/abc-123"})])
    assert c.trigger_refresh("ws", "ds") == "abc-123"
    method, url, kwargs = s.requests[0]
    assert (method, url) == ("POST", BASE)
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["json"]["type"] == "full"


def test_trigger_rejects_non_202():
    c, _ = client([Resp(400, text="Another refresh is in progress")])
    with pytest.raises(PowerBIError, match="in progress"):
        c.trigger_refresh("ws", "ds")


def test_trigger_without_location_is_an_error():
    c, _ = client([Resp(202, {})])
    with pytest.raises(PowerBIError, match="no refresh id"):
        c.trigger_refresh("ws", "ds")


def test_throttling_retries_with_retry_after():
    sleeps = []
    c, s = client([Resp(429, {"Retry-After": "7"}), Resp(202, {"Location": f"{BASE}/r1"})], sleeps)
    assert c.trigger_refresh("ws", "ds") == "r1"
    assert sleeps == [7.0]
    assert len(s.requests) == 2


def test_get_refresh_reads_specific_id_and_first_error():
    body = {
        "status": "Failed",
        "extendedStatus": "Failed",
        "messages": [
            {"type": "Warning", "message": "slow"},
            {"type": "Error", "message": "Credentials expired"},
        ],
    }
    c, s = client([Resp(200, json_data=body)])
    out = c.get_refresh("ws", "ds", "r1")
    assert s.requests[0][1] == f"{BASE}/r1"
    assert out == {"status": "Failed", "extended_status": "Failed", "error": "Credentials expired"}


@pytest.mark.parametrize(
    "status,expected",
    [
        ("Completed", Outcome.SUCCEEDED),
        ("Failed", Outcome.FAILED),
        ("Cancelled", Outcome.FAILED),
        ("Disabled", Outcome.FAILED),
        ("Unknown", Outcome.RUNNING),
        ("NotStarted", Outcome.RUNNING),
        (None, Outcome.RUNNING),
    ],
)
def test_classify(status, expected):
    assert classify(status) is expected


def test_backoff_is_capped():
    assert [next_poll_delay(i) for i in range(6)] == [30, 60, 120, 240, 300, 300]


def test_fake_client_lifecycle():
    now = [1000.0]
    fake = FakePowerBIClient(duration_seconds=20, clock=lambda: now[0])
    rid = fake.trigger_refresh("ws", "sales")
    assert fake.get_refresh("ws", "sales", rid)["status"] == "Unknown"
    now[0] += 21
    assert fake.get_refresh("ws", "sales", rid)["status"] == "Completed"
    assert fake.get_refresh("ws", "will-fail", rid)["status"] == "Failed"


def test_summary_formatting_and_webhook():
    summary = {
        "ok": False,
        "succeeded": 1,
        "failed": 1,
        "skipped": ["Executive"],
        "results": [
            {"name": "Sales", "status": "Succeeded", "duration_seconds": 90, "error": None},
            {"name": "Finance", "status": "Failed", "duration_seconds": 30, "error": "boom"},
        ],
    }
    text = format_summary(summary)
    assert text.startswith("❌") and "Finance: Failed in 30s (boom)" in text
    assert "Skipped (upstream failure): Executive" in text

    session = FakeSession([Resp(200)])
    assert send_summary(summary, "https://hooks.example.com/x", session=session) is True
    assert session.requests[0][2]["json"] == {"text": text}
    assert send_summary(summary, None) is False
