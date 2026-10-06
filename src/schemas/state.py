"""AgentCore Platform v1.0 - CMN-C2-278 freee HR Leave Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects (and nested dict/list
# containers) are not msgpack-safe. Extend AgentState with agent-specific
# fields only, and declare every domain field NotRequired[...] (fields are
# absent until their producer node writes them). freee_hr_payload /
# freee_hr_config / redaction_flags / caller_fields are dicts/lists at the
# point of use but are stored in State as JSON strings via to_json/from_json
# below. Do NOT add credentials, secrets, or Pydantic models. The freee HR
# integration token is NEVER stored here - it is read via ctx.secrets in
# CallFreeeHrApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """freee HR Leave agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only freee-HR-workflow fields are added below, all NotRequired (the state
    contract). All values are JSON/msgpack-serializable primitives - the freee
    HR integration token is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target hints (employee number / leave-request id from
    # input_context). Never inferred; resolution is explicit-only -
    # pass-through when the hint or the request text already carries the
    # identifier.
    employee_hint: NotRequired[str]
    request_hint: NotRequired[str]
    employee_id: NotRequired[str]  # resolved freee HR employee number

    # The VALIDATED caller contract, as produced by PreProcessNode. Stored as a
    # JSON string (msgpack-safe) and read back at the graph boundary, where it
    # crosses into the inner graph over the context bridge.
    caller_fields: NotRequired[Optional[str]]

    # ValidateInput (deterministic sensitive-value scan)
    # JSON list[str] of pattern categories redacted from the text before
    # logging (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferFreeeHrFields
    leave_type: NotRequired[str]  # leave category (paid_holiday / sick_leave / half_day)
    # JSON - assembled freee HR REST API v1 request body (stored as a JSON
    # string, not a native dict; (de)serialize via to_json/from_json).
    freee_hr_payload: NotRequired[Optional[str]]

    # The `freee_hr:` section of config/config.yaml, forwarded by
    # _parent_config() and injected by the inner graph's
    # _extra_initial_state() (JSON string).
    freee_hr_config: NotRequired[Optional[str]]

    # CallFreeeHrApi
    record_id: NotRequired[str]  # leave-request id / employee number returned by freee HR
    record_ref: NotRequired[str]  # human-readable reference (freee-hr://leave-requests/<id>)
    leave_summary: NotRequired[str]  # human-readable balance/status summary

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
