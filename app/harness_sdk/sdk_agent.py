"""harness_sdk runtime: one CTF character played by a plain Strands Agent.

Ported from `local/agent/refined/harness_sdk.py`. The shared pieces (prompts, hooks, MCP
connection, runner, payload validation) live in `ctf_common`; this module only wires them
to a Strands `Agent`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from strands import Agent

from ctf_common.auth import AgentCoreM2MTokenProvider, TokenProvider
from ctf_common.config import default_ssm_client, load_config
from ctf_common.hooks import (
    DEFAULT_WINDOW,
    ConnectionLostHook,
    StopRequestedHook,
    StripStaticWallsHook,
    TickCounterHook,
    make_conversation_manager,
)
from ctf_common.mcp import open_mcp_client, raise_on_auth_failure
from ctf_common.prompts import MODEL_ALIASES, SYSTEM_PROMPT, build_instruction
from ctf_common.runner import AsyncTaskApp, MatchContext, MatchRunner, PlayFn, make_preflight

RUNTIME = "harness_sdk"

log = logging.getLogger(RUNTIME)

McpFactory = Callable[[str, TokenProvider], Any]


def play(ctx: MatchContext, *, mcp_factory: McpFactory = open_mcp_client, model: Any = None) -> None:
    """Play one character until match_over, a stop request, or a failure.

    A fresh Agent (and therefore empty message history) is built for every match. The MCP
    connection is opened here, which costs exactly one token (see `ctf_common.mcp`).
    """
    request = ctx.request
    window = int(request.options.get("window", DEFAULT_WINDOW))
    client = mcp_factory(ctx.config.mcp_url, ctx.tokens)
    log.info(
        "match %s: %s joining as '%s' (team=%s, model=%s)",
        request.match_id,
        RUNTIME,
        request.name,
        request.team,
        request.model,
    )
    connection = ConnectionLostHook()
    with raise_on_auth_failure():
        with client:
            agent = Agent(
                name=request.name,
                model=model if model is not None else MODEL_ALIASES[request.model],
                system_prompt=SYSTEM_PROMPT,
                tools=client.list_tools_sync(),
                conversation_manager=make_conversation_manager(window),
                hooks=[
                    StripStaticWallsHook(),
                    TickCounterHook(ctx.tick),
                    StopRequestedHook(ctx.stop),
                    connection,
                ],
                callback_handler=None,
            )
            agent(build_instruction(request.match_id, request.name, request.team))
    if connection.lost:
        # Reached only if closing the client did not already raise the underlying error.
        raise RuntimeError("MCP connection lost mid-match")
    log.info("match %s: %s finished", request.match_id, request.name)


def build_runner(
    app: AsyncTaskApp,
    env: Mapping[str, str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    ssm_client: Any | None = None,
    play: PlayFn = play,
) -> MatchRunner:
    """Wire the runner. Nothing touches AWS until a `start` arrives (preflight), so /ping stays healthy."""
    environment = os.environ if env is None else env

    def preflight(request):
        tokens = token_provider or AgentCoreM2MTokenProvider.from_env(environment)

        def load():
            return load_config(ssm_client or default_ssm_client_if_needed(environment), environment)

        return make_preflight(load, tokens)(request)

    return MatchRunner(app, play, preflight, runtime=RUNTIME)


def default_ssm_client_if_needed(env: Mapping[str, str]) -> Any | None:
    """Skip creating an SSM client when the MCP URL is overridden locally."""
    return None if env.get("CTF_MCP_URL", "").strip() else default_ssm_client()
