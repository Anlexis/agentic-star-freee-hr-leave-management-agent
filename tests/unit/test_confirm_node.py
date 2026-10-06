# CMN-C2-278 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust -> input gate ->
# execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "1001",
        "record_ref": "freee-hr://employees/1001/leave-balances",
        "leave_summary": "paid_holiday: 12.5 days remaining (3 taken)",
        "intent": "lookup_balance",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved leave balance" in result["confirmation"]
        assert "paid_holiday: 12.5 days remaining (3 taken)" in result["confirmation"]
        assert "ref=freee-hr://employees/1001/leave-balances" in result["confirmation"]
        assert "id=1001" in result["confirmation"]
        assert result["result"]["record_id"] == "1001"
        assert result["result"]["record_ref"] == "freee-hr://employees/1001/leave-balances"

    def test_submit_verb(self):
        result = self.node(
            _state(
                intent="submit_request",
                record_id="lr-1a2b3c4d",
                record_ref="freee-hr://leave-requests/lr-1a2b3c4d",
                leave_summary="submitted (in_progress)",
            )
        )
        assert "Submitted leave request" in result["confirmation"]

    def test_status_check_verb(self):
        result = self.node(_state(intent="check_status", leave_summary="status: approved"))
        assert "Checked leave request status" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed leave request" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=1001" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_summary_missing(self):
        result = self.node(_state(leave_summary=""))
        assert "'1001'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
