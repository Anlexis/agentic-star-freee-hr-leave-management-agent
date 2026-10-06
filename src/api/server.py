"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import FreeeHRLeaveAgent, load_runtime_config

app = FastAPI(title="Agent")

# Runtime values (max_retry, timeout_s, the freee HR settings) come from
# config/config.yaml. Constructing the graph without them would leave every
# declared setting unread, so the standalone entry point loads them the same
# way the platform registry does.
agent = FreeeHRLeaveAgent(config=load_runtime_config())
agent.compile()
# namespace/agent_name match the manifest's `namespace` and `name` values.
agent.provision_secrets(secrets_factory(namespace="cmn", agent_name="FreeeHRLeaveAgent"))

# Caller-supplied structured data travels in `input_context` alongside the
# free-text instruction. Bounds are enforced in the pipeline's first node,
# which owns the caller contract; the adapter only caps the envelope so a
# hostile body cannot make the request itself expensive to parse.
_MAX_CONTEXT_KEYS = 16
_MAX_CONTEXT_BYTES = 256 * 1024


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    input_context: dict[str, Any] = {}


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> "dict[str, Any]":
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary — a deployment-level caller
    # credential, not an agent secret, so ctx.secrets does not apply (no
    # InvocationContext exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    if len(req.input_context) > _MAX_CONTEXT_KEYS:
        raise HTTPException(status_code=413, detail="input_context has too many keys.")
    if len(json.dumps(req.input_context, default=str)) > _MAX_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context is too large.")

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result = agent.invoke(req.input, ctx=ctx, input_context=req.input_context)
        return cast("dict[str, Any]", result)


@app.get("/health")
def health() -> "dict[str, str]":
    return {"status": "ok", "agent": "FreeeHRLeaveAgent"}
