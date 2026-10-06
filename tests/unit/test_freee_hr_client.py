# CMN-C2-278 - Unit tests: FreeeHrClient service (freee HR REST API v1 shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.freee_hr_client import FreeeHrApiError, FreeeHrClient


def test_get_leave_balance_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {
            "employee_id": "1001",
            "leave_balances": [{"leave_type": "paid_holiday", "remaining_days": 12.5, "taken_days": 3}],
        }

    client = FreeeHrClient("https://freee.example.test/hr/api/v1/", get=get)
    resp = client.get_leave_balance("1001", "tok123")
    assert resp["leave_balances"][0]["leave_type"] == "paid_holiday"
    assert captured["url"] == "https://freee.example.test/hr/api/v1/employees/1001/holidays"
    # freee HR REST API v1 auth: OAuth2 bearer - the per-call token travels in
    # the Authorization header (assembled at runtime, never persisted).
    assert captured["headers"]["Authorization"] == "Bearer " + "tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["employee_id"] == "1001"


def test_company_id_scopes_the_request_body():
    captured = {}

    def get(url, headers, body):
        captured["body"] = body
        return 200, {"employee_id": "1001", "leave_balances": []}

    client = FreeeHrClient("https://freee.example.test/hr/api/v1", get=get)
    client.get_leave_balance("1001", "tok", company_id="c-77")
    assert captured["body"]["company_id"] == "c-77"


def test_submit_leave_request_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"request_id": "lr-9002", "status": "in_progress"}

    client = FreeeHrClient("https://freee.example.test/hr/api/v1", post=post)
    payload = {
        "leave_request": {
            "employee_id": "1001",
            "leave_type": "paid_holiday",
            "start_date": "2026-08-01",
            "end_date": "2026-08-03",
        }
    }
    resp = client.submit_leave_request(payload, "tok")
    assert resp["request_id"] == "lr-9002"
    assert captured["url"] == "https://freee.example.test/hr/api/v1/approval_requests/paid_holidays"
    assert captured["body"] == payload


def test_get_request_status_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"request_id": "lr-9002", "status": "approved"}

    client = FreeeHrClient("https://freee.example.test/hr/api/v1", get=get)
    resp = client.get_request_status("lr-9002", "tok")
    assert resp["status"] == "approved"
    assert captured["url"] == "https://freee.example.test/hr/api/v1/approval_requests/paid_holidays/lr-9002"
    assert captured["body"]["request_id"] == "lr-9002"


def test_non_2xx_raises_freee_hr_api_error():
    def post(url, headers, body):
        return 400, {"errors": ["leave_request is malformed"]}

    client = FreeeHrClient("https://freee.example.test/hr/api/v1", post=post)
    with pytest.raises(FreeeHrApiError) as exc:
        client.submit_leave_request({"leave_request": {}}, "tok")
    assert exc.value.status_code == 400
    assert "leave_request is malformed" in str(exc.value)


def test_default_stub_transport_balance_shape():
    # No transport injected -> deterministic, network-free stub.
    client = FreeeHrClient()
    assert client.uses_stub_transport is True
    resp = client.get_leave_balance("1001", "tok")
    assert resp.get("_stub") is True
    assert resp["employee_id"] == "1001"
    balance = resp["leave_balances"][0]
    assert balance["leave_type"] == "paid_holiday"
    assert isinstance(balance["remaining_days"], float)
    assert isinstance(balance["taken_days"], int)


def test_default_stub_transport_submit_returns_receipt():
    client = FreeeHrClient()
    resp = client.submit_leave_request({"leave_request": {"employee_id": "1001"}}, "tok")
    assert resp.get("_stub") is True
    assert resp["request_id"].startswith("lr-")
    assert resp["status"] == "in_progress"


def test_default_stub_transport_status_echoes_request_id():
    client = FreeeHrClient()
    resp = client.get_request_status("lr-1a2b3c4d", "tok")
    assert resp.get("_stub") is True
    assert resp["request_id"] == "lr-1a2b3c4d"
    assert resp["status"] in ("approved", "in_progress")


def test_injected_transport_disables_stub_flag():
    client = FreeeHrClient(get=lambda url, headers, body: (200, {"leave_balances": []}))
    assert client.uses_stub_transport is False
