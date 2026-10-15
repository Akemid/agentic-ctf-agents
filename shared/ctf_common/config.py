"""Runtime configuration, resolved at session preflight (design A10).

Environment variables carry only parameter NAMES; values come from SSM at the start of a
match, so a recreated game stack is picked up without redeploying the runtimes. Local runs
use `CTF_MCP_URL` / `CTF_DECIDER_URL` to skip SSM.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MCP_URL_PARAM = "/agentic-ctf/mcp-url"

ENV_MCP_URL_PARAM = "CTF_MCP_URL_PARAM"
ENV_DECIDER_ENDPOINT_PARAM = "CTF_DECIDER_ENDPOINT_PARAM"
ENV_MCP_URL = "CTF_MCP_URL"
ENV_DECIDER_URL = "CTF_DECIDER_URL"


class ConfigError(RuntimeError):
    """A required setting is missing or malformed. The message names the parameter or variable."""


@dataclass(frozen=True)
class RuntimeConfig:
    mcp_url: str
    decider_endpoint_name: str | None = None
    decider_url: str | None = None


def load_config(ssm: Any | None, env: Mapping[str, str], *, needs_decider: bool = False) -> RuntimeConfig:
    """Resolve the configuration. `ssm` is a boto3 SSM client (or None for fully local runs)."""
    mcp_override = env.get(ENV_MCP_URL, "").strip()
    if mcp_override:
        mcp_url = _require_http(mcp_override, ENV_MCP_URL)
    else:
        param = env.get(ENV_MCP_URL_PARAM, "").strip() or DEFAULT_MCP_URL_PARAM
        mcp_url = _require_http(_read_param(ssm, param), param)

    endpoint_name: str | None = None
    decider_url: str | None = None
    if needs_decider:
        decider_url = env.get(ENV_DECIDER_URL, "").strip() or None
        if decider_url is None:
            param = env.get(ENV_DECIDER_ENDPOINT_PARAM, "").strip()
            if not param:
                raise ConfigError(
                    f"{ENV_DECIDER_ENDPOINT_PARAM} is not set (name of the decider endpoint parameter)"
                )
            endpoint_name = _read_param(ssm, param)
    return RuntimeConfig(mcp_url=mcp_url, decider_endpoint_name=endpoint_name, decider_url=decider_url)


def _read_param(ssm: Any | None, name: str) -> str:
    if ssm is None:
        raise ConfigError(f"SSM parameter {name} is required but no SSM client is available")
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:  # botocore ClientError and friends
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        if code == "ParameterNotFound":
            raise ConfigError(f"SSM parameter {name} not found") from exc
        raise ConfigError(f"cannot read SSM parameter {name}: {code}") from exc
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise ConfigError(f"SSM parameter {name} is empty")
    return value


def _require_http(value: str, source: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise ConfigError(f"{source} must be an http(s) URL")
    return value


def default_ssm_client() -> Any:
    """Real SSM client for deployed runtimes (imported lazily so tests need no AWS setup)."""
    import boto3  # noqa: PLC0415

    return boto3.client("ssm")
