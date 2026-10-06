# Template Design Specification — CMN-C2-278 freee HR Leave Agent

## Position in AgentCore Architecture

| Aspect | Value |
|---|---|
| Agent class | `FreeeHRLeaveAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` (outer) + `BaseGraph` (inner) — direct framework inheritance |
| Category | Cat 2 (multi-step domain workflow, ToolCallingAgent) |
| Base type | ToolCallingAgent — classify intent, extract leave-request fields, build a freee HR REST API request, call the tool, format the confirmation. No retrieval, no autonomous loop. |

The outer `AgentBaseGraph` provides the fixed 5-node backbone; the domain pipeline
is encapsulated in a `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
(`src/graph/domain_workflow_graph.py`).

**Three-layer separation:**

- State: flat TypedDict `State(AgentState)` (no Pydantic — not msgpack-safe)
- Node: framework inheritance (Template Method: override `execute(self, state) -> dict` only)
- Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
  is not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | validate the caller contract; screen both caller channels; sanitize and serialize the request into `validated_input` | user_input, input_context | validated_input, employee_hint, request_hint, caller_fields | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner freee HR workflow subgraph (`classify_intent` inside it attempts an LLM override of the keyword heuristic; see "LLM-enhanced intent classification" below) | validated_input, caller_fields | result, intent, employee_id, record_id, record_ref, leave_type, leave_summary, confirmation, freee_hr_payload | GraphNode (caller context forwarded unchanged) | FreeeHrWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; enforce the output invariant via the module-level `_security_gate_output()` | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` (a fully custom linear topology). The five
pipeline steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's `InvocationContext`
is forwarded into the subgraph unchanged, so the single external trust gate stays on
the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; re-apply the instruction-override screen to the text that actually arrived; deterministic (regex) flag-and-redact of email/token-like strings before logging; read the validated caller contract off the inner state | validated_input, employee_hint, request_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification → lookup_balance / submit_request / check_status, always computed; an AzureOpenAIClient call then attempts to override it (graceful degrade to the keyword result on any failure); low confidence (from either source) → lookup_balance (read-only default — never a write) | intent | ANONYMOUS |
| infer_freee_hr_fields | 3 InferFreeeHrFields | extract employee number / leave type / date range / request id; assemble the freee HR REST API v1 request body per intent; an unresolved identifier is left empty (never invented); every value that can render is locked to an inert identifier alphabet | employee_id, leave_type, freee_hr_payload | ANONYMOUS |
| call_freee_hr_api | 4 CallFreeeHrApi | GET leave balances (lookup) / POST leave request (submit) / GET request status (check) via `FreeeHrClient`; token via `ctx.secrets`; response values bounded and shape-checked before they can render; 4xx/5xx → status=error | record_id, record_ref, employee_id, leave_type, leave_summary | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference + summary into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / FreeeHrWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_freee_hr_fields
              -> call_freee_hr_api -> confirm -> END
```

The instruction text travels as a JSON string: `pre_process` serializes `{"text"}`
into `validated_input`, `FreeeHrWorkflowGraphNode.extract_input()` hands that JSON to
the subgraph, and the first inner node parses it back.

The **validated caller contract travels separately**, over the context bridge
(`src/graph/context_bridge.py`). It cannot ride inside the `validated_input` JSON:
the framework masks that field at every node boundary, so the caller's target could
be rewritten between hops. It also cannot ride on `input_context`, because the
framework invokes a subgraph without forwarding it. `extract_input()` stashes the
contract on a `ContextVar` — the last point that still sees the outer state — and the
inner graph's `_extra_initial_state()` reads it back inside `subgraph.invoke()`.

## Caller-data contract

`POST /invoke` accepts `input_context` alongside the free-text instruction. Every
field is optional; the adapter caps the envelope (16 keys, 256 KB) and
`PreProcessNode` owns the field-level contract.

| Field (aliases) | Meaning | Bounds |
|---|---|---|
| `employee_id` / `employee_number` / `employee_hint` | target freee HR employee number | string or integer; `^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$`; integers in 0…9,999,999,999 |
| `request_id` / `request_number` / `request_hint` | target leave-request id | string or integer; `^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$` |

Rules, all failing CLOSED:

- floats, booleans, mappings and lists are refused outright, never coerced —
  `str(float("nan"))` is `"nan"` and `str(1e9)` is `"1000000000.0"`, so coercion
  would let a non-finite or unbounded value name the employee a leave request is
  filed for;
- non-finite values are refused in **both** wire forms: as a bare JSON token (which
  arrives as a float) and quoted (a string that also satisfies the identifier
  alphabet). Only the literal nan/inf spellings are affected — an ordinary code like
  `NANO` still passes;
- instruction-override content is refused on both caller channels — the raw
  instruction text and every decoded string in `input_context`, keys included, at any
  depth. The scan runs on the PARSED mapping, so JSON escaping cannot smuggle a
  phrase past it, and it covers undeclared keys as well as contract fields;
- a refusal names the FIELD and never echoes the value; an unrecognised (possibly
  hostile) field NAME is masked rather than echoed;
- an absent hint is simply absent: the workflow falls back to an identifier named in
  the instruction text, and an unresolvable target is a clean error, never a guess.

## Configuration

Two files, two jobs:

| File | Read by | Contents |
|---|---|---|
| `config/agent.yaml` | the platform registry, at ROOT level | the static manifest: id, name, namespace, category, industry, `generation_mode`, the single dotted `class:` entry point, `required_trust_level`, and the `requires` compile-time contract |
| `config/config.yaml` | the graph, as `Graph(config=…)` | runtime parameters: `max_retry`, `timeout_s`, and the `freee_hr:` integration section |

`load_runtime_config()` in `src/graph/graph.py` reads `config/config.yaml`; the
standalone entry point constructs the agent with it, exactly as the registry does,
because a graph built without it would leave every declared value unread.
`FreeeHrWorkflowGraphNode._parent_config()` forwards the `freee_hr:` section plus the
runtime values to the subgraph under `config["configurable"]`, and the inner graph's
`_extra_initial_state()` injects the integration section into State as a JSON string
(`freee_hr_config`), where `CallFreeeHrApiNode` reads it. An explicit
`config["configurable"]["freee_hr"]` override is also honoured for direct invocation.

Nodes take **no constructor arguments** (SDK v1 nodes are no-arg; constructor
arguments raise `TypeError` at graph build), so configuration never rides on node
instances.

`requires.secrets` is empty, and that is deliberate: `requires.secrets` is a
COMPILE-TIME contract (`require_at_compile()`, wired unconditionally from the
manifest — a listed key fails the agent at start-up, 503, if not provisioned,
regardless of how gracefully the node's own code handles its absence at runtime).
The freee HR integration token is read with `ctx.secrets.get()` on a path that
tolerates its absence (the network-free transport); the three Azure OpenAI secrets
are read with `ctx.secrets.require()` inside a broad `try/except` that falls back to
the keyword heuristic. Both are optional-by-design, so declaring either in
`requires.secrets` would make the default (no-token, no-LLM-key) configuration
unstartable — exactly what the graceful-degrade design is meant to avoid.
`requires.extras` does list `openai`: unlike `requires.secrets`, `extras` is an
install-time dependency declaration, not a compile-time secret gate —
`AzureOpenAIClient` imports `langchain_openai` unconditionally when constructed, so
the extra is a real requirement regardless of whether a key is ever provisioned.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone,
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`. Every inner
  domain node — **including the write-capable `CallFreeeHrApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL` would
  deny a legitimate external caller before the call runs — the boundary is therefore
  enforced exactly once, at `pre_process`. The agent-level default trust is declared
  in `config/agent.yaml`. `src/api/server.py` enforces the standalone entry-point
  Bearer-token auth boundary (`INVOKE_AUTH_TOKEN` → VERIFIED_EXTERNAL elevation).
- **Input screening — the template's own, not the framework's** — the
  instruction-override screen lives in `src/services/security.py` and is applied by
  `PreProcessNode` and `ValidateInputNode` in their own `execute()` paths. It holds
  whether or not any framework gate sits in front of it, which matters because the
  platform gate covers only `user_input`/`validated_input` (never the structured
  caller channel), rejects only high-confidence findings, and is not guaranteed to be
  active on every host.
  It recognises chat-template control tokens (`<|…|>`, `[INST]`, `<<SYS>>`) as a
  class, not only directive phrases, and it screens each string **both as received
  and after the markup strip**: the markup strip removes `<|im_start|>` as if it were
  a tag, so screening only the sanitized text would silently forward the directive
  residue — turning a detectable token attack into undetectable plain text — while
  the post-strip pass catches a directive spliced with markup (`ig<b>nore…`) that
  re-assembles into the forwarded string.
  Every alternative is anchored on a full directive phrase aimed at the MODEL or on a
  control token, because ordinary leave-request prose is full of directive verbs:
  "please ignore my previous request", "my manager overrode the rules", "you are now
  the approver" all pass unaffected, and the suite probes that direction as well as
  the attack direction.
- **Input redaction** — `ValidateInputNode` runs a deterministic (regex, not model)
  scan for email addresses and access-token-like strings (`eyJ…`, `secret_…`, `sk-…`)
  and redacts them before any logging. A leave request legitimately names an
  employee, so this is flag-and-redact for safe logging rather than a hard reject;
  the framework's PII mask additionally masks emails/phones/names in
  `user_input`/`validated_input`. The hard rejections are the empty/non-request guard
  and the override screen.
- **Secrets** — the integration token is read via
  `ctx.secrets.get("FREEE_HR_ACCESS_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. A missing token is tolerated **only**
  while the network-free stub transport is active (no live call is made); with a live
  transport injected, a missing token is a hard `status=error` — a real API is never
  called unauthenticated.
- **Untrusted responses** — values lifted out of a freee HR response are rendered to
  the caller, so they are treated as untrusted. Identifiers and status labels must
  match an inert identifier shape, and the day counts go through a finite + in-range
  parser: a NaN or an Infinity parses fine and then compares False against every
  bound, which would put a meaningless balance in front of the person deciding
  whether to take leave. Anything outside those bounds is a clean `status=error`,
  never a rendered guess.
- **Output gate** — the module-level `_security_gate_output()` in
  `src/nodes/post_process_node.py`, called from `PostProcessNode.execute()`. It is
  deliberately not an instance method and not the framework `_extra_security_gate_output`
  hook: the framework gate methods are `@final` on `FunctionNode` and the SDK
  auto-wraps `_extra_` hooks, which breaks the `.invoke()` chain.

  **This template renders no monetary aggregates**, so a numeric rounding grid has
  nothing to enforce here — its caller-facing output is an employee number, a
  `freee-hr://` reference, a leave-type label, a day-count summary and a confirmation
  sentence. The invariant the gate owns instead is: (1) a SUCCESS response always
  carries record evidence, so a success envelope can never misrepresent what happened
  in freee HR; (2) no credential material leaves the agent, anywhere in the output.

  The gate walks the WHOLE output structure — nested mappings, lists and mapping keys
  — not just top-level strings, because `freee_hr_payload` is a nested mapping and a
  scan of top-level values would step straight past a credential inside it. It
  recognises every credential family the framework's own scan does, deliberately: the
  framework RAISES on a finding, and a raise is not containment. A violation names the
  offending PATH, never the value — a credential-shaped mapping key is withheld from
  the label rather than quoted — and is written to `error_log` only; it never enters
  the caller's envelope.

  **Every non-success return goes through the one module-level `_contain()` helper**
  — a gate violation and a pre-existing inner-workflow error alike. It CLEARS every
  output-bearing state field (`_CLEARED_ON_ERROR`): `AgentBaseGraph.get_output` falls
  back to `state.get("result")` even on an error status, so a path that merely
  returned an error would still ship the un-gated inner answer inside the error
  envelope — and the framework's raise additionally carries a traceback with absolute
  source paths into the error log. Failing closed means overwriting `result`,
  `confirmation`, `leave_summary`, `leave_type`, `intent`, `record_ref`, `record_id`
  and `employee_id`, and replacing `formatted_output` with the closed-set envelope.
- **Error-path containment (the error envelope is a closed set)** — on every
  non-success path `formatted_output` is `{"reason": <code>}`, with the code drawn
  from the module's own `ERROR_REASONS`: `freee_hr_workflow_failed` (the inner
  workflow reported an error) or `output_withheld_by_gate` (the gate refused the
  response). Nothing else: never `error_log`, never the gate's violation entries,
  never record evidence. Node-authored error text can embed an upstream API error
  body, identifiers or names, and truncating or redacting it is not a closed set;
  `error_log` stays the internal channel (state reducer + audit trail) and is never
  projected to the caller. The errored branch does not re-emit the inner entries
  either — the reducer appends, so re-emitting would duplicate every line.

  `record_id`/`record_ref` are this agent's WRITE EVIDENCE — the gate above REFUSES a
  SUCCESS that lacks them — so returning them under an ERROR status would tell a
  caller being informed of failure that a leave request was nonetheless filed, and
  whose it is; freee HR is an HR system, so the employee number, the leave type and
  the balance summary are personal data. Omitting a field from one envelope is not
  clearing it: the clearing above is what stops a checkpoint or a downstream reader
  recovering it. The constant `reason` key keeps the envelope TRUTHY, so the
  `formatted_output or result` projection stops there rather than falling back onto
  whatever survived.

  The error REASONS in `error_log` are closed-set labels as well, because that channel
  is logged and correlated on: `CallFreeeHrApiNode` emits what was not found, the HTTP
  status (`freee HR API error <code>`) or the exception type — never the employee
  number, the leave content, or the upstream body.
- **Audit** — every node's `execute()` emits a positional
  `emit_trace_event("<event>", {small non-PII payload}, state)` on its reachable path
  (intent and presence signals only — never request text, leave content, or
  credentials). `__call__()` is never overridden. Event names, documented for
  operations:

  | Node | Event |
  |------|-------|
  | pre_process | `pre_process_complete`, `pre_process_validation_failed` |
  | validate_input | `validate_input_complete`, `validate_input_refused` |
  | classify_intent | `classify_intent_complete` |
  | infer_freee_hr_fields | `infer_freee_hr_fields_complete` |
  | call_freee_hr_api | `call_freee_hr_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete`, `post_process_blocked`, `post_process_error_contained` |

## Deterministic baseline, LLM-enhanced classification

Field inference (`InferFreeeHrFieldsNode`) uses regex and line-structure extraction
only — no model is involved there. Intent classification (`ClassifyIntentNode`) always
computes the deterministic keyword heuristic first, then attempts an Azure OpenAI call
to override it for better accuracy on ambiguous phrasing (see "LLM-enhanced intent
classification" below). The template remains fully runnable and testable without a
language model or any Azure OpenAI credentials: every failure of the LLM call — no key
configured, an API error, a malformed or wrong-shape response — is caught broadly and
falls back to the keyword result unchanged, silently. `generation_mode` in the manifest
is `llm` because an LLM is genuinely in the classification path, even though it is not
on the critical path to a successful response.

### LLM-enhanced intent classification

- **Client** — `AzureOpenAIClient` (wraps `langchain_openai.ChatOpenAI`), built fresh
  inside `ClassifyIntentNode._resolve_llm()` on every invocation, never cached on the
  node instance. Node instances are constructed once in `register_nodes()` and reused
  across invocations via the registry's node cache, so caching a client built from one
  caller's secrets would leave it visible to the next caller.
- **Secrets** — `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`,
  `AZURE_OPENAI_DEPLOYMENT`, resolved via `ctx.secrets.require(...)`
  (`InvocationContext.from_state(state)`). Deliberately **not** declared in
  `config/agent.yaml requires.secrets` — see "Configuration" below for why.
- **Test seam** — `ClassifyIntentNode.__init__(self, llm=None)` accepts an injected
  test double exposing `complete(messages) -> {"content": ...}`, the same shape as
  `AzureOpenAIClient.complete()`. `register_nodes()` never passes one; only the unit
  test suite does. No test makes a real network call.
- **Failure handling** — `_classify_via_llm()` wraps client construction and the call
  in one `except Exception: return None`; `execute()` then falls back to the keyword
  heuristic result whenever the LLM path returns `None` or an intent outside the
  three-value set. The node never raises and never returns `status=error` because the
  LLM was unavailable — a classification pipeline must always produce something.
- **Response parsing** — `shared.utils.llm_json.extract_json_object()` (never raises)
  pulls the `{"intent": "..."}` object out of the response, tolerating markdown-fence
  or prose wrapping; the parsed `intent` is accepted only if it is one of the three
  valid values.

## Limitation — the shipped freee HR client (documented)

`src/services/freee_hr_client.py` ships a **deterministic, network-free stub** as its
default transport: it returns stable freee-HR-shaped responses (a `leave_balances`
list for balance lookups; a request-receipt shape with a synthetic `request_id` for
submissions; a status shape for status checks, all derived deterministically from the
request) so the pipeline is runnable and testable without a live freee HR tenant or
the `requests` package. It does **not** perform a live freee HR call — the template
never fakes one.

To go live, inject real `post`/`get` transports at construction. The method contracts
are modelled on the freee HR REST API v1 resource families (employees / approval
requests) and the live transport adapter owns final path fidelity, so no
business-logic change is required. A live transport also requires a real integration
token — the stub runs without one because no request ever leaves the process.

## Framework Utilization

### Shared Components Used

- [x] `InvocationContext` — read in `CallFreeeHrApiNode` and `ClassifyIntentNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (including `CallFreeeHrApiNode`) declare `TrustLevel.ANONYMOUS`
- [x] Secrets — `ctx.secrets.get("FREEE_HR_ACCESS_TOKEN")`, `ctx.secrets.require(...)` for the three Azure OpenAI keys; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `AzureOpenAIClient` (`shared.services.llm.azure_openai_client`) — built fresh per invocation in `ClassifyIntentNode`, never cached
- [x] `emit_trace_event()` — one positional call per node on its reachable path; framework lifecycle events (node_start/node_complete/node_error/trust denial) are not re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: the inner `FreeeHrWorkflowGraph` (`BaseGraph`) via `FreeeHrWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `FreeeHrWorkflowGraphNode._parent_config()` reads `config/config.yaml` and forwards `{freee_hr, agent}` under `config["configurable"]`.
- **Caller-contract forwarding**: the `ContextVar` bridge in `src/graph/context_bridge.py`.
- **Error propagation**: `propagate` (default) — inner errors re-raised as `SubgraphError`; per-step `status=error` + `error_log` for API and validation failures (no silent pass).

### Conditional routing

`AgentBaseGraph` wires `add_conditional_edges("main", self.route)` on the outer graph
and is not overridden here. The inner graph's `route()` is required by the `BaseGraph`
ABC and is annotated with **this graph's own `State`**: LangGraph reads a path
callable's annotation as its input schema and projects away fields absent from it, so
a wider annotation would make the routing fields unreadable at the exact moment they
are needed.

## Import Isolation Confirmation

- [x] The template imports `framework/` and `shared/` only; no platform-SDK import anywhere
- [x] `src/services/freee_hr_client.py` and `src/services/security.py` have no framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; five domain steps live in the inner graph |
| Model dependency | model client in the pipeline | deterministic pipeline | LLM-enhanced with a deterministic fallback | field extraction stays deterministic; intent classification gains an LLM override that gracefully degrades to the keyword heuristic on any failure, so the template still runs and tests without a model or Azure OpenAI credentials |
| LLM secret declaration | list the three Azure OpenAI secrets under `requires.secrets` | leave `requires.secrets` empty, same as `FREEE_HR_ACCESS_TOKEN` | leave empty | `requires.secrets` is a compile-time contract enforced via `require_at_compile()` — a listed key 503s the agent at start-up if unprovisioned, regardless of the node's own graceful degrade at runtime; declaring an optional key there would defeat the fallback |
| freee HR client | live `requests` call | injectable transport + documented stub default | injectable + stub default | no live network in the shipped default; the limitation is documented rather than faked |
| Node configuration | constructor-arg dependency injection | no-arg nodes + config forwarding via `_parent_config()` → `configurable` → state | no-arg nodes | SDK v1 nodes are no-arg (constructor args raise TypeError at graph build); the config files stay the single source |
| Caller contract across the graph boundary | smuggle it inside the `validated_input` JSON | a ContextVar bridge | ContextVar bridge | the framework masks `validated_input` at every node boundary, so the caller's target could be rewritten between hops; the bridge channel is not masked |
| Write target | infer the employee number freely from the text | caller-supplied or explicitly named only; unresolved left empty | explicit only | never file a leave request for the wrong employee; an unresolved number is `status=error`, not a guess |
| Default intent | submit_request | lookup_balance | lookup_balance | a low-confidence classification must never default to a write |
| Output-gate violation | raise | clear the output-bearing fields and return an error | clear and return | the response envelope falls back to `state["result"]` even on an error status, so raising ships the un-gated answer |
| Inner-workflow error response | echo `record_id`/`record_ref` back so the caller can correlate the failure | record-free envelope + the same clearing | record-free + clear | the identifiers are the WRITE evidence; naming them tells a caller told the run failed that a leave request was filed anyway, and whose |
| Caller-visible error content | forward `error_log` (truncated / credential-redacted) as `formatted_output["error"]` | a constant reason code from `ERROR_REASONS`, nothing read from `error_log` or the violations | closed-set envelope | node-authored text can embed an upstream body, identifiers or names; truncation or redaction is not a closed set. `error_log` stays internal |
| Upstream API error reason | forward the freee HR error text | closed-set label (HTTP status / exception type / what was not found) | closed-set label | `error_log` is the audit channel — an upstream body is unbounded third-party text and can quote the record it refused |
