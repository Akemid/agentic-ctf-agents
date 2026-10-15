"""strand_decider runtime: a Strands Agent whose `tag` and `grab_flag` calls are reviewed by the decider.

Same agent as `harness_sdk` (model, prompt, MCP tools, bounded history, act-fast prompt) plus the
`ToolCallReviewer` intervention from `gate.py`. The decider is the SageMaker endpoint in AgentCore mode and a
local `strands-decider serve` when `CTF_DECIDER_URL` is set.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ctf_common import wiring
from ctf_common.auth import TokenProvider
from ctf_common.decider import DeciderPort, make_decider
from ctf_common.mcp import open_mcp_client
from ctf_common.runner import AsyncTaskApp, MatchContext, MatchRunner, PlayFn
from ctf_common.strands_match import McpFactory, play_strands_match
from gate import ToolCallReviewer

RUNTIME = "strand_decider"


def build_decider(ctx: MatchContext) -> DeciderPort:
    """A fresh decider (and circuit breaker) per match, from the config resolved at preflight."""
    return make_decider(ctx.config)


def play(
    ctx: MatchContext,
    *,
    mcp_factory: McpFactory = open_mcp_client,
    model: Any = None,
    decider: DeciderPort | None = None,
) -> None:
    """Play one character until match_over, a stop request, or a failure."""
    reviewer = ToolCallReviewer(decider or build_decider(ctx))
    play_strands_match(ctx, runtime=RUNTIME, mcp_factory=mcp_factory, model=model, interventions=[reviewer])


def build_runner(
    app: AsyncTaskApp,
    env: Mapping[str, str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    ssm_client: Any | None = None,
    play: PlayFn = play,
) -> MatchRunner:
    """Wire the runner. The preflight also resolves the decider endpoint name (SSM)."""
    return wiring.build_runner(
        app,
        play,
        runtime=RUNTIME,
        env=env,
        token_provider=token_provider,
        ssm_client=ssm_client,
        needs_decider=True,
    )
