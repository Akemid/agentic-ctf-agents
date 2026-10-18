"""Tests for the `--prompt direct` flag of the refined agents (pure logic: no network, no live match).

`--prompt direct` differs from the default refined run ONLY in the system prompt (and the default `--name`),
so the experiment measures the prompt's effect alone. unittest-style so they run under
`python -m unittest discover -s tests` (pytest is not installed).

`strands` / `strands_harness` are replaced by light stubs when they are not importable.
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "agent"
REFINED_DIR = AGENT_DIR / "refined"
for path in (REFINED_DIR, AGENT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _stub(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


def install_strands_stubs() -> None:
    """Register minimal fakes for the third-party modules the agents import, only if missing."""
    try:
        import strands  # noqa: F401
        import strands_harness  # noqa: F401
        return
    except ImportError:
        pass

    class Anything:
        def __init__(self, *args, **kwargs) -> None:
            self.args, self.kwargs = args, kwargs

    _stub("strands", Agent=Anything)
    _stub("strands.agent")
    _stub("strands.agent.conversation_manager", SlidingWindowConversationManager=Anything)
    _stub("strands.hooks", AfterToolCallEvent=Anything, AfterToolsEvent=Anything)
    _stub("strands.interventions", Deny=Anything, Guide=Anything, Proceed=Anything,
          InterventionHandler=Anything)
    _stub("strands.tools")
    _stub("strands.tools.mcp", MCPClient=Anything)
    _stub("strands_harness", create_harness=Anything)


install_strands_stubs()

# Other test modules load these by name; drop any stale (possibly half-initialised) copies.
for _name in ("decider_loop", "harness", "harness_sdk", "prompts", "strand_decider"):
    sys.modules.pop(_name, None)

import decider_loop  # noqa: E402
import harness  # noqa: E402
import harness_sdk  # noqa: E402
import prompts  # noqa: E402
import strand_decider  # noqa: E402

DIRECT_PROMPT = (
    "You are a Capture the Flag game agent. Play with a fast and aggressive tactical "
    "style—make quick strategic decisions to capture the flag and win. Use the MCP connection "
    "to interact with the game state, move swiftly, and respond to opportunities with minimal "
    "deliberation. Be direct in your actions and focus entirely on achieving victory."
)

DIRECT_NAMES = {
    harness: "refined-direct-harness",
    harness_sdk: "refined-direct-sdk",
    strand_decider: "refined-direct-strands-decider",
    decider_loop: "refined-direct-decider",
}
REFINED_NAMES = {
    harness: "refined-harness",
    harness_sdk: "refined-sdk",
    strand_decider: "refined-strands-decider",
    decider_loop: "refined-decider",
}


def parsed(module, *argv):
    with mock.patch.object(sys, "argv", ["prog", *argv]):
        return module.parse_args()


class DirectPromptTests(unittest.TestCase):
    def test_constant_is_the_short_generic_prompt(self):
        self.assertEqual(prompts.DIRECT_SYSTEM_PROMPT, DIRECT_PROMPT)

    def test_no_agent_name_is_baked_in(self):
        self.assertNotIn("demo-paraguay", prompts.DIRECT_SYSTEM_PROMPT)
        self.assertNotIn("AmazingPlayer", prompts.DIRECT_SYSTEM_PROMPT)

    def test_it_is_much_shorter_than_the_refined_prompt(self):
        self.assertLess(len(prompts.DIRECT_SYSTEM_PROMPT.split()), 80)
        self.assertGreater(len(prompts.SDK_SYSTEM_PROMPT.split()), 250)


class PromptsModuleIsTheSingleSourceTests(unittest.TestCase):
    def test_agents_use_the_constants_from_prompts(self):
        self.assertIs(harness_sdk.SYSTEM_PROMPT, prompts.SDK_SYSTEM_PROMPT)
        self.assertIs(harness.SYSTEM_PROMPT, prompts.SDK_SYSTEM_PROMPT)
        self.assertIs(strand_decider.SYSTEM_PROMPT, prompts.SDK_SYSTEM_PROMPT)
        self.assertIs(decider_loop.SYSTEM_PROMPT, prompts.DECIDER_LOOP_SYSTEM_PROMPT)
        self.assertIs(decider_loop.DECIDER_INSTRUCTIONS, prompts.DECIDER_INSTRUCTIONS)
        self.assertIs(decider_loop.LLM_PLAN_NOTE, prompts.LLM_PLAN_NOTE)

    def test_refined_prompts_keep_their_shape(self):
        self.assertTrue(prompts.SDK_SYSTEM_PROMPT.startswith("You are AmazingPlayer"))
        self.assertTrue(prompts.SDK_SYSTEM_PROMPT.endswith(prompts.ACT_FAST_PROMPT))
        self.assertTrue(prompts.DECIDER_LOOP_SYSTEM_PROMPT.startswith("You are AmazingPlayer"))

    def test_agent_modules_define_no_prompt_text_of_their_own(self):
        for module in (harness, harness_sdk, strand_decider, decider_loop):
            source = Path(module.__file__).read_text()
            self.assertNotIn("You are AmazingPlayer", source, module.__name__)

    def test_removed_wrappers_and_prompt_module_are_gone(self):
        for name in ("direct_prompt", "harness_direct", "harness_sdk_direct",
                     "strand_decider_direct", "decider_loop_direct"):
            self.assertFalse((REFINED_DIR / f"{name}.py").exists(), name)


class PromptFlagTests(unittest.TestCase):
    def test_default_is_refined_with_the_refined_name(self):
        for module, name in REFINED_NAMES.items():
            args = parsed(module)
            self.assertEqual((args.prompt, args.name), ("refined", name), module.__name__)

    def test_direct_uses_the_direct_default_name(self):
        for module, name in DIRECT_NAMES.items():
            args = parsed(module, "--prompt", "direct")
            self.assertEqual((args.prompt, args.name), ("direct", name), module.__name__)

    def test_explicit_name_wins_in_both_modes(self):
        for module in DIRECT_NAMES:
            for mode in ("refined", "direct"):
                self.assertEqual(parsed(module, "--prompt", mode, "--name", "x").name, "x")

    def test_unknown_prompt_is_rejected(self):
        for module in DIRECT_NAMES:
            with mock.patch.object(sys, "argv", ["prog", "--prompt", "bogus"]), \
                    mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                module.parse_args()


class HarnessPromptFlagTests(unittest.TestCase):
    def captured(self, *argv):
        with mock.patch.object(harness, "create_harness") as create, \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            harness.main()
        create.assert_called_once()
        agent = create.return_value
        agent.assert_called_once()
        return dict(create.call_args.kwargs), agent.call_args.args

    def test_only_instructions_differ(self):
        refined, refined_call = self.captured("--name", "x")
        direct, direct_call = self.captured("--name", "x", "--prompt", "direct")
        self.assertEqual(direct.pop("instructions"), DIRECT_PROMPT)
        self.assertEqual(refined.pop("instructions"), prompts.SDK_SYSTEM_PROMPT)
        self.assertEqual(direct, refined)
        self.assertEqual(direct_call, refined_call)

    def test_direct_default_name_is_used_to_create_the_harness(self):
        direct, _ = self.captured("--prompt", "direct")
        self.assertEqual(direct["name"], "refined-direct-harness")


class HarnessSdkPromptFlagTests(unittest.TestCase):
    def captured(self, *argv):
        with mock.patch.object(harness_sdk, "Agent") as agent_cls, \
                mock.patch.object(harness_sdk, "MCPClient") as mcp, \
                mock.patch.object(harness_sdk, "make_conversation_manager",
                                  side_effect=lambda window: ("window", window)), \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            mcp.load_servers.return_value = "clients"
            harness_sdk.main()
        agent_cls.assert_called_once()
        return dict(agent_cls.call_args.kwargs), agent_cls.return_value.call_args.args

    def test_only_the_system_prompt_differs(self):
        refined, refined_call = self.captured("--name", "x", "--window", "8")
        direct, direct_call = self.captured("--name", "x", "--window", "8", "--prompt", "direct")
        self.assertEqual(direct.pop("system_prompt"), DIRECT_PROMPT)
        self.assertEqual(refined.pop("system_prompt"), prompts.SDK_SYSTEM_PROMPT)
        self.assertEqual(direct, refined)
        self.assertEqual(direct_call, refined_call)
        self.assertEqual(refined["conversation_manager"], ("window", 8))

    def test_window_and_model_defaults_are_inherited(self):
        direct, _ = self.captured("--prompt", "direct")
        self.assertEqual(direct["conversation_manager"], ("window", harness_sdk.DEFAULT_WINDOW))
        self.assertEqual(direct["model"], harness_sdk.MODELS["haiku"])
        self.assertEqual(direct["name"], "refined-direct-sdk")


class StrandDeciderPromptFlagTests(unittest.TestCase):
    def captured(self, *argv):
        with mock.patch.object(strand_decider, "Agent") as agent_cls, \
                mock.patch.object(strand_decider, "MCPClient") as mcp, \
                mock.patch.object(strand_decider, "Decider") as decider_cls, \
                mock.patch.object(strand_decider, "make_conversation_manager",
                                  side_effect=lambda window: ("window", window)), \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            mcp.load_servers.return_value = "clients"
            decider_cls.return_value.banner.return_value = "banner"
            strand_decider.main()
        agent_cls.assert_called_once()
        kwargs = dict(agent_cls.call_args.kwargs)
        kwargs["interventions"] = [type(handler) for handler in kwargs["interventions"]]
        return kwargs, agent_cls.return_value.call_args.args

    def test_only_the_system_prompt_differs(self):
        refined, refined_call = self.captured("--name", "x")
        direct, direct_call = self.captured("--name", "x", "--prompt", "direct")
        self.assertEqual(direct.pop("system_prompt"), DIRECT_PROMPT)
        self.assertEqual(refined.pop("system_prompt"), prompts.SDK_SYSTEM_PROMPT)
        self.assertEqual(direct, refined)
        self.assertEqual(direct_call, refined_call)
        self.assertEqual(len(refined["interventions"]), 1)


class DeciderLoopPromptFlagTests(unittest.TestCase):
    def test_agent_factory_uses_the_given_prompt(self):
        with mock.patch.object(decider_loop, "Agent") as agent_cls:
            factory = decider_loop.build_agent_factory("haiku-id", ["tool"], "PROMPT")
            factory(None)
        kwargs = agent_cls.call_args.kwargs
        self.assertEqual(kwargs["system_prompt"], "PROMPT")
        self.assertEqual(kwargs["model"], "haiku-id")
        self.assertEqual(kwargs["tools"], ["tool"])
        self.assertEqual(kwargs["interventions"], [])

    def captured_play_args(self, *argv):
        with mock.patch.object(decider_loop, "MCPClient"), \
                mock.patch.object(decider_loop, "with_client",
                                  side_effect=lambda client, body: body()), \
                mock.patch.object(decider_loop, "play") as play, \
                mock.patch.object(decider_loop, "Decider"), \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            play.return_value.summary.return_value = "stats"
            decider_loop.main()
        return play.call_args

    def test_play_receives_the_prompt(self):
        refined = self.captured_play_args("--llm-only", "--name", "x")
        direct = self.captured_play_args("--llm-only", "--name", "x", "--prompt", "direct")
        self.assertEqual(refined.kwargs["system_prompt"], prompts.DECIDER_LOOP_SYSTEM_PROMPT)
        self.assertEqual(direct.kwargs["system_prompt"], DIRECT_PROMPT)
        direct_args, refined_args = vars(direct.args[2]), vars(refined.args[2])
        self.assertEqual(direct_args.pop("prompt"), "direct")
        self.assertEqual(refined_args.pop("prompt"), "refined")
        self.assertEqual(direct_args, refined_args)

    def test_cli_options_are_inherited(self):
        direct, refined = vars(parsed(decider_loop, "--prompt", "direct")), vars(parsed(decider_loop))
        self.assertEqual(direct.pop("name"), "refined-direct-decider")
        self.assertEqual(refined.pop("name"), "refined-decider")
        self.assertEqual(direct.pop("prompt"), "direct")
        self.assertEqual(refined.pop("prompt"), "refined")
        self.assertEqual(direct, refined)


if __name__ == "__main__":
    unittest.main()
