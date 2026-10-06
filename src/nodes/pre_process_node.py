"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: validate the caller's request (raw NL text + the
structured caller fields) and serialize it into `validated_input` for the inner
freee HR workflow graph. This node OWNS the caller-data contract: every field a
caller can supply is checked here, against explicit bounds, before any of it
reaches the workflow. Business rules (intent, entity extraction) live in the
inner graph.

Caller contract (`input_context`), every field optional:

    employee_id / employee_number / employee_hint   target employee number
    request_id / request_number / request_hint      target leave-request id

Rules applied to it:
  - the value must be a STRING or an INTEGER. A float, boolean, mapping or
    list is refused outright, never coerced: str(float("nan")) is "nan" and
    str(1e9) is "1000000000.0", so coercion would let a non-finite or
    unbounded value name the target of a leave submission. An integer is
    accepted because an employee number is often a bare number on the wire,
    but it goes through the same finite + in-range check as everything else.
  - the value must match the identifier SHAPE - a short alphanumeric token
    (letters, digits, `_`, `-`) within its documented length. The identifier
    renders into the caller-facing confirmation and into the `freee-hr://`
    reference, so anything but an inert identifier there would be
    caller-controlled output.
  - instruction-override text (directives aimed at the MODEL: role
    reassignment, system-prompt manipulation, chat-template control tokens) is
    REFUSED, fail closed, on BOTH caller text channels - the raw instruction
    text and every decoded string in input_context, keys included, at any
    depth - before anything is carried forward. The screen is the template's
    own (src/services/security.py), never delegated to the framework gate.
  - a refusal names the FIELD and never echoes the offending value; a hostile
    field NAME is masked, never echoed either.
  - an absent hint is simply absent: the workflow falls back to an identifier
    named in the instruction text, and an unresolvable target is a clean error
    rather than a guess.
"""

import json
import math
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import contains_instruction_override, sanitize_query

# A freee HR employee number / leave-request id: a short alphanumeric token.
# The same shapes the inner field-inference step applies to identifiers named
# in the request text, so both channels resolve to the same alphabet.
_EMPLOYEE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
_REQUEST_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$")

# A bare-numeric identifier supplied as an int must still be a plausible
# employee/record number rather than an arbitrary magnitude.
_NUMERIC_MIN = 0
_NUMERIC_MAX = 9_999_999_999

# Aliases accepted for each target, first supplied wins.
_EMPLOYEE_ALIASES = ("employee_id", "employee_number", "employee_hint")
_REQUEST_ALIASES = ("request_id", "request_number", "request_hint")


class CallerFieldError(ValueError):
    """A caller-supplied field failed its contract. Carries the field name only."""


def _parses_as_non_finite(text: str) -> bool:
    """True for the nan/inf spellings float() accepts (any case, signed)."""
    try:
        return not math.isfinite(float(text))
    except (TypeError, ValueError):
        return False


# Contract field names that may be echoed into a refusal message. Any other
# input_context key is caller-controlled text, so its spot in the reported path
# shows a placeholder - a hostile field NAME is never echoed either.
_KNOWN_CONTEXT_FIELDS = frozenset(_EMPLOYEE_ALIASES + _REQUEST_ALIASES)


def _find_instruction_override(value: object, path: str = "input_context") -> "str | None":
    """Depth-first scan of every decoded string in the mapping - keys included.

    Returns the path of the first string carrying an instruction-override
    directive, or None. The walk runs on the PARSED mapping, so JSON escaping
    cannot smuggle a phrase past it, and it covers undeclared keys too: the
    screen must hold on what the caller SENT, not only on what the contract
    keeps. Path components outside the declared contract are masked, so the
    returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return path if contains_instruction_override(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in _KNOWN_CONTEXT_FIELDS else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}"
            if isinstance(key, str) and contains_instruction_override(key):
                return key_path
            found = _find_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _validate_identifier(raw: object, field: str, shape: "re.Pattern[str]") -> str:
    """Validate a target reference: a finite, in-range, inert identifier.

    Fails CLOSED on every other shape. bool is rejected explicitly because
    `isinstance(True, int)` is True in Python, and a float is rejected outright
    rather than truncated - NaN and Infinity parse fine and then compare False
    against any bound, which is exactly how a non-finite value slips through a
    range check and ends up naming the employee a leave request is filed for.
    """
    if isinstance(raw, bool):
        raise CallerFieldError(f"'{field}' must be an identifier")
    if isinstance(raw, int):
        if not math.isfinite(raw) or not (_NUMERIC_MIN <= raw <= _NUMERIC_MAX):
            raise CallerFieldError(f"'{field}' is out of range")
        text = str(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise CallerFieldError(f"'{field}' must not be empty")
        # "NaN" / "Infinity" satisfy the identifier alphabet, so the shape check
        # alone would admit them. They are refused here as well: this field is
        # compared as a string and nothing downstream would divide by it, so
        # this is defence in depth rather than a live fail-open - but a value
        # that is a non-finite number in every other reading has no business
        # naming the employee a leave request is filed for, and admitting it
        # would leave the classic non-finite probe answered by an argument
        # instead of a refusal. Only the literal nan/inf spellings are affected;
        # an ordinary code like "NANO" does not parse as a float.
        if _parses_as_non_finite(text):
            raise CallerFieldError(f"'{field}' must not be a non-finite number")
    else:
        # float included: a non-integral or non-finite value can never be an
        # identifier, and math.isfinite() would still admit 1e9.
        raise CallerFieldError(f"'{field}' must be an identifier")
    if not shape.match(text):
        raise CallerFieldError(f"'{field}' is not a valid identifier")
    return text


def validate_caller_fields(input_context: object) -> "dict[str, str]":
    """Validate the caller contract. Raises CallerFieldError on any breach."""
    if input_context in (None, {}):
        return {}
    if not isinstance(input_context, dict):
        raise CallerFieldError("'input_context' must be a mapping")

    fields: dict[str, str] = {}
    for target, aliases, shape in (
        ("employee_hint", _EMPLOYEE_ALIASES, _EMPLOYEE_SHAPE_RE),
        ("request_hint", _REQUEST_ALIASES, _REQUEST_SHAPE_RE),
    ):
        supplied = [alias for alias in aliases if input_context.get(alias) is not None]
        if supplied:
            fields[target] = _validate_identifier(input_context[supplied[0]], supplied[0], shape)
    return fields


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner freee HR call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ────────
        # Runs on BOTH caller text channels before anything is carried forward:
        # a refusal leaves no validated_input, no hints and no caller_fields
        # for any downstream node. The screen lives in this node's own
        # execute() path - calling execute() directly still refuses, so the
        # guarantee does not depend on any framework gate being present or
        # configured on.
        if contains_instruction_override(user_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": "user_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request refused - instruction-override content in user_input"],
            }

        override_path = _find_instruction_override(input_context) if isinstance(input_context, (dict, list)) else None
        if override_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": override_path},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused - instruction-override content in {override_path}"],
            }

        try:
            caller_fields = validate_caller_fields(input_context)
        except CallerFieldError as exc:
            # Fail closed, naming the field only - the rejected value is never
            # echoed into the log.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - {exc}"],
            }

        # Strip markup + cap length before serialization.
        sanitized_input = sanitize_query(user_input.strip())

        employee_hint = caller_fields.get("employee_hint", "")
        request_hint = caller_fields.get("request_hint", "")
        validated_input = json.dumps({"text": sanitized_input})

        # Audit the shaped request - hint presence only, never the raw text.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_employee_hint": bool(employee_hint),
                "has_request_hint": bool(request_hint),
                "caller_fields": sorted(caller_fields),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "employee_hint": employee_hint,
            "request_hint": request_hint,
            # Stored as a JSON string (the state contract keeps every value
            # msgpack-safe); the graph node reads it back at the boundary.
            "caller_fields": to_json(caller_fields),
            "status": AgentStatus.SUCCESS.value,
        }
