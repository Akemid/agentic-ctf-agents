"""ForceJoinArgsHook: the model never decides where the character sits (join arguments are forced)."""

import json
from types import SimpleNamespace

import pytest
from strands import Agent

from ctf_common import hooks
from ctf_common.hooks import ForceJoinArgsHook
from ctf_common.join import JoinSignal
from ctf_common.mcp import open_mcp_client
from fake_mcp import FakeGame, in_memory_transport
from fake_model import ScriptedPlayer


def make_hook(team="red", agent_index=1, match_id="m1", name="p1"):
    signal = JoinSignal()
    return ForceJoinArgsHook(match_id, name, team, agent_index, signal), signal


def before(tool="ctf-game_join_match", **input_):
    return SimpleNamespace(tool_use={"name": tool, "input": input_}, cancel_tool=False)


def after(result, name="ctf-game_join_match", cancel_message=None):
    return SimpleNamespace(tool_use={"name": name, "input": {}}, result=result, cancel_message=cancel_message)


def ok(**payload):
    return {"status": "success", "content": [{"text": json.dumps(payload)}]}


def refused(reason):
    return {"status": "error", "content": [{"text": json.dumps({"reason": reason})}]}


class TestBeforeJoin:
    def test_every_argument_is_overwritten_with_the_requested_ones(self):
        hook, _ = make_hook(team="blue", agent_index=2, match_id="m9", name="real")
        event = before(match_id="evil", name="evil", team="red", agent_index=0, extra="x")
        hook.before_tool_call(event)
        assert event.tool_use["input"] == {"match_id": "m9", "name": "real", "team": "blue", "agent_index": 2}

    def test_without_a_slot_agent_index_is_removed_not_left_to_the_model(self):
        hook, _ = make_hook(team="red", agent_index=None)
        event = before(match_id="m1", name="p1", team="red", agent_index=2)
        hook.before_tool_call(event)
        assert "agent_index" not in event.tool_use["input"]

    @pytest.mark.parametrize("tool", ["join_match", "ctf-game_join_match", "x_join_match"])
    def test_the_prefixed_and_the_raw_tool_names_both_match(self, tool):
        hook, _ = make_hook()
        event = before(tool, team="blue")
        hook.before_tool_call(event)
        assert event.tool_use["input"]["team"] == "red"

    def test_other_tools_are_untouched(self):
        hook, _ = make_hook()
        event = before("ctf-game_move", tick=3, direction="up")
        hook.before_tool_call(event)
        assert event.tool_use["input"] == {"tick": 3, "direction": "up"}
        assert event.cancel_tool is False

    def test_a_second_join_after_success_is_cancelled_instead_of_reaching_the_server(self):
        hook, signal = make_hook()
        signal.succeed("red", 1)
        event = before(team="blue")
        hook.before_tool_call(event)
        assert isinstance(event.cancel_tool, str) and "already" in event.cancel_tool.lower()

    def test_a_join_after_a_failed_join_is_cancelled(self):
        hook, signal = make_hook()
        signal.refuse("slot_taken")
        event = before()
        hook.before_tool_call(event)
        assert isinstance(event.cancel_tool, str)

    def test_team_auto_offers_red_first_then_blue(self):
        hook, _ = make_hook(team="auto", agent_index=None)
        first = before(team="blue")
        hook.before_tool_call(first)
        assert first.tool_use["input"]["team"] == "red"


class TestAfterJoin:
    def test_success_reports_the_team_and_agent_index_the_server_assigned(self):
        hook, signal = make_hook(team="auto", agent_index=None)
        hook.before_tool_call(before())
        hook.after_tool_call(after(ok(agent_id="blue-1", team="blue", agent_index=0)))
        assert (signal.outcome.kind, signal.outcome.team, signal.outcome.agent_index) == ("joined", "blue", 0)

    def test_a_refusal_reports_the_reason_and_ends_the_turn_so_the_model_cannot_retry(self):
        hook, signal = make_hook()
        hook.after_tool_call(after(refused("slot_taken")))
        batch = SimpleNamespace(end_turn=False)
        hook.after_tools(batch)
        assert (signal.outcome.kind, signal.outcome.reason) == ("refused", "slot_taken")
        assert isinstance(batch.end_turn, str) and hook.failed

    def test_a_successful_join_does_not_end_the_turn(self):
        hook, _ = make_hook()
        hook.after_tool_call(after(ok(team="red", agent_index=1)))
        batch = SimpleNamespace(end_turn=False)
        hook.after_tools(batch)
        assert batch.end_turn is False and not hook.failed

    def test_a_cancelled_second_join_is_not_reported_and_never_fails_the_match(self):
        hook, signal = make_hook()
        signal.succeed("red", 1)
        hook.after_tool_call(after(refused("already_joined"), cancel_message="already joined"))
        hook.after_tool_call(after(refused("already_joined")))  # even if the server did answer
        batch = SimpleNamespace(end_turn=False)
        hook.after_tools(batch)
        assert signal.outcome.kind == "joined" and batch.end_turn is False and not hook.failed

    def test_a_parallel_duplicate_join_answering_already_joined_first_is_a_no_op(self):
        """Two join_match calls in one batch: the server answers one with `already_joined`."""
        hook, signal = make_hook()
        hook.before_tool_call(before())
        hook.before_tool_call(before())
        hook.after_tool_call(after(refused("already_joined")))
        assert signal.outcome is None and not hook.failed  # not a refusal: the other call will report
        hook.after_tool_call(after(ok(team="red", agent_index=1)))
        batch = SimpleNamespace(end_turn=False)
        hook.after_tools(batch)
        assert (signal.outcome.kind, signal.outcome.team, signal.outcome.agent_index) == ("joined", "red", 1)
        assert batch.end_turn is False and not hook.failed

    def test_other_tools_are_never_reported(self):
        hook, signal = make_hook()
        hook.after_tool_call(after(ok(team="blue", agent_index=0), name="ctf-game_move"))
        assert signal.outcome is None

    def test_auto_retries_blue_after_red_is_full_and_only_then_gives_up(self):
        hook, signal = make_hook(team="auto", agent_index=None)
        red, blue = before(), before()
        hook.before_tool_call(red)
        hook.after_tool_call(after(refused("match_full")))
        batch = SimpleNamespace(end_turn=False)
        hook.after_tools(batch)
        assert signal.outcome is None and batch.end_turn is False  # the model may try the other team
        hook.before_tool_call(blue)
        assert blue.tool_use["input"]["team"] == "blue"
        hook.after_tool_call(after(refused("match_full")))
        hook.after_tools(batch)
        assert signal.outcome.kind == "refused" and signal.outcome.reason == "match_full"
        assert isinstance(batch.end_turn, str)

    def test_auto_does_not_retry_after_a_reason_other_than_match_full(self):
        hook, signal = make_hook(team="auto", agent_index=None)
        hook.before_tool_call(before())
        hook.after_tool_call(after(refused("match_already_started")))
        assert signal.outcome.reason == "match_already_started"

    def test_a_turn_that_ends_after_match_full_on_red_reports_that_refusal(self):
        hook, signal = make_hook(team="auto", agent_index=None)
        hook.before_tool_call(before())
        hook.after_tool_call(after(refused("match_full")))
        assert signal.outcome is None  # the model may still try blue
        hook.settle_unanswered()  # ... but the turn ended without another join
        assert (signal.outcome.kind, signal.outcome.reason) == ("refused", "match_full")

    def test_settling_never_overrides_a_join_that_answered(self):
        hook, signal = make_hook(team="auto", agent_index=None)
        hook.before_tool_call(before())
        hook.after_tool_call(after(refused("match_full")))
        hook.before_tool_call(before())
        hook.after_tool_call(after(ok(team="blue", agent_index=0)))
        hook.settle_unanswered()
        assert signal.outcome.kind == "joined"

    def test_settling_without_any_refusal_leaves_the_join_pending(self):
        hook, signal = make_hook(team="auto", agent_index=None)
        hook.settle_unanswered()
        assert signal.outcome is None

    def test_a_dead_connection_on_join_is_an_error_not_a_refusal(self):
        hook, signal = make_hook()
        dead = {"status": "error", "content": [{"text": "Connection to the MCP server was closed"}]}
        hook.after_tool_call(after(dead))
        assert signal.outcome.kind == "error"

    def test_registers_on_before_after_and_after_tools(self):
        registered = []
        registry = SimpleNamespace(add_callback=lambda event_type, cb: registered.append(event_type))
        make_hook()[0].register_hooks(registry)
        assert {e.__name__ for e in registered} == {
            "BeforeToolCallEvent",
            "AfterToolCallEvent",
            "AfterToolsEvent",
        }


def run_agent(game, hook, model):
    """A real Strands Agent, the real hook machinery, an in-memory MCP game and a scripted model."""
    client = open_mcp_client(
        "https://x/mcp",
        SimpleNamespace(get_token=lambda: "t"),
        connect=lambda u, h: in_memory_transport(game.server),
    )
    with client:
        agent = Agent(
            model=model,
            tools=client.list_tools_sync(),
            hooks=[hook],
            callback_handler=None,
        )
        agent("join and play")


class TestWithARealAgent:
    """The claim the whole design rests on: a hook mutation of `tool_use["input"]` reaches the server."""

    def test_the_server_sees_the_forced_arguments_not_the_models(self):
        game = FakeGame(turns=1)
        hook, signal = make_hook(team="blue", agent_index=2, match_id="m1", name="p1")
        model = ScriptedPlayer({"match_id": "evil", "team": "red", "name": "evil", "agent_index": 0})
        run_agent(game, hook, model)
        assert game.actions[0] == (
            "join_match",
            {"match_id": "m1", "name": "p1", "team": "blue", "agent_index": 2},
        )
        assert (signal.outcome.team, signal.outcome.agent_index) == ("blue", 2)

    def test_a_refused_join_ends_the_agent_loop_without_a_second_model_call_or_another_slot(self):
        game = FakeGame(turns=1, taken_slots=(("red", 1),))
        hook, signal = make_hook(team="red", agent_index=1)
        model = ScriptedPlayer({"match_id": "m1", "team": "red", "name": "p1"})
        run_agent(game, hook, model)
        assert signal.outcome.kind == "refused" and signal.outcome.reason == "slot_taken"
        assert len(model.calls) == 1
        assert [n for n, _ in game.actions] == ["join_match"]  # one attempt, no fallback, nothing else

    def test_the_hook_module_exports_the_force_join_hook(self):
        assert hooks.ForceJoinArgsHook is ForceJoinArgsHook
