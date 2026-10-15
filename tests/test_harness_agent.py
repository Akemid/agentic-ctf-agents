"""harness runtime: `strands_harness.create_harness` wired to the CTF MCP server (no Bedrock, no AWS)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from strands_harness import create_harness

import harness_agent
from ctf_common.config import RuntimeConfig
from ctf_common.hooks import ConnectionLostHook, StopRequestedHook, TickCounterHook
from ctf_common.mcp import McpAuthError, open_mcp_client
from ctf_common.payload import Action, MatchRequest
from ctf_common.prompts import MODEL_ALIASES, SYSTEM_PROMPT
from ctf_common.runner import MatchContext
from fake_mcp import FakeGame, in_memory_transport
from fake_model import ScriptedPlayer

APP_DIR = Path(__file__).resolve().parents[1] / "app" / "harness"
MCP_URL = "https://mcp.example/mcp"


class CountingTokens:
    def __init__(self):
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return f"token-{self.calls}"


def make_request(**overrides):
    values = dict(action=Action.START, match_id="m1", name="p1", team="auto", model="haiku", options={})
    return MatchRequest(**{**values, **overrides})


def make_ctx(tokens=None, *, url=MCP_URL, on_tick=None, stop=None, **overrides):
    ticks = []

    def tick():
        ticks.append(1)
        if on_tick:
            on_tick(len(ticks))

    ctx = MatchContext(
        request=make_request(**overrides),
        config=RuntimeConfig(mcp_url=url),
        tokens=tokens or CountingTokens(),
        stop=stop or threading.Event(),
        tick=tick,
    )
    return ctx, ticks


class TestHarnessKwargs:
    """The call into create_harness: S1 headers config, bedrock/ prefix, state dirs, hooks."""

    def kwargs(self, **overrides):
        ctx, _ = make_ctx(**overrides)
        return harness_agent.harness_kwargs(
            ctx.request, ctx.config, "tok-123", state_dir="/state", hooks=["hook-a", "hook-b"]
        )

    def test_mcp_server_config_carries_the_bearer_header_and_is_fatal_when_it_fails(self):
        server = self.kwargs()["mcp_servers"]["ctf-game"]
        assert server["url"] == MCP_URL
        assert (
            "type" not in server
        )  # not a MCPServerConfig key: strands warns and ignores it, url implies http
        assert server["headers"] == {"Authorization": "Bearer tok-123"}
        assert (
            server["continue_on_error"] is False
        )  # a dead MCP server must fail the match, not yield no tools

    @pytest.mark.parametrize("alias", sorted(MODEL_ALIASES))
    def test_model_ids_get_the_bedrock_prefix(self, alias):
        assert self.kwargs(model=alias)["model"] == f"bedrock/{MODEL_ALIASES[alias]}"

    def test_session_and_memory_live_under_the_state_dir(self):
        kwargs = self.kwargs()
        assert kwargs["session"] == {"dir": "/state/sessions"}
        assert kwargs["memory"] == {"dir": "/state/memory"}

    def test_the_agent_is_the_original_harness_agent(self):
        kwargs = self.kwargs(name="red-one")
        assert kwargs["name"] == "red-one"
        assert kwargs["instructions"] == SYSTEM_PROMPT
        assert kwargs["effort"] == "low"
        assert kwargs["skills"] is False
        assert kwargs["builtin_tools"] == {
            "web_search": False,
            "programmatic_tool_caller": True,
            "subagent": False,
        }
        assert kwargs["callback_handler"] is None

    def test_hooks_are_passed_to_the_agent(self):
        assert self.kwargs()["hooks"] == ["hook-a", "hook-b"]

    def test_state_dir_comes_from_the_environment_with_a_tmp_default(self):
        assert harness_agent.agent_state_dir({"CTF_AGENT_STATE_DIR": "/data/agent"}) == "/data/agent"
        assert harness_agent.agent_state_dir({}) == "/tmp/.agent"
        assert harness_agent.agent_state_dir({"CTF_AGENT_STATE_DIR": "  "}) == "/tmp/.agent"


def play_against(game, ctx, model=None, seen=None):
    """Run harness_agent.play with the real create_harness, an in-memory MCP game and a scripted model."""
    model = model or ScriptedPlayer()

    def create(**kwargs):
        if seen is not None:
            seen.append(dict(kwargs))
        server = kwargs.pop("mcp_servers")["ctf-game"]
        client = open_mcp_client(
            server["url"],
            _HeaderToken(server["headers"]),
            connect=lambda u, h: in_memory_transport(game.server),
        )
        return create_harness(
            **{**kwargs, "model": model, "tools": [client], "session": False, "memory": False}
        )

    harness_agent.play(ctx, create=create, state_dir="/unused")
    return model


class _HeaderToken:
    """Turns the bearer header of the harness config back into a TokenProvider for the fake transport."""

    def __init__(self, headers):
        self._token = headers["Authorization"].removeprefix("Bearer ")

    def get_token(self):
        return self._token


class TestPlayLoop:
    def test_plays_every_turn_until_match_over(self):
        game, (ctx, ticks) = FakeGame(turns=3), make_ctx()
        play_against(game, ctx)
        moves = [a for name, a in game.actions if name == "move"]
        assert [m["tick"] for m in moves] == [1, 2, 3]
        assert len(ticks) == 3

    def test_joins_with_the_requested_match_name_and_team(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx(team="blue")
        model = play_against(game, ctx, ScriptedPlayer({"match_id": "m1", "name": "p1", "team": "blue"}))
        text = model.calls[0][0]["content"][0]["text"]
        assert "match m1" in text and "on team blue" in text and "'p1'" in text
        assert game.actions[0] == ("join_match", {"match_id": "m1", "name": "p1", "team": "blue"})

    def test_history_starts_empty_and_the_prompt_includes_the_ctf_instructions(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        model = play_against(game, ctx)
        assert len(model.calls[0]) == 1
        assert SYSTEM_PROMPT in model.system_prompts[0]

    def test_exactly_one_token_is_fetched_per_harness_build(self):
        tokens = CountingTokens()
        game, (ctx, _) = FakeGame(turns=20), make_ctx(tokens)
        seen = []
        play_against(game, ctx, seen=seen)
        assert len(game.actions) == 21 and tokens.calls == 1
        assert seen[0]["mcp_servers"]["ctf-game"]["headers"] == {"Authorization": "Bearer token-1"}

    def test_stop_ends_the_loop_without_playing_the_whole_match(self):
        stop = threading.Event()
        game = FakeGame(turns=500)
        ctx, ticks = make_ctx(stop=stop, on_tick=lambda n: stop.set() if n == 2 else None)
        play_against(game, ctx)
        assert game.tick == 2 and len(ticks) == 2
        assert len([a for name, a in game.actions if name == "move"]) == 1  # no action after the stop

    def test_the_runtime_hooks_are_registered_on_the_agent(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        seen = []
        play_against(game, ctx, seen=seen)
        kinds = {type(hook) for hook in seen[0]["hooks"]}
        assert kinds == {TickCounterHook, StopRequestedHook, ConnectionLostHook}

    def test_the_harness_session_and_memory_dirs_are_passed_through(self):
        game, (ctx, _) = FakeGame(turns=1), make_ctx()
        seen = []
        play_against(game, ctx, seen=seen)
        assert seen[0]["session"] == {"dir": "/unused/sessions"} and seen[0]["memory"] == {
            "dir": "/unused/memory"
        }


class McpServer(BaseHTTPRequestHandler):
    """A minimal streamable-HTTP MCP server: handshake and tools/list succeed, tools/call is 401."""

    calls: list[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        method, rpc_id = body.get("method"), body.get("id")
        type(self).calls.append(f"{method}:{self.headers.get('authorization')}")
        if method == "tools/call":
            return self._send(401, {"error": "invalid_token"})
        if rpc_id is None:
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()
            return None
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
        return self._send(200, {"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def do_GET(self):  # noqa: N802
        self.send_response(405)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_DELETE(self):  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def mcp_server():
    handler = type("Handler", (McpServer,), {"calls": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/mcp", handler
    server.shutdown()
    server.server_close()


def real_transport_create(model):
    """The real create_harness and the real HTTP MCP transport; only the model is scripted."""

    def create(**kwargs):
        return create_harness(**{**kwargs, "model": model, "session": False, "memory": False})

    return create


class TestMidMatchUnauthorized:
    def test_401_after_the_handshake_fails_the_match_with_an_auth_error_and_no_refresh(self, mcp_server):
        url, handler = mcp_server
        tokens = CountingTokens()
        ctx, _ = make_ctx(tokens, url=url)
        model = ScriptedPlayer()
        with pytest.raises(McpAuthError, match="401"):
            harness_agent.play(ctx, create=real_transport_create(model), state_dir="/unused")
        assert tokens.calls == 1  # no refresh
        assert len(model.calls) == 1  # the loop did not go on after the 401
        assert handler.calls[0].endswith("Bearer token-1")  # the header reached the server


class TestUnreachableServer:
    def test_a_dead_mcp_server_fails_the_match_instead_of_playing_with_no_tools(self):
        ctx, _ = make_ctx(url="http://127.0.0.1:1/mcp")
        model = ScriptedPlayer()
        with pytest.raises(Exception):  # noqa: B017, PT011 - any failure is right; silently playing is the bug
            harness_agent.play(ctx, create=real_transport_create(model), state_dir="/unused")
        assert model.calls == []
