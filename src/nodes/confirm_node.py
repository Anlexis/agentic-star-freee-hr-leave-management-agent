"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the looked-up balance / submitted request / checked status (record id +
reference + summary) into a human-readable confirmation message, surfacing the
affected record for human review (risk mitigation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VERBS = {
    "lookup_balance": "Retrieved leave balance",
    "submit_request": "Submitted leave request",
    "check_status": "Checked leave request status",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-fetched data - the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        record_id = state.get("record_id", "")
        record_ref = state.get("record_ref", "")
        leave_summary = state.get("leave_summary", "")
        intent = state.get("intent", "lookup_balance")

        if not record_id and not record_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no record_id/record_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed leave request")
        parts = [f"{verb} '{leave_summary or record_id}'"]
        if record_ref:
            parts.append(f"ref={record_ref}")
        if record_id:
            parts.append(f"id={record_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_record_ref": bool(record_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "record_id": record_id,
                "record_ref": record_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
