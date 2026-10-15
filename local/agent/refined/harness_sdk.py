"""REFINED minimal CTF agent built with the plain Strands SDK (no strands_harness).

Refined version of `agent/agent-harness-sdk.py` (the original is untouched). The agent owns its
loop, so every tick costs a full LLM turn over the whole conversation. Changes to keep that cheap:

- Bounded history: `SlidingWindowConversationManager` keeps roughly the last 3 ticks and pins the
  initial instruction (`--window`).
- Act-fast prompt: wait_for_turn, then exactly one action, no narration, no repeated get_rules.
- `StripStaticWallsHook`: `state.walls` is static, so it is removed from `wait_for_turn` results
  after the first successful turn (the first one keeps it).
- Default `--name` is `refined-sdk`; the initial instruction is in English.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AfterToolCallEvent
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
    "min_enemy_distance, then call move with that route's first_step. A tag route with "
    "length 0 means the enemy is adjacent: call tag with that enemy_id. Call grab_flag when "
    "standing on the enemy flag. Call exactly one action per tick, with the current tick."
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


class StripStaticWallsHook:
    """Removes the static `state.walls` list from `wait_for_turn` results after the first turn.

    The map never changes during a match, so repeating it every tick only grows the context and
    the input tokens. The first successful turn keeps it so the model sees the map once.
    Verified in strands hooks/events.py: `AfterToolCallEvent.result` is writable, and the
    executor uses the (possibly replaced) result. MCP tool names carry the server prefix
    (`ctf-game_wait_for_turn`), hence the suffix match.
    """

    def __init__(self) -> None:
        self.walls_seen = False

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    @staticmethod
    def _is_wait_for_turn(name: Any) -> bool:
        return isinstance(name, str) and (name == "wait_for_turn" or name.endswith("_wait_for_turn"))

    def after_tool_call(self, event: Any) -> None:
        if not self._is_wait_for_turn((getattr(event, "tool_use", None) or {}).get("name")):
            return
        result = getattr(event, "result", None)
        if not isinstance(result, dict) or result.get("status") == "error":
            return
        content = result.get("content")
        if not isinstance(content, list):
            return
        for index, block in enumerate(content):
            text = block.get("text") if isinstance(block, dict) else None
            if not isinstance(text, str):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return
            state = payload.get("state") if isinstance(payload, dict) else None
            if not isinstance(state, dict) or "walls" not in state:
                return
            if not self.walls_seen:
                self.walls_seen = True
                return
            del state["walls"]
            new_content = list(content)
            new_content[index] = {**block, "text": json.dumps(payload)}
            event.result = {**result, "content": new_content}
            return


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
        hooks=[StripStaticWallsHook()],
    )

    agent(build_instruction(args.match, args.name))


if __name__ == "__main__":
    main()
