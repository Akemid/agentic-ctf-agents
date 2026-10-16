"""harness_sdk play loop against an in-memory MCP game and a scripted model (no Bedrock, no network)."""

import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import sdk_agent
from ctf_common.auth import StaticTokenProvider
from ctf_common.config import RuntimeConfig
from ctf_common.mcp import McpAuthError, open_mcp_client
from ctf_common.payload import Action, MatchRequest
from ctf_common.prompts import SYSTEM_PROMPT
from ctf_common.runner import MatchContext
from fake_mcp import FakeGame, handshake_ok_then_401_on_tool_call, in_memory_transport
from fake_model import ScriptedPlayer, SequencePlayer

APP_DIR = Path(__file__).resolve().parents[1] / "app" / "harness_sdk"
TIMEOUT = 20


class CountingTokens:
    def __init__(self):
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return f"token-{self.calls}"


def make_ctx(tokens=None, *, team="auto", options=None, on_tick=None, stop=None, agent_index=None):
    ticks = []
    request = MatchRequest(
        action=Action.START,
        match_id="m1",
        name="p1",
        team=team,
        model="haiku",
        options=options or {},
        agent_index=agent_index,
    )

    def tick():
        ticks.append(1)
        if on_tick:
            on_tick(len(ticks))

    ctx = MatchContext(
        request=request,
        config=RuntimeConfig(mcp_url="https://mcp.example/mcp"),
        tokens=tokens or CountingTokens(),
        stop=stop or threading.Event(),
        tick=tick,
    )
    return ctx, ticks


def play_against(game, ctx, model=None):
    model = model or ScriptedPlayer()

    def mcp_factory(url, tokens):
        return open_mcp_client(url, tokens, connect=lambda u, headers: in_memory_transport(game.server))

    sdk_agent.play(ctx, mcp_factory=mcp_factory, model=model)
    return model


class TestPlayLoop:
    def test_plays_every_turn_until_match_over(self):
        game, (ctx, ticks) = FakeGame(turns=3), make_ctx()
        play_against(game, ctx)
        moves = [a for name, a in game.actions if name == "move"]
        assert [m["tick"] for m in moves] == [1, 2, 3]
        assert len(ticks) == 3

    def test_the_runtime_joins_in_code_with_the_requested_match_and_name(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        model = play_against(game, ctx)
        first_user = model.calls[0][0]
        assert first_user["role"] == "user"
        text = first_user["content"][0]["text"]
        assert "already joined match m1" in text and "'p1'" in text
        assert game.actions[0] == ("join_match", {"match_id": "m1", "name": "p1", "team": "red"})
        assert [n for n, _ in game.actions].count("join_match") == 1  # the model never joined

    def test_fixed_team_and_seat_are_forced_and_part_of_the_instruction(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx(team="blue", agent_index=2)
        model = play_against(game, ctx)
        assert game.actions[0] == (
            "join_match",
            {"match_id": "m1", "name": "p1", "team": "blue", "agent_index": 2},
        )
        assert "team blue, seat 3" in model.calls[0][0]["content"][0]["text"]
        assert (ctx.join.outcome.kind, ctx.join.outcome.team, ctx.join.outcome.agent_index) == (
            "joined",
            "blue",
            2,
        )

    def test_the_join_result_with_the_map_reaches_the_model_in_the_instruction(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        model = play_against(game, ctx)
        assert '"flag_bases"' in model.calls[0][0]["content"][0]["text"]

    def test_a_refused_join_never_reaches_the_model_and_never_tries_another_slot(self):
        game = FakeGame(turns=3, taken_slots=(("red", 1),))
        ctx, ticks = make_ctx(team="red", agent_index=1)
        model = play_against(game, ctx)
        assert model.calls == [] and ticks == []
        assert [n for n, _ in game.actions] == ["join_match"]
        assert (ctx.join.outcome.kind, ctx.join.outcome.reason) == ("refused", "slot_taken")

    def test_team_auto_falls_back_to_blue_in_code_only_on_a_full_team(self):
        game, (ctx, _) = FakeGame(turns=1, reject_teams=("red",)), make_ctx()
        play_against(game, ctx)
        assert [a["team"] for n, a in game.actions if n == "join_match"] == ["red", "blue"]
        assert ctx.join.outcome.team == "blue"

    def test_a_model_initiated_join_is_cancelled_and_does_not_fail_the_match(self):
        game, (ctx, ticks) = FakeGame(turns=2), make_ctx(team="red", agent_index=1)
        script = SequencePlayer(
            [
                ("ctf-game_join_match", {"match_id": "x", "team": "blue", "name": "evil", "agent_index": 0}),
                ("ctf-game_wait_for_turn", {}),
                ("ctf-game_move", {"tick": 1, "direction": "up"}),
            ]
        )
        play_against(game, ctx, script)
        joins = [a for n, a in game.actions if n == "join_match"]
        assert joins == [{"match_id": "m1", "name": "p1", "team": "red", "agent_index": 1}]
        assert ticks == [1] and ctx.join.outcome.kind == "joined"

    def test_a_stop_that_arrived_before_the_join_takes_no_seat_and_never_starts_the_model(self):
        stop = threading.Event()
        stop.set()
        game, (ctx, _) = FakeGame(turns=3), make_ctx(stop=stop)
        model = play_against(game, ctx)
        assert model.calls == [] and game.actions == []

    def test_history_starts_empty_and_uses_the_shared_system_prompt(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        model = play_against(game, ctx)
        assert len(model.calls[0]) == 1  # only the instruction, nothing caller-supplied
        assert model.system_prompts[0] == SYSTEM_PROMPT

    def test_one_token_for_the_whole_match(self):
        tokens = CountingTokens()
        game, (ctx, _) = FakeGame(turns=25), make_ctx(tokens)
        play_against(game, ctx)
        assert len(game.actions) == 26 and tokens.calls == 1

    def test_turns_reach_the_model_untouched_and_carry_no_static_board_data(self):
        # Contract v2: the server no longer sends walls/fields/grid in turns, so nothing is rewritten.
        game, (ctx, _) = FakeGame(turns=3), make_ctx()
        model = play_against(game, ctx)
        texts = [
            part["text"]
            for call in model.calls
            for msg in call
            for block in msg["content"]
            if "toolResult" in block
            for part in block["toolResult"]["content"]
            if '"status": "turn"' in part["text"]
        ]
        by_tick = {json.loads(t)["tick"]: json.loads(t) for t in texts}
        assert set(by_tick) == {1, 2, 3}
        for turn in by_tick.values():
            assert {"grid", "walls", "fields"}.isdisjoint(turn["state"])
            assert "last_action" in turn
        assert by_tick[1]["last_action"] is None and by_tick[2]["last_action"]["action"] == "move:up"

    def test_the_model_is_given_get_map_so_it_can_recover_the_static_map(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        model = play_against(game, ctx)
        assert "get_map" in model.system_prompts[0]

    def test_window_option_bounds_the_history_sent_to_the_model(self):
        game, (ctx, _) = FakeGame(turns=12), make_ctx(options={"window": 8})
        model = play_against(game, ctx)
        assert max(len(call) for call in model.calls) <= 9  # window + pinned instruction
        assert model.calls[-1][0]["content"][0]["text"].startswith("You have already joined match")

    def test_default_window_is_used_when_not_given(self):
        game, (ctx, _) = FakeGame(turns=12), make_ctx()
        model = play_against(game, ctx)
        assert max(len(call) for call in model.calls) <= 13

    def test_stop_ends_the_loop_without_playing_the_whole_match(self):
        stop = threading.Event()
        game = FakeGame(turns=500)
        ctx, ticks = make_ctx(stop=stop, on_tick=lambda n: stop.set() if n == 2 else None)
        play_against(game, ctx)
        assert game.tick == 2 and len(ticks) == 2
        assert len([a for name, a in game.actions if name == "move"]) == 1  # no action after the stop

    def test_401_fails_the_match_with_an_auth_error_and_no_second_token(self):
        tokens = CountingTokens()
        ctx, _ = make_ctx(tokens)

        def client_factory(headers=None, timeout=None, auth=None):
            handler = lambda request: httpx.Response(401, json={"error": "invalid_token"})  # noqa: E731
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)

        def mcp_factory(url, token_source):
            return open_mcp_client(url, token_source, client_factory=client_factory)

        with pytest.raises(McpAuthError):
            sdk_agent.play(ctx, mcp_factory=mcp_factory, model=ScriptedPlayer())
        assert tokens.calls == 1


class TestMidMatchUnauthorized:
    def test_401_on_a_later_tool_call_fails_the_match_with_an_auth_error_and_no_refresh(self):
        tokens = CountingTokens()
        ctx, _ = make_ctx(tokens)

        def client_factory(headers=None, timeout=None, auth=None):
            transport = httpx.MockTransport(handshake_ok_then_401_on_tool_call)
            return httpx.AsyncClient(transport=transport, headers=headers)

        def mcp_factory(url, token_source):
            return open_mcp_client(url, token_source, client_factory=client_factory)

        model = ScriptedPlayer()
        with pytest.raises(McpAuthError, match="401"):
            sdk_agent.play(ctx, mcp_factory=mcp_factory, model=model)
        assert tokens.calls == 1  # no refresh
        assert model.calls == []  # the code-owned join met the 401: the model was never consulted


class TestRunnerIntegration:
    def test_runner_drives_a_full_match_and_reports_ticks(self):
        from ctf_common.runner import MatchRunner

        class App:
            def __init__(self):
                self.done = []

            def add_async_task(self, name, metadata=None):
                return 7

            def complete_async_task(self, task_id):
                self.done.append(task_id)
                return True

        app, game = App(), FakeGame(turns=4)

        def play(ctx):
            return play_against(game, ctx)

        runner = sdk_agent.build_runner(
            app,
            {"CTF_MCP_URL": "https://mcp.example/mcp"},
            token_provider=StaticTokenProvider("tok"),
            play=play,
        )
        assert isinstance(runner, MatchRunner)
        resp = runner.start(
            MatchRequest(
                action=Action.START, match_id="m1", name="p1", team="auto", model="haiku", options={}
            )
        )
        assert resp["status"] == "started"
        assert runner.wait(TIMEOUT)
        status = runner.status()
        assert status["status"] == "finished" and status["ticks"] == 4
        assert app.done == [7]

    def start(self, game, **fields):
        class App:
            def __init__(self):
                self.added, self.done = [], []

            def add_async_task(self, name, metadata=None):
                self.added.append(name)
                return len(self.added)

            def complete_async_task(self, task_id):
                self.done.append(task_id)
                return True

        app = App()
        runner = sdk_agent.build_runner(
            app,
            {"CTF_MCP_URL": "https://mcp.example/mcp"},
            token_provider=StaticTokenProvider("tok"),
            play=lambda ctx: play_against(game, ctx),
        )
        values = dict(action=Action.START, match_id="m1", name="p1", team="auto", model="haiku", options={})
        return app, runner, runner.start(MatchRequest(**{**values, **fields}))

    def test_start_answers_with_the_seat_the_server_assigned(self):
        app, runner, resp = self.start(FakeGame(turns=2), team="blue", agent_index=2)
        assert resp["status"] == "started"
        assert (resp["match_id"], resp["name"], resp["team"], resp["agent_index"]) == ("m1", "p1", "blue", 2)
        assert runner.wait(TIMEOUT) and runner.status()["status"] == "finished"
        assert app.added == ["match"] and app.done == [1]

    def test_a_payload_with_only_slot_joins_that_exact_index_and_the_response_echoes_slot(self):
        from ctf_common.payload import parse_payload

        game = FakeGame(turns=1)
        request = parse_payload(
            {"match_id": "m1", "team": "blue", "slot": 2}, runtime="harness_sdk", session_id="s"
        )
        assert request.agent_index == 2
        _, runner, resp = self.start(game, team=request.team, agent_index=request.agent_index)
        assert game.actions[0] == (
            "join_match",
            {"match_id": "m1", "name": "p1", "team": "blue", "agent_index": 2},
        )
        assert (resp["status"], resp["slot"], resp["agent_index"]) == ("started", 2, 2)
        runner.wait(TIMEOUT)

    def test_start_with_team_auto_reports_the_team_the_server_picked(self):
        _, runner, resp = self.start(FakeGame(turns=1, reject_teams=("red",)))
        assert (resp["status"], resp["team"], resp["agent_index"]) == ("started", "blue", 0)
        runner.wait(TIMEOUT)

    @pytest.mark.parametrize(
        ("flags", "reason"),
        [
            ({"taken_slots": (("red", 1),)}, "slot_taken"),
            ({"full": True}, "match_full"),
            ({"started": True}, "match_already_started"),
            ({"finished": True}, "match_finished"),
            ({"disconnected_slots": (("red", 1),)}, "unauthorized"),
        ],
    )
    def test_each_join_refusal_is_a_rejected_start_with_its_reason_and_no_match_is_played(
        self, flags, reason
    ):
        game = FakeGame(turns=3, **flags)
        app, runner, resp = self.start(game, team="red", agent_index=1)
        assert resp["status"] == "rejected" and resp["error"] == f"join_match failed: {reason}"
        assert runner.wait(TIMEOUT) and runner.status()["status"] == "failed"
        assert [n for n, _ in game.actions] == ["join_match"]  # one attempt: no fallback slot
        assert app.added == ["match"] and app.done == [1]  # the task balance holds on the failure path

    def test_a_refused_start_can_be_retried_on_another_slot_at_once(self):
        game = FakeGame(turns=1, taken_slots=(("red", 1),))
        _, runner, first = self.start(game, team="red", agent_index=1)
        assert first["status"] == "rejected"
        second = runner.start(
            MatchRequest(
                action=Action.START,
                match_id="m1",
                name="p1",
                team="red",
                model="haiku",
                options={},
                agent_index=2,
            )
        )
        assert second["status"] == "started" and second["agent_index"] == 2
        runner.wait(TIMEOUT)

    def test_preflight_failure_names_the_missing_parameter(self):
        class App:
            def add_async_task(self, *a, **k):
                raise AssertionError("must not start")

            def complete_async_task(self, *a):
                raise AssertionError("must not start")

        class NoSsm:
            def get_parameter(self, Name):
                raise RuntimeError("unused")

        runner = sdk_agent.build_runner(
            App(), {}, token_provider=StaticTokenProvider("tok"), ssm_client=NoSsm(), play=lambda ctx: None
        )
        resp = runner.start(
            MatchRequest(
                action=Action.START, match_id="m1", name="p1", team="auto", model="haiku", options={}
            )
        )
        assert resp["status"] == "failed" and "/agentic-ctf/mcp-url" in resp["error"]


def load_main():
    spec = importlib.util.spec_from_file_location("harness_sdk_main", APP_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def main():
    return load_main()


class TestEntrypoint:
    def ctx(self):
        return SimpleNamespace(session_id="session-0123456789-0123456789-0123456789-abc123")

    def test_entrypoint_is_a_plain_sync_function(self, main):
        import inspect

        assert not inspect.iscoroutinefunction(main.invoke)
        assert not inspect.isasyncgenfunction(main.invoke)

    def test_status_on_a_fresh_session_is_idle(self, main):
        assert main.invoke({"action": "status"}, self.ctx()) == {"status": "idle"}

    def test_caller_supplied_history_is_rejected(self, main):
        resp = main.invoke({"match_id": "m1", "messages": [{"role": "user", "content": []}]}, self.ctx())
        assert resp["status"] == "rejected" and "messages" in resp["error"]

    def test_non_string_prompt_is_rejected(self, main):
        assert main.invoke({"prompt": 42}, self.ctx())["status"] == "rejected"

    def test_missing_context_does_not_crash(self, main):
        assert main.invoke({"action": "status"}, None) == {"status": "idle"}

    def test_template_leftovers_are_gone(self):
        for leftover in ("mcp_client", "model", "skills"):
            assert not (APP_DIR / leftover).exists(), leftover
        source = (APP_DIR / "main.py").read_text() + (APP_DIR / "sdk_agent.py").read_text()
        assert "add_numbers" not in source
        assert "exa.ai" not in source and "get_streamable_http_mcp_client" not in source
