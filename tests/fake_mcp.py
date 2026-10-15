"""An in-memory stand-in for the agentic-ctf MCP server (real MCP protocol, no network).

It mirrors the server's MCP turn contract v2 (`packages/server/src/mcp/tools.ts`, `docs/mcp-agents.md`):

- `get_map` (joined sessions only) and `join_match.map` carry the static board; the turn `state` does not
  (no `grid`, `walls`, `fields` or `flags[].base`);
- every turn carries a top-level `last_action` (null on the first turn);
- `routes[]` carry `closest_threat`, never `min_enemy_distance`;
- `match_over` is `{score, winner, reason}`; action acks are `accepted` / `rejected` with a reason.

`tests/test_fake_mcp_contract.py` pins this shape independently of the fake.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, TextContent

WALLS = [{"x": 1, "y": 1}, {"x": 2, "y": 1}]
GRID = {"width": 15, "height": 15}
FIELDS = {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7}
FLAG_BASES = {"red": {"x": 1, "y": 7}, "blue": {"x": 13, "y": 7}}
LEGEND = {
    "#": "wall",
    ".": "open cell",
    ":": "neutral column (open cell on the line between the two fields)",
    "R": "red flag base",
    "B": "blue flag base",
}


def _ascii(walls: list[dict]) -> list[str]:
    """One string per row, one glyph per column. Precedence: wall, flag base, neutral column, open."""
    wall_cells = {(w["x"], w["y"]) for w in walls}
    red, blue = FLAG_BASES["red"], FLAG_BASES["blue"]
    rows = []
    for y in range(GRID["height"]):
        row = ""
        for x in range(GRID["width"]):
            if (x, y) in wall_cells:
                row += "#"
            elif (x, y) == (red["x"], red["y"]):
                row += "R"
            elif (x, y) == (blue["x"], blue["y"]):
                row += "B"
            elif x == FIELDS["neutral_x"]:
                row += ":"
            else:
                row += "."
        rows.append(row)
    return rows


MAP: dict[str, Any] = {
    "grid": GRID,
    "walls": WALLS,
    "fields": FIELDS,
    "flag_bases": FLAG_BASES,
    "ascii": _ascii(WALLS),
    "legend": LEGEND,
}

# Keys the server no longer sends in a turn state: the fake refuses them so a test cannot reintroduce them.
_STATIC_STATE_KEYS = ("grid", "walls", "fields")


def make_route(
    rid: str,
    goal: str,
    first_step: str | None,
    length: int,
    *,
    risks: tuple[tuple[int, str], ...] = (),
    blocked: bool = False,
    threat: tuple[str, int, int] | None = None,
    path: list[dict] | None = None,
) -> dict[str, Any]:
    """A route in the v2 shape. `threat` is `(enemy_id, step, distance)` for `closest_threat`."""
    return {
        "id": rid,
        "goal": goal,
        "first_step": first_step,
        "length": length,
        "path": path if path is not None else [],
        "blocked_now": blocked,
        "blocked_reason": "contention" if blocked else None,
        "risks": [{"cell": {"x": 0, "y": 0}, "step": step, "enemy_id": enemy} for step, enemy in risks],
        "closest_threat": (
            None if threat is None else {"enemy_id": threat[0], "step": threat[1], "distance": threat[2]}
        ),
    }


def _check_state_matches_contract(state: dict) -> None:
    stale = [key for key in _STATIC_STATE_KEYS if key in state]
    if stale:
        raise ValueError(f"state keys {stale} are not part of the v2 turn contract (use join_match.map)")
    for flag in state.get("flags") or []:
        if "base" in flag:
            raise ValueError("flags[].base is not part of the v2 turn contract (use join_match.map)")
    for route in state.get("routes") or []:
        if "min_enemy_distance" in route:
            raise ValueError("min_enemy_distance is not part of the v2 turn contract (use closest_threat)")


def _tool_error(reason: str, detail: str | None = None) -> CallToolResult:
    payload = {"reason": reason} if detail is None else {"reason": reason, "detail": detail}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], isError=True)


class FakeGame:
    """wait_for_turn yields `turns` turns, then match_over. Actions are recorded."""

    def __init__(
        self,
        turns: int = 3,
        *,
        state: dict | None = None,
        deadline_ms: int = 5000,
        deadline_remaining_ms: int | None = None,
        reject_teams: tuple[str, ...] = (),
        finished: bool = False,
        stopped: bool = False,
        include_map_in_join: bool = True,
        serve_get_map: bool = True,
        reject_actions: str | None = None,
        deadline_misses: bool = False,
    ) -> None:
        self.turns = turns
        self.extra_state = state or {}  # merged into every turn's `state` (routes, moves, agents, ...)
        _check_state_matches_contract(self.extra_state)
        self.deadline_ms = deadline_ms
        self.deadline_remaining_ms = deadline_ms if deadline_remaining_ms is None else deadline_remaining_ms
        self.reject_teams = reject_teams
        self.finished = finished  # joining answers `match_finished`
        self.stopped = stopped  # match_over has reason "stopped" and no winner
        self.include_map_in_join = include_map_in_join  # False simulates a server without `join_match.map`
        self.serve_get_map = serve_get_map  # False: get_map answers a tool error (a server without the tool)
        self.reject_actions = reject_actions  # an ack reason (e.g. "match_not_started") for every action
        # True: a tick without an accepted action also carries the agent's own `action_rejected(deadline)`
        # event, as the server does when it misses the deadline. False: such a tick reports `unknown`.
        self.deadline_misses = deadline_misses
        self.tick = 0
        self.joined = False
        self.over = False
        self.actions: list[tuple[str, dict]] = []
        self._last: tuple[int, str] | None = None  # (tick, label) of the latest accepted action
        self._previous_turn_tick: int | None = None
        self.server = FastMCP("ctf-game")
        self._register()

    def _ack(self, tick: int, label: str) -> str:
        if not self.joined:
            return json.dumps({"status": "rejected", "tick": tick, "reason": "not_joined"})
        if self.over:
            return json.dumps({"status": "rejected", "tick": tick, "reason": "match_over"})
        if self.reject_actions is not None:
            return json.dumps({"status": "rejected", "tick": tick, "reason": self.reject_actions})
        self._last = (tick, label)
        return json.dumps({"status": "accepted", "tick": tick})

    def _last_action(self, tick: int) -> dict | None:
        """Mirror of the server's `deriveLastAction` (last-action.ts) for the turn at `tick`.

        null on the first turn; the label and outcome of this session's own accepted action for the previous
        tick; with none, `rejected/deadline` only when the agent's own `action_rejected(deadline)` event is
        present (`deadline_misses`), otherwise `unknown`. Ticks are never skipped here, so the
        "previous delivered tick is not tick - 1" branch of the server cannot occur.
        """
        if self._previous_turn_tick is None:
            return None
        expected = tick - 1
        if self._last is None or self._last[0] != expected:
            if self.deadline_misses:
                return {"action": "none", "tick": expected, "result": "rejected", "reason": "deadline"}
            return {"action": "none", "tick": expected, "result": "unknown"}
        label = self._last[1]
        result = {"move": "moved", "tag": "tagged", "grab_flag": "grabbed", "wait": "waited"}[
            label.split(":", 1)[0]
        ]
        return {"action": label, "tick": expected, "result": result}

    def _events(self, tick: int) -> list[dict]:
        if self.deadline_misses and self._previous_turn_tick is not None:
            if self._last is None or self._last[0] != tick - 1:
                rejection = {"type": "action_rejected", "agent_id": "red-1", "tick": tick - 1}
                return [{**rejection, "reason": "deadline"}]
        return []

    def _register(self) -> None:
        game = self

        @self.server.tool()
        def get_rules() -> str:
            """Return the game rules."""
            return json.dumps({"rules": "fake", "tick_model": "fake", "actions": []})

        @self.server.tool()
        def get_map() -> Any:
            """Return the static board map (joined sessions only)."""
            game.actions.append(("get_map", {}))
            if not game.serve_get_map:
                return _tool_error("unknown_tool")
            if not game.joined:
                return _tool_error("not_joined")
            return json.dumps(MAP)

        @self.server.tool()
        def join_match(
            match_id: str, team: str, agent_index: int | None = None, name: str | None = None
        ) -> Any:
            """Join a match."""
            game.actions.append(
                ("join_match", {"match_id": match_id, "name": name or "mcp-fake", "team": team})
            )
            if game.finished:
                return _tool_error("match_finished")
            if team in game.reject_teams:
                return _tool_error("match_full")
            game.joined = True
            index = 0 if agent_index is None else agent_index
            result: dict[str, Any] = {
                "agent_id": f"{team}-{index + 1}",
                "team": team,
                "agent_index": index,
                "ticks": game.turns,
                "deadline_ms": game.deadline_ms,
                "grid": GRID,
                "min_tick_ms": 0,
            }
            if game.include_map_in_join:
                result["map"] = MAP
            return json.dumps(result)

        @self.server.tool()
        def wait_for_turn(timeout_ms: int | None = None) -> Any:
            """Block until it is this agent's turn."""
            if not game.joined:
                return _tool_error("not_joined")
            if game.tick >= game.turns:
                game.over = True
                return json.dumps(
                    {
                        "status": "match_over",
                        "score": {"red": 0, "blue": 0},
                        "winner": None if game.stopped else "tie",
                        "reason": "stopped" if game.stopped else "completed",
                    }
                )
            game.tick += 1
            state = {"tick": game.tick, "self": "red-1", **game.extra_state}
            turn = {
                "status": "turn",
                "tick": game.tick,
                "deadline_ms": game.deadline_ms,
                "deadline_remaining_ms": game.deadline_remaining_ms,
                "state": state,
                "events": game._events(game.tick),  # noqa: SLF001
                "last_action": game._last_action(game.tick),  # noqa: SLF001
            }
            game._previous_turn_tick = game.tick  # noqa: SLF001
            return json.dumps(turn)

        @self.server.tool()
        def wait(tick: int) -> str:
            """Do nothing this tick."""
            game.actions.append(("wait", {"tick": tick}))
            return game._ack(tick, "wait")  # noqa: SLF001

        @self.server.tool()
        def tag(tick: int, enemy_id: str) -> str:
            """Tag an adjacent enemy."""
            game.actions.append(("tag", {"tick": tick, "enemy_id": enemy_id}))
            return game._ack(tick, f"tag:{enemy_id}")  # noqa: SLF001

        @self.server.tool()
        def grab_flag(tick: int) -> str:
            """Pick up the enemy flag."""
            game.actions.append(("grab_flag", {"tick": tick}))
            return game._ack(tick, "grab_flag")  # noqa: SLF001

        @self.server.tool()
        def move(tick: int, direction: str) -> str:
            """Move one step."""
            game.actions.append(("move", {"tick": tick, "direction": direction}))
            return game._ack(tick, f"move:{direction}")  # noqa: SLF001


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
