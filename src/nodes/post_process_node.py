"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner freee HR workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the SDK auto-wraps `_extra_` hooks (which breaks the
.invoke() chain), so domain checks live in a module-level helper invoked inline.

This template renders no monetary aggregates: its caller-facing output is an
employee number, a `freee-hr://` reference, a leave-type label, a day-count
summary and a confirmation sentence, so a monetary rounding grid has nothing to
enforce here. The output invariant it owns instead is:

  1. A SUCCESS response always carries record evidence (record_id/record_ref) -
     never a success envelope that misrepresents what happened in freee HR.
  2. No credential material leaves the agent, anywhere in the output.

The gate walks the WHOLE output structure, not just its top-level string
values: `freee_hr_payload` is a nested mapping whose entries carry
caller-derived text, so a scan that only looked at top-level strings would step
straight past a credential sitting in a request field. Keys are scanned as well
as values.

EVERY non-success return - a gate violation AND a pre-existing inner-workflow
error - goes through the one module-level `_contain()` helper. It CLEARS every
output-bearing state field, not just the status: the response envelope reads
`formatted_output` falling back to `result` with no status check, so a bare
error status would still ship the un-gated inner answer inside the error
envelope. The replacement `formatted_output` is a truthy mapping - an
empty/falsy value would activate the `result` fallback it exists to prevent.

What the ERROR envelope may say: closed-set labels only. It carries a constant
reason code chosen by this module (one of `ERROR_REASONS`) and nothing else -
never `error_log`, never the gate's violation entries, never any other
node-authored text. Those lines can embed upstream response text (an API error
body), identifiers, names or caller-derived fragments, and truncating or
redacting them is not a closed set. `error_log` stays the INTERNAL channel: the
state reducer appends to it and the audit trail needs it; it is simply never
projected to the caller. Gate violations are written to `error_log` naming the
offending PATH (fixed keys and indices, never the value - and a
credential-shaped mapping key is withheld from the label rather than quoted),
and the audit event carries a count.

No error envelope carries freee HR record evidence either. `record_id` /
`record_ref` are this agent's WRITE EVIDENCE - the SUCCESS branch of the gate
below REFUSES an output that lacks them - so returning them under an ERROR
status would tell a caller being told the operation failed that a leave
request was nonetheless filed, and whose it is. freee HR is an HR system: the
employee number, the leave type and the balance summary are personal data.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller. The framework's
# own credential scan covers the same families, but it RAISES on a finding and
# does not scan mapping KEYS - so this gate has to recognise every family the
# framework does, or a value it catches and this one misses would be raised
# past the clearing below and ship inside the error envelope.
_CREDENTIAL_LIKE_RE = re.compile(
    r"sk_(?:live|test)_[A-Za-z0-9]{16,}"  # payment-provider secret keys
    r"|sk-[A-Za-z0-9]{20,}"  # generic sk- API keys
    r"|eyJ[A-Za-z0-9._-]{10,}"  # JWTs
    r"|AKIA[A-Z0-9]{16}"  # cloud IAM access key ids
    r"|Bearer\s+[A-Za-z0-9._-]{16,}"  # HTTP bearer tokens
    r"|(?:postgresql|mysql|mongodb|redis)://\S{8,}"  # connection strings
)

# Stand-in for a mapping key that cannot itself be written into a path label.
_UNNAMEABLE_KEY = "<withheld>"

# The content must be cleared from EVERY output-bearing state field on EVERY
# error return - the gate-violation path and the inner-workflow error path
# alike. The response envelope reads formatted_output/result even on an error
# status, and the other fields feed downstream formatting and the checkpoint.
# Omitting a field from one envelope is NOT clearing it: a later reader of the
# same state picks it straight back up. The formatted_output replacement is
# built by _contain() (truthy, closed set).
_CLEARED_ON_ERROR: "dict[str, Any]" = {
    "result": None,
    "confirmation": "",
    "freee_hr_payload": None,
    "leave_summary": "",
    "leave_type": "",
    "intent": "",
    # freee HR record evidence. Cleared alongside the content fields so the
    # identifiers cannot be picked up out of state by a checkpoint or a
    # downstream reader after the caller has been told the operation failed.
    "record_ref": "",
    "record_id": "",
    "employee_id": "",
}

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which record and never in whose words.
_REASON_WORKFLOW_FAILED = "freee_hr_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """The node result for ANY non-success outcome - the single error shape.

    Error status, every output-bearing field cleared (_CLEARED_ON_ERROR), and
    an envelope made of closed-set labels only: `reason` is one of
    ERROR_REASONS. `new_errors` (gate violations - path labels only) are
    appended to `error_log`, the internal channel the state reducer
    accumulates, and never enter the envelope. Nothing is read out of state:
    not the record, not `error_log`.

    The constant `reason` key keeps the mapping TRUTHY, so the framework's
    `formatted_output or result` projection (AgentBaseGraph.get_output()
    applies no status check) serves this envelope and never whatever survived
    in `result`.
    """
    contained: dict[str, Any] = dict(_CLEARED_ON_ERROR)
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


def _walk_strings(value: object, path: str) -> "list[tuple[str, str]]":
    """Yield every (path, string) in the output, however deeply it is nested.

    Mappings, sequences and bare strings are all reachable representations of a
    caller-facing value, so all three are walked - keys included, since a
    mapping key renders just as visibly as its value.

    A credential-shaped KEY is reported as a violation by the caller of this
    walk, so the path label must not repeat it: the label travels in
    error_log, where the framework's own credential scan would raise on it
    and replace the cleared result around it with a bare error - restoring
    the very leak the clearing closed.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            label = str(key)
            if isinstance(key, str):
                found.append((f"{path}(key)", key))
                if _CREDENTIAL_LIKE_RE.search(key):
                    label = _UNNAMEABLE_KEY
            found.extend(_walk_strings(item, f"{path}['{label}']"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{path}[{index}]"))
    return found


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no record evidence (record_id/record_ref),
        which would misrepresent the freee HR action outcome to the caller;
      - any credential-shaped string ANYWHERE in the caller-facing output,
        including inside nested payload mappings and lists.

    A violation names the offending PATH, never the value. Violations are
    error_log entries (internal); they never reach the caller.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    for path, text in _walk_strings(formatted_output, "formatted_output"):
        if _CREDENTIAL_LIKE_RE.search(text):
            # Name the location, never the matched value.
            problems.append(f"PostProcess output gate: credential-like value in {path}")
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it: error_log already carries the inner
        # entries (the state reducer appends, so re-emitting them here would
        # duplicate every line) and the caller receives the reason code only.
        # The delta clears every output-bearing field, so the identifiers and
        # the leave data cannot be recovered from the checkpoint or by a
        # downstream reader either.
        if state.get("status") == AgentStatus.ERROR.value:
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for HR record content or error text.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "errors": len(state.get("error_log", []))},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "leave_type": state.get("leave_type", ""),
            "leave_summary": state.get("leave_summary", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "freee_hr_payload": from_json(state.get("freee_hr_payload"), {}),
        }

        # Domain output gate (module-level helper - see module docstring). A
        # refusal is contained the same way as an inner error: the violations
        # go to error_log only, the caller receives the reason code only.
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            emit_trace_event(
                "post_process_blocked",
                {"reason": _REASON_OUTPUT_WITHHELD, "violations": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
