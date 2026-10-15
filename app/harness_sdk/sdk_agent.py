"""harness_sdk runtime: one CTF character played by a plain Strands Agent.

Ported from `local/agent/refined/harness_sdk.py`. The shared pieces (prompts, hooks, MCP
connection, runner, payload validation, the Strands match loop) live in `ctf_common`; this module
only names the runtime and wires it to the runner.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ctf_common import wiring
from ctf_common.auth import TokenProvider
from ctf_common.mcp import open_mcp_client
from ctf_common.runner import AsyncTaskApp, MatchContext, MatchRunner, PlayFn
from ctf_common.strands_match import McpFactory, play_strands_match

RUNTIME = "harness_sdk"


def play(ctx: MatchContext, *, mcp_factory: McpFactory = open_mcp_client, model: Any = None) -> None:
    """Play one character until match_over, a stop request, or a failure."""
    play_strands_match(ctx, runtime=RUNTIME, mcp_factory=mcp_factory, model=model)


def build_runner(
    app: AsyncTaskApp,
    env: Mapping[str, str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    ssm_client: Any | None = None,
    play: PlayFn = play,
) -> MatchRunner:
    """Wire the runner. Nothing touches AWS until a `start` arrives (preflight), so /ping stays healthy."""
    return wiring.build_runner(
        app, play, runtime=RUNTIME, env=env, token_provider=token_provider, ssm_client=ssm_client
    )
