"""REFINED minimal CTF agent built with the plain Strands SDK (no strands_harness).

The agent owns its
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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prompts import ACT_FAST_PROMPT, DIRECT_SYSTEM_PROMPT  # noqa: E402, F401
from prompts import SDK_SYSTEM_PROMPT as SYSTEM_PROMPT  # noqa: E402
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.tools.mcp import MCPClient

MODELS = {
    "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "opus": "global.anthropic.claude-opus-4-6-v1",
    "nova-lite": "global.amazon.nova-2-lite-v1:0",
}

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


PROMPT_CHOICES = ("refined", "direct")
DEFAULT_NAME = "refined-sdk"
DIRECT_NAME = "refined-direct-sdk"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refined AmazingPlayer CTF agent (plain Strands SDK).")
    parser.add_argument("--model", choices=sorted(MODELS.keys()), default="haiku",
                        help="Model to run the agent with (default: haiku).")
    parser.add_argument("--match", default="llm-1", help="Match id to join (default: llm-1).")
    parser.add_argument("--name", default=None,
                        help=f"Agent name used to join the match (default: {DEFAULT_NAME}; {DIRECT_NAME} with --prompt direct).")
    parser.add_argument("--prompt", choices=PROMPT_CHOICES, default="refined",
                        help="System prompt: the refined act-fast prompt or the short direct A/B prompt (default: refined).")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help=f"Conversation window in messages, 4 per tick (default: {DEFAULT_WINDOW}, ~3 ticks).")
    args = parser.parse_args()
    args.name = args.name or (DIRECT_NAME if args.prompt == "direct" else DEFAULT_NAME)
    return args


def main() -> None:
    args = parse_args()
    system_prompt = DIRECT_SYSTEM_PROMPT if args.prompt == "direct" else SYSTEM_PROMPT
    clients = MCPClient.load_servers(MCP_SERVERS, prefix_with_server_name=True)

    agent = Agent(
        name=args.name,
        model=MODELS[args.model],
        system_prompt=system_prompt,
        tools=clients,  # MCP clients expose their discovered tools to the agent
        conversation_manager=make_conversation_manager(args.window),
    )

    agent(build_instruction(args.match, args.name))


if __name__ == "__main__":
    main()
