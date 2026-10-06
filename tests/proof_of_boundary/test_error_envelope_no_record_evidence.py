"""Regression: an ERROR envelope must not disclose freee HR record evidence.

Molt source review, 2026-09-04 (wave-8 batch): "the success-gate violation path
has containment, but the independent existing-ERROR branch rebuilds a truthy
response with `record_id` / `record_ref` and does not clear the merged
output-bearing state."

The `errored` branch of PostProcessNode rebuilt `formatted_output` from
`record_id` / `record_ref` read straight back out of state, and returned ONLY
that plus `status` - so every other output-bearing field (`result`,
`confirmation`, `leave_type`, `leave_summary`, `freee_hr_payload`,
`employee_id`, `intent`) survived in state untouched.

Those identifiers ARE the freee HR write evidence: this node's own gate
(`_security_gate_output`, is_success=True) REFUSES a SUCCESS that lacks them. An
error envelope carrying them tells a caller who is being informed of a FAILURE
that a leave request was nonetheless submitted, and which employee it belongs
to. freee HR is an HR system: the employee number, the leave type and the
balance summary are personal data (a `sick_leave` balance is health-adjacent).

Three properties are asserted, and they are three DIFFERENT properties:
  1. the shipped envelope carries no record evidence, AND stays TRUTHY - a falsy
     `formatted_output` re-opens the framework's `formatted_output or result`
     projection (AgentBaseGraph.get_output() applies no status check) onto
     whatever survived in state;
  2. the returned delta CLEARS the output-bearing state fields, so a checkpoint
     or a downstream reader cannot pick them up either;
  3. the error REASONS carry closed-set labels only. `error_log` is the internal
     channel (never projected to the caller - the envelope carries a constant
     reason code), but it is logged and correlated on, so a reason that
     interpolates the employee number - or quotes an upstream freee HR response
     body - would put the same evidence into the audit trail.

A success-path control is included: without it every containment assertion above
would pass vacuously against a node that simply returned an empty envelope.
"""

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.call_freee_hr_api_node import CallFreeeHrApiNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import to_json

# Record evidence + the HR data hanging off it.
_RECORD_ID = "lr-2001"
_RECORD_REF = "freee-hr://leave-requests/lr-2001"
_EMPLOYEE_ID = "EMP-1001"
_LEAVE_TYPE = "sick_leave"
_LEAVE_SUMMARY = "sick_leave: 4.5 days remaining (7.5 taken)"


def _errored_state() -> dict:
    """An error raised AFTER the freee HR call resolved a record.

    The realistic shape - the call succeeded and a later step failed - and the
    only shape in which record evidence is present on an error at all.
    """
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": ["ConfirmNode: downstream failure after the freee HR call"],
        "record_id": _RECORD_ID,
        "record_ref": _RECORD_REF,
        "employee_id": _EMPLOYEE_ID,
        "leave_type": _LEAVE_TYPE,
        "leave_summary": _LEAVE_SUMMARY,
        "intent": "submit_request",
        "confirmation": f"Submitted leave request '{_LEAVE_TYPE}' - ref={_RECORD_REF} - id={_RECORD_ID}",
        "freee_hr_payload": to_json(
            {
                "leave_request": {
                    "employee_id": _EMPLOYEE_ID,
                    "leave_type": _LEAVE_TYPE,
                    "start_date": "2026-09-01",
                }
            }
        ),
        "result": {"record_id": _RECORD_ID, "confirmation": "done"},
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "pb-error-envelope",
        "session_id": "pb-s1",
        "thread_id": "pb-th1",
        "trace_id": "pb-t1",
        "node_history": [],
        "execution_time": {},
    }


def _flatten(value) -> str:
    """Render every reachable string in a returned value - nesting is not cover."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(k) + " " + _flatten(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


class TestErrorEnvelopeContainment:
    def test_error_envelope_carries_no_record_evidence(self):
        out = PostProcessNode().execute(_errored_state())

        assert out["status"] == AgentStatus.ERROR.value

        # formatted_output must be PRESENT and TRUTHY. The framework projects
        # `formatted_output or result` with no status check, so a falsy value
        # re-opens the fallback onto whatever survived in state.
        assert "formatted_output" in out, "the error path must ship an envelope"
        assert out["formatted_output"], "falsy formatted_output re-opens the `or result` fallback"

        shipped = _flatten(out["formatted_output"])
        leaked = [
            name
            for name, value in (
                ("record_id", _RECORD_ID),
                ("record_ref", _RECORD_REF),
                ("employee_id", _EMPLOYEE_ID),
                ("leave_summary", _LEAVE_SUMMARY),
                ("confirmation", "Submitted leave request"),
                ("freee-hr scheme", "freee-hr://"),
            )
            if value in shipped
        ]
        assert not leaked, f"error envelope leaked freee HR record evidence: {leaked}"

    def test_error_path_clears_output_bearing_state(self):
        """Omitting a field from ONE envelope is not clearing it from state."""
        out = PostProcessNode().execute(_errored_state())
        retained = [
            field
            for field in (
                "result",
                "confirmation",
                "leave_summary",
                "leave_type",
                "freee_hr_payload",
                "record_id",
                "record_ref",
                "employee_id",
                "intent",
            )
            if field not in out or out[field]
        ]
        assert not retained, f"output-bearing state not cleared on the error path: {retained}"

    def test_success_path_still_returns_the_answer(self):
        """CONTROL. Without it every containment assertion above passes
        vacuously - a node that returned an empty envelope would satisfy them
        all. The success path must still carry the record evidence."""
        out = PostProcessNode().execute(
            {
                "status": AgentStatus.SUCCESS.value,
                "record_id": _RECORD_ID,
                "record_ref": _RECORD_REF,
                "employee_id": _EMPLOYEE_ID,
                "leave_type": _LEAVE_TYPE,
                "leave_summary": _LEAVE_SUMMARY,
                "intent": "lookup_balance",
                "confirmation": f"Retrieved leave balance - id={_RECORD_ID}",
                "freee_hr_payload": to_json({"employee_id": _EMPLOYEE_ID}),
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "correlation_id": "pb-error-envelope-control",
                "node_history": [],
                "error_log": [],
                "execution_time": {},
            }
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        shipped = _flatten(out["formatted_output"])
        assert _RECORD_ID in shipped, "the success path must still return the record evidence"
        assert _RECORD_REF in shipped
        assert _LEAVE_SUMMARY in shipped


class TestErrorLogNamesNoRecord:
    """The node-authored diagnostics are a channel of their own.

    `error_log` never reaches the caller (post_process publishes a constant
    reason code), but it is the audit channel, and an upstream freee HR error
    body is unbounded third-party text that can quote the very record it
    refused. Reasons must be closed-set labels: the record TYPE, the HTTP
    status, the exception TYPE.
    """

    def _state(self, **overrides) -> dict:
        state = {
            "freee_hr_payload": to_json({"employee_id": _EMPLOYEE_ID}),
            "intent": "lookup_balance",
            "employee_id": _EMPLOYEE_ID,
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
            "correlation_id": "pb-error-reason",
            "session_id": "pb-s1",
            "thread_id": "pb-th1",
            "trace_id": "pb-t1",
            "node_history": [],
            "error_log": [],
            "execution_time": {},
        }
        state.update(overrides)
        return state

    def test_balance_not_found_reason_does_not_name_the_employee(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.emit_trace_event", lambda *a, **k: None)

        class _EmptyBalanceClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_leave_balance(self, employee_id, api_token, company_id=""):
                return {"employee_id": employee_id, "leave_balances": []}

        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.FreeeHrClient", _EmptyBalanceClient)
        out = CallFreeeHrApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert _EMPLOYEE_ID not in joined, "the not-found reason names the employee number"
        # The reason still has to be actionable: it names WHAT was not found,
        # a closed-set label, rather than for whom.
        assert "leave balance" in joined

    def test_upstream_api_failure_reason_carries_no_upstream_body(self, monkeypatch):
        """A live tenant's error body is unbounded third-party text; only the
        HTTP status - a closed-set signal - belongs in a caller-facing reason."""
        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.emit_trace_event", lambda *a, **k: None)
        from src.services.freee_hr_client import FreeeHrApiError

        class _ApiErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_leave_balance(self, employee_id, api_token, company_id=""):
                raise FreeeHrApiError(403, f"denied for {_EMPLOYEE_ID} ({_LEAVE_TYPE}) - {_LEAVE_SUMMARY}")

        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.FreeeHrClient", _ApiErrorClient)
        out = CallFreeeHrApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "403" in joined, "the HTTP status is the actionable signal - keep it"
        assert _EMPLOYEE_ID not in joined
        assert _LEAVE_TYPE not in joined
        assert _LEAVE_SUMMARY not in joined

    def test_transport_failure_reason_carries_only_the_exception_type(self, monkeypatch):
        """A transport error string can carry the request URL and the record id."""
        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.emit_trace_event", lambda *a, **k: None)

        class _TransportErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_leave_balance(self, employee_id, api_token, company_id=""):
                raise ConnectionError(
                    f"failed to reach https://api.freee.co.jp/hr/api/v1/employees/{_EMPLOYEE_ID}/leave_balances"
                )

        monkeypatch.setattr("src.nodes.call_freee_hr_api_node.FreeeHrClient", _TransportErrorClient)
        out = CallFreeeHrApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "ConnectionError" in joined, "the exception TYPE is the actionable signal - keep it"
        assert _EMPLOYEE_ID not in joined
        assert "api.freee.co.jp" not in joined
