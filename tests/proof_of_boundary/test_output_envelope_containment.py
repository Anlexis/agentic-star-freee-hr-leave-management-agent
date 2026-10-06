# PB (containment): what the caller-facing envelope carries after the output
# gate refuses a response, or after the inner workflow fails.
#
# The envelope resolves its payload as `formatted_output or result` and never
# looks at the status; by the time post_process runs, `result` already holds
# the inner workflow's answer. So an error return that merely sets the status,
# or that replaces formatted_output with something falsy, ships the answer it
# refused inside an envelope that calls itself an error.
#
# These tests assert the property at the boundary that matters: on every
# non-success path the envelope the caller receives carries closed-set labels
# only - never error_log, never the gate's violation entries, never record
# evidence - and stays truthy. The clean-path control is deliberate: it fails
# if the gate ever starts refusing everything, so containment cannot be
# "achieved" by returning nothing at all.
#
# post_process is driven through its real call path where the framework
# pipeline allows (node(state) - trust gate, input gate, execute, credential
# scan) and its returned partial state is merged the way the state reducer
# merges it (error_log / node_history append); an errored state is driven
# through execute(), because BaseNode.__call__ short-circuits on it and the
# backbone routes an error straight to finalize, so that branch is only
# reachable from inside. The merged state is then projected through the
# agent's get_output() (the framework's - this agent does not override it),
# which is exactly what the caller reads.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import FreeeHRLeaveAgent, load_runtime_config
from src.nodes.post_process_node import (
    ERROR_REASONS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
)
from src.schemas.state import to_json

_RECORD_ID = "1001"
_RECORD_REF = "freee-hr://employees/1001/leave-balances"
_LEAVE_SUMMARY = "paid_holiday: 12.5 days remaining (3 taken)"
_CONFIRMATION = f"Retrieved leave balance '{_LEAVE_SUMMARY}' - ref={_RECORD_REF} - id={_RECORD_ID}"

# Fields the state reducer accumulates rather than replaces.
_ACCUMULATED = ("error_log", "node_history")


def _bearer() -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + "a" * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'employee':'A. Tanaka','token':'" + token + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state_after_a_successful_inner_run(**overrides) -> dict:
    """The outer state as it stands when post_process runs after a clean inner
    workflow: the answer is already merged into `result` and every domain
    field is populated."""
    state = {
        "status": AgentStatus.SUCCESS.value,
        "result": {"record_id": _RECORD_ID, "record_ref": _RECORD_REF, "confirmation": _CONFIRMATION},
        "record_id": _RECORD_ID,
        "record_ref": _RECORD_REF,
        "employee_id": _RECORD_ID,
        "leave_type": "paid_holiday",
        "leave_summary": _LEAVE_SUMMARY,
        "intent": "lookup_balance",
        "confirmation": _CONFIRMATION,
        "freee_hr_payload": to_json({"employee_id": _RECORD_ID}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "pb-envelope",
        "trace_id": "pb-envelope-trace",
        "node_history": ["initialize", "pre_process", "main"],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _state_after_a_failed_inner_run(**overrides) -> dict:
    """The outer state when the inner workflow reported an error after the
    freee HR call had resolved a record: the answer is still merged into
    `result`, and error_log carries the upstream failure text."""
    return _state_after_a_successful_inner_run(
        status=AgentStatus.ERROR.value,
        error_log=[_sentinel()],
        **overrides,
    )


def _merge(state: dict, partial: dict) -> dict:
    merged = dict(state)
    for key, value in partial.items():
        if key in _ACCUMULATED:
            merged[key] = list(merged.get(key, [])) + list(value)
        else:
            merged[key] = value
    return merged


def _run(state: dict) -> tuple[dict, dict]:
    """Run post_process over `state`; return (merged state, caller envelope)."""
    node = PostProcessNode()
    errored = state.get("status") == AgentStatus.ERROR.value
    partial = node.execute(state) if errored else node(state)
    merged = _merge(state, partial)
    return merged, FreeeHRLeaveAgent(config=load_runtime_config()).get_output(merged)


def _envelope(state: dict) -> dict:
    return _run(state)[1]


_REFUSED_STATES = [
    pytest.param(
        _state_after_a_successful_inner_run(freee_hr_payload=to_json({"leave_request": {"note": _bearer()}})),
        id="credential-nested-in-payload",
    ),
    pytest.param(
        _state_after_a_successful_inner_run(record_id="", record_ref=""),
        id="success-without-record-evidence",
    ),
]


class TestRefusedResponseIsContained:
    """A refusal must publish nothing of what it refused."""

    @pytest.mark.parametrize("state", _REFUSED_STATES)
    def test_envelope_carries_none_of_the_refused_response(self, state):
        envelope = _envelope(state)
        rendered = json.dumps(envelope, default=str)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert _bearer() not in rendered
        assert _CONFIRMATION not in rendered
        assert "Retrieved leave balance" not in rendered
        assert _LEAVE_SUMMARY not in rendered
        assert "freee-hr://" not in rendered
        assert "Traceback" not in rendered

    @pytest.mark.parametrize("state", _REFUSED_STATES)
    def test_reason_code_holds_the_payload_slot(self, state):
        """The payload must be non-empty: the envelope falls through to
        `result` on any falsy payload, so an empty payload would silently
        restore the answer this refusal exists to withhold - the failure mode
        looks identical to no fix at all."""
        envelope = _envelope(state)

        assert envelope["output"], "a falsy payload re-opens the `result` fallback"
        assert envelope["output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_refusal_names_the_location_in_error_log_and_never_in_the_envelope(self):
        """The violation entry names a place, not a value, and it stays
        internal: it is written to error_log and never enters the envelope."""
        state = _state_after_a_successful_inner_run(
            freee_hr_payload=to_json({"leave_request": {"note": _bearer()}}),
        )
        merged, envelope = _run(state)
        reported = " ".join(merged["error_log"])

        assert _bearer() not in reported
        assert "formatted_output['freee_hr_payload']['leave_request']['note']" in reported
        assert "freee_hr_payload" not in json.dumps(envelope, default=str)
        assert "output gate" not in json.dumps(envelope, default=str)


class TestErrorEnvelopeIsClosedSet:
    """On every non-success path the caller receives closed-set labels only."""

    @pytest.mark.parametrize(
        "state",
        [*_REFUSED_STATES, pytest.param(_state_after_a_failed_inner_run(), id="inner-workflow-error")],
    )
    def test_every_envelope_value_is_a_declared_constant(self, state):
        envelope = _envelope(state)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert set(envelope["output"]) == {"reason"}, envelope["output"]
        assert set(envelope["output"].values()) <= ERROR_REASONS, envelope["output"]

    def test_inner_error_envelope_carries_the_reason_code_only(self):
        """The inner workflow's error_log can carry upstream response text -
        names, identifiers, tokens. None of it, none of the record evidence,
        and none of the answer that was merged into `result`, reaches the
        caller."""
        merged, envelope = _run(_state_after_a_failed_inner_run())
        rendered = json.dumps(envelope, default=str)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert envelope["output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered
        assert _RECORD_REF not in rendered
        assert _LEAVE_SUMMARY not in rendered
        assert "Retrieved leave balance" not in rendered
        # The internal channel still has the line (once - not re-emitted).
        assert merged["error_log"].count(_sentinel()) == 1

    def test_caller_derived_text_never_reaches_the_envelope(self):
        """Caller-derived text lands in the assembled payload as a VALUE; a
        refusal that echoed anything of the payload would publish it."""
        note = "A. Tanaka <a.tanaka@" + "example.com> " + _bearer()
        state = _state_after_a_successful_inner_run(
            freee_hr_payload=to_json({"leave_request": {"employee_id": _RECORD_ID, "note": note}}),
        )
        merged, envelope = _run(state)
        rendered = json.dumps(envelope, default=str)

        assert envelope["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert "Tanaka" not in rendered
        assert "@example" not in rendered
        assert _bearer() not in rendered
        assert "Tanaka" not in " ".join(merged["error_log"])


class TestUnrefusedResponseStillShips:
    """CONTROL: a clean response is delivered intact - otherwise every
    containment assertion above would pass vacuously."""

    def test_clean_response_carries_the_answer(self):
        envelope = _envelope(_state_after_a_successful_inner_run())

        assert envelope["status"] == AgentStatus.SUCCESS.value
        out = envelope["output"]
        assert out["record_id"] == _RECORD_ID
        assert out["record_ref"] == _RECORD_REF
        assert out["leave_summary"] == _LEAVE_SUMMARY
        assert out["intent"] == "lookup_balance"
        assert out["confirmation"] == _CONFIRMATION

    def test_clean_response_carries_no_reason_code(self):
        envelope = _envelope(_state_after_a_successful_inner_run())
        rendered = json.dumps(envelope, default=str)

        assert "reason" not in envelope["output"]
        assert not any(reason in rendered for reason in ERROR_REASONS)
