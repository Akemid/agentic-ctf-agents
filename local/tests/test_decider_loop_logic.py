"""Tests for the pure logic in agent/refined/decider_loop.py (ported from the removed original script's suite).

Written with unittest-style classes so they run under both pytest and
`python -m unittest` (pytest is not installed in this project's venv).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
REFINED_DIR = AGENT_DIR / "refined"
for _path in (REFINED_DIR, AGENT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

_spec = importlib.util.spec_from_file_location("refined_decider_loop_logic", REFINED_DIR / "decider_loop.py")
loop = importlib.util.module_from_spec(_spec)
sys.modules["refined_decider_loop_logic"] = loop  # dataclasses resolves string annotations via sys.modules
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
            "agents": [
                {"id": "blue-1", "team": "blue", "position": {"x": me[0], "y": me[1]},
                 "carrying": carrying, "disconnected": False},
                {"id": "red-1", "team": "red", "position": {"x": enemy[0], "y": enemy[1]},
                 "carrying": None, "disconnected": False},
            ],
            "flags": [
                {"team": "red", "position": {"x": 1, "y": 7}, "carried_by": flag_carried_by},
                {"team": "blue", "position": {"x": 13, "y": 7}, "carried_by": None},
            ],
            "score": {"red": 0, "blue": 0},
            "self": "blue-1",
            "self_position": {"x": me[0], "y": me[1]},
            "moves": moves or {"up": "open", "down": "open", "left": "open", "right": "out_of_bounds"},
            "teammates": [],
        },
        "events": [{"type": "moved", "agent_id": "red-1", "from": {"x": 0, "y": 7}, "to": {"x": 1, "y": 7}}],
        "_is_error": False,
    }

# The static map as `join_match.map` / `get_map` return it: the turn state no longer carries it.
MAP = loop.parse_map({
    "grid": {"width": 15, "height": 15},
    "walls": [{"x": 0, "y": 2}, {"x": 1, "y": 3}],
    "fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7},
    "flag_bases": {"red": {"x": 1, "y": 7}, "blue": {"x": 13, "y": 7}},
})


def build_options(turn, plan=None):
    return loop.build_options(turn, plan, MAP)


def render_state(turn, plan=None, **kwargs):
    return loop.render_state(turn, plan, MAP, **kwargs)


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
        opts = build_options(TURN)
        self.assertNotIn("move_right", opts)
        for d in ("up", "down", "left"):
            self.assertIn(f"move_{d}", opts)

    def test_wall_move_excluded(self):
        turn = make_turn(moves={"up": "wall", "down": "open", "left": "open", "right": "open"})
        self.assertNotIn("move_up", build_options(turn))

    def test_wait_always_present(self):
        self.assertIn("wait", build_options({"state": {}}))
        self.assertIn("wait", build_options(TURN))

    def test_no_moves_when_state_missing(self):
        self.assertEqual(list(build_options({})), ["wait"])

    def test_empty_routes_falls_back_to_moves(self):
        turn = make_turn()
        turn["state"]["routes"] = []
        self.assertEqual(list(build_options(turn)), ["move_up", "move_down", "move_left", "wait"])

    def test_tag_when_adjacent_and_in_own_field(self):
        self.assertIn("tag_red-1", build_options(make_turn(me=(8, 7), enemy=(8, 6))))

    def test_no_tag_when_adjacent_but_outside_own_field(self):
        self.assertNotIn("tag_red-1", build_options(make_turn(me=(7, 7), enemy=(6, 7))))

    def test_no_tag_when_in_field_but_not_adjacent(self):
        self.assertNotIn("tag_red-1", build_options(make_turn(me=(9, 7), enemy=(9, 9))))

    def test_no_tag_when_diagonal(self):
        self.assertNotIn("tag_red-1", build_options(make_turn(me=(9, 7), enemy=(10, 8))))

    def test_no_tag_for_disconnected_enemy(self):
        turn = make_turn(me=(8, 7), enemy=(8, 6))
        turn["state"]["agents"][1]["disconnected"] = True
        self.assertNotIn("tag_red-1", build_options(turn))

    def test_no_tag_for_teammate(self):
        turn = make_turn(me=(8, 7), enemy=(8, 6))
        turn["state"]["agents"][1]["team"] = "blue"
        self.assertNotIn("tag_red-1", build_options(turn))

    def test_grab_flag_only_on_enemy_flag_cell(self):
        self.assertIn("grab_flag", build_options(make_turn(me=(1, 7), enemy=(5, 5))))
        self.assertNotIn("grab_flag", build_options(make_turn(me=(2, 7), enemy=(5, 5))))

    def test_no_grab_flag_when_flag_already_carried(self):
        turn = make_turn(me=(1, 7), enemy=(5, 5), flag_carried_by="red-1")
        self.assertNotIn("grab_flag", build_options(turn))

    def test_observation_fallback_when_state_missing(self):
        turn = {"tick": 1, "observation": {"self": "x", "moves": {"up": "open", "down": "wall",
                                                                  "left": "wall", "right": "wall"}}}
        opts = build_options(turn)
        self.assertIn("move_up", opts)
        self.assertNotIn("move_down", opts)


DETOUR = {"id": "enemy_flag_detour", "goal": "enemy_flag", "first_step": "down", "length": 8,
          "path": [{"x": 7, "y": 8}], "blocked_now": False, "blocked_reason": None,
          "risks": [], "closest_threat": {"enemy_id": "blue-1", "step": 1, "distance": 2}}
DIRECT = {"id": "enemy_flag_direct", "goal": "enemy_flag", "first_step": "right", "length": 6,
          "path": [{"x": 8, "y": 7}], "blocked_now": True, "blocked_reason": "contention",
          "risks": [{"cell": {"x": 8, "y": 7}, "step": 1, "enemy_id": "blue-1"}],
          "closest_threat": {"enemy_id": "blue-1", "step": 1, "distance": 1}}
TAG_ADJACENT = {"id": "tag:blue-2", "goal": "tag", "first_step": None, "length": 0, "path": [],
                "blocked_now": False, "blocked_reason": None, "risks": [], "closest_threat": {"enemy_id": "blue-1", "step": 1, "distance": 1}}
TAG_FAR = {"id": "tag:blue-1", "goal": "tag", "first_step": "up", "length": 3, "path": [],
           "blocked_now": False, "blocked_reason": None, "risks": [], "closest_threat": {"enemy_id": "blue-1", "step": 1, "distance": 2}}


def make_route_turn(routes=None, me=(7, 7), enemy=(8, 6), events=None, tick=7):
    """red-1 (red) vs blue-1 (blue): red field 0..6, blue field 8..14, neutral line x=7, with server routes."""
    turn = make_deadlock_turn(me=me, enemy=enemy, events=events, tick=tick)
    turn["state"]["routes"] = [DETOUR, DIRECT] if routes is None else routes
    return turn


class TestRouteOptions(unittest.TestCase):
    def test_one_option_per_route_plus_wait(self):
        opts = build_options(make_route_turn())
        self.assertEqual(list(opts), ["enemy_flag_detour", "enemy_flag_direct", "wait"])

    def test_no_per_direction_options_when_routes_exist(self):
        opts = build_options(make_route_turn())
        self.assertFalse([k for k in opts if k.startswith("move_")])

    def test_description_mentions_blocked_and_risk_facts(self):
        text = build_options(make_route_turn())["enemy_flag_direct"]
        self.assertIn("goal enemy_flag", text)
        self.assertIn("6 steps", text)
        self.assertIn("first step right", text)
        self.assertIn("BLOCKED now (contention)", text)
        self.assertIn("1 risk: can be tagged by blue-1 at step 1", text)
        self.assertIn("closest threat blue-1 at step 1, distance 1", text)

    def test_unblocked_riskless_description(self):
        text = build_options(make_route_turn())["enemy_flag_detour"]
        self.assertIn("8 steps", text)
        self.assertNotIn("BLOCKED", text)
        self.assertNotIn("risk", text)

    def test_no_recommendation_wording(self):
        blob = " ".join(build_options(make_route_turn()).values()) + render_state(make_route_turn())
        for word in ("recommend", "suggest"):
            self.assertNotIn(word, blob.lower())

    def test_adjacent_tag_route_description(self):
        text = build_options(make_route_turn(routes=[TAG_ADJACENT]))["tag:blue-2"]
        self.assertIn("goal tag blue-2", text)
        self.assertIn("adjacent now: tag immediately", text)

    def test_grab_flag_still_offered_on_the_flag_cell(self):
        turn = make_route_turn(me=(13, 7))
        self.assertIn("grab_flag", build_options(turn))
        self.assertNotIn("grab_flag", build_options(make_route_turn()))

    def test_malformed_routes_are_ignored(self):
        turn = make_route_turn(routes=[{"nope": 1}, "x", DETOUR])
        self.assertEqual(list(build_options(turn)), ["enemy_flag_detour", "wait"])


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

    def _arrived(self, route_id, goal):
        # Real tick-16 shape: standing on the target, the server sends length 0 and first_step null.
        return {"id": route_id, "goal": goal, "first_step": None, "length": 0, "path": [],
                "blocked_now": False, "blocked_reason": None, "risks": [], "closest_threat": {"enemy_id": "blue-1", "step": 0, "distance": 12}}

    def test_arrived_enemy_flag_route_maps_to_grab_flag(self):
        turn = make_route_turn(routes=[self._arrived("enemy_flag_direct", "enemy_flag")], me=(13, 7))
        tool, args = loop.label_to_call("enemy_flag_direct", 16, turn)
        self.assertEqual(tool, "grab_flag")
        self.assertEqual(args["tick"], 16)
        self.assertIn("enemy_flag_direct", args["rationale"])

    def test_arrived_own_base_and_defend_routes_map_to_wait(self):
        for route_id, goal in (("own_base_direct", "own_base"), ("defend_flag", "defend_flag")):
            turn = make_route_turn(routes=[self._arrived(route_id, goal)])
            tool, args = loop.label_to_call(route_id, 4, turn)
            self.assertEqual((tool, args["tick"]), ("wait", 4))

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
        text = render_state(TURN)
        self.assertIn("tick: 1", text)
        self.assertIn("blue-1", text)
        self.assertIn("(14, 7)", text)
        self.assertIn("red-1", text)
        self.assertIn("right: out_of_bounds", text)  # moves line kept when routes are empty
        self.assertNotIn("suggested direction", text)
        self.assertNotIn("target:", text)

    def test_has_no_walls(self):
        text = render_state(TURN)
        self.assertNotIn("walls", text)
        self.assertNotIn('"x"', text)
        self.assertLess(len(text), 1200)

    def test_carrying(self):
        text = render_state(make_turn(me=(9, 7), carrying="red", flag_carried_by="blue-1"))
        self.assertIn("carrying the flag: yes", text)

    def test_field_membership(self):
        self.assertIn("in your own field", render_state(make_turn(me=(9, 7))))
        self.assertIn("in the enemy field", render_state(make_turn(me=(3, 7))))
        self.assertIn("on the neutral line", render_state(make_turn(me=(7, 7))))

    def test_routes_block_lists_route_facts(self):
        text = render_state(make_route_turn())
        self.assertIn("routes:", text)
        self.assertIn("enemy_flag_detour: goal enemy_flag, 8 steps", text)
        self.assertIn("enemy_flag_direct: goal enemy_flag, 6 steps, first step right, BLOCKED now (contention)", text)
        self.assertIn("can be tagged by blue-1 at step 1", text)

    def test_moves_line_only_when_routes_empty(self):
        self.assertNotIn("moves:", render_state(make_route_turn()))
        self.assertIn("moves:", render_state(make_route_turn(routes=[])))
        self.assertNotIn("routes:", render_state(make_route_turn(routes=[])))

    def test_no_client_side_risk_lines(self):
        self.assertNotIn("risk: move", render_state(make_route_turn(routes=[])))

    def test_blocked_event_rendered_in_full(self):
        text = render_state(make_route_turn())
        self.assertIn("move_blocked red-1 right -> (8, 7): contention", text)

    def test_other_events_keep_meaningful_fields(self):
        events = [{"type": "tagged", "agent_id": "blue-1", "target_id": "red-2", "position": {"x": 9, "y": 4}}]
        text = render_state(make_route_turn(events=events))
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
                {"team": "red", "position": {"x": 1, "y": 7}, "carried_by": None},
                {"team": "blue", "position": {"x": target[0], "y": target[1]},
                 "carried_by": None},
            ],
            "score": {"red": 0, "blue": 0},
            "self": "red-1",
            "self_position": {"x": me[0], "y": me[1]},
            "moves": {"up": "open", "down": "open", "left": "open", "right": "open"},
            "teammates": [],
        },
        "events": [BLOCKED_EVENT] if events is None else events,
    }


# ---------------------------------------------------------------------------
# Plan memory (informational only: the model still decides every tick)
# ---------------------------------------------------------------------------


def tool_use_message(name, tool_input):
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": "u", "name": name, "input": tool_input}}]}


def cells(*xy):
    return [{"x": x, "y": y} for x, y in xy]


def path_route(route_id, goal, path, first_step="right"):
    return {"id": route_id, "goal": goal, "first_step": first_step, "length": len(path), "path": cells(*path),
            "blocked_now": False, "blocked_reason": None, "risks": [], "closest_threat": {"enemy_id": "blue-1", "step": 1, "distance": 5}}


# Real regression: plan chosen at (2,7) heading right along y=7 (8 cells, current cell excluded).
PREV_PATH = tuple((x, 7) for x in range(3, 11))


def reg_plan(**overrides):
    fields = dict(route_id="enemy_flag_direct", goal="enemy_flag", since_tick=5, last_action="move right",
                  path=PREV_PATH, position=(2, 7))
    fields.update(overrides)
    return loop.Plan(**fields)


def reg_turn(me=(3, 7), tick=6, routes=None):
    """Next tick: I am at (3,7). The direct route keeps going right; the detour is recomputed backwards."""
    direct = path_route("enemy_flag_direct", "enemy_flag", [(x, 7) for x in range(4, 12)])
    detour = path_route("enemy_flag_detour", "enemy_flag",
                        [(2, 7), (2, 6), (2, 5), (2, 4), (2, 3), (3, 3), (4, 3), (5, 3)], first_step="left")
    return make_route_turn(routes=[direct, detour] if routes is None else routes, me=me, tick=tick)


class TestContinuesPath(unittest.TestCase):
    def test_moved_one_step_along_path_and_route_continues(self):
        route = [(x, 7) for x in range(4, 12)]  # 8 cells; expected remainder has 7 -> compare 7
        self.assertTrue(loop.continues_path(PREV_PATH, (2, 7), (3, 7), route))

    def test_moved_along_path_but_route_goes_backwards(self):
        route = [(2, 7), (2, 6), (2, 5)]
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), (3, 7), route))

    def test_shorter_route_compares_overlapping_prefix(self):
        self.assertTrue(loop.continues_path(PREV_PATH, (2, 7), (3, 7), [(4, 7), (5, 7)]))
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), (3, 7), [(4, 7), (4, 8)]))

    def test_previous_path_of_length_one_and_now_on_its_end(self):
        self.assertFalse(loop.continues_path(((3, 7),), (2, 7), (3, 7), [(4, 7), (5, 7)]))

    def test_not_moved_route_starts_with_previous_path(self):
        self.assertTrue(loop.continues_path(PREV_PATH, (2, 7), (2, 7), list(PREV_PATH)))
        self.assertTrue(loop.continues_path(PREV_PATH, (2, 7), (2, 7), [(3, 7), (4, 7)]))

    def test_not_moved_route_differs(self):
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), (2, 7), [(1, 7), (0, 7)]))

    def test_respawned_or_moved_elsewhere(self):
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), (14, 7), list(PREV_PATH)))
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), (4, 7), [(5, 7), (6, 7)]))  # two steps

    def test_malformed_or_empty_inputs(self):
        self.assertFalse(loop.continues_path(None, (2, 7), (3, 7), [(4, 7)]))
        self.assertFalse(loop.continues_path((), (2, 7), (2, 7), [(3, 7)]))
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), None, [(4, 7)]))
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), (3, 7), None))
        self.assertFalse(loop.continues_path(PREV_PATH, (2, 7), (3, 7), []))
        self.assertFalse(loop.continues_path(PREV_PATH, None, None, [(4, 7)]))


class TestUpdatePlan(unittest.TestCase):
    def test_empty_plan_starts_fresh(self):
        plan = loop.update_plan(loop.Plan(), "enemy_flag_direct", "enemy_flag", "move down", 5)
        self.assertEqual(plan, loop.Plan("enemy_flag_direct", "enemy_flag", 5, "move down"))

    def test_stores_path_and_position(self):
        plan = loop.update_plan(loop.Plan(), "enemy_flag_direct", "enemy_flag", "move right", 5,
                                [(3, 7), (4, 7)], (2, 7))
        self.assertEqual(plan, loop.Plan("enemy_flag_direct", "enemy_flag", 5, "move right",
                                         ((3, 7), (4, 7)), (2, 7)))

    def test_same_goal_without_continuing_path_resets_since_tick(self):
        plan = reg_plan()
        plan = loop.update_plan(plan, "enemy_flag_detour", "enemy_flag", "move left", 6,
                                [(2, 7), (2, 6)], (3, 7))
        self.assertEqual(plan.since_tick, 6)
        self.assertEqual((plan.route_id, plan.path, plan.position), ("enemy_flag_detour", ((2, 7), (2, 6)), (3, 7)))

    def test_continuing_path_keeps_since_tick_even_if_route_id_changes(self):
        plan = loop.update_plan(reg_plan(), "enemy_flag_detour", "enemy_flag", "move right", 6,
                                [(x, 7) for x in range(4, 12)], (3, 7))
        self.assertEqual(plan.since_tick, 5)
        self.assertEqual(plan.route_id, "enemy_flag_detour")
        self.assertEqual(plan.position, (3, 7))
        self.assertEqual(plan.path[0], (4, 7))

    def test_blocked_move_keeps_since_tick_when_path_still_continues(self):
        plan = loop.update_plan(reg_plan(), "enemy_flag_direct", "enemy_flag", "move right", 6,
                                list(PREV_PATH), (2, 7))
        self.assertEqual(plan.since_tick, 5)

    def test_goal_change_resets_since_tick(self):
        plan = loop.update_plan(reg_plan(), "tag:blue-1", "tag", "move right", 6,
                                [(x, 7) for x in range(4, 12)], (3, 7))
        self.assertEqual(plan.since_tick, 6)
        self.assertEqual(plan.goal, "tag")

    def test_respawn_resets_since_tick(self):
        plan = loop.update_plan(reg_plan(), "enemy_flag_direct", "enemy_flag", "move left", 9,
                                [(13, 7), (12, 7)], (14, 7))
        self.assertEqual(plan.since_tick, 9)

    def test_wait_records_last_action_and_keeps_plan(self):
        plan = loop.update_plan(reg_plan(), None, None, "wait", 6)
        self.assertEqual(plan, reg_plan(last_action="wait"))

    def test_grab_flag_clears_plan(self):
        plan = loop.update_plan(reg_plan(), None, None, "grab_flag", 6)
        self.assertEqual(plan, loop.Plan(None, None, None, "grab_flag"))
        self.assertIsNone(plan.path)
        self.assertIsNone(plan.position)

    def test_does_not_mutate_input(self):
        plan = loop.Plan("a", "g", 1, "move up")
        loop.update_plan(plan, "b", "h", "move down", 2, [(1, 1)], (0, 1))
        self.assertEqual(plan, loop.Plan("a", "g", 1, "move up"))

    def test_route_path_helper_reads_offered_route(self):
        turn = reg_turn()
        self.assertEqual(loop.route_path(turn, "enemy_flag_detour")[:2], ((2, 7), (2, 6)))
        self.assertIsNone(loop.route_path(turn, "nope"))
        self.assertIsNone(loop.route_path(turn, None))


PLAN = loop.Plan("enemy_flag_direct", "enemy_flag", 5, "move down")


class TestRenderStatePlan(unittest.TestCase):
    def test_no_plan_output_unchanged(self):
        turn = make_route_turn()
        self.assertEqual(render_state(turn, None), render_state(turn))
        self.assertNotIn("current plan", render_state(turn))
        self.assertNotIn("current plan", render_state(turn, loop.Plan()))

    def test_plan_line_when_a_route_continues_the_path(self):
        text = render_state(reg_turn(), reg_plan())
        self.assertIn(
            "current plan: goal enemy_flag, heading to (10, 7) since tick 5, 1 tick; last action: move right", text)
        self.assertNotIn("no offered route", text)

    def test_plan_line_when_no_route_continues_the_path(self):
        detour_only = [reg_turn()["state"]["routes"][1]]
        text = render_state(reg_turn(routes=detour_only), reg_plan())
        self.assertIn(
            "current plan: goal enemy_flag, heading to (10, 7) since tick 5 \u2014 no offered route continues that path",
            text)
        self.assertNotIn("no longer offered", text)

    def test_plan_line_without_known_path(self):
        text = render_state(make_route_turn(tick=8), PLAN)
        self.assertIn("current plan: goal enemy_flag since tick 5", text)
        self.assertNotIn("heading to", text)

    def test_plan_wording_is_neutral(self):
        text = render_state(reg_turn(), reg_plan()).lower()
        for word in ("recommend", "should"):
            self.assertNotIn(word, text)


class TestBuildOptionsPlan(unittest.TestCase):
    def test_no_plan_unchanged(self):
        turn = make_route_turn()
        self.assertEqual(build_options(turn, None), build_options(turn))
        self.assertEqual(build_options(turn, loop.Plan()), build_options(turn))

    def test_regression_backwards_detour_is_not_a_continuation(self):
        opts = build_options(reg_turn(), reg_plan())
        self.assertTrue(opts["enemy_flag_direct"].endswith(" | continues your current path (1 tick)"))
        self.assertTrue(opts["enemy_flag_detour"].endswith(" | same goal as your current plan, different path"))
        self.assertNotIn("continues", opts["enemy_flag_detour"])

    def test_same_id_does_not_imply_continuation(self):
        # The direct route now goes backwards; only the differently named route continues the path.
        direct = path_route("enemy_flag_direct", "enemy_flag", [(2, 7), (2, 6)], first_step="left")
        other = path_route("enemy_flag_alt", "enemy_flag", [(x, 7) for x in range(4, 12)])
        opts = build_options(reg_turn(routes=[direct, other]), reg_plan())
        self.assertIn("different path", opts["enemy_flag_direct"])
        self.assertIn("continues your current path", opts["enemy_flag_alt"])

    def test_blocked_move_case(self):
        # I did not move: still at (2,7); the route that starts with the previous path continues.
        direct = path_route("enemy_flag_direct", "enemy_flag", list(PREV_PATH))
        detour = path_route("enemy_flag_detour", "enemy_flag", [(1, 7), (0, 7)], first_step="left")
        opts = build_options(reg_turn(me=(2, 7), tick=6, routes=[direct, detour]), reg_plan())
        self.assertIn("continues your current path", opts["enemy_flag_direct"])
        self.assertIn("different path", opts["enemy_flag_detour"])

    def test_respawn_case_nothing_continues(self):
        direct = path_route("enemy_flag_direct", "enemy_flag", [(13, 7), (12, 7)], first_step="left")
        opts = build_options(reg_turn(me=(14, 7), tick=9, routes=[direct]), reg_plan())
        self.assertNotIn("continues", opts["enemy_flag_direct"])
        self.assertIn("different path", opts["enemy_flag_direct"])

    def test_multiple_routes_can_continue(self):
        a = path_route("a_route", "enemy_flag", [(x, 7) for x in range(4, 12)])
        b = path_route("b_route", "enemy_flag", [(4, 7), (5, 7), (5, 8)])  # diverges after 2 cells -> no
        c = path_route("c_route", "enemy_flag", [(x, 7) for x in range(4, 8)])
        opts = build_options(reg_turn(routes=[a, b, c]), reg_plan())
        self.assertIn("continues your current path", opts["a_route"])
        self.assertIn("different path", opts["b_route"])
        self.assertIn("continues your current path", opts["c_route"])

    def test_zero_routes_continue(self):
        opts = build_options(reg_turn(routes=[reg_turn()["state"]["routes"][1]]), reg_plan())
        self.assertNotIn("continues", " ".join(opts.values()))

    def test_other_goals_and_wait_unchanged(self):
        turn = reg_turn(routes=reg_turn()["state"]["routes"] + [TAG_FAR])
        base, opts = build_options(turn), build_options(turn, reg_plan())
        self.assertEqual(opts["tag:blue-1"], base["tag:blue-1"])
        self.assertEqual(opts["wait"], base["wait"])

    def test_labels_and_order_unchanged(self):
        turn = reg_turn()
        self.assertEqual(list(build_options(turn, reg_plan())), list(build_options(turn)))


class TestExecutedRouteInference(unittest.TestCase):
    def setUp(self):
        self.turn = make_route_turn(routes=[DETOUR, DIRECT, TAG_FAR], tick=8)

    def test_extract_action_call(self):
        msgs = [{"role": "user", "content": [{"text": "hi"}]},
                tool_use_message("move", {"direction": "up", "tick": 8})]
        self.assertEqual(loop.extract_action_call(msgs), ("move", {"direction": "up", "tick": 8}))

    def test_extract_action_call_none_when_no_tool_use(self):
        self.assertIsNone(loop.extract_action_call([{"role": "assistant", "content": [{"text": "ok"}]}]))
        self.assertIsNone(loop.extract_action_call([]))
        self.assertIsNone(loop.extract_action_call(None))

    def test_rationale_route_is_parsed(self):
        call = ("move", {"direction": "down", "rationale": "route enemy_flag_direct"})
        self.assertEqual(loop.infer_executed_route(call, self.turn, loop.Plan()), "enemy_flag_direct")

    def test_rationale_with_tag_route_id(self):
        call = ("move", {"direction": "up", "rationale": "route tag:blue-1."})
        self.assertEqual(loop.infer_executed_route(call, self.turn, loop.Plan()), "tag:blue-1")

    def test_rationale_with_unoffered_route_falls_back_to_direction(self):
        call = ("move", {"direction": "right", "rationale": "route nonexistent"})
        self.assertEqual(loop.infer_executed_route(call, self.turn, loop.Plan()), "enemy_flag_direct")

    def test_direction_match_when_no_rationale(self):
        call = ("move", {"direction": "down"})
        self.assertEqual(loop.infer_executed_route(call, self.turn, loop.Plan()), "enemy_flag_detour")

    def test_ambiguous_direction_prefers_plan_goal(self):
        other = dict(TAG_FAR, id="tag:blue-9", first_step="down")  # same first step as DETOUR, other goal
        turn = make_route_turn(routes=[other, DETOUR], tick=8)
        plan = loop.Plan("enemy_flag_direct", "enemy_flag", 5, "move down")
        self.assertEqual(loop.infer_executed_route(("move", {"direction": "down"}, ), turn, plan), "enemy_flag_detour")
        tag_plan = loop.Plan("tag:blue-1", "tag", 5, "move up")
        self.assertEqual(loop.infer_executed_route(("move", {"direction": "down"}), turn, tag_plan), "tag:blue-9")

    def test_ambiguous_direction_without_plan_takes_first_by_id(self):
        a = dict(DETOUR, id="b_route", first_step="left")
        b = dict(DETOUR, id="a_route", first_step="left")
        turn = make_route_turn(routes=[a, b], tick=8)
        self.assertEqual(loop.infer_executed_route(("move", {"direction": "left"}), turn, loop.Plan()), "a_route")

    def test_tag_maps_to_tag_route(self):
        turn = make_route_turn(routes=[TAG_ADJACENT], tick=8)
        self.assertEqual(loop.infer_executed_route(("tag", {"enemy_id": "blue-2"}), turn, loop.Plan()), "tag:blue-2")

    def test_tag_without_offered_route_is_none(self):
        self.assertIsNone(loop.infer_executed_route(("tag", {"enemy_id": "zzz"}), self.turn, loop.Plan()))

    def test_non_route_tools_and_missing_call_are_none(self):
        for call in (("wait", {"tick": 8}), ("grab_flag", {"tick": 8}), None):
            self.assertIsNone(loop.infer_executed_route(call, self.turn, loop.Plan()))

    def test_unmatched_direction_is_none(self):
        self.assertIsNone(loop.infer_executed_route(("move", {"direction": "left"}), self.turn, loop.Plan()))


class TestActionText(unittest.TestCase):
    def test_describe_action(self):
        self.assertEqual(loop.describe_action("move", {"direction": "down", "tick": 1}), "move down")
        self.assertEqual(loop.describe_action("tag", {"enemy_id": "blue-2"}), "tag blue-2")
        self.assertEqual(loop.describe_action("wait", {"tick": 1}), "wait")
        self.assertEqual(loop.describe_action("grab_flag", {"tick": 1}), "grab_flag")

    def test_plan_suffix(self):
        self.assertEqual(loop.plan_suffix(PLAN), " [plan enemy_flag_direct since 5]")
        self.assertEqual(loop.plan_suffix(loop.Plan()), "")

    def test_instructions_mention_plan(self):
        self.assertIn("current plan", loop.DECIDER_INSTRUCTIONS)
        self.assertIn("current plan", loop.LLM_PLAN_NOTE)
        self.assertNotIn("should", loop.DECIDER_INSTRUCTIONS.split("Your current plan")[1])

    def test_instructions_explain_route_ids_are_recomputed_neutrally(self):
        for text in (loop.DECIDER_INSTRUCTIONS, loop.LLM_PLAN_NOTE):
            self.assertIn("continue your current path", text)
            self.assertIn("recomputed every tick", text)
            self.assertNotIn("switching routes every tick", text)
            self.assertNotIn("recommend", text.lower())


class TestRemainingMs(unittest.TestCase):
    def test_subtracts_elapsed(self):
        self.assertEqual(loop.remaining_ms(5000, 1200), 3800)

    def test_can_go_negative(self):
        self.assertEqual(loop.remaining_ms(1000, 1500), -500)

    def test_none_falls_back_to_default(self):
        self.assertEqual(loop.remaining_ms(None, 1000), loop.DEFAULT_DEADLINE_MS - 1000)

    def test_turn_uses_remaining_then_deadline_then_default(self):
        self.assertEqual(loop.turn_remaining_ms({"deadline_remaining_ms": 2646, "deadline_ms": 5000}, 600), 2046)
        self.assertEqual(loop.turn_remaining_ms({"deadline_ms": 4000}, 500), 3500)
        self.assertEqual(loop.turn_remaining_ms({}, 500), 4500)

    def test_turn_ignores_non_numeric(self):
        self.assertEqual(loop.turn_remaining_ms({"deadline_remaining_ms": "x", "deadline_ms": True}, 0), 5000)


class TestChoosePath(unittest.TestCase):
    OPTIONS = {"enemy_flag_direct": "d", "wait": "w"}

    def path(self, chosen="enemy_flag_direct", confidence=0.9, remaining=3000.0, down=False, options=None):
        return loop.choose_path(chosen, confidence, 0.85, self.OPTIONS if options is None else options,
                                remaining, 4500, 300, down)

    def test_above_threshold_is_decider_even_without_time(self):
        self.assertEqual(self.path(remaining=10), "decider")

    def test_low_confidence_enough_time_is_llm(self):
        self.assertEqual(self.path(confidence=0.31, remaining=4801), "llm")

    def test_low_confidence_no_time_valid_option_is_budget(self):
        self.assertEqual(self.path(confidence=0.31, remaining=1234), "budget")

    def test_exactly_needed_time_is_llm(self):
        self.assertEqual(self.path(confidence=0.31, remaining=4800), "llm")

    def test_invalid_choice_no_time_is_wait(self):
        self.assertEqual(self.path(chosen="nope", confidence=0.31, remaining=1000), "wait")

    def test_none_choice_no_time_is_wait(self):
        self.assertEqual(self.path(chosen=None, confidence=0.0, remaining=1000), "wait")

    def test_decider_down_no_time_is_wait(self):
        self.assertEqual(self.path(chosen=None, confidence=0.0, remaining=1000, down=True), "wait")

    def test_decider_down_with_time_is_llm(self):
        self.assertEqual(self.path(chosen=None, confidence=0.0, remaining=9000, down=True), "llm")

    def test_invalid_choice_with_time_is_llm(self):
        self.assertEqual(self.path(chosen="nope", confidence=0.5, remaining=9000), "llm")


class TestWarmup(unittest.TestCase):
    def test_shape(self):
        state, questions = loop.warmup_question()
        self.assertIsInstance(state, str)
        action = questions["action"]
        self.assertEqual(action["type"], "choice")
        self.assertGreaterEqual(len(action["criteria"]), 4)
        self.assertIn("wait", action["criteria"])

    def test_state_length_is_representative(self):
        state, _ = loop.warmup_question()
        self.assertGreaterEqual(len(state.splitlines()), 12)
        self.assertGreater(len(state), 500)
        self.assertLess(len(state), 1500)

    def test_state_lists_three_or_four_routes(self):
        state, questions = loop.warmup_question()
        routes = [k for k in questions["action"]["criteria"] if k != "wait"]
        self.assertTrue(all(f"  {r}:" in state for r in routes if r.startswith(("enemy", "tag:", "home"))))


class TestStatsBudget(unittest.TestCase):
    def test_summary_includes_budget_ticks(self):
        stats = loop.Stats()
        stats.budget_ticks = 3
        self.assertIn("by budget: 3", stats.summary())


class TestStatsTimeouts(unittest.TestCase):
    def test_summary_includes_llm_timeouts(self):
        stats = loop.Stats()
        stats.llm_timeouts = 2
        self.assertIn("by budget: 0, LLM timeouts: 2,", stats.summary())


class TestLlmTimeoutS(unittest.TestCase):
    def test_remaining_minus_margin_in_seconds(self):
        self.assertAlmostEqual(loop.llm_timeout_s(5000, 300), 4.7)

    def test_clamped_to_minimum(self):
        self.assertEqual(loop.llm_timeout_s(100, 300), 0.05)
        self.assertEqual(loop.llm_timeout_s(-50, 300), 0.05)


class TestTimeoutAction(unittest.TestCase):
    OPTIONS = {"enemy_flag_direct": "d", "wait": "w"}

    def act(self, chosen="enemy_flag_direct", down=False, acted=False, options=None):
        return loop.timeout_action(chosen, self.OPTIONS if options is None else options, down, acted)

    def test_valid_choice_runs_decider(self):
        self.assertEqual(self.act(), "decider")

    def test_invalid_or_missing_choice_waits(self):
        self.assertEqual(self.act(chosen="nope"), "wait")
        self.assertEqual(self.act(chosen=None), "wait")

    def test_decider_down_waits(self):
        self.assertEqual(self.act(down=True), "wait")

    def test_already_acted_does_nothing(self):
        self.assertEqual(self.act(acted=True), "none")
        self.assertEqual(self.act(acted=True, down=True, chosen=None), "none")


class FakeToolEvent:
    def __init__(self, name="move", tool_input=None):
        self.tool_use = {"name": name, "input": tool_input or {"direction": "up", "tick": 3}}


class TestTickCancelHandler(unittest.TestCase):
    def setUp(self):
        from strands.interventions import Deny, Proceed
        self.Deny, self.Proceed = Deny, Proceed
        self.cancel = threading.Event()
        self.handler = loop.TickCancelHandler(self.cancel)

    def test_proceeds_before_cancel_and_records_action(self):
        self.assertIsInstance(self.handler.before_tool_call(FakeToolEvent("move")), self.Proceed)
        self.assertTrue(self.handler.executed_action)

    def test_non_action_tool_does_not_record(self):
        self.assertIsInstance(self.handler.before_tool_call(FakeToolEvent("get_rules")), self.Proceed)
        self.assertFalse(self.handler.executed_action)

    def test_denies_after_cancel_without_recording(self):
        self.cancel.set()
        decision = self.handler.before_tool_call(FakeToolEvent("move"))
        self.assertIsInstance(decision, self.Deny)
        self.assertEqual(decision.reason, "tick timed out")
        self.assertFalse(self.handler.executed_action)

    def test_is_an_intervention_handler_with_a_name(self):
        from strands.interventions import InterventionHandler
        self.assertIsInstance(self.handler, InterventionHandler)
        self.assertTrue(self.handler.name)


class TestRunWithTimeout(unittest.TestCase):
    def test_slow_callable_times_out_quickly(self):
        started = time.monotonic()
        timed_out, value = loop.run_with_timeout(lambda: time.sleep(0.3) or "late", 0.05)
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertTrue(timed_out)
        self.assertIsNone(value)

    def test_fast_callable_returns_value(self):
        self.assertEqual(loop.run_with_timeout(lambda: 42, 1.0), (False, 42))

    def test_exceptions_propagate(self):
        def boom():
            raise RuntimeError("x")
        with self.assertRaises(RuntimeError):
            loop.run_with_timeout(boom, 1.0)


class FakeHookRegistry:
    def __init__(self):
        self.callbacks = []

    def add_callback(self, event_type, callback, **kwargs):
        self.callbacks.append((event_type, callback))


class FakeAfterToolsEvent:
    def __init__(self):
        self.end_turn = False


class FakeAfterToolCallEvent:
    def __init__(self, name="move", cancel_message=None):
        self.tool_use = {"name": name, "input": {}}
        self.cancel_message = cancel_message


class TestStopAfterActionHook(unittest.TestCase):
    """After one action tool completes, the event loop must end the turn (no second model call)."""

    def setUp(self):
        self.hook = loop.StopAfterActionHook()

    def run_batch(self, *calls):
        for call in calls:
            self.hook.after_tool_call(call)
        done = FakeAfterToolsEvent()
        self.hook.after_tools(done)
        return done

    def test_ends_turn_after_an_action_tool(self):
        self.assertTrue(self.run_batch(FakeAfterToolCallEvent("move")).end_turn)

    def test_every_action_tool_ends_the_turn(self):
        for name in loop.ACTION_TOOLS:
            self.setUp()
            self.assertTrue(self.run_batch(FakeAfterToolCallEvent(name)).end_turn, name)

    def test_does_not_end_turn_after_non_action_tool(self):
        self.assertFalse(self.run_batch(FakeAfterToolCallEvent("get_rules")).end_turn)

    def test_does_not_end_turn_without_any_tool_call(self):
        self.assertFalse(self.run_batch().end_turn)

    def test_cancelled_action_does_not_end_turn(self):
        self.assertFalse(self.run_batch(FakeAfterToolCallEvent("move", cancel_message="tick timed out")).end_turn)

    def test_registers_both_callbacks_on_the_real_event_types(self):
        from strands.hooks import AfterToolCallEvent, AfterToolsEvent
        registry = FakeHookRegistry()
        self.hook.register_hooks(registry)
        self.assertEqual({t for t, _ in registry.callbacks}, {AfterToolCallEvent, AfterToolsEvent})

    def test_real_event_allows_writing_end_turn(self):
        from strands.hooks import AfterToolsEvent
        event = AfterToolsEvent(agent=None, message={"role": "user", "content": []}, invocation_state={})
        self.hook.after_tool_call(FakeAfterToolCallEvent("move"))
        self.hook.after_tools(event)
        self.assertTrue(event.end_turn)


class FakeThread:
    def __init__(self, name, daemon, alive=True):
        self.name, self.daemon, self._alive = name, daemon, alive

    def is_alive(self):
        return self._alive


class TestLingeringThreads(unittest.TestCase):
    def test_only_alive_non_daemon_non_main_threads(self):
        main = threading.main_thread()
        threads = [main, FakeThread("a", daemon=True), FakeThread("b", daemon=False),
                   FakeThread("c", daemon=False, alive=False)]
        self.assertEqual([t.name for t in loop.lingering_threads(threads)], ["b"])

    def test_describe_lists_names_and_daemon_flags(self):
        text = loop.describe_threads([FakeThread("worker", False), FakeThread("llm-tick", True)])
        self.assertIn("worker (daemon=False)", text)
        self.assertIn("llm-tick (daemon=True)", text)


class TestExitIfThreadsLinger(unittest.TestCase):
    def call(self, threads, code=0, grace_s=2.0):
        self.exits, self.log = [], []
        clock = {"t": 0.0}

        def sleep(seconds):
            clock["t"] += seconds

        return loop.exit_if_threads_linger(
            code, grace_s, exit_fn=self.exits.append, enumerate_fn=lambda: threads,
            sleep=sleep, clock=lambda: clock["t"], log=self.log.append)

    def test_no_exit_when_only_daemon_threads(self):
        self.assertFalse(self.call([threading.main_thread(), FakeThread("d", True)]))
        self.assertEqual(self.exits, [])

    def test_force_exits_with_code_when_non_daemon_thread_survives_grace(self):
        self.assertTrue(self.call([FakeThread("stuck", False)], code=3))
        self.assertEqual(self.exits, [3])
        self.assertIn("stuck (daemon=False)", "\n".join(self.log))

    def test_no_exit_when_thread_finishes_within_grace(self):
        thread = FakeThread("slow", False)
        calls = {"n": 0}

        def enumerate_fn():
            calls["n"] += 1
            thread._alive = calls["n"] < 3
            return [thread]

        exits, clock = [], {"t": 0.0}

        def sleep(seconds):
            clock["t"] += seconds

        result = loop.exit_if_threads_linger(0, 2.0, exit_fn=exits.append, enumerate_fn=enumerate_fn,
                                             sleep=sleep, clock=lambda: clock["t"], log=lambda m: None)
        self.assertFalse(result)
        self.assertEqual(exits, [])


class TestRunGuarded(unittest.TestCase):
    def run_it(self, fn):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return loop.run_guarded(fn), err.getvalue()

    def test_returns_function_code(self):
        self.assertEqual(self.run_it(lambda: 0)[0], 0)

    def test_exception_prints_traceback_and_returns_nonzero(self):
        def boom():
            raise RuntimeError("kaput")
        code, err = self.run_it(boom)
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: kaput", err)
        self.assertIn("Traceback", err)

    def test_keyboard_interrupt_returns_130(self):
        def interrupted():
            raise KeyboardInterrupt
        self.assertEqual(self.run_it(interrupted)[0], 130)

    def test_system_exit_code_is_preserved(self):
        def exits():
            raise SystemExit(2)
        self.assertEqual(self.run_it(exits)[0], 2)

    def test_system_exit_none_is_zero(self):
        def exits():
            raise SystemExit()
        self.assertEqual(self.run_it(exits)[0], 0)


class FakeLifecycleClient:
    def __init__(self, start_error=None):
        self.events, self.start_error = [], start_error

    def start(self):
        self.events.append("start")
        if self.start_error:
            raise self.start_error
        return self

    def stop(self, *exc):
        self.events.append("stop")


class TestWithClient(unittest.TestCase):
    def test_stops_on_normal_exit(self):
        client = FakeLifecycleClient()
        self.assertEqual(loop.with_client(client, lambda: "ok"), "ok")
        self.assertEqual(client.events, ["start", "stop"])

    def test_stops_when_body_raises(self):
        client = FakeLifecycleClient()
        with self.assertRaises(RuntimeError):
            loop.with_client(client, lambda: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertEqual(client.events, ["start", "stop"])

    def test_stops_on_keyboard_interrupt(self):
        client = FakeLifecycleClient()

        def interrupted():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            loop.with_client(client, interrupted)
        self.assertEqual(client.events, ["start", "stop"])

    def test_stops_even_when_start_fails(self):
        client = FakeLifecycleClient(start_error=OSError("no server"))
        with self.assertRaises(OSError):
            loop.with_client(client, lambda: None)
        self.assertEqual(client.events, ["start", "stop"])

    def test_stop_failure_does_not_mask_body_error(self):
        client = FakeLifecycleClient()
        client.stop = lambda *a: (_ for _ in ()).throw(RuntimeError("stop failed"))
        with self.assertRaises(ValueError):
            loop.with_client(client, lambda: (_ for _ in ()).throw(ValueError("body")))


class TestArgs(unittest.TestCase):
    def test_new_flags_defaults(self):
        old = sys.argv
        sys.argv = ["x"]
        try:
            args = loop.parse_args()
        finally:
            sys.argv = old
        self.assertEqual((args.llm_budget_ms, args.safety_margin_ms, args.no_warmup), (4500, 300, False))


class FakeClient:
    def __init__(self, result: dict) -> None:
        self._result = result

    def call_tool_sync(self, tool_use_id, name, arguments):
        return self._result


class CallToolErrorTests(unittest.TestCase):
    def test_tool_error_flagged_by_is_error(self):
        parsed = loop.call_tool(FakeClient(tool_result({"reason": "match_full"}, is_error=True)), "join_match", {})
        self.assertTrue(parsed["_is_error"])

    def test_transport_error_flagged_by_status_only(self):
        # strands' _handle_tool_execution_error (e.g. "session not found") sets status="error"
        # and no isError key.
        result = {"status": "error", "toolUseId": "t",
                  "content": [{"text": "Tool execution failed: session not found"}]}
        parsed = loop.call_tool(FakeClient(result), "wait_for_turn", {})
        self.assertTrue(parsed["_is_error"])

    def test_success_not_flagged(self):
        parsed = loop.call_tool(FakeClient(tool_result({"status": "turn", "tick": 1})), "wait_for_turn", {})
        self.assertFalse(parsed["_is_error"])


class TestStaticMapV2(unittest.TestCase):
    """MCP turn contract v2: the static board is captured once, not read from the turn state."""

    RAW = {"fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7},
           "walls": [], "flag_bases": {"red": {"x": 1, "y": 7}, "blue": {"x": 13, "y": 7}}}

    def test_parse_map(self):
        info = loop.parse_map(self.RAW)
        self.assertEqual(info.fields, {"red": (0, 6), "blue": (8, 14)})
        self.assertEqual(info.neutral_x, 7)
        self.assertEqual(info.flag_bases, {"red": (1, 7), "blue": (13, 7)})
        for raw in (None, {}, {"fields": {}}, "map"):
            self.assertIsNone(loop.parse_map(raw))

    def test_without_the_map_the_position_is_not_guessed_and_no_tag_is_invented(self):
        turn = make_turn(me=(8, 7), enemy=(8, 6))
        text = loop.render_state(turn, None, None)
        self.assertIn("at (8, 7)", text)
        self.assertNotIn("field", text)
        self.assertNotIn("tag_red-1", loop.build_options(turn, None, None))

    def test_closest_threat_is_described_and_replaces_nearest_enemy(self):
        route = {"id": "r", "goal": "enemy_flag", "first_step": "up", "length": 2, "closest_threat":
                 {"enemy_id": "red-1", "step": 3, "distance": 2}}
        self.assertIn("closest threat red-1 at step 3, distance 2", loop.describe_route(route))
        self.assertNotIn("threat", loop.describe_route({**route, "closest_threat": None}))
        self.assertNotIn("threat", loop.describe_route({k: v for k, v in route.items() if k != "closest_threat"}))

    def test_the_servers_last_action_is_fallback_only(self):
        turn = {**make_turn(), "last_action": {"action": "move:up", "tick": 0, "result": "moved"}}
        self.assertNotIn("previous action", render_state(turn))
        self.assertIn("outcome of your previous action: move:up at tick 0 -> moved",
                      render_state(turn, include_last_action=True))

    def test_match_over_logging_handles_no_winner(self):
        self.assertEqual(loop.describe_match_over({"status": "match_over", "score": {"red": 0, "blue": 0},
                                                   "winner": None, "reason": "stopped"}),
                         "match over (stopped): no winner, score red 0, blue 0")


if __name__ == "__main__":
    unittest.main()
