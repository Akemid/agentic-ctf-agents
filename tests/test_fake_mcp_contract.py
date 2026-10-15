"""The FakeGame must mirror the game server's MCP turn contract (v2), not the pre-v2 one.

The contract is written out here, independently of the fake, from the server's `docs/mcp-agents.md`
(`packages/server/src/mcp/tools.ts`). If the fake drifts back to the old shape these tests fail, which is
the point: a fake that encodes an outdated contract makes every other test pass while the real agents
degrade silently against the real server.
"""

import json
from contextlib import AsyncExitStack

import pytest
from mcp.client.session import ClientSession

from fake_mcp import FakeGame, in_memory_transport, make_route

JOIN_KEYS = {"agent_id", "team", "agent_index", "ticks", "deadline_ms", "grid", "min_tick_ms", "map"}
MAP_KEYS = {"grid", "walls", "fields", "flag_bases", "ascii", "legend"}
TURN_KEYS = {"status", "tick", "deadline_ms", "deadline_remaining_ms", "state", "events", "last_action"}
STATIC_STATE_KEYS = {"grid", "walls", "fields"}


async def call(session, name, **arguments):
    """(is_error, payload) of one tool call."""
    result = await session.call_tool(name, arguments)
    return result.isError, json.loads(result.content[0].text)


class Client:
    def __init__(self, game):
        self.game = game
        self._stack = AsyncExitStack()
        self.session: ClientSession

    async def __aenter__(self):
        try:
            read, write, _ = await self._stack.enter_async_context(in_memory_transport(self.game.server))
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)

    async def join(self, team="red"):
        return await call(self.session, "join_match", match_id="m1", team=team)

    async def tool(self, name, **arguments):
        return await call(self.session, name, **arguments)


class TestJoinResult:
    async def test_join_returns_the_documented_keys_and_the_static_map(self):
        async with Client(FakeGame(turns=1)) as c:
            is_error, joined = await c.join()
        assert not is_error
        assert set(joined) == JOIN_KEYS
        assert set(joined["map"]) == MAP_KEYS
        assert joined["team"] == "red" and joined["agent_id"].startswith("red-")

    async def test_the_map_is_self_consistent(self):
        async with Client(FakeGame(turns=1)) as c:
            _, joined = await c.join()
        board = joined["map"]
        assert board["grid"] == joined["grid"]
        assert len(board["ascii"]) == board["grid"]["height"]
        assert {len(row) for row in board["ascii"]} == {board["grid"]["width"]}
        fields = board["fields"]
        assert set(fields) == {"red", "blue", "neutral_x"}
        assert set(fields["red"]) == {"min_x", "max_x"}
        assert set(board["flag_bases"]) == {"red", "blue"}

    async def test_a_full_team_is_a_tool_error_with_a_reason(self):
        async with Client(FakeGame(turns=1, reject_teams=("red",))) as c:
            is_error, payload = await c.join("red")
        assert is_error and payload["reason"] == "match_full"

    async def test_joining_a_finished_match_is_a_match_finished_error(self):
        async with Client(FakeGame(turns=1, finished=True)) as c:
            is_error, payload = await c.join()
        assert is_error and payload["reason"] == "match_finished"


class TestGetMap:
    async def test_before_joining_it_is_a_not_joined_tool_error(self):
        async with Client(FakeGame(turns=1)) as c:
            is_error, payload = await c.tool("get_map")
        assert is_error and payload == {"reason": "not_joined"}

    async def test_after_joining_it_returns_the_same_object_as_join_match_map(self):
        async with Client(FakeGame(turns=1)) as c:
            _, joined = await c.join()
            is_error, board = await c.tool("get_map")
            _, again = await c.tool("get_map")
        assert not is_error and board == joined["map"] == again


class TestTurn:
    async def test_a_turn_has_the_documented_keys_and_no_static_board_data(self):
        async with Client(
            FakeGame(turns=2, state={"flags": [{"team": "red", "position": {"x": 1, "y": 7}}]})
        ) as c:
            await c.join()
            _, turn = await c.tool("wait_for_turn")
        assert set(turn) == TURN_KEYS and turn["status"] == "turn"
        assert STATIC_STATE_KEYS.isdisjoint(turn["state"])
        assert all("base" not in flag for flag in turn["state"]["flags"])
        assert turn["last_action"] is None  # first turn

    async def test_wait_for_turn_before_joining_is_a_not_joined_tool_error(self):
        async with Client(FakeGame(turns=1)) as c:
            is_error, payload = await c.tool("wait_for_turn")
        assert is_error and payload == {"reason": "not_joined"}

    @pytest.mark.parametrize(
        "tool,args,expected",
        [
            ("move", {"direction": "up"}, {"action": "move:up", "result": "moved"}),
            ("tag", {"enemy_id": "blue-1"}, {"action": "tag:blue-1", "result": "tagged"}),
            ("grab_flag", {}, {"action": "grab_flag", "result": "grabbed"}),
            ("wait", {}, {"action": "wait", "result": "waited"}),
        ],
    )
    async def test_last_action_reports_the_previous_action_of_this_session(self, tool, args, expected):
        async with Client(FakeGame(turns=3)) as c:
            await c.join()
            _, first = await c.tool("wait_for_turn")
            await c.tool(tool, tick=first["tick"], **args)
            _, second = await c.tool("wait_for_turn")
        assert second["last_action"] == {"tick": first["tick"], **expected}

    async def test_routes_carry_closest_threat_and_never_min_enemy_distance(self):
        route = make_route("enemy_flag_direct", "enemy_flag", "left", 8, threat=("blue-1", 6, 2))
        assert "min_enemy_distance" not in route
        assert route["closest_threat"] == {"enemy_id": "blue-1", "step": 6, "distance": 2}
        assert make_route("x", "enemy_flag", None, 0)["closest_threat"] is None
        async with Client(FakeGame(turns=1, state={"routes": [route]})) as c:
            await c.join()
            _, turn = await c.tool("wait_for_turn")
        assert turn["state"]["routes"] == [route]

    @pytest.mark.parametrize(
        "extra",
        [
            {"walls": []},
            {"grid": {"width": 15, "height": 15}},
            {"fields": {}},
            {"flags": [{"team": "red", "base": {"x": 1, "y": 7}}]},
            {"routes": [{"id": "r", "min_enemy_distance": 3}]},
        ],
    )
    def test_the_fake_refuses_state_that_belongs_to_the_old_contract(self, extra):
        with pytest.raises(ValueError, match="contract"):
            FakeGame(turns=1, state=extra)


class TestMatchOverAndAcks:
    async def test_match_over_has_score_winner_and_reason(self):
        async with Client(FakeGame(turns=0)) as c:
            await c.join()
            _, over = await c.tool("wait_for_turn")
        assert set(over) == {"status", "score", "winner", "reason"}
        assert over["status"] == "match_over" and over["reason"] == "completed"
        assert over["winner"] in {"red", "blue", "tie"}  # the server never sends "draw"
        assert set(over["score"]) == {"red", "blue"}

    async def test_a_stopped_match_has_no_winner(self):
        async with Client(FakeGame(turns=0, stopped=True)) as c:
            await c.join()
            _, over = await c.tool("wait_for_turn")
        assert over["reason"] == "stopped" and over["winner"] is None

    async def test_a_skipped_tick_is_unknown_unless_the_server_recorded_a_deadline_rejection(self):
        # last-action.ts: without an accepted submission for the previous tick, `rejected/deadline` needs the
        # agent's own `action_rejected` event; otherwise the outcome is `unknown`.
        async with Client(FakeGame(turns=3)) as c:
            await c.join()
            await c.tool("wait_for_turn")  # tick 1, no action submitted
            _, turn = await c.tool("wait_for_turn")
        assert turn["last_action"] == {"action": "none", "tick": 1, "result": "unknown"}
        assert turn["events"] == []

    async def test_a_deadline_rejection_is_reported_when_the_server_would_emit_one(self):
        async with Client(FakeGame(turns=3, deadline_misses=True)) as c:
            await c.join()
            await c.tool("wait_for_turn")
            _, turn = await c.tool("wait_for_turn")
        rejected = {"result": "rejected", "reason": "deadline"}
        assert turn["last_action"] == {"action": "none", "tick": 1, **rejected}
        assert {"type": "action_rejected", "agent_id": "red-1", "tick": 1, "reason": "deadline"} in turn[
            "events"
        ]

    async def test_actions_are_acknowledged_with_status_and_tick(self):
        async with Client(FakeGame(turns=2)) as c:
            await c.join()
            _, turn = await c.tool("wait_for_turn")
            is_error, ack = await c.tool("move", tick=turn["tick"], direction="up")
        assert not is_error and ack == {"status": "accepted", "tick": turn["tick"]}

    async def test_an_action_before_joining_is_rejected_not_joined(self):
        async with Client(FakeGame(turns=1)) as c:
            is_error, ack = await c.tool("wait", tick=1)
        assert not is_error and ack == {"status": "rejected", "tick": 1, "reason": "not_joined"}

    async def test_an_action_after_match_over_is_rejected_match_over(self):
        async with Client(FakeGame(turns=0)) as c:
            await c.join()
            await c.tool("wait_for_turn")  # match_over
            is_error, ack = await c.tool("wait", tick=1)
        assert not is_error and ack == {"status": "rejected", "tick": 1, "reason": "match_over"}

    async def test_an_action_can_be_rejected_with_a_gateway_reason(self):
        async with Client(FakeGame(turns=1, reject_actions="match_not_started")) as c:
            await c.join()
            _, turn = await c.tool("wait_for_turn")
            _, ack = await c.tool("wait", tick=turn["tick"])
        assert ack == {"status": "rejected", "tick": turn["tick"], "reason": "match_not_started"}
