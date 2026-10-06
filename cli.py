"""AGENTIC STAR Marketplace entry point — the container's ``CMD``.

Entry adapter only, in the same sense ``src/api/server.py`` is one. The runner owns the
lifecycle: it constructs the graph class, compiles it, builds the SecretProvider, provisions
secrets, then runs one Marketplace execution (identity, input, progress events, terminal
delivery, exit) and exits.

Config is loaded via ``src.graph.graph.load_runtime_config`` — the same helper
``src/api/server.py`` uses to construct the agent — so both entry paths resolve
identical runtime values from config/config.yaml.

``agent_name`` / ``namespace`` mirror the values ``src/api/server.py`` passes to
``secrets_factory``, so secrets resolve from one place whichever transport started the agent.
"""

from shared.bootstrap.marketplace_app import run_agent_marketplace

from src.graph.graph import FreeeHRLeaveAgent, load_runtime_config

# Identity for the Marketplace runner. Mirrors what src/api/server.py hands to
# secrets_factory so both entry paths resolve secrets from the same location; an empty
# or defaulted value here would silently point the Marketplace path elsewhere.
_AGENT_NAME = "FreeeHRLeaveAgent"
_NAMESPACE = "cmn"

# Add overrides here to set values without editing config/config.yaml.
extend_config: dict = {}

if __name__ == "__main__":
    run_agent_marketplace(
        FreeeHRLeaveAgent,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config={**load_runtime_config(), **extend_config},
    )
