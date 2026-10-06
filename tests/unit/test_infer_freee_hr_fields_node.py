# CMN-C2-278 - Unit tests: InferFreeeHrFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust -> input gate ->
# execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework PII mask rewrites Title-Case
# bigrams in validated_input (even across newlines), so `Key: value` request
# lines use lower-case keys and employee codes stay short (<= 4 digits).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_freee_hr_fields_node import InferFreeeHrFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_freee_hr_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_balance", employee_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "employee_hint": employee_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferFreeeHrFieldsNode:
    def setup_method(self):
        self.node = InferFreeeHrFieldsNode()

    def test_lookup_extracts_code_from_text(self):
        result = self.node(
            _state("Look up the remaining leave balance for employee code 1001 and summarize the days available.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "1001"
        # freee_hr_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["freee_hr_payload"], str)
        assert from_json(result["freee_hr_payload"], {}) == {"employee_id": "1001"}

    def test_code_shaped_hint_used_when_text_has_no_code(self):
        result = self.node(_state("Show the current leave balance summary", employee_hint="A123"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "A123"
        assert from_json(result["freee_hr_payload"], {}) == {"employee_id": "A123"}

    def test_submit_builds_leave_request_payload(self):
        # `key: value` request lines stay lower-case: the framework PII name
        # mask rewrites Title-Case word pairs even ACROSS newlines before
        # execute() sees the text.
        text = "I want to take sick leave for employee code 1001\nstart: 2026-08-01\nend: 2026-08-03"
        result = self.node(_state(text, intent="submit_request"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "1001"
        assert result["leave_type"] == "sick_leave"
        payload = from_json(result["freee_hr_payload"], {})
        request = payload["leave_request"]
        assert request["employee_id"] == "1001"
        assert request["leave_type"] == "sick_leave"
        assert request["start_date"] == "2026-08-01"
        assert request["end_date"] == "2026-08-03"

    def test_submit_single_date_falls_back_to_start(self):
        text = "Submit a paid holiday for employee code 1001 on 2026-09-15"
        result = self.node(_state(text, intent="submit_request"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["leave_type"] == "paid_holiday"
        request = from_json(result["freee_hr_payload"], {})["leave_request"]
        assert request["start_date"] == "2026-09-15"
        assert request["end_date"] == "2026-09-15"

    def test_status_check_extracts_request_id(self):
        text = "Check the status of request id lr-1a2b3c4d"
        result = self.node(_state(text, intent="check_status"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["freee_hr_payload"], {}) == {"request_id": "lr-1a2b3c4d"}

    def test_leave_type_defaults_to_paid_holiday(self):
        result = self.node(_state("Look up the remaining leave balance for employee code 1001."))
        assert result["leave_type"] == "paid_holiday"

    def test_unresolved_code_left_empty_never_invented(self):
        result = self.node(_state("Show the leave balance summary for the flagged record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == ""
        assert from_json(result["freee_hr_payload"], {}) == {"employee_id": ""}

    def test_non_code_shaped_hint_left_unresolved(self):
        result = self.node(_state("Show the leave balance summary", employee_hint="not a valid code!"))
        assert result["employee_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
