"""An in-memory stand-in for the agentic-ctf MCP server (real MCP protocol, no network)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import anyio
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

WALLS = [{"x": 1, "y": 1}, {"x": 2, "y": 1}]


class FakeGame:
    """wait_for_turn yields `turns` turns, then match_over. Actions are recorded."""

    def __init__(
        self,
        turns: int = 3,
        *,
        state: dict | None = None,
        deadline_remaining_ms: int | None = None,
        reject_teams: tuple[str, ...] = (),
    ) -> None:
        self.turns = turns
        self.extra_state = state or {}  # merged into every turn's `state` (routes, moves, agents, ...)
        self.deadline_remaining_ms = deadline_remaining_ms
        self.reject_teams = reject_teams
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
            if team in game.reject_teams:
                raise ValueError(f"team {team} is full")
            return json.dumps({"status": "joined", "match_id": match_id, "name": name, "team": team})

        @self.server.tool()
        def wait_for_turn() -> str:
            """Block until it is this agent's turn."""
            if game.tick >= game.turns:
                return json.dumps({"status": "match_over"})
            game.tick += 1
            state = {"tick": game.tick, "walls": WALLS, "self": "red-1", **game.extra_state}
            turn = {"status": "turn", "tick": game.tick, "state": state}
            if game.deadline_remaining_ms is not None:
                turn["deadline_remaining_ms"] = game.deadline_remaining_ms
            return json.dumps(turn)

        @self.server.tool()
        def wait(tick: int) -> str:
            """Do nothing this tick."""
            game.actions.append(("wait", {"tick": tick}))
            return json.dumps({"status": "ok"})

        @self.server.tool()
        def tag(tick: int, enemy_id: str) -> str:
            """Tag an adjacent enemy."""
            game.actions.append(("tag", {"tick": tick, "enemy_id": enemy_id}))
            return json.dumps({"status": "ok"})

        @self.server.tool()
        def grab_flag(tick: int) -> str:
            """Pick up the enemy flag."""
            game.actions.append(("grab_flag", {"tick": tick}))
            return json.dumps({"status": "ok"})

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


def handshake_ok_then_401_on_tool_call(request: httpx.Request) -> httpx.Response:
    """A minimal MCP server over plain JSON: initialize and tools/list succeed, tools/call is 401."""
    body = json.loads(request.content or b"{}")
    method, rpc_id = body.get("method"), body.get("id")
    if method == "tools/call":
        return httpx.Response(401, json={"error": "invalid_token"})
    if rpc_id is None:  # notifications (initialized, ...)
        return httpx.Response(202)
    if method == "initialize":
        result = {
            "protocolVersion": body["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ctf-game", "version": "0"},
        }
    elif method == "tools/list":
        schema = {"type": "object", "properties": {}}
        result = {"tools": [{"name": "join_match", "description": "Join.", "inputSchema": schema}]}
    else:
        result = {}
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc_id, "result": result})
