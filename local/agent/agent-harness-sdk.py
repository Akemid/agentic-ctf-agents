"""Minimal CTF agent built with the plain Strands SDK (no strands_harness).

This mirrors what ``create_harness`` does, but keeps ONLY what a CTF agent needs:
a model, a system prompt, and the game's MCP tools. All the harness extras
(shell/read/write/edit tools, web search, subagents, context manager, telemetry)
are intentionally dropped — they add nothing to a turn-based game loop.

Session and memory persistence are available but off by default (see notes below).
"""

import argparse

from strands import Agent
from strands.tools.mcp import MCPClient

# Mapeo de alias de modelo (CLI) -> model id de Bedrock.
# IDs verificados por invocación real (Converse) en la cuenta/región us-east-2.
# Nota: opus 5.5 y haiku 5.5 NO tienen acceso habilitado en esta cuenta.
#
# IMPORTANTE: a diferencia de create_harness, el Agent nativo de Strands NO
# interpreta el prefijo "bedrock/" como proveedor — pasa el string tal cual a la
# API de Bedrock. Por eso aquí los IDs van SIN el prefijo "bedrock/".
MODELS = {
    "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "opus": "global.anthropic.claude-opus-4-6-v1",
    "nova-lite": "global.amazon.nova-2-lite-v1:0",
}

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
)

# MCP server config. The SDK auto-detects transport from the fields present:
# a "url" key selects streamable-http. (The harness used a "type" key, which the
# plain SDK does not recognize — that was the source of the "unknown_keys=['type']"
# warning. We drop it here.)
MCP_SERVERS = {
    "ctf-game": {
        "url": "http://localhost:8080/mcp",
        "transport": "streamable-http",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AmazingPlayer CTF agent (plain Strands SDK).")
    parser.add_argument(
        "--model",
        choices=sorted(MODELS.keys()),
        default="haiku",
        help="Model to run the agent with (default: haiku).",
    )
    parser.add_argument(
        "--match",
        default="llm-1",
        help="Match id to join (default: llm-1).",
    )
    parser.add_argument(
        "--name",
        default="AmazingPlayer",
        help="Agent name used to join the match (default: AmazingPlayer).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    model_id = MODELS[args.model]

    # load_servers returns one MCPClient per server. In the modern SDK each client
    # is a ToolProvider: passing it to Agent(tools=...) lets the agent's tool
    # registry start the connection and manage its lifecycle. Do NOT also open it
    # with `with` — that double-starts the session and raises
    # "the client session is currently running".
    clients = MCPClient.load_servers(MCP_SERVERS, prefix_with_server_name=True)

    agent = Agent(
        name=args.name,
        model=model_id,
        system_prompt=SYSTEM_PROMPT,
        tools=clients,  # MCP clients expose their discovered tools to the agent
    )

    agent(
        f"Únete a la partida {args.match} a un equipo que este disponible "
        f"(primero intenta con red) con el nombre '{args.name}' y juega "
        "llamando a wait_for_turn y a una acción en cada turno hasta recibir match_over."
    )


if __name__ == "__main__":
    main()
