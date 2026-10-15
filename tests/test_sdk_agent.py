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
from fake_mcp import FakeGame, in_memory_transport
from fake_model import ScriptedPlayer

APP_DIR = Path(__file__).resolve().parents[1] / "app" / "harness_sdk"
TIMEOUT = 20


class CountingTokens:
    def __init__(self):
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return f"token-{self.calls}"


def make_ctx(tokens=None, *, team="auto", options=None, on_tick=None, stop=None):
    ticks = []
    request = MatchRequest(
        action=Action.START, match_id="m1", name="p1", team=team, model="haiku", options=options or {}
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

    def test_joins_with_the_requested_match_and_name(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        model = play_against(game, ctx, ScriptedPlayer({"match_id": "m1", "name": "p1"}))
        first_user = model.calls[0][0]
        assert first_user["role"] == "user"
        text = first_user["content"][0]["text"]
        assert "match m1" in text and "'p1'" in text and "whichever team" in text
        assert game.actions[0] == ("join_match", {"match_id": "m1", "name": "p1", "team": "red"})

    def test_fixed_team_is_part_of_the_instruction(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx(team="blue")
        model = play_against(game, ctx)
        assert "on team blue" in model.calls[0][0]["content"][0]["text"]

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

    def test_static_walls_are_stripped_after_the_first_turn(self):
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
        by_tick = {json.loads(t)["tick"]: json.loads(t)["state"] for t in texts}
        assert "walls" in by_tick[1] and "walls" not in by_tick[2] and "walls" not in by_tick[3]

    def test_window_option_bounds_the_history_sent_to_the_model(self):
        game, (ctx, _) = FakeGame(turns=12), make_ctx(options={"window": 8})
        model = play_against(game, ctx)
        assert max(len(call) for call in model.calls) <= 9  # window + pinned instruction
        assert model.calls[-1][0]["content"][0]["text"].startswith("Join match")

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
        assert len(model.calls) == 1  # the loop did not go on after the 401


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
