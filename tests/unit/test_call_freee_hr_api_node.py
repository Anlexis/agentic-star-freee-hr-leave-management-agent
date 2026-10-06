# CMN-C2-278 - Unit tests: CallFreeeHrApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust -> input gate ->
# execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, the trust gate is unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's FreeeHrClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_freee_hr_api_node import CallFreeeHrApiNode
from src.services.freee_hr_client import FreeeHrApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_freee_hr_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "freee_hr_payload": to_json({"employee_id": "1001"}),
        "intent": "lookup_balance",
        "employee_id": "1001",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-freee-hr-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for FreeeHrClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_leave_balance(self, employee_id, api_token, company_id=""):
        raise FreeeHrApiError(403, "forbidden by integration permissions")


class _FakeLiveClient:
    """Stands in for FreeeHrClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_leave_balance(self, employee_id, api_token, company_id=""):
        _FakeLiveClient.captured = {"employee_id": employee_id, "api_token": api_token}
        return {
            "employee_id": employee_id,
            "leave_balances": [{"leave_type": "paid_holiday", "remaining_days": 12.5, "taken_days": 3}],
        }


class TestCallFreeeHrApiNode:
    def setup_method(self):
        self.node = CallFreeeHrApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"
        assert result["record_ref"] == "freee-hr://employees/1001/leave-balances"
        assert result["employee_id"] == "1001"
        assert "days remaining" in result["leave_summary"]

    def test_submit_success_via_default_v1_stub(self):
        state = _state(
            intent="submit_request",
            freee_hr_payload=to_json(
                {
                    "leave_request": {
                        "employee_id": "1001",
                        "leave_type": "paid_holiday",
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-03",
                    }
                }
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"].startswith("lr-")
        assert result["record_ref"] == f"freee-hr://leave-requests/{result['record_id']}"
        assert result["leave_summary"] == "submitted (in_progress)"

    def test_status_check_success_via_default_v1_stub(self):
        state = _state(
            intent="check_status",
            freee_hr_payload=to_json({"request_id": "lr-1a2b3c4d"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "lr-1a2b3c4d"
        assert result["record_ref"] == "freee-hr://leave-requests/lr-1a2b3c4d"
        assert result["leave_summary"].startswith("status: ")

    def test_freee_hr_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `freee_hr:` section as the JSON
        # freee_hr_config state field; the stub transport still serves the call.
        state = _state(freee_hr_config=to_json({"base_url": "https://freee.example.test/hr/api/v1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "freee-hr://employees/1001/leave-balances"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"freee_hr": {"base_url": "https://freee.example.test/hr/api/v1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"

    def test_missing_payload_errors(self):
        result = self.node(_state(freee_hr_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_code_errors(self):
        state = _state(employee_id="", freee_hr_payload=to_json({"employee_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved employee number" in entry for entry in result["error_log"])

    def test_submit_without_start_date_errors(self):
        state = _state(
            intent="submit_request",
            freee_hr_payload=to_json(
                {
                    "leave_request": {
                        "employee_id": "1001",
                        "leave_type": "paid_holiday",
                        "start_date": "",
                        "end_date": "",
                    }
                }
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved leave start date" in entry for entry in result["error_log"])

    def test_status_check_without_request_id_errors(self):
        state = _state(intent="check_status", freee_hr_payload=to_json({"request_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved request id" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_request"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error_with_the_status_only(self, monkeypatch):
        """A remote error body is upstream content - it can carry identifiers,
        names or echoed request fields - so the log line carries the HTTP
        status only, never the API's own message."""
        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.FreeeHrClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        entry = next(e for e in result["error_log"] if "403" in e)
        assert "freee HR API error 403" in entry
        assert "forbidden by integration permissions" not in entry
        assert "forbidden" not in entry

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing FREEE_HR_ACCESS_TOKEN is a hard
        # error - a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.FreeeHrClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.FreeeHrClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"FREEE_HR_ACCESS_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["employee_id"] == "1001"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_freee_hr_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_freee_hr_api_complete"]
        assert payload["intent"] == "lookup_balance"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True
