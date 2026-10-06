# CMN-C2-278 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust -> input gate ->
# execute -> credential scan); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The domain
# output gate is the MODULE-LEVEL _security_gate_output() helper (the framework
# gate methods are @final and the real SDK auto-wraps _extra_ hooks), so the
# helper is also unit-tested directly as a plain function.
#
# The errored-state branch is driven through execute() DIRECTLY where noted:
# BaseNode.__call__ short-circuits on an incoming errored state and the
# backbone routes an error straight to finalize, so that branch is only
# reachable from inside - and it must still be a closed set.
#
# Credential-shaped strings are assembled at runtime so no credential-shaped
# literal is committed to the repository.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _CLEARED_ON_ERROR,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    _UNNAMEABLE_KEY,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json

_BEARER_LIKE = "Bearer " + "a" * 24
_JWT_LIKE = "eyJ" + "b" * 20
_SK_LIKE = "sk-" + "c" * 24


def _bearer(fill: str = "a") -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + fill * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'employee':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in a returned mapping.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "1001",
        "record_ref": "freee-hr://employees/1001/leave-balances",
        "leave_type": "paid_holiday",
        "leave_summary": "paid_holiday: 12.5 days remaining (3 taken)",
        "intent": "lookup_balance",
        "confirmation": "Retrieved leave balance 'paid_holiday: 12.5 days remaining (3 taken)' - id=1001",
        "freee_hr_payload": to_json({"employee_id": "1001"}),
        "result": {"record_id": "1001", "confirmation": "ok"},
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "1001"
        assert out["record_ref"] == "freee-hr://employees/1001/leave-balances"
        assert out["leave_type"] == "paid_holiday"
        assert out["intent"] == "lookup_balance"
        assert out["confirmation"].startswith("Retrieved leave balance")
        # Round-trip: the JSON freee_hr_payload string surfaces parsed.
        assert out["freee_hr_payload"] == {"employee_id": "1001"}
        assert "reason" not in out

    def test_status_is_plain_string_not_enum(self):
        """State status must be the `.value` string, never a bare AgentStatus
        enum member (str-enum equality masks the difference in `==` asserts, so
        pin the concrete type here)."""
        result = self.node(_state())
        assert result["status"].__class__ is str
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallFreeeHrApiNode: freee HR API error 403"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "freee HR API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """A SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("record_id/record_ref" in entry for entry in result["error_log"])


class TestViolationIsContained:
    """A violating gate must CLEAR the output-bearing fields.

    The response envelope falls back to state["result"] even on an error
    status, so returning an error without clearing would still ship the
    un-gated inner answer inside the error envelope. The errored-state cases
    drive execute() directly, because BaseNode.__call__ short-circuits on an
    incoming errored state and would never reach this node's own branch.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def test_credential_in_a_nested_payload_field_clears_every_output_field(self):
        """The realistic leak: the credential rides inside the nested request
        body, not in a top-level string. A gate that scanned only top-level
        values would report ZERO findings here."""
        state = _state(freee_hr_payload=to_json({"leave_request": {"note": _BEARER_LIKE}}))
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] is None
        for field in ("confirmation", "leave_summary", "leave_type", "record_ref", "record_id", "employee_id"):
            assert not result[field], field
        assert _BEARER_LIKE not in str(result)

    def test_top_level_control_proves_the_probe_itself_works(self):
        """The nested probe alone cannot distinguish "gate is blind" from
        "probe is wrong" - so the same content is also pushed through a
        top-level field, where it must be caught too."""
        result = self.node.execute(_state(confirmation=_BEARER_LIKE))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_credential_in_a_mapping_key_is_caught(self):
        state = _state(freee_hr_payload=to_json({_JWT_LIKE: "value"}))
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_credential_in_a_nested_list_is_caught(self):
        state = _state(freee_hr_payload=to_json({"notes": ["fine", [_SK_LIKE]]}))
        assert self.node.execute(state)["status"] == AgentStatus.ERROR.value

    def test_credential_in_error_log_never_reaches_the_caller(self):
        """A remote 4xx body would arrive in error_log. The error path
        publishes the reason code only, so the body cannot reach the caller
        through it - and the inner entries are not re-emitted either.

        execute() is called DIRECTLY: BaseNode.__call__ short-circuits on an
        already-errored state, so this branch is only reachable from inside."""
        state = _state(status=AgentStatus.ERROR.value, error_log=[f"call failed: {_BEARER_LIKE}"])
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert _BEARER_LIKE not in json.dumps(result, default=str)
        assert "error_log" not in result

    def test_block_names_the_location_never_the_value(self):
        result = self.node.execute(_state(confirmation=_BEARER_LIKE))
        rendered = "\n".join(result["error_log"])
        assert "formatted_output['confirmation']" in rendered
        assert _BEARER_LIKE not in rendered

    def test_clean_output_is_not_cleared(self):
        result = self.node.execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["record_id"] == "1001"

    def test_domain_gate_must_fire_before_the_framework_scan_does(self, monkeypatch):
        """Why this gate has to recognise every credential family the framework
        does, rather than deferring to it.

        Driven through the full node call. With the domain gate silenced, the
        framework's own scan catches the same value and RAISES - and a raise is
        not containment: the node returns no cleared fields, so the response
        falls back to the ungated inner result, and the error text it does
        return carries a traceback with absolute source paths. With the domain
        gate active the same input yields cleared fields and a short message.
        """
        import src.nodes.post_process_node as module

        state = _state(record_id=_JWT_LIKE, record_ref=f"freee-hr://leave-requests/{_JWT_LIKE}")

        blocked = self.node(dict(state))
        assert blocked["status"] == AgentStatus.ERROR.value
        assert blocked["result"] is None
        assert blocked["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        rendered = "\n".join(blocked["error_log"])
        assert "Traceback" not in rendered
        assert "/src/" not in rendered

        monkeypatch.setattr(module, "_security_gate_output", lambda fo, is_success: [])
        unguarded = self.node(dict(state))
        assert unguarded["status"] == AgentStatus.ERROR.value
        # Nothing was cleared, so the outer state keeps the ungated answer.
        assert "result" not in unguarded
        assert "Traceback" in "\n".join(unguarded["error_log"])


class TestSecurityGateOutputHelper:
    """The module-level domain gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {
                "record_id": "1001",
                "record_ref": "freee-hr://employees/1001/leave-balances",
                "confirmation": "ok",
            },
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    @pytest.mark.parametrize("credential", [_BEARER_LIKE, _JWT_LIKE, _SK_LIKE, "AKIA" + "D" * 16])
    def test_blocks_every_credential_family_the_framework_recognises(self, credential):
        violations = _security_gate_output({"record_id": "1001", "note": credential}, is_success=True)
        assert any("note" in v for v in violations)

    def test_credential_shaped_mapping_key_is_caught_and_never_repeated_in_the_label(self):
        """The label is built from mapping keys, so a credential-shaped key
        would otherwise be quoted by every violation reported beneath it - and
        that label travels in error_log, where the framework's credential scan
        raises and discards the cleared result. The key is withheld instead."""
        formatted = {"record_id": "1001", "freee_hr_payload": {_JWT_LIKE: {"note": _bearer()}}}
        violations = _security_gate_output(formatted, is_success=True)
        assert len(violations) == 2
        assert all(_JWT_LIKE not in v for v in violations)
        assert all(_bearer() not in v for v in violations)
        assert any(v.endswith(f"formatted_output['freee_hr_payload']['{_UNNAMEABLE_KEY}']['note']") for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        assert _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False) == []

    def test_ordinary_identifiers_and_day_counts_are_untouched(self):
        """The counter-direction: nothing in a legitimate leave response looks
        like a credential to this gate."""
        violations = _security_gate_output(
            {
                "record_id": "1001",
                "record_ref": "freee-hr://leave-requests/lr-1a2b3c4d",
                "leave_summary": "paid_holiday: 12.5 days remaining (3 taken)",
                "freee_hr_payload": {"leave_request": {"employee_id": "1001", "start_date": "2026-09-01"}},
            },
            is_success=True,
        )
        assert violations == []


# Every non-success path this node has. `via` says how the path is reached:
# node(state) runs the framework pipeline; execute() is used for an errored
# state because BaseNode.__call__ short-circuits on it (and the backbone routes
# an error straight to finalize), so that branch is only reachable from inside.
_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "record_id": "", "record_ref": ""},
        "execute",
        id="inner-workflow-error",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "result": {"record_id": "1001", "confirmation": "Retrieved leave balance 'paid_holiday: 12.5 days'"},
        },
        "execute",
        id="inner-workflow-error-with-answer-in-result",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "error_log": [_sentinel(), "CallFreeeHrApiNode: rejected " + _bearer()],
        },
        "execute",
        id="inner-workflow-error-with-credential-in-error-log",
    ),
    pytest.param({"record_id": "", "record_ref": ""}, "call", id="gate-missing-record-evidence"),
    pytest.param(
        {"freee_hr_payload": to_json({"leave_request": {"note": _bearer()}})},
        "call",
        id="gate-credential-nested-in-payload",
    ),
    pytest.param(
        {"freee_hr_payload": to_json({"leave_request": {"sk-" + "a" * 20: _bearer()}})},
        "call",
        id="gate-credential-shaped-mapping-key",
    ),
]


class TestErrorEnvelopeIsClosedSet:
    """Whatever the non-success path, the caller-visible envelope is made of
    this module's own constants - never of node-authored text.

    error_log is seeded with a recognisable sentinel on every path: a name and
    a credential-shaped token inside an echoed upstream body, which is exactly
    what an API-call failure can put there. Truncating or redacting such a line
    is not a closed set, so it must appear nowhere in what the node returns.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, overrides: dict, via: str) -> dict:
        state = _state(**{"error_log": [_sentinel()], **overrides})
        return self.node.execute(state) if via == "execute" else self.node(state)

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, overrides, via):
        result = self._drive(overrides, via)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_stays_truthy_and_the_answer_is_cleared(self, overrides, via):
        """`formatted_output or result`: a falsy envelope re-opens the fallback,
        and a surviving `result` is what it would fall back onto."""
        result = self._drive(overrides, via)

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        assert result["result"] is None
        cleared = {field: result[field] for field in _CLEARED_ON_ERROR if field != "result"}
        assert not any(cleared.values()), cleared

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, overrides, via):
        result = self._drive(overrides, via)

        leaves = list(_leaves(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)
        rendered = json.dumps(result, default=str)
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered

    def test_the_reason_names_the_path_taken(self):
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert errored["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert withheld["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; re-emitting the inner entries
        would duplicate every line, and the caller never sees them anyway."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))

        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "output gate" not in json.dumps(result["formatted_output"])

    def test_a_caller_derived_value_lands_in_a_value_and_the_label_names_fixed_keys_only(self):
        """The assembled payload's mapping keys are fixed by the field-inference
        step; anything caller-derived lands as a VALUE. So the violation label
        is built from fixed keys and indices and cannot carry caller text - and
        the caller receives the reason code only, so it could not reach them
        through the envelope anyway."""
        note = "A. Tanaka <a.tanaka@" + "example.com> " + _bearer()
        payload = {"leave_request": {"employee_id": "1001", "note": note}}
        result = self.node(_state(freee_hr_payload=to_json(payload), error_log=[_sentinel()]))

        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        label = " ".join(result["error_log"])
        assert "formatted_output['freee_hr_payload']['leave_request']['note']" in label
        assert "Tanaka" not in label
        assert "@example" not in label
        assert _bearer() not in label

    def test_a_credential_shaped_mapping_key_is_withheld_from_the_label(self):
        """A key that is itself credential-shaped is refused AND kept out of
        the label: quoted, it would travel in error_log, where the framework
        credential scan raises on the node result and replaces the cleared
        result - restoring the leak the clearing just closed. The clearing
        surviving the full node call is the proof that nothing raised."""
        credential_shaped_key = "sk-" + "a" * 20
        payload = {"leave_request": {credential_shaped_key: _bearer()}}
        result = self.node(_state(freee_hr_payload=to_json(payload)))

        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] is None
        assert credential_shaped_key not in json.dumps(result, default=str)
        assert any(_UNNAMEABLE_KEY in entry for entry in result["error_log"])
        assert "Traceback" not in json.dumps(result, default=str)
