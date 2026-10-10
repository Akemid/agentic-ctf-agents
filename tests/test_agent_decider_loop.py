"""Tests for the pure logic in agent/agent-decider-loop.py.

Written with unittest-style classes so they run under both pytest and
`python -m unittest` (pytest is not installed in this project's venv).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT_DIR))

_spec = importlib.util.spec_from_file_location("agent_decider_loop", AGENT_DIR / "agent-decider-loop.py")
loop = importlib.util.module_from_spec(_spec)
sys.modules["agent_decider_loop"] = loop  # dataclasses resolves string annotations via sys.modules
_spec.loader.exec_module(loop)


def tool_result(payload: dict, is_error: bool = False) -> dict:
    return {"status": "error" if is_error else "success", "toolUseId": "t",
            "content": [{"text": json.dumps(payload)}], "isError": is_error}


def make_turn(me=(14, 7), enemy=(1, 7), carrying=None, moves=None, flag_carried_by=None, tick=1):
    """Real wait_for_turn shape (tick 1 of a live match), parameterised for the cases we test."""
    return {
        "status": "turn", "tick": tick, "deadline_ms": 15000, "deadline_remaining_ms": 15000,
        "state": {
            "tick": tick,
            "grid": {"width": 15, "height": 15},
            "walls": [{"x": 0, "y": 2}, {"x": 1, "y": 3}],
            "agents": [
                {"id": "blue-1", "team": "blue", "position": {"x": me[0], "y": me[1]},
                 "carrying": carrying, "disconnected": False},
                {"id": "red-1", "team": "red", "position": {"x": enemy[0], "y": enemy[1]},
                 "carrying": None, "disconnected": False},
            ],
            "flags": [
                {"team": "red", "base": {"x": 1, "y": 7}, "position": {"x": 1, "y": 7}, "carried_by": flag_carried_by},
                {"team": "blue", "base": {"x": 13, "y": 7}, "position": {"x": 13, "y": 7}, "carried_by": None},
            ],
            "score": {"red": 0, "blue": 0},
            "fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7},
            "self": "blue-1",
            "self_position": {"x": me[0], "y": me[1]},
            "moves": moves or {"up": "open", "down": "open", "left": "open", "right": "out_of_bounds"},
            "teammates": [],
        },
        "events": [{"type": "moved", "agent_id": "red-1", "from": {"x": 0, "y": 7}, "to": {"x": 1, "y": 7}}],
        "_is_error": False,
    }

TURN = make_turn()

class TestParseToolResult(unittest.TestCase):
    def test_decodes_json_text_block(self):
        self.assertEqual(loop.parse_tool_result(tool_result({"a": 1})), {"a": 1})

    def test_non_json_text_is_wrapped(self):
        r = {"status": "success", "content": [{"text": "boom"}], "isError": False}
        self.assertEqual(loop.parse_tool_result(r), {"text": "boom"})

    def test_empty_content(self):
        self.assertEqual(loop.parse_tool_result({"content": []}), {})


class TestTickAndStatus(unittest.TestCase):
    def test_top_level_tick(self):
        self.assertEqual(loop.extract_tick(make_turn(tick=7)), 7)

    def test_state_tick_when_no_top_level(self):
        self.assertEqual(loop.extract_tick({"state": {"tick": 3}}), 3)

    def test_top_level_preferred_over_state(self):
        self.assertEqual(loop.extract_tick({"tick": 5, "state": {"tick": 3}}), 5)

    def test_missing_tick_raises(self):
        with self.assertRaises(ValueError):
            loop.extract_tick({"status": "turn"})

class TestBuildOptionsFallback(unittest.TestCase):
    """No `routes` in the state: the agent falls back to per-direction options."""

    def test_out_of_bounds_move_excluded(self):
        opts = loop.build_options(TURN)
        self.assertNotIn("move_right", opts)
        for d in ("up", "down", "left"):
            self.assertIn(f"move_{d}", opts)

    def test_wall_move_excluded(self):
        turn = make_turn(moves={"up": "wall", "down": "open", "left": "open", "right": "open"})
        self.assertNotIn("move_up", loop.build_options(turn))

    def test_wait_always_present(self):
        self.assertIn("wait", loop.build_options({"state": {}}))
        self.assertIn("wait", loop.build_options(TURN))

    def test_no_moves_when_state_missing(self):
        self.assertEqual(list(loop.build_options({})), ["wait"])

    def test_empty_routes_falls_back_to_moves(self):
        turn = make_turn()
        turn["state"]["routes"] = []
        self.assertEqual(list(loop.build_options(turn)), ["move_up", "move_down", "move_left", "wait"])

    def test_tag_when_adjacent_and_in_own_field(self):
        self.assertIn("tag_red-1", loop.build_options(make_turn(me=(8, 7), enemy=(8, 6))))

    def test_no_tag_when_adjacent_but_outside_own_field(self):
        self.assertNotIn("tag_red-1", loop.build_options(make_turn(me=(7, 7), enemy=(6, 7))))

    def test_no_tag_when_in_field_but_not_adjacent(self):
        self.assertNotIn("tag_red-1", loop.build_options(make_turn(me=(9, 7), enemy=(9, 9))))

    def test_no_tag_when_diagonal(self):
        self.assertNotIn("tag_red-1", loop.build_options(make_turn(me=(9, 7), enemy=(10, 8))))

    def test_no_tag_for_disconnected_enemy(self):
        turn = make_turn(me=(8, 7), enemy=(8, 6))
        turn["state"]["agents"][1]["disconnected"] = True
        self.assertNotIn("tag_red-1", loop.build_options(turn))

    def test_no_tag_for_teammate(self):
        turn = make_turn(me=(8, 7), enemy=(8, 6))
        turn["state"]["agents"][1]["team"] = "blue"
        self.assertNotIn("tag_red-1", loop.build_options(turn))

    def test_grab_flag_only_on_enemy_flag_cell(self):
        self.assertIn("grab_flag", loop.build_options(make_turn(me=(1, 7), enemy=(5, 5))))
        self.assertNotIn("grab_flag", loop.build_options(make_turn(me=(2, 7), enemy=(5, 5))))

    def test_no_grab_flag_when_flag_already_carried(self):
        turn = make_turn(me=(1, 7), enemy=(5, 5), flag_carried_by="red-1")
        self.assertNotIn("grab_flag", loop.build_options(turn))

    def test_observation_fallback_when_state_missing(self):
        turn = {"tick": 1, "observation": {"self": "x", "moves": {"up": "open", "down": "wall",
                                                                  "left": "wall", "right": "wall"}}}
        opts = loop.build_options(turn)
        self.assertIn("move_up", opts)
        self.assertNotIn("move_down", opts)


DETOUR = {"id": "enemy_flag_detour", "goal": "enemy_flag", "first_step": "down", "length": 8,
          "path": [{"x": 7, "y": 8}], "blocked_now": False, "blocked_reason": None,
          "risks": [], "min_enemy_distance": 2}
DIRECT = {"id": "enemy_flag_direct", "goal": "enemy_flag", "first_step": "right", "length": 6,
          "path": [{"x": 8, "y": 7}], "blocked_now": True, "blocked_reason": "contention",
          "risks": [{"cell": {"x": 8, "y": 7}, "step": 1, "enemy_id": "blue-1"}],
          "min_enemy_distance": 1}
TAG_ADJACENT = {"id": "tag:blue-2", "goal": "tag", "first_step": None, "length": 0, "path": [],
                "blocked_now": False, "blocked_reason": None, "risks": [], "min_enemy_distance": 1}
TAG_FAR = {"id": "tag:blue-1", "goal": "tag", "first_step": "up", "length": 3, "path": [],
           "blocked_now": False, "blocked_reason": None, "risks": [], "min_enemy_distance": 2}


def make_route_turn(routes=None, me=(7, 7), enemy=(8, 6), events=None, tick=7):
    """red-1 (red) vs blue-1 (blue): red field 0..6, blue field 8..14, neutral line x=7, with server routes."""
    turn = make_deadlock_turn(me=me, enemy=enemy, events=events, tick=tick)
    turn["state"]["routes"] = [DETOUR, DIRECT] if routes is None else routes
    return turn


class TestRouteOptions(unittest.TestCase):
    def test_one_option_per_route_plus_wait(self):
        opts = loop.build_options(make_route_turn())
        self.assertEqual(list(opts), ["enemy_flag_detour", "enemy_flag_direct", "wait"])

    def test_no_per_direction_options_when_routes_exist(self):
        opts = loop.build_options(make_route_turn())
        self.assertFalse([k for k in opts if k.startswith("move_")])

    def test_description_mentions_blocked_and_risk_facts(self):
        text = loop.build_options(make_route_turn())["enemy_flag_direct"]
        self.assertIn("goal enemy_flag", text)
        self.assertIn("6 steps", text)
        self.assertIn("first step right", text)
        self.assertIn("BLOCKED now (contention)", text)
        self.assertIn("1 risk: can be tagged by blue-1 at step 1", text)
        self.assertIn("nearest enemy 1", text)

    def test_unblocked_riskless_description(self):
        text = loop.build_options(make_route_turn())["enemy_flag_detour"]
        self.assertIn("8 steps", text)
        self.assertNotIn("BLOCKED", text)
        self.assertNotIn("risk", text)

    def test_no_recommendation_wording(self):
        blob = " ".join(loop.build_options(make_route_turn()).values()) + loop.render_state(make_route_turn())
        for word in ("recommend", "suggest"):
            self.assertNotIn(word, blob.lower())

    def test_adjacent_tag_route_description(self):
        text = loop.build_options(make_route_turn(routes=[TAG_ADJACENT]))["tag:blue-2"]
        self.assertIn("goal tag blue-2", text)
        self.assertIn("adjacent now: tag immediately", text)

    def test_grab_flag_still_offered_on_the_flag_cell(self):
        turn = make_route_turn(me=(13, 7))
        self.assertIn("grab_flag", loop.build_options(turn))
        self.assertNotIn("grab_flag", loop.build_options(make_route_turn()))

    def test_malformed_routes_are_ignored(self):
        turn = make_route_turn(routes=[{"nope": 1}, "x", DETOUR])
        self.assertEqual(list(loop.build_options(turn)), ["enemy_flag_detour", "wait"])


class TestLabelToCall(unittest.TestCase):
    def test_move(self):
        self.assertEqual(loop.label_to_call("move_left", 5), ("move", {"direction": "left", "tick": 5}))

    def test_tag(self):
        self.assertEqual(loop.label_to_call("tag_blue-1", 5), ("tag", {"enemy_id": "blue-1", "tick": 5}))

    def test_grab_and_wait(self):
        self.assertEqual(loop.label_to_call("grab_flag", 2), ("grab_flag", {"tick": 2}))
        self.assertEqual(loop.label_to_call("wait", 2), ("wait", {"tick": 2}))

    def test_unknown_label_raises(self):
        with self.assertRaises(ValueError):
            loop.label_to_call("teleport", 1)

    def test_route_maps_to_move_with_first_step(self):
        tool, args = loop.label_to_call("enemy_flag_direct", 7, make_route_turn())
        self.assertEqual(tool, "move")
        self.assertEqual((args["direction"], args["tick"]), ("right", 7))
        self.assertIn("enemy_flag_direct", args["rationale"])

    def test_adjacent_tag_route_maps_to_tag(self):
        turn = make_route_turn(routes=[TAG_ADJACENT])
        tool, args = loop.label_to_call("tag:blue-2", 7, turn)
        self.assertEqual(tool, "tag")
        self.assertEqual((args["enemy_id"], args["tick"]), ("blue-2", 7))
        self.assertIn("tag:blue-2", args["rationale"])

    def test_far_tag_route_maps_to_move(self):
        turn = make_route_turn(routes=[TAG_FAR])
        tool, args = loop.label_to_call("tag:blue-1", 7, turn)
        self.assertEqual((tool, args["direction"]), ("move", "up"))

    def test_route_label_without_turn_raises(self):
        with self.assertRaises(ValueError):
            loop.label_to_call("enemy_flag_direct", 7)


class TestParseChoice(unittest.TestCase):
    def test_verified_shape(self):
        ans = {"type": "choice", "choice": "move_east", "confidence": 0.31,
               "probabilities": {"move_east": 0.48}}
        self.assertEqual(loop.parse_choice(ans), ("move_east", 0.31))

    def test_missing_confidence_falls_back_to_probability(self):
        ans = {"choice": "wait", "probabilities": {"wait": 0.9, "x": 0.1}}
        self.assertEqual(loop.parse_choice(ans), ("wait", 0.9))

    def test_garbage_gives_zero_confidence(self):
        self.assertEqual(loop.parse_choice({}), (None, 0.0))


class TestDecisionPolicy(unittest.TestCase):
    def test_threshold_inclusive(self):
        self.assertTrue(loop.accept_choice("wait", 0.85, 0.85))

    def test_below_threshold(self):
        self.assertFalse(loop.accept_choice("wait", 0.5, 0.85))

    def test_label_must_be_offered(self):
        self.assertFalse(loop.accept_choice(None, 0.99, 0.85))


class TestMisc(unittest.TestCase):
    def test_sanitize_name(self):
        self.assertEqual(loop.sanitize_name("AmazingPlayer 1!"), "amazingplayer-1")
        self.assertEqual(loop.sanitize_name("x" * 50), "x" * 32)

    def test_decider_instructions_are_neutral_about_routes(self):
        text = loop.DECIDER_INSTRUCTIONS
        self.assertIn("route", text)
        self.assertIn("tag", text)
        self.assertNotIn("enemy_flag_direct", text)
        self.assertNotIn("enemy_flag_safe", text)


class TestRenderState(unittest.TestCase):
    def test_contains_key_facts_without_client_hints(self):
        text = loop.render_state(TURN)
        self.assertIn("tick: 1", text)
        self.assertIn("blue-1", text)
        self.assertIn("(14, 7)", text)
        self.assertIn("red-1", text)
        self.assertIn("right: out_of_bounds", text)  # moves line kept when routes are empty
        self.assertNotIn("suggested direction", text)
        self.assertNotIn("target:", text)

    def test_has_no_walls(self):
        text = loop.render_state(TURN)
        self.assertNotIn("walls", text)
        self.assertNotIn('"x"', text)
        self.assertLess(len(text), 1200)

    def test_carrying(self):
        text = loop.render_state(make_turn(me=(9, 7), carrying="red", flag_carried_by="blue-1"))
        self.assertIn("carrying the flag: yes", text)

    def test_field_membership(self):
        self.assertIn("in your own field", loop.render_state(make_turn(me=(9, 7))))
        self.assertIn("in the enemy field", loop.render_state(make_turn(me=(3, 7))))
        self.assertIn("on the neutral line", loop.render_state(make_turn(me=(7, 7))))

    def test_routes_block_lists_route_facts(self):
        text = loop.render_state(make_route_turn())
        self.assertIn("routes:", text)
        self.assertIn("enemy_flag_detour: goal enemy_flag, 8 steps", text)
        self.assertIn("enemy_flag_direct: goal enemy_flag, 6 steps, first step right, BLOCKED now (contention)", text)
        self.assertIn("can be tagged by blue-1 at step 1", text)

    def test_moves_line_only_when_routes_empty(self):
        self.assertNotIn("moves:", loop.render_state(make_route_turn()))
        self.assertIn("moves:", loop.render_state(make_route_turn(routes=[])))
        self.assertNotIn("routes:", loop.render_state(make_route_turn(routes=[])))

    def test_no_client_side_risk_lines(self):
        self.assertNotIn("risk: move", loop.render_state(make_route_turn(routes=[])))

    def test_blocked_event_rendered_in_full(self):
        text = loop.render_state(make_route_turn())
        self.assertIn("move_blocked red-1 right -> (8, 7): contention", text)

    def test_other_events_keep_meaningful_fields(self):
        events = [{"type": "tagged", "agent_id": "blue-1", "target_id": "red-2", "position": {"x": 9, "y": 4}}]
        text = loop.render_state(make_route_turn(events=events))
        self.assertIn("tagged blue-1", text)
        self.assertIn("target_id=red-2", text)
        self.assertIn("position=(9, 4)", text)


BLOCKED_EVENT = {"type": "move_blocked", "agent_id": "red-1", "from": {"x": 7, "y": 7},
                 "direction": "right", "target": {"x": 8, "y": 7}, "reason": "contention"}


def make_deadlock_turn(me=(7, 7), enemy=(8, 6), target=(13, 7), events=None, tick=7):
    """red-1 (red) vs blue-1 (blue): red field 0..6, blue field 8..14, neutral line x=7."""
    return {
        "status": "turn", "tick": tick,
        "state": {
            "tick": tick,
            "agents": [
                {"id": "red-1", "team": "red", "position": {"x": me[0], "y": me[1]},
                 "carrying": None, "disconnected": False},
                {"id": "blue-1", "team": "blue", "position": {"x": enemy[0], "y": enemy[1]},
                 "carrying": None, "disconnected": False},
            ],
            "flags": [
                {"team": "red", "base": {"x": 1, "y": 7}, "position": {"x": 1, "y": 7}, "carried_by": None},
                {"team": "blue", "base": {"x": 13, "y": 7}, "position": {"x": target[0], "y": target[1]},
                 "carried_by": None},
            ],
            "score": {"red": 0, "blue": 0},
            "fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7},
            "self": "red-1",
            "self_position": {"x": me[0], "y": me[1]},
            "moves": {"up": "open", "down": "open", "left": "open", "right": "open"},
            "teammates": [],
        },
        "events": [BLOCKED_EVENT] if events is None else events,
    }


if __name__ == "__main__":
    unittest.main()
