"""Tests for agent/refined/* except decider_loop (see test_refined_decider_loop.py; pure logic only: no network, no live match).

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


sdk = load("harness_sdk", "harness_sdk.py")
sd = load("refined_strand_decider", "strand_decider.py")
hn = load("refined_harness", "harness.py")

ACT_FAST = ("Each tick: call wait_for_turn, then immediately call exactly one action tool with that tick. "
            "Do not write any text between tool calls. Do not call get_rules again after joining.")


def tool_result(payload):
    return {"status": "success", "toolUseId": "t", "content": [{"text": json.dumps(payload)}]}


class NoStaticStrippingTests(unittest.TestCase):
    """Contract v2: turns carry no static board data, so the walls-stripping hook is gone."""

    def test_the_hook_no_longer_exists_in_any_refined_module(self):
        for module in (sdk, sd, hn):
            self.assertFalse(hasattr(module, "StripStaticWallsHook"), module.__name__)

    def test_the_prompt_teaches_the_v2_contract(self):
        text = sdk.SYSTEM_PROMPT
        self.assertNotIn("min_enemy_distance", text)
        self.assertIn("closest_threat", text)
        self.assertIn("get_map", text)
        self.assertIn("join_match", text)
        self.assertIn("last_action", text)


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


class LegacyAgentPromptTests(unittest.TestCase):
    """The original `agent/agent-*.py` scripts carry their own copy of the prompt: same v2 fix."""

    def test_no_script_mentions_the_removed_route_field(self):
        for name in ("agent-harness.py", "agent-harness-sdk.py", "agent-strand-decider.py"):
            text = (AGENT_DIR / name).read_text()
            self.assertNotIn("min_enemy_distance", text, name)
        for name in ("agent-harness.py", "agent-harness-sdk.py", "agent-strand-decider.py"):
            text = (AGENT_DIR / name).read_text()
            self.assertIn("closest_threat", text, name)
            self.assertIn("get_map", text, name)


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
