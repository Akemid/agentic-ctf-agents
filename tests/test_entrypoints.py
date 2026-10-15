"""The three ported runtimes' AgentCore entrypoints: thin, sync, payload-validating."""

import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
RUNTIMES = ("harness", "strand_decider", "decider_loop")
SESSION = SimpleNamespace(session_id="session-0123456789-0123456789-0123456789-abc123")


def load_main(runtime):
    spec = importlib.util.spec_from_file_location(f"{runtime}_main", APP / runtime / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=RUNTIMES)
def main(request):
    module = load_main(request.param)
    module.runtime_name = request.param
    return module


class TestEntrypoint:
    def test_entrypoint_is_a_plain_sync_function(self, main):
        assert not inspect.iscoroutinefunction(main.invoke)
        assert not inspect.isasyncgenfunction(main.invoke)

    def test_status_on_a_fresh_session_is_idle(self, main):
        assert main.invoke({"action": "status"}, SESSION) == {"status": "idle"}

    def test_stop_on_a_fresh_session_is_idle(self, main):
        assert main.invoke({"action": "stop"}, SESSION) == {"status": "idle"}

    def test_caller_supplied_history_is_rejected(self, main):
        resp = main.invoke({"match_id": "m1", "messages": [{"role": "user", "content": []}]}, SESSION)
        assert resp["status"] == "rejected" and "messages" in resp["error"]

    def test_non_string_prompt_is_rejected(self, main):
        assert main.invoke({"prompt": 42}, SESSION)["status"] == "rejected"

    def test_missing_context_does_not_crash(self, main):
        assert main.invoke({"action": "status"}, None) == {"status": "idle"}

    def test_the_runtime_name_is_the_directory_name(self, main):
        assert main.RUNTIME == main.runtime_name

    def test_an_unknown_option_is_rejected_before_any_work(self, main):
        resp = main.invoke({"match_id": "m1", "options": {"bogus": 1}}, SESSION)
        assert resp["status"] == "rejected" and "bogus" in resp["error"]


class TestOptionsPerRuntime:
    @pytest.mark.parametrize(
        "runtime,option",
        [
            ("harness", "window"),
            ("strand_decider", "threshold"),
            ("strand_decider", "llm_budget_ms"),
            ("decider_loop", "window"),
        ],
    )
    def test_options_a_runtime_does_not_understand_are_rejected(self, runtime, option):
        resp = load_main(runtime).invoke({"match_id": "m1", "options": {option: 1}}, SESSION)
        assert resp["status"] == "rejected" and option in resp["error"]

    @pytest.mark.parametrize(
        "runtime,option,value",
        [
            ("strand_decider", "window", 8),
            ("decider_loop", "threshold", 0.9),
            ("decider_loop", "threat_threshold", 0.6),
            ("decider_loop", "llm_budget_ms", 3000),
            ("decider_loop", "safety_margin_ms", 200),
        ],
    )
    def test_supported_options_pass_validation_and_reach_the_preflight(self, runtime, option, value):
        """A valid start gets past payload validation: the failure (no AWS here) comes from the preflight."""
        resp = load_main(runtime).invoke({"match_id": "m1", "options": {option: value}}, SESSION)
        assert resp["status"] == "failed"  # preflight needs CTF_OAUTH_PROVIDER / SSM
