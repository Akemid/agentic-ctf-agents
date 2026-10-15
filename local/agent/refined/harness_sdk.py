"""REFINED minimal CTF agent built with the plain Strands SDK (no strands_harness).

Refined version of `agent/agent-harness-sdk.py` (the original is untouched). The agent owns its
loop, so every tick costs a full LLM turn over the whole conversation. Changes to keep that cheap:

- Bounded history: `SlidingWindowConversationManager` keeps roughly the last 3 ticks and pins the
  initial instruction (`--window`).
- Act-fast prompt: wait_for_turn, then exactly one action, no narration, no repeated get_rules.
- No static-data stripping: since the game server's MCP turn contract v2 the turn `state` no longer
  carries `grid`, `walls`, `fields` or `flags[].base`. The map comes from `join_match.map` / `get_map`
  and the system prompt tells the model to call `get_map` if it loses track of it (the join result
  leaves the sliding window after a few ticks).
- Default `--name` is `refined-sdk`; the initial instruction is in English.
"""

from __future__ import annotations

import argparse

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.tools.mcp import MCPClient

MODELS = {
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

MCP_SERVERS = {
    "ctf-game": {
        "url": "http://localhost:8080/mcp",
        "transport": "streamable-http",
    },
}

# One tick is 4 messages: assistant(wait_for_turn) + result + assistant(action) + result.
# 12 messages therefore keep about the last 3 ticks, enough to see where we are heading
# without paying for the whole match on every model call. The `window_size` limit counts
# messages (not tokens). `pin_first=1` protects the initial instruction. `per_turn=True`
# is REQUIRED: the whole match is ONE agent call, and by default management only runs at the
# end of a call, i.e. never. Verified in strands sliding_window_conversation_manager.py.
DEFAULT_WINDOW = 12


def make_conversation_manager(window: int = DEFAULT_WINDOW) -> SlidingWindowConversationManager:
    return SlidingWindowConversationManager(
        window_size=window,
        should_truncate_results=True,
        per_turn=True,
        pin_first=1,
    )


def build_instruction(match: str, name: str) -> str:
    return (f"Join match {match} on whichever team is available (try red first) with the name '{name}' "
            "and play by calling wait_for_turn and one action each tick until you receive match_over.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refined AmazingPlayer CTF agent (plain Strands SDK).")
    parser.add_argument("--model", choices=sorted(MODELS.keys()), default="haiku",
                        help="Model to run the agent with (default: haiku).")
    parser.add_argument("--match", default="llm-1", help="Match id to join (default: llm-1).")
    parser.add_argument("--name", default="refined-sdk",
                        help="Agent name used to join the match (default: refined-sdk).")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help=f"Conversation window in messages, 4 per tick (default: {DEFAULT_WINDOW}, ~3 ticks).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    clients = MCPClient.load_servers(MCP_SERVERS, prefix_with_server_name=True)

    agent = Agent(
        name=args.name,
        model=MODELS[args.model],
        system_prompt=SYSTEM_PROMPT,
        tools=clients,  # MCP clients expose their discovered tools to the agent
        conversation_manager=make_conversation_manager(args.window),
    )

    agent(build_instruction(args.match, args.name))


if __name__ == "__main__":
    main()
