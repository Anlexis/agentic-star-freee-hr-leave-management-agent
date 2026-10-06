"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input, re-applies the template's own
instruction-override screen to the text that actually arrived, and runs a
deterministic (regex, NOT LLM) scan of the inbound text for email addresses /
access-token-like strings, which are flag-and-redacted before anything is
logged. A leave-management request legitimately names an employee, so that
second scan is flag-and-redact for safe logging, not a hard reject. The hard
rejections are the empty / non-request guard and the override screen.

The override screen runs here as well as on the outer backbone because this is
the node that owns the text the rest of the inner workflow consumes: it holds
whether or not any framework gate sits in front of it, and whether or not the
inner graph is driven directly.

The caller contract (employee / leave-request hints) arrives on the inner
state's `input_context`, seeded by the graph's _extra_initial_state() from the
context bridge - the framework does not forward input_context into a subgraph.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import contains_instruction_override, redact_sensitive

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3

# Caller-contract keys carried across the graph boundary.
_EMPLOYEE_HINT_KEY = "employee_hint"
_REQUEST_HINT_KEY = "request_hint"


class ValidateInputNode(FunctionNode):
    """Validate + flag-and-redact the inbound leave-management request."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct inner invocation.
        text = raw
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                text = obj.get("text", "")

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        if contains_instruction_override(text):
            emit_trace_event(
                "validate_input_refused",
                {"reason": "instruction_override"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: request refused - instruction-override content"],
            }

        # The validated caller contract, seeded onto the inner state by the
        # graph's _extra_initial_state(). Already bounds-checked by
        # PreProcessNode; a direct inner invoke may also pass it explicitly.
        caller_context = state.get("input_context") or {}
        employee_hint = str(caller_context.get(_EMPLOYEE_HINT_KEY, "") or state.get(_EMPLOYEE_HINT_KEY, "") or "")
        request_hint = str(caller_context.get(_REQUEST_HINT_KEY, "") or state.get(_REQUEST_HINT_KEY, "") or "")

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        redacted, flags = redact_sensitive(text)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_employee_hint": bool(employee_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "employee_hint": employee_hint,
            "request_hint": request_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
