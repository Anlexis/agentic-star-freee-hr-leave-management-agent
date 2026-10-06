# Test Specification — CMN-C2-278 freee HR Leave Agent

## Test Strategy

- Test types: unit (per node, service, config and inner graph) and proof-of-boundary
  (the real compiled outer graph, the real ASGI entry point, import isolation, state
  safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end coverage lives in the proof-of-boundary suite, which
  drives the real compiled agent).
- The freee HR call is exercised through the deterministic, network-free stub
  transport (the default) and through monkeypatched fake transports. No live freee
  HR call is made anywhere in the suite.
- **Trust-gate routing convention**: every per-node unit test invokes the node as
  `node(state)` — `BaseNode.__call__` routes the full security pipeline (trust gate
  → PII mask → `execute()` → credential scan) — never bare `node.execute(state)`.
  State builders set `caller_trust_level = TrustLevel.VERIFIED_EXTERNAL.value` for
  `PreProcessNode` (the single external gate) and `TrustLevel.ANONYMOUS.value` for
  every other node. The trust-denial test asserts on the RETURNED error dict
  (`status == AgentStatus.ERROR.value`, "trust gate denied" in `error_log`,
  execute-only keys absent) — `__call__` never raises for a trust denial.
- **The exception, and why it exists**: the refusal tests call `execute()` DIRECTLY.
  A guarantee the template owns has to hold with no framework wrapper in front of
  it — the platform input gate covers only `user_input`/`validated_input`, blocks
  only high-confidence findings, and is not guaranteed to be active on every host.
  A test that asserts "the framework refused" proves nothing about the template.
  The same applies to the output gate: the framework's own credential scan raises
  on the same content and would mask what the node returns.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history` (never `formatted_output` at the
  invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value` (lowercase
  `success`/`error`); the outer graph is called as `invoke(user_input=..., ctx=...,
  input_context=...)`; free text may be masked (`[MASKED]`) so record evidence is
  asserted by presence, not raw repr; audit spies assert on `call.args[1]` (the
  event payload), never the whole-call repr.
- Framework behaviours the suite encodes: `__call__` short-circuits on an incoming
  errored state (`execute()` is skipped; error status/error_log pass through); the
  framework PII mask rewrites Title-Case bigrams (across newlines), emails and digit
  groups in `user_input`/`validated_input` to `[MASKED]` before `execute()` sees the
  text — positive payloads are therefore PII-free (employee codes stay ≤ 4 digits,
  `key: value` request lines use lower-case keys) and intentional-PII tests assert
  the `[MASKED]` path.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).
- Credential-shaped strings used in assertions are assembled at runtime, so no
  credential-shaped literal is committed to the repository.

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS an error dict ("trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | the caller-data contract: identifier type/shape/range bounds, alias priority, integer form, non-finite refusal in both wire forms, field-name-only refusals, masked hostile field names; the instruction-override screen on both caller channels (raw and post-markup-strip, depth-first including keys and escaped payloads); markup strip; request serialization | valid hints resolved and carried on `caller_fields`; every malformed identifier refused with no `validated_input`; attacks refused via `execute()` directly; ordinary leave-request prose containing the same words unaffected |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; caller contract read from `input_context`; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`); the override screen re-applied inside the inner graph | email → `[MASKED]` before execute; token → `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short → error; attacks refused through `execute()`; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_balance / submit_request / check_status (keyword, status-first priority, read-only default); LLM override path (test-double seam only, no real network call): well-formed JSON response overrides the heuristic, markdown-fence-wrapped response still parses, malformed/wrong-shape response and a raising LLM both fall back to the heuristic unchanged, no `llm=` injected + no secret bound falls back to the heuristic (the real production shape), empty input never calls the LLM at all | correct intent per keyword; status wins over submit on follow-ups; no-signal defaults to lookup_balance with a non-fatal note; empty → error, LLM not called; audit emits the intent label + `source` (`llm`/`heuristic`); LLM failure of any kind never raises and never returns `status=error` |
| U-05 | test_infer_freee_hr_fields_node.py | employee-number resolution (text > shaped hint; never invented); leave-type keywords with the inert-identifier lock on an unrecognised label; date-range extraction (`start:`/`end:` lines + in-text dates, single date → end=start); request-id resolution (text > shaped caller hint > shape-checked field line); payload per intent (JSON string) | lookup `{employee_id}`; submit `{leave_request:{employee_id, leave_type, start_date, end_date}}`; status `{request_id}`; unresolved identifier left `""`; empty input → error |
| U-06 | test_call_freee_hr_api_node.py | balance/submit/status via the network-free stub; `freee_hr_config` state field + `execute(state, config=...)` override (the documented direct-execute exception); API error / unresolved employee number / missing start date / missing request id / unknown intent / missing payload; secret posture (a live transport refuses to run unauthenticated; the token is read via `ctx.secrets`, never env or state) | record_id/record_ref + leave_summary on success; 403 surfaces in error_log as the HTTP status alone (`freee HR API error 403` — the upstream message is asserted absent); live+no-secret → error "unauthenticated"; live+bound secret → token passed to the client; audit emits presence signals with `stub_transport=True` |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; summary fallback to record_id | "Retrieved leave balance / Submitted leave request / Checked leave request status … ref=… id=…"; missing evidence → error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (payload round-trip); errored state passes through `__call__` un-masked; the domain output gate: record-evidence rule, credential families, the NESTED walk (payload mappings, lists and mapping keys), and containment — every output-bearing field cleared on a violation; the closed-set ERROR envelope, parameterised over every non-success path (inner error; inner error with the answer in `result`; inner error with a credential in `error_log`; missing record evidence; credential nested in the payload; credential-shaped mapping key) with `error_log` seeded with a sentinel (a name and a runtime-assembled credential-shaped token inside an echoed upstream body) | success shape with parsed `freee_hr_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without record evidence blocked; on every non-success path the envelope is exactly `{"reason": <one of ERROR_REASONS>}` and truthy, `result`/`confirmation`/`leave_summary`/`leave_type`/`intent`/`record_ref`/`record_id`/`employee_id` are cleared, the sentinel appears nowhere in the returned mapping (nested keys and values walked), the reason names the path taken, inner `error_log` entries are not re-emitted, gate violations travel in `error_log` only naming the location and never the value, a credential-shaped mapping key is withheld from the label with the clearing intact through the full node call, and no traceback or source path is in the log |
| U-09 | test_freee_hr_client.py | freee HR REST API v1 client: leave-balance lookup / leave-request submission / status check; `Authorization: Bearer` header; `company_id` tenant scoping; `FreeeHrApiError` on non-2xx; stub shapes (`leave_balances` / `lr-*` request receipt / status echo, `_stub` marker); `uses_stub_transport` | correct URLs/headers/bodies; 400 raises with joined `errors`; stub shapes deterministic |
| U-10 | test_config.py | both config files: the flat registry manifest and the runtime parameters, the latter asserted through the loader the code uses | manifest flat (no `agent:` block), single dotted `class:`, VERIFIED_EXTERNAL, `requires.secrets` empty (`FREEE_HR_ACCESS_TOKEN` and the three Azure OpenAI secrets are both optional-by-design and deliberately excluded), `requires.extras == ["openai"]`, `generation_mode: llm`; runtime `max_retry`/`timeout_s`/`freee_hr.base_url` load and reach the inner graph |
| U-11 | test_domain_workflow_graph.py | inner `FreeeHrWorkflowGraph`: identity, `_extra_initial_state()` config injection AND caller-contract seeding from the context bridge, the `route()` annotation, error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as a JSON string; the bridge value reaches the inner state; `route` is annotated with this graph's own `State`; error → END; inner invoke runs validate → classify → infer → call → confirm to SUCCESS with record evidence |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no platform-SDK imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic and no credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | the payload is byte-equal to `deploy/invoke_payload.json`'s "input" (asserted); a VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, FreeeHrWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; an ANONYMOUS caller is denied at pre_process (error, no post_process, no output); blank input → error, not a crash |
| PB (e2e) | The real `/invoke` entry point | test_invoke_e2e.py | driven through the real ASGI interface with Bearer auth. Auth boundary (401 without/with a wrong token); the public path does real work on all three intents; caller-supplied identifiers reach the inner workflow over the context bridge, with the negative half asserting the identical instruction fails when the field is absent; declared runtime config reaches the running agent; the caller contract fails closed (malformed identifiers, non-finite literals over the wire, adapter size and key caps, control-token and override attacks on both channels, ordinary prose unaffected, under-trusted caller denied); the output boundary (identifiers verbatim across the whole accepted alphabet, day counts rendered unmodified, non-finite day counts fail closed, credential material contained, no traceback or source path in any envelope, SUCCESS always carries record evidence); error text never reaches the caller on the wire — a 403 whose body echoes a name and a token, a node writing that line straight into `error_log`, and a refused response (SUCCESS inner run merged without record evidence, answer in `result`, sentinel in `error_log`): every key and value of the invoke body is walked for the sentinel, the refused response's `output` is exactly `{"reason": "output_withheld_by_gate"}`, and no answer text, violation label, traceback or source path is in the body |
| PB (error containment) | The ERROR envelope boundary | test_error_envelope_no_record_evidence.py | the existing-ERROR branch of `post_process`: the shipped `formatted_output` is PRESENT and TRUTHY (a falsy value re-opens the framework's `formatted_output or result` projection) and carries no `record_id`, `record_ref`, `employee_id`, `leave_summary`, confirmation text or `freee-hr://` reference; the returned delta CLEARS `result`, `confirmation`, `leave_summary`, `leave_type`, `freee_hr_payload`, `record_id`, `record_ref`, `employee_id`, `intent`; the error REASONS name no record — the not-found reason omits the employee number, the API-failure reason carries the HTTP status and not the upstream body, the transport reason carries the exception type and not the URL; plus a success-path control so the containment assertions cannot pass vacuously |
| PB (closed-set envelope) | The caller envelope through `get_output()` | test_output_envelope_containment.py | post_process driven through its real call path (an errored state through `execute()`, since `__call__` short-circuits on it), the partial merged the way the reducer merges it, and projected through the agent's `get_output()`: on every non-success state (credential nested in the payload; SUCCESS without record evidence; inner-workflow error with the answer in `result` and a sentinel in `error_log`) `output` is exactly `{"reason": <one of ERROR_REASONS>}` and truthy; the inner-error envelope carries none of the sentinel, the record reference, the summary or the confirmation, and the sentinel is present once in `error_log`; a refusal's violation label names the path in `error_log` only; caller-derived text in a payload value reaches neither the envelope nor the label; clean-path controls (the answer ships intact, no reason code) |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived — non-HITL** (`config/config.yaml` sets no `hitl.enabled: true`): module-level skipif; the stub bodies are real AssertionErrors, so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); the agent constructs and compiles via the supported path; `/invoke` and `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite by the emit-spy tests
> (validate / classify / call nodes assert on the event payload, `call.args[1]`).
> PB-3 (a live external service) is not exercised here — the shipped transport is
> the documented network-free stub.

## Output invariant under test

This template renders no monetary aggregates, so there is no rounding grid to
enforce. The invariant its output boundary owns instead is asserted directly:

1. a SUCCESS response always carries record evidence (`record_id`/`record_ref`);
2. no credential material leaves the agent, anywhere in the output — including
   inside nested payload mappings, lists and mapping keys, and on the error shape
   as much as the success shape;
3. on a violation every output-bearing field is cleared, because the response
   envelope falls back to `state["result"]` even on an error status.

Both directions are pinned: the leak forms are blocked, and legitimate output —
employee numbers across the whole accepted alphabet including a pure-digit run, day
counts, `freee-hr://` references and request payloads — crosses the boundary
byte-identical.

## Test Execution Summary

- Runner: `python -m pytest tests/ -v` against the real published
  `agenticstar-agentcore` wheel (never an import shim).
- Total tests: 287
- Pass: 285 / Fail: 0 / Skip: 2 (PB-7 A/B — auto-waived, non-HITL)
