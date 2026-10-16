"""harness runtime: one CTF character played by a `strands_harness` agent.

Ported from `local/agent/refined/harness.py`. Differences from the local script: the MCP server is
the authenticated agentic-ctf endpoint (bearer header, one token per harness build), the model id
carries the `bedrock/` prefix `create_harness` expects, session and memory live under
`CTF_AGENT_STATE_DIR`, and the runtime hooks (tick counter, stop, lost connection) are registered.

`create_harness` owns its context manager, which takes precedence over a conversation manager, so
there is no `window` option for this runtime (same as the original).

Unlike the other runtimes this one cannot join in code: `create_harness` builds and owns the MCP client
from the `mcp_servers` config, so the seat has to be taken through that same client, by the model's first
tool call. `ForceJoinArgsHook` overwrites the arguments of that call (match, name, team, agent_index),
reports the outcome to `start` through `ctx.join`, and ends the turn on a refusal. Because the model has to
get there (a Bedrock call, a cold start), this runtime waits longer for the join than the code-joined ones.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from strands_harness import create_harness

from ctf_common import wiring
from ctf_common.auth import TokenProvider
from ctf_common.config import RuntimeConfig
from ctf_common.hooks import ConnectionLostHook, ForceJoinArgsHook, StopRequestedHook, TickCounterHook
from ctf_common.mcp import DEFAULT_SERVER_NAME, raise_on_auth_failure
from ctf_common.payload import MatchRequest, require_match_id
from ctf_common.prompts import MODEL_ALIASES, SYSTEM_PROMPT, build_instruction
from ctf_common.runner import AsyncTaskApp, MatchContext, MatchRunner, PlayFn

RUNTIME = "harness"
# The model must reach its first tool call (cold start + one Bedrock turn) before the seat exists.
JOIN_TIMEOUT_S = 30.0
ENV_STATE_DIR = "CTF_AGENT_STATE_DIR"
DEFAULT_STATE_DIR = "/tmp/.agent"  # noqa: S108 - the only writable path in an AgentCore microVM

DESCRIPTION = (
    "A Capture the Flag game agent that plays as a member of a team, connecting via custom MCP server. "
    "Direct and tactical decision-making for fast-paced gameplay."
)

log = logging.getLogger(RUNTIME)

CreateHarness = Callable[..., Any]


def agent_state_dir(env: Mapping[str, str]) -> str:
    return env.get(ENV_STATE_DIR, "").strip() or DEFAULT_STATE_DIR


def harness_kwargs(
    request: MatchRequest,
    config: RuntimeConfig,
    token: str,
    *,
    state_dir: str,
    hooks: list[Any],
) -> dict[str, Any]:
    """Keyword arguments for `create_harness` (pure; the token is the only secret in here)."""
    return {
        "name": request.name,
        "description": DESCRIPTION,
        "model": f"bedrock/{MODEL_ALIASES[request.model]}",
        "instructions": SYSTEM_PROMPT,
        "effort": "low",
        "mcp_servers": {
            DEFAULT_SERVER_NAME: {
                "url": config.mcp_url,
                "headers": {"Authorization": f"Bearer {token}"},
                # create_harness defaults to skipping a server that fails to start, which would
                # silently leave the agent without game tools. A dead MCP server must fail the match.
                "continue_on_error": False,
            },
        },
        "builtin_tools": {"web_search": False, "programmatic_tool_caller": True, "subagent": False},
        "skills": False,
        "session": {"dir": f"{state_dir}/sessions"},
        "memory": {"dir": f"{state_dir}/memory"},
        "hooks": hooks,
        "callback_handler": None,
    }


def play(
    ctx: MatchContext,
    *,
    create: CreateHarness = create_harness,
    state_dir: str | None = None,
) -> None:
    """Play one character until match_over, a stop request, or a failure.

    The bearer header is fixed when the harness is built, so the match costs exactly one token.
    """
    request = ctx.request
    connection = ConnectionLostHook()
    match_id = require_match_id(request)
    force_join = ForceJoinArgsHook(match_id, request.name, request.team, request.agent_index, ctx.join)
    hooks = [TickCounterHook(ctx.tick), StopRequestedHook(ctx.stop), connection, force_join]
    kwargs = harness_kwargs(
        request,
        ctx.config,
        ctx.tokens.get_token(),
        state_dir=state_dir or agent_state_dir(os.environ),
        hooks=hooks,
    )
    log.info(
        "match %s: %s joining as '%s' (team=%s, model=%s)",
        request.match_id,
        RUNTIME,
        request.name,
        request.team,
        request.model,
    )
    with raise_on_auth_failure():
        agent = create(**kwargs)
        try:
            agent(build_instruction(match_id, request.name, request.team, request.agent_index))
            force_join.settle_unanswered()  # team:auto, red full, blue never tried: match_full
        finally:
            agent.cleanup()  # stops the MCP client; a 401 that killed the connection surfaces here
    if connection.lost:
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
    return wiring.build_runner(
        app,
        play,
        runtime=RUNTIME,
        env=env,
        token_provider=token_provider,
        ssm_client=ssm_client,
        join_timeout_s=JOIN_TIMEOUT_S,
    )
