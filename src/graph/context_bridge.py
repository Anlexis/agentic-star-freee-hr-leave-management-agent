"""AgentCore Platform v1.0 - caller-context bridge across the graph boundary."""

# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# the outer state's input_context, so an inner-node read of
# state["input_context"] would always see {} through the nested graph. The
# sanctioned subclass hooks bridge it:
#
#   FreeeHrWorkflowGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       -> set_caller_input_context(state["caller_fields"])
#   FreeeHrWorkflowGraph._extra_initial_state()    [runs INSIDE subgraph.invoke]
#       -> returns {"input_context": get_caller_input_context()}
#
# What crosses the bridge is the VALIDATED caller contract produced by
# PreProcessNode - never the raw request body - so the inner graph is only ever
# handed fields that already passed their type, shape and length bounds.
#
# The alternative, smuggling caller data inside the validated_input JSON, is
# not usable here: the framework masks that field at every node boundary, so
# the caller's target could be rewritten between hops. This channel is not
# masked.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's context.

from contextvars import ContextVar
from typing import Any

_CALLER_INPUT_CONTEXT: ContextVar["dict[str, Any] | None"] = ContextVar("cmn_c2_278_caller_input_context", default=None)


def set_caller_input_context(input_context: "dict[str, Any] | None") -> None:
    """Stash the validated caller contract for the imminent inner-graph invoke."""
    _CALLER_INPUT_CONTEXT.set(dict(input_context) if input_context else {})


def get_caller_input_context() -> "dict[str, Any]":
    """Read (without consuming) the stashed contract; {} when none was set."""
    return _CALLER_INPUT_CONTEXT.get() or {}
