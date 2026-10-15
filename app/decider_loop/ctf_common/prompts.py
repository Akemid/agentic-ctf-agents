"""Prompts and model aliases shared by every agent runtime.

Extracted verbatim from `local/agent/refined/harness_sdk.py` (characterization-tested).
"""

from __future__ import annotations

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
    "min_enemy_distance, then call move with that route's first_step. A tag route with "
    "length 0 means the enemy is adjacent: call tag with that enemy_id. Call grab_flag when "
    "standing on the enemy flag. Call exactly one action per tick, with the current tick."
    " " + ACT_FAST_PROMPT
)

_PLAY_SUFFIX = "and play by calling wait_for_turn and one action each tick until you receive match_over."


def build_instruction(match: str, name: str, team: str = "auto") -> str:
    """The first user message. `auto` keeps the original wording (red first, any free team)."""
    if team == "auto":
        side = "on whichever team is available (try red first)"
    elif team in ("red", "blue"):
        side = f"on team {team}"
    else:
        raise ValueError(f"team must be red, blue or auto, got {team!r}")
    return f"Join match {match} {side} with the name '{name}' {_PLAY_SUFFIX}"
