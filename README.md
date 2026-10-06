# freee HR Leave Management Agent

AI agent for handling leave requests in freee HR, built with Agentic Star.

> **Category**: Cat 2 (a fixed multi-step pipeline for one job-to-be-done)
> **Industry**: Common
> **Template ID**: CMN-C2-278

## Overview

Turns a plain-language leave request into a single, confirmed action against freee HR.

Give it a sentence like *"look up the remaining leave balance for employee code 1001 and
summarize the days available"* and it works out which operation you meant, resolves the target
employee number and dates from the wording or from caller-supplied data, assembles the matching
freee HR REST API request, and hands back a confirmation carrying the affected record id and a
`freee-hr://` reference.

Three intents are supported — look up a balance, submit a leave request, and check a request's
status. Submitting writes to the HR system, so the agent never invents a target: a request with
no explicit employee number or start date fails with a clear error rather than filing leave
against whatever a guess would have landed on, and an unclassifiable request falls back to the
read-only balance lookup, never to a write.

Field extraction is deterministic (pattern based). Intent classification is a deterministic
keyword heuristic that an Azure OpenAI call attempts to override for better accuracy on ambiguous
phrasing; any failure of that call (no key configured, an API error, a malformed response) falls
back to the heuristic silently, so the pipeline still runs and is fully testable without a language
model or any Azure OpenAI credentials. Out of the box it ships with a network-free stub transport
that returns the documented freee HR response shapes, which makes the template runnable end to end
before you connect a real tenant; injecting a live HTTP transport and providing an integration
token is all that is needed to go live.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent.yaml (registry manifest) and config.yaml (runtime parameters)
docs/         design document and test specification
```

See `docs/` for the design document and the test specification.

## Customising

1. Set your tenant's freee HR `base_url` and `company_id` under `freee_hr:` in
   `config/config.yaml`.
2. Inject live `get`/`post` transports when constructing `FreeeHrClient`
   (`src/services/freee_hr_client.py`) and provision `FREEE_HR_ACCESS_TOKEN` through the secrets
   provider — the shipped default is a deterministic, network-free stub.
3. Adjust the intent keywords and field-extraction patterns under `src/nodes/` for your own
   wording and leave-type scheme.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
