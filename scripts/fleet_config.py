"""The one place that names the fleet (design A9, slice 4).

Everything that has to agree on names reads this table: the hydrate script (SSM paths and
`.env.local` variables), the launcher, and the tests that compare it with `agentcore.json`.
The agentic-ctf agent names (`agentic_ctf_name`) are owned by another team and still being
finalised: if they change, edit `FLEET` here and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

# Scopes and parameter paths shared with the runtimes (`ctf_common.auth`, `ctf_common.config`).
SCOPES = ("agentic-ctf/mcp",)
AGENTIC_CTF_SSM_PREFIX = "/agentic-ctf"
DISCOVERY_URL_PARAM = f"{AGENTIC_CTF_SSM_PREFIX}/cognito/discovery-url"
MCP_URL_PARAM = f"{AGENTIC_CTF_SSM_PREFIX}/mcp-url"
DECIDER_ENDPOINT_PARAM = "/ctf-decider/endpoint-name"

# `agentcore.json` is committed in a public repository, so it never holds the real Cognito
# discovery URL. This reserved-TLD placeholder is what is committed; `hydrate_credentials.py`
# swaps it for the real URL before a deploy and `--clean` swaps it back afterwards.
DISCOVERY_PLACEHOLDER = "https://discovery-url-not-hydrated.invalid/.well-known/openid-configuration"

# Launcher caps (spec: fleet-operations "Fleet launcher").
MAX_CHARACTERS_PER_TYPE = 3
MAX_TEAMS = 2

# `lifecycleConfiguration.maxLifetime` of every runtime in agentcore.json (a test keeps them equal):
# after this many seconds a session is gone and invoking it would boot a fresh, empty microVM.
MAX_LIFETIME_SECONDS = 14400


@dataclass(frozen=True)
class FleetMember:
    runtime: str  # name in agentcore.json (and in app/<runtime>/)
    agentic_ctf_name: str  # agent client name in the agentic-ctf stack (SSM: /agentic-ctf/agents/<name>/...)
    uses_decider: bool

    @property
    def credential(self) -> str:
        """AgentCore OAuth credential name, also the value of `CTF_OAUTH_PROVIDER` in the runtime."""
        return f"ctf_{self.runtime}"


FLEET: tuple[FleetMember, ...] = (
    FleetMember("harness", "harness", uses_decider=False),
    FleetMember("harness_sdk", "harness-sdk", uses_decider=False),
    FleetMember("strand_decider", "strand-decider", uses_decider=True),
    FleetMember("decider_loop", "decider-loop", uses_decider=True),
)


def member(runtime: str) -> FleetMember:
    for candidate in FLEET:
        if candidate.runtime == runtime:
            return candidate
    raise KeyError(f"unknown runtime: {runtime}")


def credential_env_vars(item: FleetMember) -> tuple[str, str]:
    """`.env.local` variable names the CLI reads (no project prefix; `-` becomes `_`, upper case)."""
    base = f"AGENTCORE_CREDENTIAL_{item.credential.replace('-', '_').upper()}"
    return f"{base}_CLIENT_ID", f"{base}_CLIENT_SECRET"


def agent_ssm_params(item: FleetMember) -> tuple[str, str]:
    """(client-id parameter, secret-arn parameter) published by the agentic-ctf stack."""
    base = f"{AGENTIC_CTF_SSM_PREFIX}/agents/{item.agentic_ctf_name}"
    return f"{base}/client-id", f"{base}/secret-arn"


def is_placeholder(url: str) -> bool:
    return url == DISCOVERY_PLACEHOLDER
