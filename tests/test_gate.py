"""strand_decider gate: tag and grab_flag are reviewed by the decider, everything else proceeds."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.interventions import Guide, Proceed

import gate
from ctf_common.decider import DeciderCircuitOpen, DeciderTimeout, ServerUnavailable

REFINED = Path(__file__).resolve().parents[1] / "local" / "agent" / "refined" / "strand_decider.py"


class FakeDecider:
    """Answers with fixed probabilities, or raises, and records what it was asked."""

    def __init__(self, args_grounded=0.9, stale_state=0.1, *, raises=None):
        self.args_grounded, self.stale_state = args_grounded, stale_state
        self.raises = list(raises or [])
        self.asked: list[tuple[str, dict]] = []
        self.kwargs: list[dict] = []

    def ask(self, state, questions, *, deadline_s=None):
        self.asked.append((state, questions))
        self.kwargs.append({"deadline_s": deadline_s})
        if self.raises:
            raise self.raises.pop(0)
        return {
            "args_grounded": {"type": "noul", "noul": self.args_grounded},
            "stale_state": {"type": "noul", "noul": self.stale_state},
        }


def event(tool="ctf-game_tag", arguments=None, messages=None):
    return SimpleNamespace(
        tool_use={
            "name": tool,
            "input": arguments if arguments is not None else {"tick": 3, "enemy_id": "red-2"},
        },
        agent=SimpleNamespace(messages=messages if messages is not None else []),
    )


class TestUngatedTools:
    @pytest.mark.parametrize(
        "tool", ["ctf-game_move", "ctf-game_wait", "ctf-game_wait_for_turn", "ctf-game_join_match", "move"]
    )
    def test_other_tools_proceed_without_asking_the_decider(self, tool):
        decider = FakeDecider()
        result = gate.ToolCallReviewer(decider).before_tool_call(event(tool))
        assert isinstance(result, Proceed) and result.reason == "ungated"
        assert decider.asked == []


class TestGatedTools:
    @pytest.mark.parametrize("tool", ["ctf-game_tag", "ctf-game_grab_flag", "tag", "grab_flag"])
    def test_tag_and_grab_flag_are_reviewed(self, tool):
        decider = FakeDecider()
        result = gate.ToolCallReviewer(decider).before_tool_call(event(tool, {"tick": 1}))
        assert isinstance(result, Proceed)
        assert len(decider.asked) == 1

    def test_the_decider_sees_the_recent_game_and_the_proposed_call(self):
        decider = FakeDecider()
        messages = [
            {"role": "user", "content": [{"text": "Join match m1"}]},
            {"role": "assistant", "content": [{"toolUse": {"name": "ctf-game_wait_for_turn", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"content": [{"text": '{"status": "turn"}'}]}}]},
        ]
        gate.ToolCallReviewer(decider).before_tool_call(
            event("ctf-game_tag", {"tick": 3, "enemy_id": "red-2"}, messages)
        )
        state, questions = decider.asked[0]
        assert "user: Join match m1" in state
        assert "assistant called ctf-game_wait_for_turn: {}" in state
        assert 'tool result: {"status": "turn"}' in state
        assert 'tool: tag\narguments: {"tick": 3, "enemy_id": "red-2"}' in state
        assert set(questions) == {"args_grounded", "stale_state"}

    def test_ungrounded_arguments_guide_the_model_back(self):
        reviewer = gate.ToolCallReviewer(FakeDecider(args_grounded=0.2))
        result = reviewer.before_tool_call(event())
        assert isinstance(result, Guide) and "not backed by the game state" in result.feedback

    def test_stale_state_guides_the_model_to_observe_first(self):
        reviewer = gate.ToolCallReviewer(FakeDecider(args_grounded=0.9, stale_state=0.45))
        result = reviewer.before_tool_call(event())
        assert isinstance(result, Guide) and "outdated game state" in result.feedback

    def test_the_boundary_values_are_the_policy_knob(self):
        assert isinstance(gate.ToolCallReviewer(FakeDecider(0.45, 0.449)).before_tool_call(event()), Proceed)
        assert isinstance(gate.ToolCallReviewer(FakeDecider(0.449, 0.0)).before_tool_call(event()), Guide)

    def test_a_confident_decider_lets_the_call_through_and_is_logged(self):
        reviewer = gate.ToolCallReviewer(FakeDecider(0.9, 0.1))
        reviewer.before_tool_call(event())
        assert reviewer.log == [
            {"tool": "tag", "args_grounded": 0.9, "stale_state": 0.1, "verdict": "proceeded"}
        ]


class TestGuideCap:
    def test_after_two_consecutive_guides_the_next_call_proceeds(self):
        decider = FakeDecider(args_grounded=0.1)
        reviewer = gate.ToolCallReviewer(decider)
        results = [reviewer.before_tool_call(event()) for _ in range(4)]
        assert [type(r) for r in results] == [Guide, Guide, Proceed, Guide]
        assert results[2].reason == "guide cap reached"
        assert len(decider.asked) == 3  # the forced proceed did not consult the decider
        assert [row["verdict"] for row in reviewer.log] == ["guided", "guided", "forced", "guided"]

    def test_a_proceed_resets_the_guide_streak(self):
        decider = FakeDecider(args_grounded=0.1)
        reviewer = gate.ToolCallReviewer(decider)
        reviewer.before_tool_call(event())
        decider.args_grounded = 0.9
        reviewer.before_tool_call(event())
        decider.args_grounded = 0.1
        results = [reviewer.before_tool_call(event()) for _ in range(2)]
        assert [type(r) for r in results] == [Guide, Guide]


class TestDeciderUnavailable:
    def test_a_failed_call_fails_open(self):
        reviewer = gate.ToolCallReviewer(FakeDecider(raises=[DeciderTimeout("slow")]))
        result = reviewer.before_tool_call(event())
        assert isinstance(result, Proceed) and result.reason == "decider unavailable"

    def test_there_is_no_permanent_down_state_the_next_call_asks_again(self):
        decider = FakeDecider(args_grounded=0.1, raises=[DeciderCircuitOpen("open")])
        reviewer = gate.ToolCallReviewer(decider)
        assert isinstance(reviewer.before_tool_call(event()), Proceed)
        assert isinstance(reviewer.before_tool_call(event()), Guide)  # the decider is back and says no
        assert len(decider.asked) == 2

    def test_an_unexpected_decider_bug_does_not_crash_the_match(self):
        reviewer = gate.ToolCallReviewer(FakeDecider(raises=[ValueError("bug")]))
        result = reviewer.before_tool_call(event())
        assert isinstance(result, Proceed) and result.reason == "decider unavailable"

    def test_unavailable_is_the_common_base_of_decider_failures(self):
        assert issubclass(DeciderTimeout, ServerUnavailable)


class RawDecider:
    """Returns whatever it is given as the answers, well-formed or not."""

    def __init__(self, answers):
        self.answers = answers

    def ask(self, state, questions, *, deadline_s=None):
        return self.answers


MALFORMED = [
    {},
    {"args_grounded": {}},
    {"args_grounded": {"noul": None}},
    {"args_grounded": "0.9", "stale_state": {"noul": 0.1}},
    {"args_grounded": {"noul": 0.9}},  # stale_state missing
    {"args_grounded": {"noul": 0.9}, "stale_state": {"noul": "high"}},
    {"args_grounded": {"noul": True}, "stale_state": {"noul": 0.1}},
    {"args_grounded": {"noul": float("nan")}, "stale_state": {"noul": 0.1}},
    None,
    "garbage",
    [],
]


class TestMalformedAnswersFailOpen:
    @pytest.mark.parametrize("answers", MALFORMED, ids=repr)
    def test_a_malformed_answer_proceeds_and_is_not_counted_as_a_guide(self, answers, caplog):
        reviewer = gate.ToolCallReviewer(RawDecider(answers))
        with caplog.at_level("WARNING", logger="strand_decider"):
            results = [reviewer.before_tool_call(event()) for _ in range(5)]
        assert all(isinstance(r, Proceed) and r.reason == "decider unavailable" for r in results)
        assert reviewer.log == []
        assert any("malformed" in r.message for r in caplog.records)

    def test_the_match_continues_with_a_good_answer_after_a_bad_one(self):
        decider = RawDecider({})
        reviewer = gate.ToolCallReviewer(decider)
        assert isinstance(reviewer.before_tool_call(event()), Proceed)
        decider.answers = {"args_grounded": {"noul": 0.1}, "stale_state": {"noul": 0.1}}
        assert isinstance(reviewer.before_tool_call(event()), Guide)

    def test_the_handler_itself_fails_open_on_error(self):
        assert gate.ToolCallReviewer(FakeDecider()).on_error == "proceed"


class TestParityWithTheLocalReviewer:
    """The deployed gate and the local script must ask the same questions and render the same game."""

    @pytest.fixture
    def local(self):
        refined = REFINED.parent
        client_dir = str(refined.parent)
        sys.path[:0] = [client_dir, str(refined)]
        try:
            spec = importlib.util.spec_from_file_location("local_strand_decider", REFINED)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            for path in (client_dir, str(refined)):
                sys.path.remove(path)
        return module

    def test_questions_match(self, local):
        assert gate.QUESTIONS == local.QUESTIONS

    def test_policy_constants_match(self, local):
        assert (gate.YES, gate.GATED_TOOLS, gate.MAX_CONSECUTIVE_GUIDES) == (
            local.YES,
            local.GATED_TOOLS,
            local.MAX_CONSECUTIVE_GUIDES,
        )
        assert (gate.MAX_STATE_MESSAGES, gate.MAX_BLOCK_CHARS) == (
            local.MAX_STATE_MESSAGES,
            local.MAX_BLOCK_CHARS,
        )

    def test_render_and_clip_match(self, local):
        long_text = "x" * 2000
        messages = [{"role": "user", "content": [{"text": f"m{i}"}]} for i in range(20)]
        messages += [
            {"role": "assistant", "content": [{"toolUse": {"name": "ctf-game_move", "input": {"tick": 1}}}]},
            {
                "role": "user",
                "content": [{"toolResult": {"content": [{"text": long_text}, {"json": {"a": 1}}]}}],
            },
        ]
        assert gate.ToolCallReviewer._render(messages) == local.ToolCallReviewer._render(messages)
        assert (
            gate.ToolCallReviewer._render([]) == local.ToolCallReviewer._render([]) == "(no conversation yet)"
        )
        assert gate.ToolCallReviewer._clip(long_text).endswith(" ...[truncated]")
        assert gate.ToolCallReviewer._clip(long_text) == local.ToolCallReviewer._clip(long_text)

    def test_short_name_strips_the_server_prefix(self, local):
        for name in ("ctf-game_tag", "tag", "other_tag"):
            assert gate.ToolCallReviewer._short_name(name) == local.ToolCallReviewer._short_name(name)
        assert gate.ToolCallReviewer._short_name("ctf-game_grab_flag") == "grab_flag"
