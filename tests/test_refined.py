"""Tests for agent/refined/* (pure logic only: no network, no live match).

unittest-style so they run under `python -m unittest discover -s tests` (pytest is not installed).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "agent"
REFINED_DIR = AGENT_DIR / "refined"
for path in (REFINED_DIR, AGENT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, REFINED_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses resolves string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


dl = load("refined_decider_loop", "decider_loop.py")
sdk = load("harness_sdk", "harness_sdk.py")
sd = load("refined_strand_decider", "strand_decider.py")
hn = load("refined_harness", "harness.py")

ACT_FAST = ("Each tick: call wait_for_turn, then immediately call exactly one action tool with that tick. "
            "Do not write any text between tool calls. Do not call get_rules again after joining.")


def route(rid, goal, step="left", length=5, path=None, blocked=False, risks=(), dist=3):
    return {"id": rid, "goal": goal, "first_step": step, "length": length, "path": path or [],
            "blocked_now": blocked, "blocked_reason": "contention" if blocked else None,
            "risks": [{"step": s, "enemy_id": e} for s, e in risks], "min_enemy_distance": dist}


def turn_with(routes, me=(9, 7), tick=5):
    return {"status": "turn", "tick": tick,
            "state": {"tick": tick, "self": "blue-1", "self_position": {"x": me[0], "y": me[1]},
                      "agents": [{"id": "blue-1", "team": "blue", "position": {"x": me[0], "y": me[1]},
                                  "carrying": None, "disconnected": False}],
                      "routes": routes}}


def options_for(turn, plan=None):
    return dl.build_options(turn, plan)


def answers(threat=0.0, route_bad=0.0, pick="enemy_flag_direct", conf=0.9, probs=None):
    return {"threat": {"type": "noul", "noul": threat},
            "route_bad": {"type": "noul", "noul": route_bad},
            "action": {"type": "choice", "choice": pick, "confidence": conf,
                       "probabilities": probs if probs is not None else {pick: conf}}}


class CombineAnswersTests(unittest.TestCase):
    def setUp(self):
        self.routes = [
            route("enemy_flag_direct", "enemy_flag", length=6, risks=[(3, "red-1")]),
            route("enemy_flag_safe", "enemy_flag", step="up", length=9),
            route("enemy_flag_blocked", "enemy_flag", length=4, blocked=True),
            route("tag:red-1", "tag", step="left", length=3),
            route("tag:red-2", "tag", step=None, length=0),
            route("defend_flag", "defend_flag", step="right", length=2),
            route("own_base_direct", "own_base", length=8),
        ]
        self.turn = turn_with(self.routes)
        self.options = options_for(self.turn)

    def combine(self, ans, **kw):
        return dl.combine_answers(ans, self.options, self.turn, dl.Plan(), **kw)

    def test_questions_cover_threat_route_bad_and_action_in_one_request(self):
        questions = dl.build_questions(self.options)
        self.assertEqual(set(questions), {"threat", "route_bad", "action"})
        self.assertEqual(questions["threat"]["type"], "noul")
        self.assertEqual(questions["route_bad"]["type"], "noul")
        self.assertEqual(questions["action"]["type"], "choice")
        self.assertEqual(set(questions["action"]["criteria"]), set(self.options))
        self.assertEqual(set(questions["threat"]["criteria"]), {"true", "false"})
        self.assertEqual(set(questions["route_bad"]["criteria"]), {"true", "false"})

    def test_no_recommendation_wording_in_options(self):
        for text in self.options.values():
            self.assertNotIn("recommend", text.lower())

    def test_fallback_to_choice(self):
        label, conf, reason = self.combine(answers(threat=0.1, route_bad=0.2, pick="enemy_flag_safe", conf=0.77))
        self.assertEqual((label, reason), ("enemy_flag_safe", "choice"))
        self.assertAlmostEqual(conf, 0.77)

    def test_threat_prefers_adjacent_tag_without_probabilities(self):
        ans = answers(threat=0.8, pick="enemy_flag_direct")
        ans["action"]["probabilities"] = {}
        label, conf, reason = self.combine(ans)
        self.assertEqual((label, reason), ("tag:red-2", "threat"))
        self.assertAlmostEqual(conf, 0.8)

    def test_threat_uses_choice_probabilities_when_available(self):
        ans = answers(threat=0.6, pick="enemy_flag_direct",
                      probs={"enemy_flag_direct": 0.5, "tag:red-1": 0.2, "defend_flag": 0.25, "tag:red-2": 0.05})
        label, conf, reason = self.combine(ans)
        self.assertEqual((label, reason), ("defend_flag", "threat"))
        self.assertAlmostEqual(conf, 0.6)

    def test_threat_without_adjacent_picks_shortest_tag_then_defend(self):
        routes = [r for r in self.routes if r["id"] != "tag:red-2"]
        turn = turn_with(routes)
        opts = options_for(turn)
        ans = answers(threat=0.9)
        ans["action"]["probabilities"] = {}
        self.assertEqual(dl.combine_answers(ans, opts, turn, dl.Plan())[0], "tag:red-1")
        routes = [r for r in routes if r["id"] != "tag:red-1"]
        turn = turn_with(routes)
        self.assertEqual(dl.combine_answers(ans, options_for(turn), turn, dl.Plan())[0], "defend_flag")

    def test_threat_without_tag_or_defend_option_falls_through(self):
        routes = [r for r in self.routes if r["goal"] not in ("tag", "defend_flag")]
        turn = turn_with(routes)
        label, _, reason = dl.combine_answers(answers(threat=0.9, pick="enemy_flag_safe"), options_for(turn), turn, dl.Plan())
        self.assertEqual((label, reason), ("enemy_flag_safe", "choice"))

    def test_threat_threshold_is_configurable(self):
        ans = answers(threat=0.6, pick="enemy_flag_safe")
        self.assertEqual(self.combine(ans, threat_threshold=0.7)[2], "choice")
        self.assertEqual(self.combine(ans, threat_threshold=0.5)[2], "threat")

    def test_blocked_tag_is_not_a_threat_target(self):
        routes = [dict(r, blocked_now=True) if r["goal"] in ("tag", "defend_flag") else r for r in self.routes]
        turn = turn_with(routes)
        label, _, reason = dl.combine_answers(answers(threat=0.9, pick="enemy_flag_safe"), options_for(turn), turn, dl.Plan())
        self.assertEqual((label, reason), ("enemy_flag_safe", "choice"))

    def test_blocked_pick_switches_to_safe_same_goal_route(self):
        label, conf, reason = self.combine(answers(route_bad=0.81, pick="enemy_flag_blocked", conf=0.31))
        self.assertEqual((label, reason), ("enemy_flag_safe", "avoid blocked/risky"))
        self.assertAlmostEqual(conf, 0.81)

    def test_risky_pick_switches_to_safe_same_goal_route(self):
        label, _, reason = self.combine(answers(route_bad=0.6, pick="enemy_flag_direct"))
        self.assertEqual((label, reason), ("enemy_flag_safe", "avoid blocked/risky"))

    def test_no_switch_when_route_bad_low(self):
        self.assertEqual(self.combine(answers(route_bad=0.49, pick="enemy_flag_blocked"))[2], "choice")

    def test_no_switch_without_safe_same_goal_alternative(self):
        # own_base_direct has no sibling; make it risky
        routes = [dict(r, risks=[{"step": 2, "enemy_id": "red-1"}]) if r["id"] == "own_base_direct" else r
                  for r in self.routes]
        turn = turn_with(routes)
        label, _, reason = dl.combine_answers(answers(route_bad=0.9, pick="own_base_direct"), options_for(turn), turn, dl.Plan())
        self.assertEqual((label, reason), ("own_base_direct", "choice"))

    def test_safe_pick_is_kept_even_when_route_bad_high(self):
        self.assertEqual(self.combine(answers(route_bad=0.9, pick="enemy_flag_safe"))[2], "choice")

    def test_threat_beats_route_bad(self):
        self.assertEqual(self.combine(answers(threat=0.9, route_bad=0.9, pick="enemy_flag_blocked"))[2], "threat")

    def test_unusable_answers_do_not_crash(self):
        label, conf, reason = self.combine({"action": {"choice": None}})
        self.assertEqual((label, conf, reason), (None, 0.0, "choice"))
        label, _, reason = self.combine({"threat": "garbage", "action": {"choice": "wait", "confidence": 0.9}})
        self.assertEqual((label, reason), ("wait", "choice"))

    def test_format_decision_log_line(self):
        line = dl.format_decision(7, "own_base_safe", 0.31, "avoid blocked/risky", 0.12, 0.81)
        self.assertEqual(line, "[tick 7] decider -> own_base_safe (0.31) reason=avoid blocked/risky "
                               "threat=0.12 route_bad=0.81")


class BlockedAnnotationTests(unittest.TestCase):
    def plan_and_turn(self, blocked):
        path = [{"x": 8, "y": 7}, {"x": 7, "y": 7}, {"x": 6, "y": 7}]
        turn = turn_with([route("enemy_flag_direct", "enemy_flag", path=path, blocked=blocked)], me=(9, 7))
        plan = dl.Plan("enemy_flag_direct", "enemy_flag", 3, "move left",
                       path=((9, 7), (8, 7), (7, 7), (6, 7)), position=(10, 7))
        return turn, plan

    def test_unblocked_route_keeps_continues_annotation(self):
        turn, plan = self.plan_and_turn(False)
        self.assertIn("continues your current path", dl.build_options(turn, plan)["enemy_flag_direct"])

    def test_blocked_route_is_not_annotated_as_continuing(self):
        turn, plan = self.plan_and_turn(True)
        text = dl.build_options(turn, plan)["enemy_flag_direct"]
        self.assertNotIn("continues your current path", text)
        self.assertIn("| would continue your path but is BLOCKED now", text)


class DeciderLoopCliTests(unittest.TestCase):
    def test_defaults(self):
        old = sys.argv
        sys.argv = ["decider_loop.py"]
        try:
            args = dl.parse_args()
        finally:
            sys.argv = old
        self.assertEqual(args.name, "refined-decider")
        self.assertEqual(args.threat_threshold, 0.5)

    def test_warmup_asks_all_three_questions(self):
        _, questions = dl.warmup_question()
        self.assertEqual(set(questions), {"threat", "route_bad", "action"})


def tool_result(payload):
    return {"status": "success", "toolUseId": "t", "content": [{"text": json.dumps(payload)}]}


def after_event(name, result):
    return SimpleNamespace(tool_use={"name": name, "input": {}}, result=result)


class StripWallsHookTests(unittest.TestCase):
    WAIT = "ctf-game_wait_for_turn"

    def payload(self, tick):
        return {"status": "turn", "tick": tick,
                "state": {"tick": tick, "walls": [{"x": 1, "y": 1}], "grid": {"width": 15}, "self": "blue-1"}}

    def state_of(self, event):
        return json.loads(event.result["content"][0]["text"])["state"]

    def test_first_turn_keeps_walls_later_turns_strip(self):
        hook = sdk.StripStaticWallsHook()
        first = after_event(self.WAIT, tool_result(self.payload(1)))
        hook.after_tool_call(first)
        self.assertIn("walls", self.state_of(first))
        second = after_event(self.WAIT, tool_result(self.payload(2)))
        hook.after_tool_call(second)
        state = self.state_of(second)
        self.assertNotIn("walls", state)
        self.assertEqual(state["grid"], {"width": 15})
        self.assertEqual(json.loads(second.result["content"][0]["text"])["tick"], 2)
        self.assertEqual(second.result["toolUseId"], "t")

    def test_unprefixed_name_also_matches(self):
        hook = sdk.StripStaticWallsHook()
        hook.after_tool_call(after_event("wait_for_turn", tool_result(self.payload(1))))
        later = after_event("wait_for_turn", tool_result(self.payload(2)))
        hook.after_tool_call(later)
        self.assertNotIn("walls", self.state_of(later))

    def test_other_tools_untouched(self):
        hook = sdk.StripStaticWallsHook()
        hook.after_tool_call(after_event(self.WAIT, tool_result(self.payload(1))))
        original = tool_result({"state": {"walls": [1]}})
        event = after_event("ctf-game_move", original)
        hook.after_tool_call(event)
        self.assertIs(event.result, original)
        self.assertEqual(json.loads(event.result["content"][0]["text"])["state"]["walls"], [1])

    def test_malformed_json_left_as_is(self):
        hook = sdk.StripStaticWallsHook()
        hook.after_tool_call(after_event(self.WAIT, tool_result(self.payload(1))))
        bad = {"status": "success", "toolUseId": "t", "content": [{"text": "not json {"}]}
        event = after_event(self.WAIT, bad)
        hook.after_tool_call(event)
        self.assertEqual(event.result["content"][0]["text"], "not json {")

    def test_error_results_do_not_count_as_first_turn(self):
        hook = sdk.StripStaticWallsHook()
        err = {"status": "error", "toolUseId": "t", "content": [{"text": "boom"}]}
        hook.after_tool_call(after_event(self.WAIT, err))
        first = after_event(self.WAIT, tool_result(self.payload(1)))
        hook.after_tool_call(first)
        self.assertIn("walls", self.state_of(first))

    def test_register_hooks_subscribes_after_tool_call(self):
        calls = []
        registry = SimpleNamespace(add_callback=lambda event_type, cb: calls.append(event_type))
        sdk.StripStaticWallsHook().register_hooks(registry)
        self.assertEqual([c.__name__ for c in calls], ["AfterToolCallEvent"])


class ConversationManagerTests(unittest.TestCase):
    def test_config(self):
        manager = sdk.make_conversation_manager(sdk.DEFAULT_WINDOW)
        self.assertEqual(manager.window_size, sdk.DEFAULT_WINDOW)
        self.assertEqual(manager.pin_first, 1)
        self.assertIs(manager.per_turn, True)
        self.assertEqual(sdk.make_conversation_manager(20).window_size, 20)

    def test_default_window_is_about_three_ticks(self):
        self.assertEqual(sdk.DEFAULT_WINDOW, 12)  # 4 messages per tick (wait_for_turn + action)

    def test_window_trims_old_ticks_and_keeps_instruction(self):
        manager = sdk.make_conversation_manager(12)
        messages = [{"role": "user", "content": [{"text": "join the match"}]}]
        for tick in range(10):
            for tool in ("wait_for_turn", "move"):
                uid = f"{tool}-{tick}"
                messages.append({"role": "assistant", "content": [{"toolUse": {"toolUseId": uid, "name": tool, "input": {}}}]})
                messages.append({"role": "user", "content": [{"toolResult": {"toolUseId": uid, "status": "success",
                                                                             "content": [{"text": str(tick)}]}}]})
        agent = SimpleNamespace(messages=messages)
        manager.apply_management(agent)
        self.assertLessEqual(len(messages), 13)
        self.assertEqual(messages[0]["content"][0]["text"], "join the match")
        self.assertEqual(messages[-1]["content"][0]["toolResult"]["content"][0]["text"], "9")
        roles = [m["role"] for m in messages]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)


class PromptAndCliTests(unittest.TestCase):
    def test_act_fast_prompt_in_all_three(self):
        for module in (sdk, sd, hn):
            self.assertTrue(module.SYSTEM_PROMPT.endswith(ACT_FAST), module.__name__)
            self.assertIn("The server never recommends one", module.SYSTEM_PROMPT)

    def test_prompts_stay_neutral(self):
        self.assertNotIn("recommended route", sdk.SYSTEM_PROMPT.lower())

    def test_default_names_and_window(self):
        for module, name in ((sdk, "refined-sdk"), (sd, "refined-strands-decider"), (hn, "refined-harness")):
            old = sys.argv
            sys.argv = ["x.py"]
            try:
                args = module.parse_args()
            finally:
                sys.argv = old
            self.assertEqual(args.name, name)
        old = sys.argv
        sys.argv = ["x.py", "--window", "8"]
        try:
            self.assertEqual(sdk.parse_args().window, 8)
            self.assertEqual(sd.parse_args().window, 8)
        finally:
            sys.argv = old


class StrandDeciderGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".lock")
        self.addCleanup(self.tmp.close)
        self._old_lock = sd.DECIDER_LOCK_FILE
        sd.DECIDER_LOCK_FILE = self.tmp.name
        self.addCleanup(setattr, sd, "DECIDER_LOCK_FILE", self._old_lock)

    def make(self, answers=None):
        calls = []

        class FakeDecider:
            def ask(self, state, questions):
                calls.append(questions)
                return answers or {"args_grounded": {"noul": 0.9}, "stale_state": {"noul": 0.1}}

        return sd.ToolCallReviewer(FakeDecider()), calls

    def event(self, name):
        return SimpleNamespace(tool_use={"name": name, "input": {"tick": 1}},
                               agent=SimpleNamespace(messages=[]))

    def test_move_and_wait_proceed_without_asking(self):
        reviewer, calls = self.make()
        for name in ("ctf-game_move", "ctf-game_wait"):
            self.assertIsInstance(reviewer.before_tool_call(self.event(name)), sd.Proceed)
        self.assertEqual(calls, [])

    def test_tag_and_grab_flag_are_reviewed(self):
        reviewer, calls = self.make()
        for name in ("ctf-game_tag", "ctf-game_grab_flag"):
            self.assertIsInstance(reviewer.before_tool_call(self.event(name)), sd.Proceed)
        self.assertEqual(len(calls), 2)

    def test_tag_can_still_be_guided(self):
        reviewer, calls = self.make({"args_grounded": {"noul": 0.1}, "stale_state": {"noul": 0.1}})
        self.assertIsInstance(reviewer.before_tool_call(self.event("ctf-game_tag")), sd.Guide)

    def test_gated_set(self):
        self.assertEqual(set(sd.GATED_TOOLS), {"tag", "grab_flag"})


if __name__ == "__main__":
    unittest.main()
