"""Runner wiring shared by every runtime (design A2, A10).

Nothing touches AWS until a `start` arrives (the preflight), so `/ping` stays healthy while the
runtime is idle. The preflight reads the configuration (SSM, or the local `CTF_*` overrides) and
fetches one token; the match thread then reuses both.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from . import config as _config
from .auth import AgentCoreM2MTokenProvider, TokenProvider
from .config import ENV_DECIDER_URL, ENV_MCP_URL, load_config
from .payload import MatchRequest
from .runner import AsyncTaskApp, MatchRunner, PlayFn, make_preflight


def ssm_needed(env: Mapping[str, str], *, needs_decider: bool) -> bool:
    """False when local overrides cover every setting, so no SSM client has to be built."""
    if not env.get(ENV_MCP_URL, "").strip():
        return True
    return needs_decider and not env.get(ENV_DECIDER_URL, "").strip()


def build_runner(
    app: AsyncTaskApp,
    play: PlayFn,
    *,
    runtime: str,
    env: Mapping[str, str] | None = None,
    token_provider: TokenProvider | None = None,
    ssm_client: Any | None = None,
    ssm_client_factory: Callable[[], Any] | None = None,
    needs_decider: bool = False,
) -> MatchRunner:
    """Wire a `MatchRunner` for one runtime. `ssm_client` / `token_provider` are test seams."""
    environment = os.environ if env is None else env

    def preflight(request: MatchRequest):
        tokens = token_provider or AgentCoreM2MTokenProvider.from_env(environment)

        def load():
            ssm = ssm_client
            if ssm is None and ssm_needed(environment, needs_decider=needs_decider):
                ssm = (ssm_client_factory or _config.default_ssm_client)()  # resolved at call time
            return load_config(ssm, environment, needs_decider=needs_decider)

        return make_preflight(load, tokens)(request)

    return MatchRunner(app, play, preflight, runtime=runtime)
