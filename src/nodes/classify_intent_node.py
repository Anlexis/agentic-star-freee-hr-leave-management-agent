"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_balance / submit_request
/ check_status. A deterministic keyword heuristic is always computed first, so
the template stays testable and runnable without a language model; an
AzureOpenAIClient call then attempts to override it with a same-set
classification. Any failure of the LLM path (missing secret, API error,
malformed/wrong-shape response) silently keeps the heuristic result - this
node never raises and never returns status=error because the LLM was
unavailable. Low-confidence / unknown (from either source) falls back to the
read-only "lookup_balance" default with a note - never a write.
"""

import logging
from typing import Any, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You classify a freee HR leave-management request into exactly one intent:
- lookup_balance: the caller wants to know a leave/PTO balance or summary (read-only)
- submit_request: the caller wants to file/submit/book a new leave request (a write)
- check_status: the caller wants to know the approval status of an existing request

Respond with a JSON object of the exact shape {"intent": "<one of the three above>"}.
If genuinely ambiguous, pick lookup_balance (the safe read-only default). Do not include
any other text.
"""

_VALID_INTENTS = ("lookup_balance", "submit_request", "check_status")

# Deterministic keyword signals (checked in priority order: status checks first
# so "check the status of my leave request" never classifies as a new submit;
# then the write; then the read-only balance lookup).
_KEYWORDS = (
    (
        "check_status",
        (
            "status",
            "approved yet",
            "approval state",
            "pending",
            "progress",
            "was my request",
            "has my request",
            "state of my",
            "承認状況",
            "進捗",
            "承認され",
            "却下",
        ),
    ),
    (
        "submit_request",
        (
            "submit",
            "apply",
            "file a",
            "book",
            "take leave",
            "take a day",
            "take paid",
            "request leave",
            "day off",
            "vacation",
            "pto",
            "time off",
            "休暇申請",
            "申請したい",
            "有給を",
            "休みたい",
            "取得したい",
        ),
    ),
    (
        "lookup_balance",
        (
            "balance",
            "remaining",
            "days left",
            "how many",
            "look up",
            "lookup",
            "show",
            "get",
            "fetch",
            "retrieve",
            "check my",
            "summarize",
            "残日数",
            "残り",
            "照会",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a freee HR leave-management operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        super().__init__()
        # Test-double seam only - register_nodes() never passes one. A real
        # AzureOpenAIClient is built fresh per invocation in _classify_via_llm(),
        # never cached on self (node instances are reused across invocations
        # via the registry's node cache; caching a client built from one
        # caller's secrets would leak it to the next caller).
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        heuristic_intent = self._classify_via_keywords(text)
        llm_intent = self._classify_via_llm(text, state)

        source = "heuristic"
        if llm_intent in _VALID_INTENTS:
            intent = llm_intent
            source = "llm"
        else:
            intent = heuristic_intent

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_balance (read-only)"]
            intent = "lookup_balance"

        # Audit the classification decision - intent label + source only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "source": source, "defaulted": bool(note)},
            state,
        )

        result: dict[str, Any] = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"

    def _classify_via_llm(self, text: str, state: dict[str, Any]) -> Optional[str]:
        """Attempt an LLM classification; None on any failure (never raises).

        Missing secret, LLM API error, and a malformed/wrong-shape response
        are all the same case here: the caller (execute()) falls back to the
        keyword heuristic. A personalization/classification pipeline must
        always produce something - an LLM outage is not a pipeline failure.
        """
        try:
            llm = self._resolve_llm(state)
            response = llm.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ]
            )
            parsed = extract_json_object(response.get("content"))
            intent = parsed.get("intent")
            return intent if isinstance(intent, str) and intent in _VALID_INTENTS else None
        except Exception:
            # Broad by design: a missing/invalid secret, an LLM API failure, or a
            # malformed response must all degrade the same way - fall back to the
            # keyword heuristic, never a hard node error.
            logger.debug("ClassifyIntentNode: LLM classification unavailable, using heuristic", exc_info=True)
            return None

    def _resolve_llm(self, state: dict[str, Any]) -> Any:
        """Return the injected test double, or a real client built fresh from ctx.secrets."""
        if self._llm is not None:
            return self._llm
        ctx = InvocationContext.from_state(state)
        return AzureOpenAIClient(
            {
                "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
            }
        )
