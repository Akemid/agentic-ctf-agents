"""The Strands `Agent` match loop shared by `harness_sdk` and `strand_decider`.

One MCP connection (one token), a fresh Agent per match, and the runtime hooks wired to the
`MatchRunner`. Strands does not raise when the MCP connection drops mid-match: it returns an error
tool result, so `ConnectionLostHook` ends the loop and this function turns that into a failed match.
A 401 only surfaces when the client closes, where `raise_on_auth_failure` maps it to `McpAuthError`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from strands import Agent

from .auth import TokenProvider
from .hooks import (
    DEFAULT_WINDOW,
    ConnectionLostHook,
    StopRequestedHook,
    TickCounterHook,
    make_conversation_manager,
)
from .mcp import open_mcp_client, raise_on_auth_failure
from .payload import require_match_id
from .prompts import MODEL_ALIASES, SYSTEM_PROMPT, build_instruction
from .runner import MatchContext

McpFactory = Callable[[str, TokenProvider], Any]


def play_strands_match(
    ctx: MatchContext,
    *,
    runtime: str,
    mcp_factory: McpFactory = open_mcp_client,
    model: Any = None,
    interventions: Sequence[Any] = (),
) -> None:
    """Play one character until match_over, a stop request, or a failure.

    A fresh Agent (and therefore empty message history) is built for every match. The MCP
    connection is opened here, which costs exactly one token (see `ctf_common.mcp`).
    """
    log = logging.getLogger(runtime)
    request = ctx.request
    window = int(request.options.get("window", DEFAULT_WINDOW))
    client = mcp_factory(ctx.config.mcp_url, ctx.tokens)
    log.info(
        "match %s: %s joining as '%s' (team=%s, model=%s)",
        request.match_id,
        runtime,
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
                    TickCounterHook(ctx.tick),
                    StopRequestedHook(ctx.stop),
                    connection,
                ],
                interventions=list(interventions),
                callback_handler=None,
            )
            agent(build_instruction(require_match_id(request), request.name, request.team))
    if connection.lost:
        raise RuntimeError("MCP connection lost mid-match")
    log.info("match %s: %s finished", request.match_id, request.name)
