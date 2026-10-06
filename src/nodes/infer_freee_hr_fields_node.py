"""AgentCore Platform v1.0 - inner workflow Step 3: InferFreeeHrFields.

Extracts the employee number, leave type, date range, and (for status checks)
the leave-request id from the (redacted) request and assembles a validated
freee HR REST API v1 request body for the classified intent. The employee
number is taken only from an explicit number in the text or the caller-supplied
employee_hint - an unresolved number is left empty rather than invented (risk
mitigation: never submit a leave request for the wrong employee; the executor
surfaces the miss as status=error). Deterministic - no language model is
involved.

Every value that can reach the caller-facing output through this node is
constrained to an inert identifier alphabet before it is written. A
"Key: value" line is caller-controlled text, so a request id or leave type
lifted out of one is shape-checked here rather than passed through: those two
values render into the confirmation and into the freee-hr:// reference.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# A freee HR employee number: short alphanumeric identifier (no spaces).
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# A freee HR leave-request id, same alphabet with a longer bound.
_REQUEST_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$")
# Leave-type labels that are not one of the known categories are still
# rendered, so they are locked to an inert identifier alphabet.
_LEAVE_TYPE_SHAPE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# Explicit employee number mention in the request text, EN or JA
# ("employee code 1001" / "employee number: A123" / "社員番号 1001").
_CODE_IN_TEXT_RE = re.compile(
    r"(?:employee|member|staff)\s+(?:code|id|number)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:社員番号|社員コード|従業員番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Explicit leave-request id mention ("request id lr-1a2b3c4d" / "申請番号 12345").
_REQUEST_ID_IN_TEXT_RE = re.compile(
    r"(?:request|application)\s+(?:id|number)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,29})"
    r"|(?:申請番号|申請ID)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,29})",
    re.IGNORECASE,
)
# ISO-ish dates (2026-08-01 / 2026/8/1) - extracted in order of appearance.
_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
# "Key: value" request-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# KV keys mapped onto the structured fields (never treated as free-form fields).
_CODE_KEYS = ("code", "employee code", "employee id", "employee number", "id")
_START_KEYS = ("start", "start date", "from")
_END_KEYS = ("end", "end date", "to", "until")
_TYPE_KEYS = ("type", "leave type")
_REQUEST_KEYS = ("request id", "request number", "application id")

# Deterministic leave-type keyword mapping (first match wins; paid default).
_LEAVE_TYPES = (
    ("sick_leave", ("sick", "illness", "病気", "傷病", "病欠")),
    ("half_day", ("half day", "half-day", "morning off", "afternoon off", "半休")),
    ("paid_holiday", ("paid", "annual leave", "vacation", "pto", "有給", "年休")),
)
_DEFAULT_LEAVE_TYPE = "paid_holiday"


class InferFreeeHrFieldsNode(FunctionNode):
    """Extract entities and assemble the freee HR REST API v1 request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_balance") or "lookup_balance"
        caller_context = state.get("input_context") or {}
        employee_hint = str(caller_context.get("employee_hint", "") or state.get("employee_hint", "") or "")
        request_hint = str(caller_context.get("request_hint", "") or state.get("request_hint", "") or "")

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferFreeeHrFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        employee_id = self._resolve_code(text, employee_hint, fields)
        leave_type = self._resolve_leave_type(text, fields)
        start_date, end_date = self._resolve_dates(text, fields)
        request_id = self._resolve_request_id(text, request_hint, fields)

        payload: dict[str, Any]
        if intent == "submit_request":
            payload = {
                "leave_request": {
                    "employee_id": employee_id,
                    "leave_type": leave_type,
                    "start_date": start_date,
                    "end_date": end_date or start_date,
                }
            }
        elif intent == "check_status":
            payload = {"request_id": request_id}
        else:  # lookup_balance (read-only default)
            payload = {"employee_id": employee_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_freee_hr_fields_complete",
            {
                "intent": intent,
                "has_employee_id": bool(employee_id),
                "has_dates": bool(start_date),
            },
            state,
        )

        return {
            "employee_id": employee_id,
            "leave_type": leave_type,
            "freee_hr_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_code(self, text: str, employee_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit number only: text mention > code-shaped hint > 'Code:' field. Never invented."""
        m = _CODE_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = employee_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS and _CODE_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_request_id(self, text: str, request_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit id only: text mention > shaped hint > 'Request id:' field.

        Every branch is shape-checked. The id renders into the confirmation and
        into the freee-hr:// reference, so a truncation (the old behaviour of
        the field branch) would have carried arbitrary caller text into the
        caller-facing output.
        """
        m = _REQUEST_ID_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = request_hint.strip()
        if hint and _REQUEST_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _REQUEST_KEYS and _REQUEST_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_leave_type(self, text: str, fields: "list[tuple[str, str]]") -> str:
        for key, value in fields:
            if key.strip().lower() in _TYPE_KEYS and value.strip():
                candidate = value.strip().lower()
                for label, words in _LEAVE_TYPES:
                    if any(w in candidate for w in words):
                        return label
                # An unrecognised label still renders, so it is admitted only
                # when it is already an inert identifier; anything else falls
                # through to the default rather than reaching the output.
                normalized = candidate.replace(" ", "_").replace("-", "_")
                if _LEAVE_TYPE_SHAPE_RE.match(normalized):
                    return normalized
        low = text.lower()
        for label, words in _LEAVE_TYPES:
            if any(w in low for w in words):
                return label
        return _DEFAULT_LEAVE_TYPE

    def _resolve_dates(self, text: str, fields: "list[tuple[str, str]]") -> "tuple[str, str]":
        """First two date mentions -> (start, end); KV Start/End lines take priority."""
        start = self._date_from_fields(fields, _START_KEYS)
        end = self._date_from_fields(fields, _END_KEYS)
        if start:
            return start, end
        found = [self._norm_date(m) for m in _DATE_RE.finditer(text)]
        if not found:
            return "", ""
        if len(found) == 1:
            return found[0], ""
        return found[0], found[1]

    def _date_from_fields(self, fields: "list[tuple[str, str]]", keys: "tuple[str, ...]") -> str:
        for key, value in fields:
            if key.strip().lower() in keys:
                m = _DATE_RE.search(value)
                if m:
                    return self._norm_date(m)
        return ""

    def _norm_date(self, m: "re.Match[str]") -> str:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] request fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields
