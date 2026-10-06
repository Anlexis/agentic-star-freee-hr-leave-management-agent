# CMN-C2-278 - Unit tests: inner FreeeHrWorkflowGraph (BaseGraph) contract.
# The compiled outer path is exercised end-to-end by the proof-of-boundary
# tests; this module unit-checks the inner graph's identity, config forwarding,
# routing, output contract, and a direct inner invoke on the network-free stub.

import pytest

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import set_caller_input_context
from src.graph.domain_workflow_graph import FreeeHrWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return FreeeHrWorkflowGraph(config=config or {})


@pytest.fixture(autouse=True)
def _clear_bridge():
    """The bridge is per-task state; clear it so one test cannot seed another."""
    set_caller_input_context({})
    yield
    set_caller_input_context({})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "freee_hr_leave_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_freee_hr_config_as_json():
    g = _graph({"configurable": {"freee_hr": {"base_url": "https://freee.example.test/hr/api/v1"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict (the state contract keeps
    # every value msgpack-safe).
    assert isinstance(extra["freee_hr_config"], str)
    assert from_json(extra["freee_hr_config"], {}) == {"base_url": "https://freee.example.test/hr/api/v1"}


def test_extra_initial_state_without_freee_hr_section_still_seeds_the_caller_contract():
    extra = _graph()._extra_initial_state()
    assert "freee_hr_config" not in extra
    assert extra["input_context"] == {}


def test_extra_initial_state_reads_the_caller_contract_off_the_bridge():
    """The framework invokes a subgraph without forwarding input_context, so
    this hook is the only place the validated caller contract can enter the
    inner state."""
    set_caller_input_context({"employee_hint": "1001"})
    assert _graph()._extra_initial_state()["input_context"] == {"employee_hint": "1001"}


def test_route_annotation_is_this_graphs_own_state():
    """A conditional-edge path callable's annotation is read as its input
    schema, and a wider annotation projects away the fields the route reads."""
    import typing

    assert typing.get_type_hints(FreeeHrWorkflowGraph.route)["state"] is State


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {
                "record_id": "1001",
                "record_ref": "freee-hr://employees/1001/leave-balances",
                "confirmation": "ok",
            },
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_balance",
            "employee_id": "1001",
            "record_id": "1001",
            "record_ref": "freee-hr://employees/1001/leave-balances",
            "leave_type": "paid_holiday",
            "leave_summary": "paid_holiday: 12.5 days remaining (3 taken)",
            "confirmation": "ok",
            "freee_hr_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_balance"
    assert out["record_ref"] == "freee-hr://employees/1001/leave-balances"
    assert out["leave_summary"] == "paid_holiday: 12.5 days remaining (3 taken)"
    assert out["confirmation"] == "ok"
    assert out["output"] == {
        "record_id": "1001",
        "record_ref": "freee-hr://employees/1001/leave-balances",
        "confirmation": "ok",
    }


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_the_stub_transport():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"freee_hr": {"base_url": "https://api.freee.co.jp/hr/api/v1"}}})
    g.compile()
    result = g.invoke(
        user_input="Look up the remaining leave balance for employee code 1001 and summarize the days available."
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "1001"
    assert result["record_ref"] == "freee-hr://employees/1001/leave-balances"
    assert result["intent"] == "lookup_balance"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferFreeeHrFieldsNode",
        "CallFreeeHrApiNode",
        "ConfirmNode",
    ]
