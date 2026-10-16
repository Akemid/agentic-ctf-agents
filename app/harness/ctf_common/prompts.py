"""Prompts and model aliases shared by every agent runtime.

Kept in lockstep with `local/agent/refined/harness_sdk.py` (`tests/test_prompts_hooks.py` compares them).
The prompt follows the game server's MCP turn contract v2: static board data comes from `join_match.map` /
`get_map`, route options carry `closest_threat`, and every turn carries `last_action`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# Bare Bedrock inference-profile ids. The `harness` runtime prefixes them with "bedrock/".
MODEL_ALIASES: dict[str, str] = {
    "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "opus": "global.anthropic.claude-opus-4-6-v1",
    "nova-lite": "global.amazon.nova-2-lite-v1:0",
}

ACT_FAST_PROMPT = (
    "Each tick: call wait_for_turn, then immediately call exactly one action tool with that tick. "
    "Do not write any text between tool calls. Do not call get_rules again after joining."
)

SYSTEM_PROMPT = (
    "You are AmazingPlayer, a Capture the Flag game agent competing as a member of a "
    "team. You are direct and tactical—make quick strategic decisions to advance "
    "your team's objectives. Use the MCP connection to interact with the game state, "
    "communicate team positions, coordinate flag captures, and respond to enemy movements "
    "with speed and precision. Analyze game state efficiently and prioritize actions that "
    "give your team the best advantage."
    " Each wait_for_turn result includes state.routes: route options computed by the "
    "server (ids like enemy_flag_direct, enemy_flag_safe, enemy_flag_detour, own_base_*, "
    "tag:<enemy_id>, defend_flag). The server never recommends one; you choose. Weigh each "
    "route's length, blocked_now/blocked_reason, risks (cells where you can be tagged) and "
    "closest_threat (the nearest enemy that can tag you along that route, as enemy_id, step and "
    "distance in moves around walls, or null when there is none), then call move with that "
    "route's first_step. A tag route with "
    "length 0 means the enemy is adjacent: call tag with that enemy_id. Call grab_flag when "
    "standing on the enemy flag. Call exactly one action per tick, with the current tick."
    " The board never changes, so turns do not repeat it: join_match returns it once as `map` "
    "(grid, walls, fields, flag_bases) and get_map returns it again at any time. If you lose "
    "track of the layout, call get_map. Each turn also carries last_action, the outcome of your "
    "previous action (null on your first turn); use it to notice a blocked or rejected move."
    " " + ACT_FAST_PROMPT
)

_PLAY_SUFFIX = "and play by calling wait_for_turn and one action each tick until you receive match_over."


def build_instruction(match: str, name: str, team: str = "auto", agent_index: int | None = None) -> str:
    """The first user message when the MODEL calls `join_match` (the `harness` runtime).

    `auto` keeps the original wording (red first, any free team). The seat is informative only: the runtime
    forces the real `join_match` arguments with `ForceJoinArgsHook`, so the prompt is not load-bearing.
    """
    if team == "auto":
        side = "on whichever team is available (try red first)"
    elif team in ("red", "blue"):
        side = f"on team {team}"
        if agent_index is not None:
            side += f", seat {agent_index + 1}"
    else:
        raise ValueError(f"team must be red, blue or auto, got {team!r}")
    return f"Join match {match} {side} with the name '{name}' {_PLAY_SUFFIX}"


def build_seated_instruction(match: str, name: str, joined: Mapping[str, Any]) -> str:
    """The first user message when the RUNTIME already called `join_match` in code.

    The join result (which carries the static `map`) is the model's only copy of it, so it is included once.
    """
    seat = f"team {joined['team']}, seat {int(joined['agent_index']) + 1}"
    body = json.dumps(joined, separators=(",", ":"))
    return (
        f"You have already joined match {match} as '{name}' ({seat}); do not call join_match. "
        f"The join result was: {body}. Now play by calling wait_for_turn and one action each tick "
        "until you receive match_over."
    )
