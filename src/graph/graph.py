"""AgentCore Platform v1.0 - CMN-C2-278 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in FreeeHrWorkflowGraphNode (`main`
slot), which wraps the inner FreeeHrWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.graph.context_bridge import set_caller_input_context
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State, from_json

if TYPE_CHECKING:  # import cycle at runtime; the real import is inside get_subgraph()
    from src.graph.domain_workflow_graph import FreeeHrWorkflowGraph

# Repo-root runtime config: src/graph/graph.py -> parents[2] = repo root.
# Runtime parameters live in config/config.yaml; config/agent.yaml is the static
# registry manifest and carries no runtime block.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Runtime keys forwarded to the inner graph as the `agent` section. `timeout_s`
# is the key the framework config validator reads, so it travels under that name.
_RUNTIME_KEYS = ("max_retry", "timeout_s")


def load_runtime_config() -> "dict[str, Any]":
    """Load config/config.yaml -> the dict passed to the graph as `config=`.

    The platform registry loads this file and constructs the agent with it; a
    standalone entry point must do the same, otherwise every declared runtime
    value (max_retry, timeout_s, the freee HR settings) is silently absent.
    Returns {} when the file is missing or unreadable - the pipeline then runs
    on its documented defaults rather than failing to start.
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else {}


class FreeeHrWorkflowGraphNode(GraphNode):
    """Wraps the inner freee HR workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which loads the runtime config file.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "FreeeHrWorkflowGraph":
        from src.graph.domain_workflow_graph import FreeeHrWorkflowGraph

        return FreeeHrWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON
        # string); the first inner node parses it back.
        #
        # The validated caller contract cannot ride along in that string: the
        # framework masks validated_input at every node boundary, so caller
        # values could be rewritten between hops. It is stashed on the bridge
        # here instead - the last point that still sees the outer state before
        # the framework invokes the subgraph without forwarding input_context.
        set_caller_input_context(from_json(state.get("caller_fields"), {}))
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "employee_id": sub_result.get("employee_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "leave_type": sub_result.get("leave_type", ""),
            "leave_summary": sub_result.get("leave_summary", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "freee_hr_payload": sub_result.get("freee_hr_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Reads config/config.yaml and forwards the `freee_hr:` integration
        section plus the runtime values (max_retry, timeout_s) as the `agent`
        section. An empty result would make every declared setting dead, so the
        values are read from the live file rather than assumed.
        """
        runtime = load_runtime_config()
        configurable: dict[str, Any] = {}
        freee_hr = runtime.get("freee_hr")
        if isinstance(freee_hr, dict) and freee_hr:
            configurable["freee_hr"] = freee_hr
        agent_cfg = {key: runtime[key] for key in _RUNTIME_KEYS if key in runtime}
        if agent_cfg:
            configurable["agent"] = agent_cfg
        return {"configurable": configurable}


class FreeeHRLeaveAgent(AgentBaseGraph):
    """CMN-C2-278 outer graph - freee HR Leave Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in FreeeHrWorkflowGraphNode (`main` slot); freee HR
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_278"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = FreeeHrWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
