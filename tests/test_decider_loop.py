"""decider_loop play loop: Python owns the loop, the decider picks the action, the LLM is the fallback."""

import json
import logging
import threading

import httpx
import pytest

import loop
import loop_logic as logic
from ctf_common.auth import StaticTokenProvider
from ctf_common.config import RuntimeConfig
from ctf_common.decider import DeciderCircuitOpen, DeciderTimeout, HttpDecider, SageMakerDecider
from ctf_common.mcp import McpAuthError, open_mcp_client
from ctf_common.payload import Action, MatchRequest
from ctf_common.runner import MatchContext
from fake_mcp import GRID, MAP, FakeGame, in_memory_transport, make_route
from fake_model import SequencePlayer

MOVES = {"up": "open", "down": "open", "left": "blocked", "right": "open"}
STATE = {"moves": MOVES}  # no server routes: options are move_<dir> + wait


class CountingTokens:
    def __init__(self):
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return f"token-{self.calls}"


class FakeDecider:
    """Answers `move_up` with a configurable confidence, or raises, and records every call."""

    def __init__(self, pick="move_up", confidence=0.95, threat=0.0, route_bad=0.0, *, raises=None):
        self.pick, self.confidence, self.threat, self.route_bad = pick, confidence, threat, route_bad
        self.raises = list(raises or [])
        self.asked: list[tuple[str, dict, float | None]] = []
        self.calls = 0
        self.input_tokens = 0
        self.last_latency_ms = 12.0
        self.last_queue_wait_ms = None

    def ask(self, state, questions, *, deadline_s=None):
        self.asked.append((state, questions, deadline_s))
        if self.raises:
            outcome = self.raises.pop(0)
            if outcome is not None:
                raise outcome
        self.calls += 1
        return {
            "action": {
                "type": "choice",
                "choice": self.pick,
                "probabilities": {self.pick: 0.9},
                "confidence": self.confidence,
            },
            "threat": {"type": "noul", "noul": self.threat},
            "route_bad": {"type": "noul", "noul": self.route_bad},
        }


def make_ctx(
    tokens=None, *, team="auto", options=None, stop=None, on_tick=None, config=None, agent_index=None
):
    ticks = []

    def tick():
        ticks.append(1)
        if on_tick:
            on_tick(len(ticks))

    request = MatchRequest(
        action=Action.START,
        match_id="m1",
        name="p1",
        team=team,
        model="haiku",
        options=options or {},
        agent_index=agent_index,
    )
    ctx = MatchContext(
        request=request,
        config=config
        or RuntimeConfig(mcp_url="https://mcp.example/mcp", decider_endpoint_name="ctf-decider"),
        tokens=tokens or CountingTokens(),
        stop=stop or threading.Event(),
        tick=tick,
    )
    return ctx, ticks


def play_against(game, ctx, decider, model=None):
    def mcp_factory(url, tokens):
        return open_mcp_client(
            url, tokens, prefix_tools=False, connect=lambda u, headers: in_memory_transport(game.server)
        )

    loop.play(ctx, mcp_factory=mcp_factory, model=model, decider=decider)


def actions(game, name):
    return [a for n, a in game.actions if n == name]


# A v2 turn state: no grid/walls/fields/flag bases. We are red-1 at x=3 (the red field is x 0..6).
POSITIONED = {
    "moves": MOVES,
    "self": "red-1",
    "self_position": {"x": 3, "y": 7},
    "agents": [{"id": "red-1", "team": "red", "position": {"x": 3, "y": 7}, "carrying": None}],
}


class TestDeciderPath:
    def test_a_confident_decider_picks_every_action_without_the_llm(self):
        game, decider, model = FakeGame(turns=3, state=STATE), FakeDecider(), SequencePlayer([])
        ctx, ticks = make_ctx()
        play_against(game, ctx, decider, model)
        assert [m["tick"] for m in actions(game, "move")] == [1, 2, 3]
        assert all(m["direction"] == "up" for m in actions(game, "move"))
        assert model.calls == []  # the LLM was never consulted
        assert len(ticks) == 3

    def test_the_first_decider_call_is_the_warm_up_and_the_match_follows(self):
        game, decider = FakeGame(turns=2, state=STATE), FakeDecider()
        play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        warm_state, warm_questions = logic.warmup_question()
        assert decider.asked[0][0] == warm_state and decider.asked[0][1] == warm_questions
        assert len(decider.asked) == 1 + 2  # warm-up + two ticks

    def test_each_tick_asks_three_questions_in_one_request_with_the_tick_deadline(self):
        game, decider = FakeGame(turns=1, state=STATE, deadline_remaining_ms=3000), FakeDecider()
        play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        state, questions, deadline_s = decider.asked[-1]
        assert set(questions) == {"threat", "route_bad", "action"}
        assert set(questions["action"]["criteria"]) == {"move_up", "move_down", "move_right", "wait"}
        assert "tick: 1" in state
        assert 0 < deadline_s <= 3.0  # the clamp input: the time left in the tick

    def test_one_token_for_the_whole_match(self):
        tokens = CountingTokens()
        play_against(FakeGame(turns=10, state=STATE), make_ctx(tokens)[0], FakeDecider(), SequencePlayer([]))
        assert tokens.calls == 1


class TestStaticMap:
    """Contract v2: the board comes once from `join_match.map` (else `get_map`), never from the turn."""

    def test_the_join_result_map_places_us_in_our_field_without_calling_get_map(self):
        game, decider = FakeGame(turns=1, state=POSITIONED), FakeDecider()
        play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        assert "at (3, 7), in your own field" in decider.asked[-1][0]
        assert actions(game, "get_map") == []

    def test_without_map_in_the_join_result_get_map_is_called_once(self):
        game, decider = FakeGame(turns=3, state=POSITIONED, include_map_in_join=False), FakeDecider()
        play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        assert len(actions(game, "get_map")) == 1  # once per match, not per tick
        assert all("in your own field" in asked[0] for asked in decider.asked[-3:])

    def test_without_any_map_the_match_still_plays_and_the_position_is_not_guessed(self, caplog):
        game = FakeGame(turns=2, state=POSITIONED, include_map_in_join=False, serve_get_map=False)
        decider = FakeDecider()
        with caplog.at_level(logging.WARNING, logger="decider_loop"):
            play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        assert [m["tick"] for m in actions(game, "move")] == [1, 2]
        state = decider.asked[-1][0]
        assert "you: red-1 (red) at (3, 7)" in state and "field" not in state
        assert "no static map" in caplog.text

    def test_the_map_is_parsed_from_the_server_shape(self):
        info = logic.parse_map(MAP)
        assert info.fields["red"] == (0, 6) and info.neutral_x == 7
        assert info.flag_bases == {"red": (1, 7), "blue": (13, 7)}
        assert GRID["width"] == 15

    def test_the_server_last_action_never_reaches_the_decider_state(self):
        game, decider = FakeGame(turns=2, state=POSITIONED), FakeDecider()
        play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        for state, _, _ in decider.asked:
            assert "previous action" not in state and "last action result" not in state

    def test_routes_with_closest_threat_are_rendered_for_the_decider(self):
        route = make_route("enemy_flag_direct", "enemy_flag", "left", 4, threat=("blue-1", 2, 1))
        game, decider = (
            FakeGame(turns=1, state={**POSITIONED, "routes": [route]}),
            FakeDecider(pick="enemy_flag_direct"),
        )
        play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        assert "closest threat blue-1 at step 2, distance 1" in decider.asked[-1][0]
        assert [m["direction"] for m in actions(game, "move")] == ["left"]


class TestMatchOutcome:
    def test_a_completed_match_is_logged_with_its_reason_and_winner(self, caplog):
        with caplog.at_level(logging.INFO, logger="decider_loop"):
            play_against(FakeGame(turns=1, state=STATE), make_ctx()[0], FakeDecider(), SequencePlayer([]))
        assert "match over (completed): winner tie, score red 0, blue 0" in caplog.text

    def test_a_stopped_match_has_no_winner_and_does_not_crash(self, caplog):
        game = FakeGame(turns=1, state=STATE, stopped=True)
        with caplog.at_level(logging.INFO, logger="decider_loop"):
            play_against(game, make_ctx()[0], FakeDecider(), SequencePlayer([]))
        assert "match over (stopped): no winner" in caplog.text

    def test_a_rejected_ack_is_logged_with_its_reason_and_the_loop_keeps_playing(self, caplog):
        game = FakeGame(turns=2, state=STATE, reject_actions="match_not_started")
        with caplog.at_level(logging.WARNING, logger="decider_loop"):
            play_against(game, make_ctx()[0], FakeDecider(), SequencePlayer([]))
        assert len(actions(game, "move")) == 2
        assert caplog.text.count("action rejected: match_not_started") == 2


class TestJoin:
    def test_auto_tries_red_then_blue(self):
        game = FakeGame(turns=1, state=STATE, reject_teams=("red",))
        play_against(game, make_ctx()[0], FakeDecider(), SequencePlayer([]))
        assert [a["team"] for a in actions(game, "join_match")] == ["red", "blue"]
        assert len(actions(game, "move")) == 1

    def test_a_fixed_team_is_the_only_one_tried(self):
        game = FakeGame(turns=1, state=STATE)
        play_against(game, make_ctx(team="blue")[0], FakeDecider(), SequencePlayer([]))
        assert [a["team"] for a in actions(game, "join_match")] == ["blue"]
        assert actions(game, "join_match")[0]["name"] == "p1"

    def test_failing_to_join_fails_the_match(self):
        game = FakeGame(turns=1, state=STATE, reject_teams=("blue",))
        with pytest.raises(RuntimeError, match="could not join"):
            play_against(game, make_ctx(team="blue")[0], FakeDecider(), SequencePlayer([]))
        assert actions(game, "move") == []

    def test_a_requested_slot_is_sent_as_agent_index_to_the_requested_team_only(self):
        game = FakeGame(turns=1, state=STATE)
        ctx, _ = make_ctx(team="blue", agent_index=2)
        play_against(game, ctx, FakeDecider(), SequencePlayer([]))
        assert actions(game, "join_match") == [
            {"match_id": "m1", "name": "p1", "team": "blue", "agent_index": 2}
        ]

    def test_without_a_slot_no_agent_index_is_sent(self):
        game = FakeGame(turns=1, state=STATE)
        play_against(game, make_ctx(team="red")[0], FakeDecider(), SequencePlayer([]))
        assert "agent_index" not in actions(game, "join_match")[0]

    def test_the_seat_the_server_assigned_is_reported_through_the_join_signal(self):
        game = FakeGame(turns=1, state=STATE, taken_slots=(("red", 0),))
        ctx, _ = make_ctx(team="red")
        play_against(game, ctx, FakeDecider(), SequencePlayer([]))
        assert (ctx.join.outcome.kind, ctx.join.outcome.team, ctx.join.outcome.agent_index) == (
            "joined",
            "red",
            1,
        )

    def test_a_taken_slot_is_reported_as_a_refusal_and_never_falls_back_to_another_slot(self):
        game = FakeGame(turns=3, state=STATE, taken_slots=(("red", 1),))
        ctx, _ = make_ctx(team="red", agent_index=1)
        with pytest.raises(RuntimeError, match="slot_taken"):
            play_against(game, ctx, FakeDecider(), SequencePlayer([]))
        assert (ctx.join.outcome.kind, ctx.join.outcome.reason) == ("refused", "slot_taken")
        assert len(actions(game, "join_match")) == 1 and actions(game, "move") == []

    def test_the_join_happens_before_the_decider_warm_up_so_start_is_not_held_by_a_cold_endpoint(self):
        game, decider = FakeGame(turns=1, state=STATE, taken_slots=(("red", 1),)), FakeDecider()
        with pytest.raises(RuntimeError):
            play_against(game, make_ctx(team="red", agent_index=1)[0], decider, SequencePlayer([]))
        assert decider.asked == []  # refused: the decider was never even warmed up

    def test_a_stop_before_the_join_takes_no_seat(self):
        stop = threading.Event()
        stop.set()
        game = FakeGame(turns=1, state=STATE)
        play_against(game, make_ctx(stop=stop)[0], FakeDecider(), SequencePlayer([]))
        assert game.actions == []


class TestFallback:
    def llm_moves(self, *ticks):
        return SequencePlayer([("move", {"tick": t, "direction": "down"}) for t in ticks])

    def test_a_low_confidence_decision_goes_to_the_llm_which_acts_through_the_action_tools(self):
        game, decider = FakeGame(turns=2, state=STATE), FakeDecider(confidence=0.3)
        model = self.llm_moves(1, 2)
        play_against(game, make_ctx(options={"llm_budget_ms": 100})[0], decider, model)
        assert [m["direction"] for m in actions(game, "move")] == [
            "down",
            "down",
        ]  # the LLM's choice, not the decider's
        assert len(model.calls) == 2

    def test_the_llm_prompt_names_the_tick_and_the_action_tools(self):
        game, decider = FakeGame(turns=1, state=STATE), FakeDecider(confidence=0.3)
        model = self.llm_moves(1)
        play_against(game, make_ctx(options={"llm_budget_ms": 100})[0], decider, model)
        prompt = model.calls[0][0]["content"][0]["text"]
        assert "This is tick 1" in prompt and "move, tag, grab_flag, wait" in prompt
        assert model.system_prompts[0] == logic.LLM_SYSTEM_PROMPT

    def test_the_llm_prompt_carries_the_servers_last_action_the_decider_text_does_not(self):
        game, decider = FakeGame(turns=2, state=POSITIONED), FakeDecider(confidence=0.3)
        model = self.llm_moves(1, 2)
        play_against(game, make_ctx(options={"llm_budget_ms": 100})[0], decider, model)
        first, second = (call[0]["content"][0]["text"] for call in model.calls)
        assert "previous action" not in first  # first turn: null
        assert "outcome of your previous action: move:down at tick 1 -> moved" in second
        assert all("previous action" not in state for state, _, _ in decider.asked)

    def test_a_decider_that_is_down_for_the_whole_match_leaves_every_tick_to_the_llm(self):
        game = FakeGame(turns=3, state=STATE)
        decider = FakeDecider(raises=[DeciderCircuitOpen("open")] * 10)
        model = self.llm_moves(1, 2, 3)
        play_against(game, make_ctx(options={"llm_budget_ms": 100})[0], decider, model)
        assert [m["direction"] for m in actions(game, "move")] == ["down"] * 3  # the match completed
        assert len(model.calls) == 3

    def test_there_is_no_permanent_down_state_the_decider_is_used_again_when_it_recovers(self):
        game = FakeGame(turns=3, state=STATE)
        # warm-up ok, tick 1 times out, ticks 2 and 3 answer
        decider = FakeDecider(raises=[None, DeciderTimeout("slow")])
        model = self.llm_moves(1)
        play_against(game, make_ctx(options={"llm_budget_ms": 100})[0], decider, model)
        directions = [m["direction"] for m in actions(game, "move")]
        assert directions == ["down", "up", "up"]  # LLM once, then the decider again

    def test_a_failed_warm_up_does_not_stop_the_match_or_disable_the_decider(self):
        game = FakeGame(turns=2, state=STATE)
        decider = FakeDecider(raises=[DeciderTimeout("cold")])  # only the warm-up fails
        play_against(game, make_ctx()[0], decider, SequencePlayer([]))
        assert [m["direction"] for m in actions(game, "move")] == ["up", "up"]

    def test_with_no_time_for_the_llm_the_decider_choice_runs_as_the_budget_path(self):
        game, decider = FakeGame(turns=1, state=STATE), FakeDecider(confidence=0.3)
        model = SequencePlayer([])
        # 600 s of "LLM budget" can never fit in the tick
        play_against(game, make_ctx(options={"llm_budget_ms": 600_000})[0], decider, model)
        assert [m["direction"] for m in actions(game, "move")] == ["up"]
        assert model.calls == []

    def test_with_no_time_and_no_decider_the_loop_waits(self):
        game = FakeGame(turns=1, state=STATE)
        decider = FakeDecider(raises=[DeciderCircuitOpen("open")] * 5)
        model = SequencePlayer([])
        play_against(game, make_ctx(options={"llm_budget_ms": 600_000})[0], decider, model)
        assert actions(game, "wait") == [{"tick": 1}] and actions(game, "move") == []

    def test_the_threshold_option_changes_what_counts_as_confident(self):
        game, decider = FakeGame(turns=1, state=STATE), FakeDecider(confidence=0.6)
        model = self.llm_moves(1)
        play_against(game, make_ctx(options={"threshold": 0.5})[0], decider, SequencePlayer([]))  # 0.6 >= 0.5
        assert actions(game, "move")[0]["direction"] == "up"
        game2 = FakeGame(turns=1, state=STATE)
        play_against(
            game2, make_ctx(options={"threshold": 0.7, "llm_budget_ms": 100})[0], decider, model
        )  # 0.6 < 0.7
        assert actions(game2, "move")[0]["direction"] == "down"


class TestStop:
    def test_stop_ends_the_loop_after_the_current_tick_without_acting_on_it(self):
        stop = threading.Event()
        game = FakeGame(turns=500, state=STATE)
        ctx, ticks = make_ctx(stop=stop, on_tick=lambda n: stop.set() if n == 2 else None)
        play_against(game, ctx, FakeDecider(), SequencePlayer([]))
        assert game.tick == 2 and len(ticks) == 2
        assert [m["tick"] for m in actions(game, "move")] == [1]  # no action after the stop

    def test_a_stop_set_before_the_first_turn_plays_nothing(self):
        stop = threading.Event()
        stop.set()
        game = FakeGame(turns=5, state=STATE)
        play_against(game, make_ctx(stop=stop)[0], FakeDecider(), SequencePlayer([]))
        assert game.tick == 0 and actions(game, "move") == []


def mcp_result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


def authorized_for_first(n_calls):
    """A JSON MCP server: the first `n_calls` tool calls succeed, later ones are 401 (token revoked)."""
    seen = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        method, rpc_id = body.get("method"), body.get("id")
        if rpc_id is None:
            return httpx.Response(202)
        if method == "tools/call":
            seen["calls"] += 1
            if seen["calls"] > n_calls:
                return httpx.Response(401, json={"error": "invalid_token"})
            name = body["params"]["name"]
            if name == "join_match":
                payload = {
                    "agent_id": "red-1",
                    "team": "red",
                    "agent_index": 0,
                    "ticks": 10,
                    "deadline_ms": 5000,
                    "grid": GRID,
                    "min_tick_ms": 0,
                    "map": MAP,
                }
            elif name == "wait_for_turn":
                payload = {
                    "status": "turn",
                    "tick": seen["calls"],
                    "state": {"tick": seen["calls"], "moves": MOVES},
                }
            else:
                payload = {"status": "ok"}
            result = mcp_result(payload)
        elif method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ctf-game", "version": "0"},
            }
        elif method == "tools/list":
            schema = {"type": "object", "properties": {}}
            result = {
                "tools": [
                    {"name": n, "description": n, "inputSchema": schema}
                    for n in ("get_map", "join_match", "wait_for_turn", "move", "tag", "grab_flag", "wait")
                ]
            }
        else:
            result = {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc_id, "result": result})

    return handler, seen


class CallSpy:
    """Delegates to a real MCPClient and records every tool call the loop attempts."""

    def __init__(self, client):
        self._client = client
        self.calls: list[str] = []

    def __enter__(self):
        self._client.__enter__()
        return self

    def __exit__(self, *exc):
        return self._client.__exit__(*exc)

    def list_tools_sync(self):
        return self._client.list_tools_sync()

    def call_tool_sync(self, tool_use_id, name, arguments):
        self.calls.append(name)
        return self._client.call_tool_sync(tool_use_id, name, arguments)


class TestConnectionLost:
    spy: CallSpy

    def play_over_http(self, handler, ctx, decider):
        def client_factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)

        def mcp_factory(url, tokens):
            self.spy = CallSpy(
                open_mcp_client(url, tokens, prefix_tools=False, client_factory=client_factory)
            )
            return self.spy

        loop.play(ctx, mcp_factory=mcp_factory, model=SequencePlayer([]), decider=decider)

    def test_401_mid_match_fails_the_match_with_an_auth_error_and_no_refresh(self):
        handler, seen = authorized_for_first(4)  # join, wait_for_turn, move, wait_for_turn... then 401
        tokens = CountingTokens()
        ctx, ticks = make_ctx(tokens)
        with pytest.raises(McpAuthError, match="401"):
            self.play_over_http(handler, ctx, FakeDecider())
        assert tokens.calls == 1  # no refresh, no reconnect
        # join, wait_for_turn, move, wait_for_turn succeeded; the next move died. The loop stopped right
        # there instead of retrying wait_for_turn on a connection that can never recover.
        assert self.spy.calls == ["join_match", "wait_for_turn", "move", "wait_for_turn", "move"]
        assert len(ticks) == 2

    def test_401_on_the_very_first_call_also_fails_the_match(self):
        handler, seen = authorized_for_first(0)
        tokens = CountingTokens()
        with pytest.raises(McpAuthError):
            self.play_over_http(handler, make_ctx(tokens)[0], FakeDecider())
        assert tokens.calls == 1 and seen["calls"] == 1
        assert self.spy.calls == ["join_match"]


class TestSetup:
    def test_the_decider_is_built_from_the_runtime_config(self):
        ctx, _ = make_ctx(config=RuntimeConfig(mcp_url="http://m", decider_endpoint_name="ctf-decider"))
        assert isinstance(loop.build_decider(ctx), SageMakerDecider)
        local, _ = make_ctx(config=RuntimeConfig(mcp_url="http://m", decider_url="http://127.0.0.1:8099"))
        assert isinstance(loop.build_decider(local), HttpDecider)

    def test_the_safety_margin_option_reaches_the_decider_clamp(self):
        ctx, _ = make_ctx(options={"safety_margin_ms": 900})
        decider = loop.build_decider(ctx)
        assert decider._margin_s == pytest.approx(0.9)  # noqa: SLF001 - the clamp input is the behavior under test

    def test_the_runner_requires_the_decider_endpoint_parameter(self):
        class App:
            def add_async_task(self, *a, **k):
                raise AssertionError("must not start")

            def complete_async_task(self, *a):
                raise AssertionError("must not start")

        runner = loop.build_runner(
            App(), {"CTF_MCP_URL": "http://m"}, token_provider=StaticTokenProvider("t"), play=lambda ctx: None
        )
        request = MatchRequest(
            action=Action.START, match_id="m1", name="p1", team="auto", model="haiku", options={}
        )
        resp = runner.start(request)
        assert resp["status"] == "failed" and "CTF_DECIDER_ENDPOINT_PARAM" in resp["error"]


class TestActionGuards:
    def test_tick_cancel_handler_blocks_actions_after_the_tick_is_cancelled(self):
        from types import SimpleNamespace

        handler = loop.TickCancelHandler(threading.Event())
        move = SimpleNamespace(tool_use={"name": "move"})
        assert handler.before_tool_call(move).type == "proceed" and handler.executed_action is True
        assert handler.cancel_tick() is True
        assert handler.before_tool_call(move).type == "deny"

    def test_a_cancel_before_any_action_reports_that_nothing_ran(self):
        handler = loop.TickCancelHandler(threading.Event())
        assert handler.cancel_tick() is False


class TestJoinHonoursStop:
    def test_a_stop_before_the_join_call_takes_no_seat_and_returns_none(self):
        game = FakeGame(turns=1, state=STATE)
        ctx, _ = make_ctx(team="red")
        stop = threading.Event()
        stop.set()
        client = open_mcp_client(
            "https://x/mcp", ctx.tokens, connect=lambda u, h: in_memory_transport(game.server)
        )
        with client:
            assert loop.join(client, ctx.request, ctx.join, stop) is None
        assert game.actions == [] and ctx.join.outcome.kind == "cancelled"
