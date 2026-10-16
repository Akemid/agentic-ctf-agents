"""strand_decider play loop: a Strands Agent whose tag/grab_flag calls are reviewed by the decider."""

import threading

import httpx
import pytest

import strand_agent
from ctf_common.auth import StaticTokenProvider
from ctf_common.config import RuntimeConfig
from ctf_common.decider import DeciderCircuitOpen, HttpDecider, SageMakerDecider
from ctf_common.mcp import McpAuthError, open_mcp_client
from ctf_common.payload import Action, MatchRequest
from ctf_common.runner import MatchContext
from fake_mcp import FakeGame, handshake_ok_then_401_on_tool_call, in_memory_transport, unauthorized_after
from fake_model import ScriptedPlayer, SequencePlayer

JOIN = ("ctf-game_join_match", {"match_id": "m1", "team": "red", "name": "p1"})
WAIT = ("ctf-game_wait_for_turn", {})


class CountingTokens:
    def __init__(self):
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return f"token-{self.calls}"


class FakeDecider:
    def __init__(self, args_grounded=0.9, stale_state=0.1, raises=None):
        self.args_grounded, self.stale_state, self.raises = args_grounded, stale_state, raises
        self.asked = 0

    def ask(self, state, questions, *, deadline_s=None):
        self.asked += 1
        if self.raises:
            raise self.raises
        return {
            "args_grounded": {"noul": self.args_grounded},
            "stale_state": {"noul": self.stale_state},
        }


def make_ctx(tokens=None, *, config=None, stop=None, options=None):
    ticks = []
    request = MatchRequest(
        action=Action.START, match_id="m1", name="p1", team="auto", model="haiku", options=options or {}
    )
    ctx = MatchContext(
        request=request,
        config=config
        or RuntimeConfig(mcp_url="https://mcp.example/mcp", decider_endpoint_name="ctf-decider"),
        tokens=tokens or CountingTokens(),
        stop=stop or threading.Event(),
        tick=lambda: ticks.append(1),
    )
    return ctx, ticks


def play_against(game, ctx, model, decider):
    def mcp_factory(url, tokens):
        return open_mcp_client(url, tokens, connect=lambda u, headers: in_memory_transport(game.server))

    strand_agent.play(ctx, mcp_factory=mcp_factory, model=model, decider=decider)


def tag_then_move():
    return SequencePlayer(
        [
            JOIN,
            WAIT,
            ("ctf-game_tag", {"tick": 1, "enemy_id": "red-2"}),
            WAIT,
            ("ctf-game_move", {"tick": 2, "direction": "up"}),
        ]
    )


def names(game):
    return [name for name, _ in game.actions]


class TestGatedActions:
    def test_a_denied_tag_is_blocked_and_the_agent_keeps_playing(self):
        game, decider = FakeGame(turns=2), FakeDecider(args_grounded=0.1)
        ctx, ticks = make_ctx()
        play_against(game, ctx, tag_then_move(), decider)
        assert "tag" not in names(game)
        assert names(game) == ["join_match", "move"]  # play went on after the block
        assert decider.asked == 1 and len(ticks) == 2

    def test_an_approved_tag_is_executed(self):
        game, decider = FakeGame(turns=2), FakeDecider()
        play_against(game, make_ctx()[0], tag_then_move(), decider)
        assert names(game) == ["join_match", "tag", "move"]
        assert decider.asked == 1

    def test_when_the_decider_is_unavailable_the_tag_proceeds(self):
        game, decider = FakeGame(turns=2), FakeDecider(raises=DeciderCircuitOpen("open"))
        play_against(game, make_ctx()[0], tag_then_move(), decider)
        assert names(game) == ["join_match", "tag", "move"]

    def test_only_tag_and_grab_flag_are_reviewed(self):
        game, decider = FakeGame(turns=3), FakeDecider()
        model = SequencePlayer(
            [
                JOIN,
                WAIT,
                ("ctf-game_move", {"tick": 1, "direction": "up"}),
                WAIT,
                ("ctf-game_grab_flag", {"tick": 2}),
                WAIT,
                ("ctf-game_move", {"tick": 3, "direction": "up"}),
            ]
        )
        play_against(game, make_ctx()[0], model, decider)
        assert names(game) == ["join_match", "move", "grab_flag", "move"]
        assert decider.asked == 1  # only grab_flag


class TestMatchPlumbing:
    def test_one_token_for_the_whole_match(self):
        tokens = CountingTokens()
        game = FakeGame(turns=2)
        play_against(game, make_ctx(tokens)[0], tag_then_move(), FakeDecider())
        assert tokens.calls == 1

    def test_stop_ends_the_loop_after_the_current_tick(self):
        stop = threading.Event()
        game = FakeGame(turns=500)
        ctx, _ = make_ctx(stop=stop)
        ctx = MatchContext(
            request=ctx.request, config=ctx.config, tokens=ctx.tokens, stop=stop, tick=lambda: stop.set()
        )
        play_against(game, ctx, ScriptedPlayer(), FakeDecider())
        assert game.tick == 1 and "move" not in names(game)

    def test_the_decider_is_built_from_the_runtime_config_when_not_injected(self):
        ctx, _ = make_ctx(config=RuntimeConfig(mcp_url="http://m", decider_endpoint_name="ctf-decider"))
        assert isinstance(strand_agent.build_decider(ctx), SageMakerDecider)
        local, _ = make_ctx(config=RuntimeConfig(mcp_url="http://m", decider_url="http://127.0.0.1:8099"))
        assert isinstance(strand_agent.build_decider(local), HttpDecider)


class TestMidMatchUnauthorized:
    def test_401_on_the_code_owned_join_fails_the_match_with_an_auth_error_and_no_refresh(self):
        tokens = CountingTokens()
        ctx, _ = make_ctx(tokens)

        def client_factory(headers=None, timeout=None, auth=None):
            transport = httpx.MockTransport(handshake_ok_then_401_on_tool_call)
            return httpx.AsyncClient(transport=transport, headers=headers)

        def mcp_factory(url, token_source):
            return open_mcp_client(url, token_source, client_factory=client_factory)

        model = ScriptedPlayer()
        with pytest.raises(McpAuthError, match="401"):
            strand_agent.play(ctx, mcp_factory=mcp_factory, model=model, decider=FakeDecider())
        assert tokens.calls == 1
        assert model.calls == []  # the join met the 401: the model was never consulted

    def test_401_after_a_successful_join_still_fails_the_match_and_stops_the_loop(self):
        tokens = CountingTokens()
        ctx, _ = make_ctx(tokens)
        transport = httpx.MockTransport(unauthorized_after(1))

        def mcp_factory(url, token_source):
            factory = lambda headers=None, timeout=None, auth=None: httpx.AsyncClient(  # noqa: E731
                transport=transport, headers=headers
            )
            return open_mcp_client(url, token_source, client_factory=factory)

        model = ScriptedPlayer()
        with pytest.raises(McpAuthError, match="401"):
            strand_agent.play(ctx, mcp_factory=mcp_factory, model=model, decider=FakeDecider())
        assert tokens.calls == 1 and len(model.calls) == 1
        assert ctx.join.outcome.kind == "joined"  # the seat was real; the failure came later


class TestRunner:
    def test_the_runner_requires_the_decider_endpoint_parameter(self):
        class App:
            def add_async_task(self, *a, **k):
                raise AssertionError("must not start")

            def complete_async_task(self, *a):
                raise AssertionError("must not start")

        runner = strand_agent.build_runner(
            App(), {"CTF_MCP_URL": "http://m"}, token_provider=StaticTokenProvider("t"), play=lambda ctx: None
        )
        request = MatchRequest(
            action=Action.START, match_id="m1", name="p1", team="auto", model="haiku", options={}
        )
        resp = runner.start(request)
        assert resp["status"] == "failed" and "CTF_DECIDER_ENDPOINT_PARAM" in resp["error"]
