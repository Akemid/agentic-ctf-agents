"""RED-first tests for the S4 benchmark helpers (pure functions only)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import s4_lib  # noqa: E402

LOG = """\
[tick 0] raw turn:
{ "status": "turn" }
[tick 0] state sent to decider:
tick: 0
you: blue-1 (blue) at (14, 7), in your own field
routes:
  enemy_flag_direct: goal enemy_flag, 13 steps
[tick 0] options: ['enemy_flag_detour', 'enemy_flag_direct', 'wait']
[tick 0] timing: decider wall 4106 ms
[tick 1] state sent to decider:
tick: 1
you: blue-1 (blue) at (13, 7), in your own field
[tick 1] options: ['enemy_flag_direct', 'wait']
"""


def test_parse_states_extracts_state_text_and_options():
    states = s4_lib.parse_states(LOG)
    assert len(states) == 2
    assert states[0]["state"].startswith("tick: 0")
    assert "enemy_flag_direct: goal enemy_flag, 13 steps" in states[0]["state"]
    assert states[0]["options"] == ["enemy_flag_detour", "enemy_flag_direct", "wait"]
    assert states[1]["options"] == ["enemy_flag_direct", "wait"]


def test_parse_states_ignores_logs_without_states():
    assert s4_lib.parse_states("nothing here\n") == []


def test_build_request_has_wire_shape_and_three_questions():
    body = s4_lib.build_request("tick: 0", ["a", "wait"])
    assert body["state"] == "tick: 0"
    assert body["model"] == "hobson-latest"
    assert set(body["questions"]) == {"threat", "route_bad", "action"}
    assert body["questions"]["threat"]["type"] == "noul"
    assert set(body["questions"]["action"]["criteria"]) == {"a", "wait"}


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert s4_lib.percentile(values, 50) == 50.0
    assert s4_lib.percentile(values, 95) == 95.0
    assert s4_lib.percentile([3.0], 95) == 3.0


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        s4_lib.percentile([], 95)


def test_max_prob_delta_compares_numeric_leaves():
    a = {"threat": {"probability": 0.10}, "action": {"confidence": 0.5, "choice": "x"}}
    b = {"threat": {"probability": 0.14}, "action": {"confidence": 0.46, "choice": "x"}}
    assert s4_lib.max_prob_delta(a, b) == pytest.approx(0.04)


def test_max_prob_delta_flags_changed_choice_as_infinite():
    a = {"action": {"choice": "x"}}
    b = {"action": {"choice": "y"}}
    assert s4_lib.max_prob_delta(a, b) == float("inf")


def test_select_usable_states_keeps_only_states_with_options_up_to_n():
    states = [{"state": "a", "options": []}, {"state": "b", "options": ["x"]}, {"state": "c", "options": ["y"]}]
    assert s4_lib.select_usable_states(states, 1) == [{"state": "b", "options": ["x"]}]


def test_select_usable_states_rejects_when_none_usable():
    with pytest.raises(ValueError, match="no usable states"):
        s4_lib.select_usable_states([{"state": "a", "options": []}], 5)


def test_check_comparable_rejects_empty_answers():
    with pytest.raises(ValueError, match="empty"):
        s4_lib.check_comparable([], [])


def test_check_comparable_rejects_length_mismatch():
    with pytest.raises(ValueError, match="different lengths"):
        s4_lib.check_comparable([1, 2], [1])


def test_check_comparable_accepts_equal_lengths():
    s4_lib.check_comparable([1], [2])
