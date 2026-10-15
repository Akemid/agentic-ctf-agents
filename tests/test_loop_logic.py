"""decider_loop pure logic: characterization tests against the local original.

`local/agent/refined/decider_loop.py` is the behavior reference. Every pure function of the port is
run on the same corpus of turns and must return exactly what the original returns. A handful of
explicit expectations anchor the behavior in plain values so the tests also read as a spec.
"""

import copy
import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

import loop_logic as port

REFINED = Path(__file__).resolve().parents[1] / "local" / "agent" / "refined" / "decider_loop.py"


@pytest.fixture(scope="module")
def local():
    parent = str(REFINED.parent.parent)
    sys.path.insert(0, parent)
    try:
        spec = importlib.util.spec_from_file_location("local_decider_loop", REFINED)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(parent)
    return module


# --------------------------------------------------------------------------- corpus


def route(rid, goal, step, length, *, risks=(), blocked=False, threat=None, path=None):
    """A route in the v2 shape: `threat` is (enemy_id, step, distance) for `closest_threat`, or None."""
    return {
        "id": rid,
        "goal": goal,
        "first_step": step,
        "length": length,
        "path": path if path is not None else [],
        "blocked_now": blocked,
        "blocked_reason": "contention" if blocked else None,
        "risks": [{"step": st, "enemy_id": en} for st, en in risks],
        "closest_threat": (
            None if threat is None else {"enemy_id": threat[0], "step": threat[1], "distance": threat[2]}
        ),
    }


def agent(aid, team, x, y, carrying=None):
    return {
        "id": aid,
        "team": team,
        "position": {"x": x, "y": y},
        "carrying": carrying,
        "disconnected": False,
    }


def base_state(routes, **overrides):
    state = {
        "tick": 12,
        "agents": [
            agent("blue-1", "blue", 9, 7),
            agent("blue-2", "blue", 11, 4),
            agent("red-1", "red", 5, 6),
            agent("red-2", "red", 4, 9),
        ],
        "flags": [
            {"team": "red", "position": {"x": 1, "y": 7}, "carried_by": None},
            {"team": "blue", "position": {"x": 13, "y": 7}, "carried_by": None},
        ],
        "score": {"red": 1, "blue": 0},
        "self": "blue-1",
        "self_position": {"x": 9, "y": 7},
        "teammates": [{"id": "blue-2", "position": {"x": 11, "y": 4}}],
        "moves": {"up": "open", "down": "open", "left": "blocked", "right": "open"},
        "routes": routes,
    }
    state.update(overrides)
    return state


def turn_of(state, events=(), **top):
    return {
        "status": "turn",
        "tick": state["tick"],
        "state": state,
        "events": list(events),
        "last_action": None,
        **top,
    }


# The static map as `join_match.map` / `get_map` return it (the turn state no longer carries it).
MAP_RAW = {
    "grid": {"width": 15, "height": 15},
    "walls": [{"x": 1, "y": 1}, {"x": 2, "y": 1}],
    "fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7},
    "flag_bases": {"red": {"x": 1, "y": 7}, "blue": {"x": 13, "y": 7}},
    "ascii": ["..."],
    "legend": {"#": "wall"},
}


PATH_A = [{"x": 8, "y": 7}, {"x": 7, "y": 7}, {"x": 6, "y": 7}]
ROUTES = [
    route(
        "enemy_flag_direct",
        "enemy_flag",
        "left",
        8,
        risks=[(3, "red-1")],
        threat=("red-1", 6, 2),
        path=PATH_A,
    ),
    route(
        "enemy_flag_safe",
        "enemy_flag",
        "up",
        11,
        threat=("red-2", 3, 4),
        path=[{"x": 9, "y": 6}, {"x": 9, "y": 5}],
    ),
    route("tag:red-1", "tag", "left", 4),
    route("home_base", "own_base", "right", 4, blocked=True),
    route("defend_flag", "defend_flag", None, 0),
]

CARRIER = base_state(
    [route("own_base_direct", "own_base", "right", 5), route("own_base_safe", "own_base", "down", 7)],
    agents=[agent("blue-1", "blue", 9, 7, carrying="red"), agent("red-1", "red", 5, 6)],
)
ON_FLAG = base_state(
    [], self_position={"x": 1, "y": 7}, agents=[agent("blue-1", "blue", 1, 7), agent("red-1", "red", 3, 6)]
)
ADJACENT_TAG = base_state(
    [],
    self_position={"x": 5, "y": 5},
    agents=[
        agent("blue-1", "red", 5, 5),
        agent("red-1", "blue", 5, 6),
    ],  # a red player in its own field, next to an enemy
)
ZERO_LENGTH = base_state(
    [route("tag:red-1", "tag", None, 0), route("enemy_flag_direct", "enemy_flag", None, 0)]
)

TURNS = {
    "routes": turn_of(
        base_state(copy.deepcopy(ROUTES)),
        events=[{"type": "moved", "agent_id": "red-1", "from": {"x": 6, "y": 6}, "to": {"x": 5, "y": 6}}],
    ),
    "carrier": turn_of(CARRIER),
    "no_routes_moves": turn_of(base_state([])),
    "last_action_blocked": turn_of(
        base_state(copy.deepcopy(ROUTES)),
        last_action={"action": "move:left", "tick": 11, "result": "blocked", "reason": "contention"},
    ),
    "last_action_moved": turn_of(
        base_state(copy.deepcopy(ROUTES)), last_action={"action": "move:up", "tick": 11, "result": "moved"}
    ),
    "on_flag": turn_of(ON_FLAG),
    "adjacent_tag": turn_of(ADJACENT_TAG),
    "zero_length": turn_of(ZERO_LENGTH),
    "observation_fallback": {"status": "turn", "tick": 3, "observation": base_state(copy.deepcopy(ROUTES))},
    "blocked_event": turn_of(
        base_state(copy.deepcopy(ROUTES)),
        events=[
            {
                "type": "move_blocked",
                "agent_id": "blue-1",
                "direction": "left",
                "target": {"x": 8, "y": 7},
                "reason": "wall",
            }
        ],
    ),
}

PLAN_FIELDS = [
    None,
    {},
    {
        "route_id": "enemy_flag_direct",
        "goal": "enemy_flag",
        "since_tick": 9,
        "last_action": "move left",
        "path": ((8, 7), (7, 7), (6, 7)),
        "position": (9, 7),
    },
    {
        "route_id": "enemy_flag_direct",
        "goal": "enemy_flag",
        "since_tick": 9,
        "last_action": "move left",
        "path": ((9, 7), (8, 7), (7, 7), (6, 7)),
        "position": (10, 7),
    },
    {
        "route_id": "x",
        "goal": "own_base",
        "since_tick": 4,
        "last_action": "wait",
        "path": ((1, 1),),
        "position": (9, 7),
    },
]


def map_for(module, with_map):
    """The module's own MapInfo for the shared raw map (or None: the map could not be obtained)."""
    return module.parse_map(MAP_RAW) if with_map else None


def make_plan(module, fields):
    return None if fields is None else module.Plan(**fields)


def plans(module):
    return [make_plan(module, f) for f in PLAN_FIELDS]


def plan_view(plan):
    return None if plan is None else vars(plan)


# --------------------------------------------------------------------------- differential tests


class TestStateRendering:
    @pytest.mark.parametrize("name", sorted(TURNS))
    @pytest.mark.parametrize("plan_index", range(len(PLAN_FIELDS)))
    @pytest.mark.parametrize("with_map", [True, False])
    def test_render_state_matches_the_original(self, local, name, plan_index, with_map):
        turn = TURNS[name]
        assert port.render_state(
            turn, plans(port)[plan_index], map_for(port, with_map)
        ) == local.render_state(turn, plans(local)[plan_index], map_for(local, with_map))

    @pytest.mark.parametrize("name", sorted(TURNS))
    @pytest.mark.parametrize("plan_index", range(len(PLAN_FIELDS)))
    def test_the_fallback_render_matches_the_original(self, local, name, plan_index):
        turn = TURNS[name]
        assert port.render_state(
            turn, plans(port)[plan_index], map_for(port, True), include_last_action=True
        ) == local.render_state(
            turn, plans(local)[plan_index], map_for(local, True), include_last_action=True
        )

    def test_the_rendered_state_has_the_expected_facts(self):
        text = port.render_state(TURNS["routes"], None, map_for(port, True))
        assert text.splitlines()[0] == "tick: 12"
        assert "you: blue-1 (blue) at (9, 7), in your own field" in text  # blue field is x 8..14
        assert "carrying the flag: no" in text
        assert "enemy red-1 at (5, 6), carrying: no" in text
        assert (
            "  enemy_flag_direct: goal enemy_flag, 8 steps, first step left, "
            "1 risk: can be tagged by red-1 at step 3, closest threat red-1 at step 6, distance 2" in text
        )
        assert "  tag:red-1: goal tag red-1, 4 steps, first step left" in text.splitlines()
        assert "recent events: moved red-1 (6, 6)->(5, 6)" in text

    def test_the_position_comes_from_the_static_map_not_from_the_turn(self):
        # `fields` is not in the turn state any more: the same turn is placed differently by different maps.
        turn = TURNS["routes"]  # blue-1 stands at x=9
        assert "at (9, 7), in your own field" in port.render_state(turn, None, map_for(port, True))
        far_blue = port.parse_map(
            {**MAP_RAW, "fields": {**MAP_RAW["fields"], "blue": {"min_x": 12, "max_x": 14}}}
        )
        assert "at (9, 7), in the enemy field" in port.render_state(turn, None, far_blue)
        on_line = {**turn, "state": {**turn["state"], "self_position": {"x": 7, "y": 3}}}
        assert "at (7, 3), on the neutral line" in port.render_state(on_line, None, map_for(port, True))

    def test_without_the_map_the_position_is_not_guessed(self):
        text = port.render_state(TURNS["routes"], None, None)
        assert "you: blue-1 (blue) at (9, 7)" in text
        assert "own field" not in text and "enemy field" not in text and "neutral" not in text

    def test_the_decider_text_never_carries_the_servers_last_action(self):
        # The decider is a trained model: its input only changes where the contract forces it.
        for name in ("last_action_blocked", "last_action_moved", "routes"):
            text = port.render_state(TURNS[name], None, map_for(port, True))
            assert "previous action" not in text and "last action result" not in text
        state, _ = port.warmup_question()
        assert "previous action" not in state and "last action result" not in state

    def test_the_fallback_text_reports_the_servers_last_action_when_present(self):
        full_map = map_for(port, True)
        text = port.render_state(TURNS["last_action_blocked"], None, full_map, include_last_action=True)
        assert "outcome of your previous action: move:left at tick 11 -> blocked (contention)" in text
        moved = port.render_state(TURNS["last_action_moved"], None, full_map, include_last_action=True)
        assert "outcome of your previous action: move:up at tick 11 -> moved" in moved
        plain = port.render_state(TURNS["routes"], None, map_for(port, True), include_last_action=True)
        assert "previous action" not in plain  # first turn: null

    def test_the_fallback_label_does_not_clash_with_the_plan_line(self):
        plan = plans(port)[1]
        full_map = map_for(port, True)
        text = port.render_state(TURNS["last_action_moved"], plan, full_map, include_last_action=True)
        assert text.count("last action:") <= 1  # only the plan line's own `last action:`
        assert "outcome of your previous action:" in text

    def test_a_route_without_closest_threat_is_described_without_one(self):
        old_server_route = {"id": "r", "goal": "enemy_flag", "first_step": "up", "length": 2}
        assert port.describe_route(old_server_route) == "goal enemy_flag, 2 steps, first step up"
        # a null threat is missing data, exactly like an absent key: no clause at all (not "no threat")
        assert port.describe_route({**old_server_route, "closest_threat": None}) == (
            "goal enemy_flag, 2 steps, first step up"
        )
        assert "min_enemy_distance" not in port.describe_route({**old_server_route, "min_enemy_distance": 3})

    def test_extract_tick_prefers_the_top_level_then_the_state(self, local):
        assert (
            port.extract_tick({"tick": 4, "state": {"tick": 9}})
            == 4
            == local.extract_tick({"tick": 4, "state": {"tick": 9}})
        )
        assert port.extract_tick({"state": {"tick": 9}}) == 9
        with pytest.raises(ValueError):
            port.extract_tick({"state": {}})


class TestOptionsAndCalls:
    @pytest.mark.parametrize("name", sorted(TURNS))
    @pytest.mark.parametrize("plan_index", range(len(PLAN_FIELDS)))
    @pytest.mark.parametrize("with_map", [True, False])
    def test_build_options_matches_the_original(self, local, name, plan_index, with_map):
        turn = TURNS[name]
        mine = port.build_options(turn, plans(port)[plan_index], map_for(port, with_map))
        theirs = local.build_options(turn, plans(local)[plan_index], map_for(local, with_map))
        assert mine == theirs
        assert "wait" in mine

    def test_options_with_routes_are_the_server_routes_plus_wait(self):
        options = port.build_options(TURNS["routes"], None)
        assert list(options) == [
            "enemy_flag_direct",
            "enemy_flag_safe",
            "tag:red-1",
            "home_base",
            "defend_flag",
            "wait",
        ]
        assert "BLOCKED now" in options["home_base"]

    def test_without_routes_open_directions_become_move_options(self):
        options = port.build_options(TURNS["no_routes_moves"], None)
        assert list(options) == ["move_up", "move_down", "move_right", "wait"]

    def test_the_adjacent_tag_fallback_needs_the_map_to_know_we_stand_in_our_own_field(self):
        turn = TURNS["adjacent_tag"]  # no routes; a red player at (5, 5) next to an enemy at (5, 6)
        with_map = port.build_options(turn, None, map_for(port, True))
        assert with_map["tag_red-1"] == "tag the adjacent enemy red-1 (you are in your own field)"
        assert not [label for label in port.build_options(turn, None, None) if label.startswith("tag_")]

    def test_grab_flag_is_offered_only_when_standing_on_the_enemy_flag(self):
        assert "grab_flag" in port.build_options(TURNS["on_flag"], None)
        assert "grab_flag" not in port.build_options(TURNS["routes"], None)

    def test_the_plan_marks_routes_that_continue_it(self):
        plan = port.Plan("enemy_flag_direct", "enemy_flag", 9, "move left", ((8, 7), (7, 7), (6, 7)), (9, 7))
        options = port.build_options(TURNS["routes"], plan)
        assert "continues your current path (3 ticks)" in options["enemy_flag_direct"]
        assert "same goal as your current plan, different path" in options["enemy_flag_safe"]

    @pytest.mark.parametrize("name", sorted(TURNS))
    def test_label_to_call_matches_the_original_for_every_option(self, local, name):
        turn = TURNS[name]
        for label in local.build_options(turn, None):
            assert port.label_to_call(label, 7, turn) == local.label_to_call(label, 7, turn)

    @pytest.mark.parametrize("label", ["nope", "move_sideways", "tag_", ""])
    def test_label_to_call_rejects_unknown_labels_like_the_original(self, local, label):
        with pytest.raises(ValueError):
            local.label_to_call(label, 1, TURNS["routes"])
        with pytest.raises(ValueError):
            port.label_to_call(label, 1, TURNS["routes"])

    def test_label_to_call_mapping(self):
        turn = TURNS["routes"]
        assert port.label_to_call("enemy_flag_direct", 12, turn) == (
            "move",
            {"direction": "left", "tick": 12, "rationale": "route enemy_flag_direct"},
        )
        assert port.label_to_call("grab_flag", 12) == ("grab_flag", {"tick": 12})
        assert port.label_to_call("move_up", 12) == ("move", {"direction": "up", "tick": 12})
        assert port.label_to_call("tag_red-1", 12) == ("tag", {"enemy_id": "red-1", "tick": 12})
        zero = TURNS["zero_length"]
        assert port.label_to_call("tag:red-1", 3, zero)[0] == "tag"
        assert port.label_to_call("enemy_flag_direct", 3, zero)[0] == "grab_flag"
        assert port.label_to_call("defend_flag", 12, TURNS["routes"]) == (
            "wait",
            {"tick": 12, "rationale": "route defend_flag"},
        )


class TestQuestionsAndAnswers:
    def test_the_three_questions_match_the_original(self, local):
        options = port.build_options(TURNS["routes"], None)
        assert port.build_questions(options) == local.build_questions(options)
        assert set(port.build_questions(options)) == {"threat", "route_bad", "action"}

    def test_warmup_question_matches_the_original(self, local):
        assert port.warmup_question() == local.warmup_question()

    def test_the_warm_up_state_has_the_v2_shape(self):
        state, questions = port.warmup_question()
        assert "closest threat red-1 at step 6, distance 2" in state
        assert "min_enemy_distance" not in state and "nearest enemy" not in state
        assert "in your own field" in state  # the warm-up carries its own static map
        assert set(questions) == {"threat", "route_bad", "action"}

    @pytest.mark.parametrize(
        "answer",
        [
            {"type": "choice", "choice": "a", "probabilities": {"a": 0.4}, "confidence": 0.31},
            {"type": "choice", "choice": "a", "probabilities": {"a": 0.4}},
            {"choice": 3},
            {},
            None,
            "x",
            {"choice": "a", "confidence": "high"},
        ],
    )
    def test_parse_choice_matches_the_original(self, local, answer):
        assert port.parse_choice(answer) == local.parse_choice(answer)

    @pytest.mark.parametrize("answer", [{"noul": 0.7}, {"noul": True}, {"noul": "x"}, {}, None, 5])
    def test_parse_noul_matches_the_original(self, local, answer):
        assert port.parse_noul(answer) == local.parse_noul(answer)

    def test_confidence_is_not_the_top_probability(self):
        assert port.parse_choice({"choice": "a", "probabilities": {"a": 0.48}, "confidence": 0.31}) == (
            "a",
            0.31,
        )

    @pytest.mark.parametrize("name", ["routes", "carrier", "no_routes_moves"])
    def test_combine_answers_matches_the_original_over_a_grid(self, local, name):
        turn = TURNS[name]
        options = local.build_options(turn, None)
        labels = list(options)
        for pick, threat, route_bad, plan_index in itertools.product(
            labels[:4], (0.0, 0.49, 0.5, 0.9), (0.0, 0.5, 0.95), range(len(PLAN_FIELDS))
        ):
            answers = {
                "action": {
                    "type": "choice",
                    "choice": pick,
                    "probabilities": {label: 0.1 + 0.1 * i for i, label in enumerate(labels)},
                    "confidence": 0.6,
                },
                "threat": {"noul": threat},
                "route_bad": {"noul": route_bad},
            }
            for threshold in (0.5, 0.95):
                mine = port.combine_answers(
                    answers, options, turn, plans(port)[plan_index], threat_threshold=threshold
                )
                theirs = local.combine_answers(
                    answers, options, turn, plans(local)[plan_index], threat_threshold=threshold
                )
                assert mine == theirs

    def test_a_threat_prefers_a_tag_or_defend_option(self):
        turn, options = TURNS["routes"], port.build_options(TURNS["routes"], None)
        answers = {
            "action": {
                "choice": "enemy_flag_direct",
                "probabilities": {"enemy_flag_direct": 0.6, "tag:red-1": 0.3},
                "confidence": 0.9,
            },
            "threat": {"noul": 0.9},
            "route_bad": {"noul": 0.0},
        }
        assert port.combine_answers(answers, options, turn) == ("tag:red-1", 0.9, "threat")

    def test_a_risky_pick_is_swapped_for_the_safe_route_with_the_same_goal(self):
        turn, options = TURNS["routes"], port.build_options(TURNS["routes"], None)
        answers = {
            "action": {"choice": "enemy_flag_direct", "probabilities": {}, "confidence": 0.95},
            "threat": {"noul": 0.0},
            "route_bad": {"noul": 0.8},
        }
        assert port.combine_answers(answers, options, turn) == ("enemy_flag_safe", 0.8, "avoid blocked/risky")

    def test_otherwise_the_choice_is_taken_as_is(self):
        turn, options = TURNS["routes"], port.build_options(TURNS["routes"], None)
        answers = {
            "action": {"choice": "enemy_flag_safe", "probabilities": {}, "confidence": 0.7},
            "threat": {"noul": 0.1},
            "route_bad": {"noul": 0.9},
        }
        assert port.combine_answers(answers, options, turn) == ("enemy_flag_safe", 0.7, "choice")


class TestTimeBudget:
    @pytest.mark.parametrize("chosen", [None, "wait", "bogus"])
    @pytest.mark.parametrize("confidence", [0.0, 0.84, 0.85, 1.0])
    @pytest.mark.parametrize("remaining", [0, 300, 4799, 4800, 9000])
    @pytest.mark.parametrize("down", [False, True])
    def test_choose_path_matches_the_original(self, local, chosen, confidence, remaining, down):
        options = {"wait": "do nothing", "move_up": "up"}
        args = (chosen, confidence, 0.85, options, remaining, 4500, 300, down)
        assert port.choose_path(*args) == local.choose_path(*args)

    def test_choose_path_decisions(self):
        options = {"wait": "w", "move_up": "u"}
        assert port.choose_path("move_up", 0.9, 0.85, options, 100, 4500, 300, False) == "decider"
        assert port.choose_path("move_up", 0.5, 0.85, options, 6000, 4500, 300, False) == "llm"
        assert port.choose_path("move_up", 0.5, 0.85, options, 1000, 4500, 300, False) == "budget"
        assert port.choose_path(None, 0.0, 0.85, options, 1000, 4500, 300, False) == "wait"
        assert port.choose_path("move_up", 0.99, 0.85, options, 1000, 4500, 300, True) == "wait"

    @pytest.mark.parametrize("remaining,margin", [(5000, 300), (200, 300), (0, 0), (-50, 10)])
    def test_llm_timeout_matches_the_original(self, local, remaining, margin):
        assert port.llm_timeout_s(remaining, margin) == local.llm_timeout_s(remaining, margin)

    def test_llm_timeout_never_drops_below_50ms(self):
        assert port.llm_timeout_s(100, 300) == 0.05 and port.llm_timeout_s(5300, 300) == 5.0

    @pytest.mark.parametrize(
        "turn", [{"deadline_remaining_ms": 3000}, {"deadline_ms": 2000}, {}, {"deadline_remaining_ms": True}]
    )
    def test_turn_remaining_matches_the_original(self, local, turn):
        assert port.turn_remaining_ms(turn, 250) == local.turn_remaining_ms(turn, 250)

    def test_turn_remaining_prefers_the_remaining_deadline(self):
        assert port.turn_remaining_ms({"deadline_remaining_ms": 3000, "deadline_ms": 5000}, 250) == 2750
        assert port.turn_remaining_ms({}, 250) == 4750

    @pytest.mark.parametrize("acted", [False, True])
    @pytest.mark.parametrize("chosen", [None, "wait", "nope"])
    @pytest.mark.parametrize("down", [False, True])
    def test_timeout_action_matches_the_original(self, local, acted, chosen, down):
        options = {"wait": "w"}
        assert port.timeout_action(chosen, options, down, acted) == local.timeout_action(
            chosen, options, down, acted
        )

    def test_a_late_llm_action_is_never_doubled(self):
        assert port.timeout_action("wait", {"wait": "w"}, False, True) == "none"
        assert port.timeout_action("wait", {"wait": "w"}, False, False) == "decider"
        assert port.timeout_action("wait", {"wait": "w"}, True, False) == "wait"


class TestPlanMemory:
    CASES = [
        ((((8, 7), (7, 7)),), None),
    ]

    @pytest.mark.parametrize(
        "prev_path", [None, [], [(8, 7), (7, 7)], [{"x": 8, "y": 7}, {"x": 7, "y": 7}], [(1,)]]
    )
    @pytest.mark.parametrize("prev_pos", [None, (9, 7), (8, 7)])
    @pytest.mark.parametrize("here", [None, (9, 7), (8, 7), (3, 3)])
    @pytest.mark.parametrize("route_path", [None, [(7, 7)], [(8, 7), (7, 7)], [(9, 7)]])
    def test_continues_path_matches_the_original(self, local, prev_path, prev_pos, here, route_path):
        assert port.continues_path(prev_path, prev_pos, here, route_path) == local.continues_path(
            prev_path, prev_pos, here, route_path
        )

    def test_continues_path_semantics(self):
        assert (
            port.continues_path([(8, 7), (7, 7)], (9, 7), (8, 7), [(7, 7), (6, 7)]) is True
        )  # advanced one step
        assert port.continues_path([(8, 7), (7, 7)], (9, 7), (9, 7), [(8, 7), (7, 7)]) is True  # did not move
        assert port.continues_path([(8, 7), (7, 7)], (9, 7), (3, 3), [(7, 7)]) is False  # respawned elsewhere

    @pytest.mark.parametrize(
        "route_id,goal,action",
        [(None, None, "wait"), (None, None, "grab_flag"), ("enemy_flag_direct", "enemy_flag", "move left")],
    )
    @pytest.mark.parametrize("plan_index", range(len(PLAN_FIELDS)))
    @pytest.mark.parametrize("path,position", [(None, None), (PATH_A, (9, 7)), ([(9, 7), (8, 7)], (10, 7))])
    def test_update_plan_matches_the_original(
        self, local, route_id, goal, action, plan_index, path, position
    ):
        mine = port.update_plan(
            plans(port)[plan_index] or port.Plan(), route_id, goal, action, 12, path, position
        )
        theirs = local.update_plan(
            plans(local)[plan_index] or local.Plan(), route_id, goal, action, 12, path, position
        )
        assert plan_view(mine) == plan_view(theirs)

    def test_a_plan_keeps_its_start_tick_only_while_the_goal_and_path_continue(self):
        plan = port.Plan("a", "enemy_flag", 9, "move left", ((8, 7), (7, 7)), (9, 7))
        kept = port.update_plan(plan, "b", "enemy_flag", "move left", 10, [(7, 7), (6, 7)], (8, 7))
        assert kept.since_tick == 9
        changed = port.update_plan(plan, "b", "own_base", "move right", 10, [(7, 7), (6, 7)], (8, 7))
        assert changed.since_tick == 10
        assert port.update_plan(plan, None, None, "grab_flag", 10) == port.Plan(last_action="grab_flag")
        assert port.update_plan(plan, None, None, "wait", 10).last_action == "wait"

    def test_plan_suffix_and_action_descriptions(self, local):
        plan = port.Plan("enemy_flag_direct", "enemy_flag", 9)
        assert (
            port.plan_suffix(plan)
            == " [plan enemy_flag_direct since 9]"
            == local.plan_suffix(local.Plan("enemy_flag_direct", "enemy_flag", 9))
        )
        assert port.plan_suffix(port.Plan()) == ""
        assert port.describe_action("move", {"direction": "up"}) == "move up"
        assert port.describe_action("tag", {"enemy_id": "red-1"}) == "tag red-1"
        assert port.describe_action("wait", {}) == "wait"

    @pytest.mark.parametrize("name", ["routes", "carrier", "zero_length"])
    def test_route_lookups_match_the_original(self, local, name):
        turn = TURNS[name]
        for rid in ["enemy_flag_direct", "tag:red-1", "nope", None]:
            assert port.route_goal(turn, rid) == local.route_goal(turn, rid)
            assert port.route_path(turn, rid) == local.route_path(turn, rid)

    @pytest.mark.parametrize(
        "call",
        [
            ("move", {"direction": "left", "rationale": "route enemy_flag_safe."}),
            ("move", {"direction": "up"}),
            ("move", {"direction": "sideways"}),
            ("tag", {"enemy_id": "red-1"}),
            ("tag", {"enemy_id": "ghost"}),
            ("grab_flag", {}),
            ("wait", {"rationale": "route nope"}),
            None,
        ],
    )
    @pytest.mark.parametrize("plan_index", [0, 2])
    def test_infer_executed_route_matches_the_original(self, local, call, plan_index):
        turn = TURNS["routes"]
        mine = port.infer_executed_route(call, turn, plans(port)[plan_index] or port.Plan())
        theirs = local.infer_executed_route(call, turn, plans(local)[plan_index] or local.Plan())
        assert mine == theirs

    def test_infer_executed_route_prefers_the_rationale(self):
        call = ("move", {"direction": "left", "rationale": "route enemy_flag_safe."})
        assert port.infer_executed_route(call, TURNS["routes"], port.Plan()) == "enemy_flag_safe"

    def test_extract_action_call_finds_the_first_action_tool(self, local):
        messages = [
            {"role": "assistant", "content": [{"toolUse": {"name": "get_rules", "input": {}}}]},
            {
                "role": "assistant",
                "content": [{"toolUse": {"name": "move", "input": {"direction": "up", "tick": 3}}}],
            },
            {"role": "assistant", "content": [{"toolUse": {"name": "tag", "input": {"enemy_id": "x"}}}]},
        ]
        assert (
            port.extract_action_call(messages)
            == ("move", {"direction": "up", "tick": 3})
            == local.extract_action_call(messages)
        )
        assert port.extract_action_call([]) is None and port.extract_action_call(None) is None


class TestStaticMap:
    @pytest.mark.parametrize(
        "raw",
        [
            MAP_RAW,
            {**MAP_RAW, "walls": []},
            {**MAP_RAW, "walls": [{"x": 1}, "x", {"x": 2, "y": 3}]},
            {**MAP_RAW, "flag_bases": None},
            {"fields": {"red": {"min_x": 0, "max_x": 3}}},
            {"fields": {"red": {"min_x": "0", "max_x": 3}, "neutral_x": True}},
            {"fields": {}},
            {"grid": {}},
            {},
            None,
            "map",
            [],
        ],
    )
    def test_parse_map_matches_the_original(self, local, raw):
        mine, theirs = port.parse_map(raw), local.parse_map(raw)
        assert (mine is None) == (theirs is None)
        if mine is not None:
            assert vars(mine) == vars(theirs)

    def test_parse_map_keeps_what_the_logic_needs(self):
        info = port.parse_map(MAP_RAW)
        assert info.fields == {"red": (0, 6), "blue": (8, 14)}
        assert info.neutral_x == 7
        assert info.walls == frozenset({(1, 1), (2, 1)})
        assert info.flag_bases == {"red": (1, 7), "blue": (13, 7)}

    @pytest.mark.parametrize("raw", [None, {}, {"fields": {}}, "map", {"fields": {"neutral_x": 7}}])
    def test_an_unusable_map_is_none_not_an_exception(self, raw):
        assert port.parse_map(raw) is None

    def test_malformed_walls_and_bases_are_skipped_not_fatal(self):
        info = port.parse_map({**MAP_RAW, "walls": [{"x": 1}, "x", {"x": 2, "y": 3}], "flag_bases": None})
        assert info.walls == frozenset({(2, 3)}) and info.flag_bases == {}

    @pytest.mark.parametrize(
        "turn",
        [
            {"status": "match_over", "score": {"red": 1, "blue": 3}, "winner": "blue", "reason": "completed"},
            {"status": "match_over", "score": {"red": 0, "blue": 0}, "winner": None, "reason": "stopped"},
            {"status": "match_over", "score": {"red": 2, "blue": 2}, "winner": "tie", "reason": "completed"},
            {"status": "match_over"},
            {"status": "match_over", "winner": None, "_is_error": False},
        ],
    )
    def test_describe_match_over_matches_the_original(self, local, turn):
        assert port.describe_match_over(turn) == local.describe_match_over(turn)

    def test_describe_match_over_reports_the_reason_and_handles_no_winner(self):
        done = {
            "status": "match_over",
            "score": {"red": 1, "blue": 3},
            "winner": "blue",
            "reason": "completed",
        }
        assert port.describe_match_over(done) == "match over (completed): winner blue, score red 1, blue 3"
        stopped = {
            "status": "match_over",
            "score": {"red": 0, "blue": 0},
            "winner": None,
            "reason": "stopped",
        }
        assert port.describe_match_over(stopped) == "match over (stopped): no winner, score red 0, blue 0"
        assert port.describe_match_over({"status": "match_over"}) == "match over (unknown): no winner"

    @pytest.mark.parametrize(
        "outcome,expected",
        [
            ({"status": "accepted", "tick": 3, "_is_error": False}, None),
            ({"status": "rejected", "tick": 3, "reason": "match_over", "_is_error": False}, "match_over"),
            ({"status": "rejected", "tick": 3, "reason": "not_joined", "_is_error": False}, "not_joined"),
            ({"status": "rejected", "tick": 3, "_is_error": False}, "unknown"),
            ({"reason": "not_joined", "_is_error": True}, None),
            ({}, None),
        ],
    )
    def test_action_rejection_names_the_ack_reason(self, local, outcome, expected):
        assert port.action_rejection(outcome) == expected == local.action_rejection(outcome)


class TestMisc:
    def test_constants_match_the_original(self, local):
        for name in (
            "TOOL_GET_MAP",
            "DEFAULT_THRESHOLD",
            "THREAT_YES",
            "ROUTE_BAD_YES",
            "MAX_CONSECUTIVE_TOOL_ERRORS",
            "DEFAULT_DEADLINE_MS",
            "DEFAULT_LLM_BUDGET_MS",
            "DEFAULT_SAFETY_MARGIN_MS",
            "DECIDER_INSTRUCTIONS",
            "LLM_PLAN_NOTE",
            "ACTION_TOOLS",
            "DIRECTIONS",
            "TOOL_JOIN",
            "TOOL_WAIT_FOR_TURN",
            "THREAT_INSTRUCTIONS",
            "THREAT_CRITERIA",
            "ROUTE_BAD_INSTRUCTIONS",
            "ROUTE_BAD_CRITERIA",
            "MIN_LLM_TIMEOUT_S",
        ):
            assert getattr(port, name) == getattr(local, name), name
        assert port.LLM_SYSTEM_PROMPT == local.SYSTEM_PROMPT

    def test_stats_summary(self, local):
        mine, theirs = port.Stats(), local.Stats()
        assert mine.summary() == theirs.summary()
        for stats in (mine, theirs):
            stats.decider_ticks, stats.llm_ticks, stats.latencies_ms = 3, 1, [100.0, 200.0]
        assert mine.summary() == theirs.summary()
        assert "decider avg latency: 150 ms" in mine.summary()

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "move_blocked", "agent_id": "a", "direction": "up", "reason": "wall"},
            {"type": "tagged", "agent_id": "a", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}},
            "weird",
            {"type": "x", "k": {"x": 1, "y": 2}, "n": 5},
        ],
    )
    def test_describe_event_matches_the_original(self, local, event):
        assert port._describe_event(event) == local._describe_event(event)

    @pytest.mark.parametrize(
        "payload",
        [
            {"content": [{"text": '{"status": "turn"}'}]},
            {"content": [{"text": "plain words"}]},
            {"content": [{"text": "[1, 2]"}]},
            {"content": []},
            {},
        ],
    )
    def test_parse_tool_result_matches_the_original(self, local, payload):
        assert port.parse_tool_result(payload) == local.parse_tool_result(payload)

    def test_run_with_timeout(self, local):
        assert port.run_with_timeout(lambda: 5, 1.0) == (False, 5)
        import threading

        gate = threading.Event()
        timed_out, value = port.run_with_timeout(gate.wait, 0.05)
        gate.set()
        assert (timed_out, value) == (True, None)
        with pytest.raises(ZeroDivisionError):
            port.run_with_timeout(lambda: 1 / 0, 1.0)
