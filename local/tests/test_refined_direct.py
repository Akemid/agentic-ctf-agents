"""Tests for the `*_direct.py` variants in agent/refined (pure logic: no network, no live match).

The direct variants differ from the originals ONLY in the system prompt (and the default `--name`),
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
for _name in ("decider_loop", "decider_loop_direct", "direct_prompt", "harness", "harness_direct",
              "harness_sdk", "harness_sdk_direct", "strand_decider", "strand_decider_direct"):
    sys.modules.pop(_name, None)

import decider_loop  # noqa: E402
import decider_loop_direct  # noqa: E402
import direct_prompt  # noqa: E402
import harness  # noqa: E402
import harness_direct  # noqa: E402
import harness_sdk  # noqa: E402
import harness_sdk_direct  # noqa: E402
import strand_decider  # noqa: E402
import strand_decider_direct  # noqa: E402

DIRECT_PROMPT = (
    "You are a Capture the Flag game agent. Play with a fast and aggressive tactical "
    "style—make quick strategic decisions to capture the flag and win. Use the MCP connection "
    "to interact with the game state, move swiftly, and respond to opportunities with minimal "
    "deliberation. Be direct in your actions and focus entirely on achieving victory."
)


def run_main(module, *argv, **main_kwargs):
    """Run `module.main` with `argv`, returning kwargs captured from the mocked constructors."""
    with mock.patch.object(sys, "argv", ["prog", *argv]):
        return module.main(**main_kwargs)


class DirectPromptTests(unittest.TestCase):
    def test_constant_is_the_short_generic_prompt(self):
        self.assertEqual(direct_prompt.DIRECT_SYSTEM_PROMPT, DIRECT_PROMPT)

    def test_no_agent_name_is_baked_in(self):
        self.assertNotIn("demo-paraguay", direct_prompt.DIRECT_SYSTEM_PROMPT)
        self.assertNotIn("AmazingPlayer", direct_prompt.DIRECT_SYSTEM_PROMPT)

    def test_it_is_much_shorter_than_the_refined_prompt(self):
        self.assertLess(len(direct_prompt.DIRECT_SYSTEM_PROMPT.split()), 80)
        self.assertGreater(len(harness_sdk.SYSTEM_PROMPT.split()), 250)


class OriginalPromptsUnchangedTests(unittest.TestCase):
    def test_refined_prompts_still_use_the_long_prompt(self):
        self.assertTrue(harness_sdk.SYSTEM_PROMPT.startswith("You are AmazingPlayer"))
        self.assertTrue(harness_sdk.SYSTEM_PROMPT.endswith(harness_sdk.ACT_FAST_PROMPT))
        self.assertTrue(decider_loop.SYSTEM_PROMPT.startswith("You are AmazingPlayer"))
        self.assertIs(harness.SYSTEM_PROMPT, harness_sdk.SYSTEM_PROMPT)
        self.assertIs(strand_decider.SYSTEM_PROMPT, harness_sdk.SYSTEM_PROMPT)

    def test_original_default_names_are_unchanged(self):
        for module, name in ((harness, "refined-harness"), (harness_sdk, "refined-sdk"),
                             (strand_decider, "refined-strands-decider"),
                             (decider_loop, "refined-decider")):
            with mock.patch.object(sys, "argv", ["prog"]):
                self.assertEqual(module.parse_args().name, name, module.__name__)


class DefaultNameTests(unittest.TestCase):
    def test_direct_variants_have_distinct_default_names(self):
        expected = {
            harness_direct: "refined-direct-harness",
            harness_sdk_direct: "refined-direct-sdk",
            strand_decider_direct: "refined-direct-strands-decider",
            decider_loop_direct: "refined-direct-decider",
        }
        for module, name in expected.items():
            with mock.patch.object(sys, "argv", ["prog"]):
                self.assertEqual(module.parse_args().name, name, module.__name__)


class HarnessDirectTests(unittest.TestCase):
    def captured(self, module, *argv, target=harness):
        with mock.patch.object(target, "create_harness") as create, \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            module.main()
        create.assert_called_once()
        agent = create.return_value
        agent.assert_called_once()
        return create.call_args.kwargs, agent.call_args.args

    def test_only_name_and_instructions_differ(self):
        original, original_call = self.captured(harness, "--name", "x")
        direct, direct_call = self.captured(harness_direct, "--name", "x")
        self.assertEqual(direct.pop("instructions"), DIRECT_PROMPT)
        self.assertEqual(original.pop("instructions"), harness_sdk.SYSTEM_PROMPT)
        self.assertEqual(direct, original)
        self.assertEqual(direct_call, original_call)


class HarnessSdkDirectTests(unittest.TestCase):
    def captured(self, module, *argv, target=harness_sdk):
        with mock.patch.object(target, "Agent") as agent_cls, \
                mock.patch.object(target, "MCPClient") as mcp, \
                mock.patch.object(target, "make_conversation_manager",
                                  side_effect=lambda window: ("window", window)), \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            mcp.load_servers.return_value = "clients"
            module.main()
        agent_cls.assert_called_once()
        return agent_cls.call_args.kwargs, agent_cls.return_value.call_args.args, mcp

    def test_only_the_system_prompt_differs(self):
        original, original_call, _ = self.captured(harness_sdk, "--name", "x", "--window", "8")
        direct, direct_call, _ = self.captured(harness_sdk_direct, "--name", "x", "--window", "8")
        self.assertEqual(direct.pop("system_prompt"), DIRECT_PROMPT)
        self.assertEqual(original.pop("system_prompt"), harness_sdk.SYSTEM_PROMPT)
        self.assertEqual(direct, original)
        self.assertEqual(direct_call, original_call)
        self.assertEqual(original["conversation_manager"], ("window", 8))

    def test_window_and_model_defaults_are_inherited(self):
        direct, _, _ = self.captured(harness_sdk_direct)
        self.assertEqual(direct["conversation_manager"], ("window", harness_sdk.DEFAULT_WINDOW))
        self.assertEqual(direct["model"], harness_sdk.MODELS["haiku"])


class StrandDeciderDirectTests(unittest.TestCase):
    def captured(self, module, *argv, target=strand_decider):
        with mock.patch.object(target, "Agent") as agent_cls, \
                mock.patch.object(target, "MCPClient") as mcp, \
                mock.patch.object(target, "Decider") as decider_cls, \
                mock.patch.object(target, "make_conversation_manager",
                                  side_effect=lambda window: ("window", window)), \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            mcp.load_servers.return_value = "clients"
            decider_cls.return_value.banner.return_value = "banner"
            module.main()
        agent_cls.assert_called_once()
        kwargs = dict(agent_cls.call_args.kwargs)
        kwargs["interventions"] = [type(handler) for handler in kwargs["interventions"]]
        return kwargs, agent_cls.return_value.call_args.args

    def test_only_the_system_prompt_differs(self):
        original, original_call = self.captured(strand_decider, "--name", "x")
        direct, direct_call = self.captured(strand_decider_direct, "--name", "x")
        self.assertEqual(direct.pop("system_prompt"), DIRECT_PROMPT)
        self.assertEqual(original.pop("system_prompt"), harness_sdk.SYSTEM_PROMPT)
        self.assertEqual(direct, original)
        self.assertEqual(direct_call, original_call)
        self.assertEqual(len(original["interventions"]), 1)


class DeciderLoopDirectTests(unittest.TestCase):
    def test_agent_factory_uses_the_given_prompt(self):
        with mock.patch.object(decider_loop, "Agent") as agent_cls:
            factory = decider_loop.build_agent_factory("haiku-id", ["tool"], "PROMPT")
            factory(None)
        kwargs = agent_cls.call_args.kwargs
        self.assertEqual(kwargs["system_prompt"], "PROMPT")
        self.assertEqual(kwargs["model"], "haiku-id")
        self.assertEqual(kwargs["tools"], ["tool"])
        self.assertEqual(kwargs["interventions"], [])

    def captured_play_args(self, module, *argv):
        with mock.patch.object(decider_loop, "MCPClient"), \
                mock.patch.object(decider_loop, "with_client",
                                  side_effect=lambda client, body: body()), \
                mock.patch.object(decider_loop, "play") as play, \
                mock.patch.object(decider_loop, "Decider"), \
                mock.patch.object(sys, "argv", ["prog", *argv]):
            play.return_value.summary.return_value = "stats"
            module.main()
        return play.call_args

    def test_play_receives_the_prompt(self):
        original = self.captured_play_args(decider_loop, "--llm-only", "--name", "x")
        direct = self.captured_play_args(decider_loop_direct, "--llm-only", "--name", "x")
        self.assertEqual(original.kwargs["system_prompt"], decider_loop.SYSTEM_PROMPT)
        self.assertEqual(direct.kwargs["system_prompt"], DIRECT_PROMPT)
        self.assertEqual(vars(direct.args[2]), vars(original.args[2]))

    def test_cli_options_are_inherited(self):
        with mock.patch.object(sys, "argv", ["prog"]):
            direct, original = decider_loop_direct.parse_args(), decider_loop.parse_args()
        direct_vars, original_vars = vars(direct), vars(original)
        self.assertEqual(direct_vars.pop("name"), "refined-direct-decider")
        self.assertEqual(original_vars.pop("name"), "refined-decider")
        self.assertEqual(direct_vars, original_vars)


class ModulesAreThinTests(unittest.TestCase):
    def test_direct_modules_define_no_prompt_of_their_own(self):
        for module in (harness_direct, harness_sdk_direct, strand_decider_direct, decider_loop_direct):
            self.assertIs(module.DIRECT_SYSTEM_PROMPT, direct_prompt.DIRECT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
