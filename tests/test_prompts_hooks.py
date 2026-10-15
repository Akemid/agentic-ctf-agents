"""Characterization tests: shared prompts and hooks must behave exactly like the refined originals
in `local/agent/refined/` (harness_sdk.py), plus the new stop and tick hooks."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctf_common import hooks, prompts

ACT_FAST = (
    "Each tick: call wait_for_turn, then immediately call exactly one action tool with that tick. "
    "Do not write any text between tool calls. Do not call get_rules again after joining."
)


ORIGINAL = Path(__file__).resolve().parents[1] / "local" / "agent" / "refined" / "harness_sdk.py"


@pytest.fixture(scope="module")
def original():
    """The refined original, imported read-only; failures below show a readable diff."""
    if not ORIGINAL.exists():
        pytest.skip("local/agent/refined/harness_sdk.py is not present")
    spec = importlib.util.spec_from_file_location("refined_harness_sdk_original", ORIGINAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPrompts:
    def test_prompts_equal_the_refined_original(self, original):
        assert prompts.ACT_FAST_PROMPT == original.ACT_FAST_PROMPT
        assert prompts.SYSTEM_PROMPT == original.SYSTEM_PROMPT

    def test_act_fast_prompt_is_verbatim(self):
        assert prompts.ACT_FAST_PROMPT == ACT_FAST

    def test_system_prompt_is_unchanged(self):
        # sha256 of SYSTEM_PROMPT in local/agent/refined/harness_sdk.py (1207 chars).
        digest = hashlib.sha256(prompts.SYSTEM_PROMPT.encode()).hexdigest()
        assert digest == "c5386af66d85bb54424ba9502f87d6fe8bcd2fddf092e01ebef541b9da031f54"
        assert len(prompts.SYSTEM_PROMPT) == 1207

    def test_system_prompt_ends_with_act_fast_and_stays_neutral(self):
        assert prompts.SYSTEM_PROMPT.endswith(ACT_FAST)
        assert "The server never recommends one" in prompts.SYSTEM_PROMPT
        assert "recommended route" not in prompts.SYSTEM_PROMPT.lower()

    def test_model_aliases(self):
        assert prompts.MODEL_ALIASES == {
            "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "opus": "global.anthropic.claude-opus-4-6-v1",
            "nova-lite": "global.amazon.nova-2-lite-v1:0",
        }

    def test_aliases_cover_every_payload_model(self):
        from ctf_common.payload import MODELS

        assert set(prompts.MODEL_ALIASES) == set(MODELS)

    def test_instruction_for_auto_team_matches_the_original(self):
        assert prompts.build_instruction("llm-1", "refined-sdk") == (
            "Join match llm-1 on whichever team is available (try red first) with the name 'refined-sdk' "
            "and play by calling wait_for_turn and one action each tick until you receive match_over."
        )
        assert prompts.build_instruction("m9", "bob", "auto") == prompts.build_instruction("m9", "bob")

    @pytest.mark.parametrize("team", ["red", "blue"])
    def test_instruction_for_a_fixed_team(self, team):
        text = prompts.build_instruction("m1", "p1", team)
        assert f"on team {team}" in text and "whichever" not in text
        assert "match m1" in text and "'p1'" in text and text.endswith("until you receive match_over.")

    def test_instruction_rejects_unknown_team(self):
        with pytest.raises(ValueError, match="team"):
            prompts.build_instruction("m1", "p1", "green")


def tool_result(payload):
    return {"status": "success", "toolUseId": "t", "content": [{"text": json.dumps(payload)}]}


def after_event(name, result):
    return SimpleNamespace(tool_use={"name": name, "input": {}}, result=result)


class TestStripStaticWallsHook:
    WAIT = "ctf-game_wait_for_turn"

    def payload(self, tick):
        return {
            "status": "turn",
            "tick": tick,
            "state": {"tick": tick, "walls": [{"x": 1, "y": 1}], "grid": {"width": 15}, "self": "blue-1"},
        }

    def state_of(self, event):
        return json.loads(event.result["content"][0]["text"])["state"]

    def test_first_turn_keeps_walls_later_turns_strip(self):
        hook = hooks.StripStaticWallsHook()
        first = after_event(self.WAIT, tool_result(self.payload(1)))
        hook.after_tool_call(first)
        assert "walls" in self.state_of(first)
        second = after_event(self.WAIT, tool_result(self.payload(2)))
        hook.after_tool_call(second)
        state = self.state_of(second)
        assert "walls" not in state and state["grid"] == {"width": 15}
        assert json.loads(second.result["content"][0]["text"])["tick"] == 2
        assert second.result["toolUseId"] == "t"

    def test_unprefixed_name_also_matches(self):
        hook = hooks.StripStaticWallsHook()
        hook.after_tool_call(after_event("wait_for_turn", tool_result(self.payload(1))))
        later = after_event("wait_for_turn", tool_result(self.payload(2)))
        hook.after_tool_call(later)
        assert "walls" not in self.state_of(later)

    def test_other_tools_untouched(self):
        hook = hooks.StripStaticWallsHook()
        hook.after_tool_call(after_event(self.WAIT, tool_result(self.payload(1))))
        original = tool_result({"state": {"walls": [1]}})
        event = after_event("ctf-game_move", original)
        hook.after_tool_call(event)
        assert event.result is original
        assert json.loads(event.result["content"][0]["text"])["state"]["walls"] == [1]

    def test_malformed_json_left_as_is(self):
        hook = hooks.StripStaticWallsHook()
        hook.after_tool_call(after_event(self.WAIT, tool_result(self.payload(1))))
        bad = {"status": "success", "toolUseId": "t", "content": [{"text": "not json {"}]}
        event = after_event(self.WAIT, bad)
        hook.after_tool_call(event)
        assert event.result["content"][0]["text"] == "not json {"

    def test_error_results_do_not_count_as_first_turn(self):
        hook = hooks.StripStaticWallsHook()
        err = {"status": "error", "toolUseId": "t", "content": [{"text": "boom"}]}
        hook.after_tool_call(after_event(self.WAIT, err))
        first = after_event(self.WAIT, tool_result(self.payload(1)))
        hook.after_tool_call(first)
        assert "walls" in self.state_of(first)

    def test_register_hooks_subscribes_after_tool_call(self):
        calls = []
        registry = SimpleNamespace(add_callback=lambda event_type, cb: calls.append(event_type))
        hooks.StripStaticWallsHook().register_hooks(registry)
        assert [c.__name__ for c in calls] == ["AfterToolCallEvent"]

    def test_a_new_hook_instance_starts_fresh_per_match(self):
        first_match, second_match = hooks.StripStaticWallsHook(), hooks.StripStaticWallsHook()
        first_match.after_tool_call(after_event(self.WAIT, tool_result(self.payload(1))))
        event = after_event(self.WAIT, tool_result(self.payload(1)))
        second_match.after_tool_call(event)
        assert "walls" in self.state_of(event)


class TestConversationManager:
    def test_config(self):
        manager = hooks.make_conversation_manager(hooks.DEFAULT_WINDOW)
        assert manager.window_size == hooks.DEFAULT_WINDOW
        assert manager.pin_first == 1
        assert manager.per_turn is True
        assert hooks.make_conversation_manager(20).window_size == 20

    def test_default_window_is_about_three_ticks(self):
        assert hooks.DEFAULT_WINDOW == 12  # 4 messages per tick (wait_for_turn + action)

    def test_window_trims_old_ticks_and_keeps_instruction(self):
        manager = hooks.make_conversation_manager(12)
        messages = [{"role": "user", "content": [{"text": "join the match"}]}]
        for tick in range(10):
            for tool in ("wait_for_turn", "move"):
                uid = f"{tool}-{tick}"
                messages.append(
                    {
                        "role": "assistant",
                        "content": [{"toolUse": {"toolUseId": uid, "name": tool, "input": {}}}],
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": uid,
                                    "status": "success",
                                    "content": [{"text": str(tick)}],
                                }
                            }
                        ],
                    }
                )
        manager.apply_management(SimpleNamespace(messages=messages))
        assert len(messages) <= 13
        assert messages[0]["content"][0]["text"] == "join the match"
        assert messages[-1]["content"][0]["toolResult"]["content"][0]["text"] == "9"
        roles = [m["role"] for m in messages]
        assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), roles


class TestStopRequestedHook:
    def test_does_not_interfere_while_running(self):
        import threading

        hook = hooks.StopRequestedHook(threading.Event())
        before = SimpleNamespace(cancel_tool=False)
        after = SimpleNamespace(end_turn=False)
        hook.before_tool_call(before)
        hook.after_tools(after)
        assert before.cancel_tool is False and after.end_turn is False

    def test_stop_cancels_the_next_tool_call_and_ends_the_loop(self):
        import threading

        stop = threading.Event()
        hook = hooks.StopRequestedHook(stop)
        stop.set()
        before = SimpleNamespace(cancel_tool=False)
        after = SimpleNamespace(end_turn=False)
        hook.before_tool_call(before)
        hook.after_tools(after)
        assert isinstance(before.cancel_tool, str) and "stop" in before.cancel_tool.lower()
        assert isinstance(after.end_turn, str) and "stop" in after.end_turn.lower()

    def test_registers_before_tool_call_and_after_tools(self):
        import threading

        calls = []
        registry = SimpleNamespace(add_callback=lambda event_type, cb: calls.append(event_type))
        hooks.StopRequestedHook(threading.Event()).register_hooks(registry)
        assert [c.__name__ for c in calls] == ["BeforeToolCallEvent", "AfterToolsEvent"]


class TestTickCounterHook:
    def test_counts_successful_wait_for_turn_only(self):
        ticks = []
        hook = hooks.TickCounterHook(lambda: ticks.append(1))
        hook.after_tool_call(after_event("ctf-game_wait_for_turn", tool_result({"status": "turn"})))
        hook.after_tool_call(after_event("ctf-game_move", tool_result({"status": "ok"})))
        hook.after_tool_call(after_event("ctf-game_wait_for_turn", {"status": "error", "content": []}))
        hook.after_tool_call(after_event("wait_for_turn", tool_result({"status": "turn"})))
        assert len(ticks) == 2

    def test_match_over_is_not_a_tick(self):
        ticks = []
        hook = hooks.TickCounterHook(lambda: ticks.append(1))
        hook.after_tool_call(after_event("ctf-game_wait_for_turn", tool_result({"status": "match_over"})))
        assert ticks == []

    def test_registers_after_tool_call(self):
        calls = []
        registry = SimpleNamespace(add_callback=lambda event_type, cb: calls.append(event_type))
        hooks.TickCounterHook(lambda: None).register_hooks(registry)
        assert [c.__name__ for c in calls] == ["AfterToolCallEvent"]


class TestConnectionLostHook:
    LOST = {
        "status": "error",
        "toolUseId": "t",
        "content": [{"text": "Tool execution failed: Connection to the MCP server was closed"}],
    }

    def test_a_dead_connection_ends_the_loop_after_the_tool_batch(self):
        hook = hooks.ConnectionLostHook()
        hook.after_tool_call(after_event("ctf-game_move", self.LOST))
        batch = SimpleNamespace(end_turn=False)
        hook.after_tools(batch)
        assert hook.lost and batch.end_turn

    def test_ordinary_tool_errors_do_not_end_the_loop(self):
        hook = hooks.ConnectionLostHook()
        err = {"status": "error", "toolUseId": "t", "content": [{"text": "invalid move"}]}
        hook.after_tool_call(after_event("ctf-game_move", err))
        hook.after_tool_call(after_event("ctf-game_move", tool_result({"status": "ok"})))
        batch = SimpleNamespace(end_turn=False)
        hook.after_tools(batch)
        assert not hook.lost and batch.end_turn is False

    def test_registers_on_both_events(self):
        registered = []
        registry = SimpleNamespace(add_callback=lambda event_type, cb: registered.append(event_type))
        hooks.ConnectionLostHook().register_hooks(registry)
        assert {e.__name__ for e in registered} == {"AfterToolCallEvent", "AfterToolsEvent"}


class TestIsConnectionLost:
    @staticmethod
    def error(text, status="error"):
        return {"status": status, "toolUseId": "t", "content": [{"text": text}]}

    @pytest.mark.parametrize(
        "text",
        ["Connection to the MCP server was closed", "boom: the client session is not running (x)"],
    )
    def test_error_results_with_a_known_marker_are_a_lost_connection(self, text):
        assert hooks.is_connection_lost(self.error(text)) is True

    def test_an_ordinary_tool_error_is_not_a_lost_connection(self):
        assert hooks.is_connection_lost(self.error("tick already acted")) is False

    def test_a_successful_result_quoting_the_marker_is_not_a_lost_connection(self):
        assert (
            hooks.is_connection_lost(self.error("Connection to the MCP server was closed", "success"))
            is False
        )

    @pytest.mark.parametrize(
        "result", [None, "text", {"status": "error"}, {"status": "error", "content": [1]}]
    )
    def test_malformed_results_are_not_a_lost_connection(self, result):
        assert hooks.is_connection_lost(result) is False
