"""An in-memory stand-in for the agentic-ctf MCP server (real MCP protocol, no network)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

WALLS = [{"x": 1, "y": 1}, {"x": 2, "y": 1}]


class FakeGame:
    """wait_for_turn yields `turns` turns, then match_over. Actions are recorded."""

    def __init__(self, turns: int = 3) -> None:
        self.turns = turns
        self.tick = 0
        self.actions: list[tuple[str, dict]] = []
        self.server = FastMCP("ctf-game")
        self._register()

    def _register(self) -> None:
        game = self

        @self.server.tool()
        def join_match(match_id: str, name: str, team: str = "red") -> str:
            """Join a match."""
            game.actions.append(("join_match", {"match_id": match_id, "name": name, "team": team}))
            return json.dumps({"status": "joined", "match_id": match_id, "name": name, "team": team})

        @self.server.tool()
        def wait_for_turn() -> str:
            """Block until it is this agent's turn."""
            if game.tick >= game.turns:
                return json.dumps({"status": "match_over"})
            game.tick += 1
            state = {"tick": game.tick, "walls": WALLS, "self": "red-1"}
            return json.dumps({"status": "turn", "tick": game.tick, "state": state})

        @self.server.tool()
        def move(tick: int, direction: str) -> str:
            """Move one step."""
            game.actions.append(("move", {"tick": tick, "direction": direction}))
            return json.dumps({"status": "ok"})


@asynccontextmanager
async def in_memory_transport(server: FastMCP):
    """Transport context manager shaped like `streamable_http_client` (read, write, session-id getter)."""
    low = server._mcp_server  # noqa: SLF001 - the low-level server is the public way to run over raw streams
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(low.run, server_streams[0], server_streams[1], low.create_initialization_options())
            try:
                yield (*client_streams, lambda: None)
            finally:
                tg.cancel_scope.cancel()
