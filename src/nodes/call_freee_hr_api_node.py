"""AgentCore Platform v1.0 - inner workflow Step 4: CallFreeeHrApi (tool side-effect).

Performs the balance-lookup / submit / status-check call against the freee HR
REST API v1 endpoints via src/services/freee_hr_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Secrets: the integration token is read via
       ctx.secrets.get("FREEE_HR_ACCESS_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored
       in state. While the deterministic NETWORK-FREE stub transport is active
       a missing token is tolerated (a sentinel placeholder is used - it is
       never sent anywhere because no request leaves the process); with a LIVE
       transport injected, a missing token is a hard status=error - a real API
       is never called unauthenticated. The key is read with .get() rather than
       .require(), so it is not declared as a compile-time requirement in the
       manifest: a declared-but-unprovisioned secret would fail the agent at
       compile time, which would make the network-free default unusable.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external HR system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass).

Response handling: the values this node lifts out of a freee HR response are
rendered to the caller, so they are treated as untrusted. Identifiers and
status labels must match an inert identifier shape, and the day counts must be
finite and in range - a NaN or an Infinity parses fine and then compares False
against every bound, which would put a meaningless balance in front of the
person deciding whether to take leave. Anything outside those bounds is a
clean status=error, never a rendered guess.

Error reasons carry CLOSED-SET labels only - what was not found, the HTTP
status, the exception type - never the employee number, the leave content, or an
upstream freee HR response body (unbounded third-party text that can quote the
very record it refused). `error_log` is the INTERNAL channel: the state reducer
accumulates it and the audit trail reads it; post_process never projects it to
the caller (the caller-visible error is a constant reason code). The reasons
stay closed-set all the same, so the internal channel is safe to log and
correlate on without a redaction pass.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). freee HR settings (base_url, company_id) arrive as the JSON
`freee_hr_config` state field - injected by the inner graph's
_extra_initial_state() from the config/config.yaml section forwarded by
FreeeHrWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["freee_hr"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

import math
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.freee_hr_client import FreeeHrApiError, FreeeHrClient

_SECRET_KEY = "FREEE_HR_ACCESS_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

# Shapes every response-derived value that reaches the caller must satisfy.
_RECORD_ID_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_LABEL_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_ -]{0,39}$")
# Leave balances are days: a year's entitlement plus carry-over, generously bounded.
_DAYS_MIN = 0.0
_DAYS_MAX = 10_000.0


def _finite_in_range(raw: object, low: float, high: float) -> "float | None":
    """Parse a response number that must be finite and within range.

    Returns None (the caller then fails closed) for bools, non-numerics and
    non-finite values. bool is rejected first because isinstance(True, int) is
    True in Python; NaN and Infinity are rejected explicitly because float()
    accepts both and every subsequent comparison against them is False.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        number = float(raw)
    elif isinstance(raw, str):
        try:
            number = float(raw.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not math.isfinite(number) or not (low <= number <= high):
        return None
    return number


def _inert(raw: object, shape: "re.Pattern[str]") -> str:
    """Return the value when it is already an inert identifier, else ""."""
    text = str(raw or "").strip()
    return text if shape.match(text) else ""


class CallFreeeHrApiNode(FunctionNode):
    """Look up a leave balance / submit a leave request / check a request status."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: "dict[str, Any] | None" = None) -> dict[str, Any]:
        payload = from_json(state.get("freee_hr_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallFreeeHrApiNode: missing freee_hr_payload"],
            }

        intent = state.get("intent", "lookup_balance") or "lookup_balance"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["freee_hr"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("freee_hr_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("freee_hr") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = FreeeHrClient(base_url=base_url) if base_url else FreeeHrClient()
        company_id = str(settings.get("company_id", "") or "").strip()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallFreeeHrApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        employee_id = state.get("employee_id", "") or str(payload.get("employee_id", "") or "")
        leave_type = state.get("leave_type", "") or ""

        try:
            if intent == "lookup_balance":
                if not employee_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallFreeeHrApiNode: unresolved employee number - " "cannot look up leave balance"
                        ],
                    }
                resp = client.get_leave_balance(employee_id, api_token, company_id) or {}
                balances = resp.get("leave_balances") or []
                if not balances:
                    # The reason names WHAT was not found (a closed-set label),
                    # never for whom: error_log is logged and correlated on,
                    # and interpolating the employee number would put the
                    # record evidence into that channel.
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeHrApiNode: no leave balance found for the requested employee"],
                    }
                first = balances[0]
                record_id = _inert(resp.get("employee_id", ""), _RECORD_ID_SHAPE_RE) or employee_id
                record_ref = f"freee-hr://employees/{record_id}/leave-balances"
                balance_label = _inert(first.get("leave_type", ""), _LABEL_SHAPE_RE)
                leave_type = leave_type or balance_label
                remaining = _finite_in_range(first.get("remaining_days"), _DAYS_MIN, _DAYS_MAX)
                taken = _finite_in_range(first.get("taken_days"), _DAYS_MIN, _DAYS_MAX)
                if remaining is None or taken is None:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeHrApiNode: leave balance day counts are missing or out of range"],
                    }
                leave_summary = f"{balance_label or 'leave'}: " f"{remaining:g} days remaining " f"({taken:g} taken)"
            elif intent == "submit_request":
                request = payload.get("leave_request") or {}
                if not employee_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallFreeeHrApiNode: unresolved employee number - " "cannot submit leave request"
                        ],
                    }
                if not str(request.get("start_date", "") or "").strip():
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallFreeeHrApiNode: unresolved leave start date - " "cannot submit leave request"
                        ],
                    }
                resp = client.submit_leave_request(payload, api_token, company_id) or {}
                record_id = _inert(resp.get("request_id", ""), _RECORD_ID_SHAPE_RE)
                if not record_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeHrApiNode: freee HR returned no usable request_id for the submission"],
                    }
                record_ref = f"freee-hr://leave-requests/{record_id}"
                leave_summary = f"submitted ({_inert(resp.get('status', ''), _LABEL_SHAPE_RE) or 'in_progress'})"
            elif intent == "check_status":
                request_id = str(payload.get("request_id", "") or "")
                if not request_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallFreeeHrApiNode: unresolved request id - " "cannot check leave request status"
                        ],
                    }
                resp = client.get_request_status(request_id, api_token, company_id) or {}
                record_id = _inert(resp.get("request_id", ""), _RECORD_ID_SHAPE_RE) or request_id
                record_ref = f"freee-hr://leave-requests/{record_id}"
                leave_summary = f"status: {_inert(resp.get('status', ''), _LABEL_SHAPE_RE) or 'unknown'}"
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallFreeeHrApiNode: unknown intent '{intent}'"],
                }
        except FreeeHrApiError as exc:
            # HTTP status only. A live tenant's error body is unbounded
            # third-party text that can echo the record it refused (employee
            # number, leave type, balance); capping or stripping it would still
            # leave arbitrary text in the log line - so the closed-set signal
            # travels, the body does not.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallFreeeHrApiNode: freee HR API error {exc.status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception TYPE only, for the same reason: a transport error
            # string can carry the request URL and the employee number in it.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallFreeeHrApiNode: freee HR call failed ({type(exc).__name__})"],
            }

        # Audit the tool side-effect - intent + presence signals only,
        # never leave content or credentials.
        emit_trace_event(
            "call_freee_hr_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "employee_id": employee_id or record_id,
            "leave_type": leave_type,
            "leave_summary": leave_summary,
            "status": AgentStatus.SUCCESS.value,
        }
