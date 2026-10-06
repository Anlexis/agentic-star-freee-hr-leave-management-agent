# CMN-C2-278 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_balance / submit_request / check_status. A deterministic
# keyword heuristic is always computed; an AzureOpenAIClient call attempts to
# override it, falling back to the heuristic on any failure (missing secret,
# API error, malformed response). No test here makes a real network call -
# the LLM path is exercised only via the `llm=` test-double seam.
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust -> input gate ->
# execute -> output gate); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


class _FakeLLM:
    """Test double matching AzureOpenAIClient.complete()'s own shape."""

    def __init__(self, content=None, raises=None):
        self._content = content
        self._raises = raises
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {"content": self._content}


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_balance(self):
        result = self.node(_state("Look up the remaining leave balance for employee code 1001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_balance"

    def test_keyword_submit_request(self):
        result = self.node(_state("I want to take paid leave from 2026-08-01 to 2026-08-03"))
        assert result["intent"] == "submit_request"

    def test_keyword_check_status(self):
        result = self.node(_state("Has my leave request been approved yet for request id lr-1a2b3c4d?"))
        assert result["intent"] == "check_status"

    def test_status_keyword_wins_over_submit(self):
        # Priority order is status-first: a "check the status of the request I
        # submitted" style follow-up classifies as the status check, never a
        # brand-new write.
        result = self.node(_state("Check the status of the leave request I submitted last week"))
        assert result["intent"] == "check_status"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_balance"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_balance" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the remaining leave balance for employee code 1001."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_balance"
        assert payloads["classify_intent_complete"]["defaulted"] is False
        assert payloads["classify_intent_complete"]["source"] == "heuristic"

    def test_no_llm_injected_and_no_secret_bound_falls_back_to_heuristic(self):
        # The real production shape in any environment without a configured
        # Azure OpenAI key: ctx.secrets.require() raises MissingSecret inside
        # the try/except, and the keyword heuristic result is used unchanged.
        node = ClassifyIntentNode()  # no llm= injected, matches register_nodes()
        result = node(_state("I want to take paid leave from 2026-08-01 to 2026-08-03"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "submit_request"


class TestClassifyIntentNodeLlmPath:
    """LLM override path, exercised only via the `llm=` test-double seam."""

    def test_well_formed_llm_response_overrides_the_heuristic(self):
        # Keyword heuristic alone would say lookup_balance ("check my ..."),
        # but a well-formed LLM response overrides it.
        fake = _FakeLLM(content='{"intent": "check_status"}')
        node = ClassifyIntentNode(llm=fake)
        result = node(_state("check my leave situation please"))
        assert result["intent"] == "check_status"
        assert fake.calls == 1

    def test_markdown_fence_wrapped_response_still_parses(self):
        fake = _FakeLLM(content='Sure, here you go:\n```json\n{"intent": "submit_request"}\n```')
        node = ClassifyIntentNode(llm=fake)
        result = node(_state("please handle this for the team"))
        assert result["intent"] == "submit_request"

    def test_malformed_response_falls_back_to_heuristic(self):
        fake = _FakeLLM(content="not json at all")
        node = ClassifyIntentNode(llm=fake)
        result = node(_state("I want to take paid leave from 2026-08-01 to 2026-08-03"))
        assert result["intent"] == "submit_request"  # heuristic result, unchanged

    def test_wrong_shape_response_falls_back_to_heuristic(self):
        # Valid JSON, but not one of the three allowed intents.
        fake = _FakeLLM(content='{"intent": "delete_everything"}')
        node = ClassifyIntentNode(llm=fake)
        result = node(_state("I want to take paid leave from 2026-08-01 to 2026-08-03"))
        assert result["intent"] == "submit_request"  # heuristic result, unchanged

    def test_llm_raising_falls_back_to_heuristic(self):
        fake = _FakeLLM(raises=RuntimeError("simulated Azure OpenAI API failure"))
        node = ClassifyIntentNode(llm=fake)
        result = node(_state("Has my leave request been approved yet for request id lr-1a2b3c4d?"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "check_status"  # heuristic result, unchanged

    def test_empty_input_never_calls_the_llm(self):
        fake = _FakeLLM(content='{"intent": "submit_request"}')
        node = ClassifyIntentNode(llm=fake)
        result = node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert fake.calls == 0
