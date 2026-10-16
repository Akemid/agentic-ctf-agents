"""Runner wiring shared by every runtime: preflight = config from SSM (or local overrides) + one token."""

import pytest

from ctf_common.auth import StaticTokenProvider
from ctf_common.payload import Action, MatchRequest
from ctf_common.wiring import build_runner, ssm_needed


class App:
    def __init__(self):
        self.added = []

    def add_async_task(self, name, metadata=None):
        self.added.append((name, metadata))
        return 1

    def complete_async_task(self, task_id):
        return True


class FakeSsm:
    def __init__(self, values):
        self.values = values
        self.asked = []

    def get_parameter(self, Name):
        self.asked.append(Name)
        return {"Parameter": {"Value": self.values[Name]}}


def request(match_id="m1"):
    return MatchRequest(
        action=Action.START, match_id=match_id, name="p1", team="auto", model="haiku", options={}
    )


def recording_play(seen):
    """A play that records its context and takes a seat, like every runtime must."""

    def play(ctx):
        seen.append(ctx)
        ctx.join.succeed("red", 0)

    return play


def run(runner):
    resp = runner.start(request())
    assert runner.wait(10)
    return resp


class TestSsmNeeded:
    def test_a_local_mcp_url_skips_ssm_for_runtimes_without_a_decider(self):
        assert ssm_needed({"CTF_MCP_URL": "http://m"}, needs_decider=False) is False

    def test_a_decider_runtime_still_needs_ssm_for_the_endpoint_name(self):
        assert ssm_needed({"CTF_MCP_URL": "http://m"}, needs_decider=True) is True

    def test_both_local_overrides_skip_ssm_for_a_decider_runtime(self):
        env = {"CTF_MCP_URL": "http://m", "CTF_DECIDER_URL": "http://d"}
        assert ssm_needed(env, needs_decider=True) is False

    def test_no_override_needs_ssm(self):
        assert ssm_needed({}, needs_decider=False) is True


class TestBuildRunner:
    def test_nothing_touches_aws_until_start(self):
        calls = []
        build_runner(
            App(),
            lambda ctx: None,
            runtime="x",
            env={},
            token_provider=StaticTokenProvider("t"),
            ssm_client_factory=lambda: calls.append(1),
        )
        assert calls == []

    def test_the_default_ssm_factory_is_resolved_at_call_time(self, monkeypatch):
        import ctf_common.config as config

        ssm = FakeSsm({"/agentic-ctf/mcp-url": "https://mcp.example/mcp"})
        monkeypatch.setattr(config, "default_ssm_client", lambda: ssm)  # patched after import
        seen = []
        runner = build_runner(
            App(), recording_play(seen), runtime="x", env={}, token_provider=StaticTokenProvider("t")
        )
        assert run(runner)["status"] == "started"
        assert seen[0].config.mcp_url == "https://mcp.example/mcp"

    def test_config_comes_from_ssm_and_the_play_sees_it(self):
        seen = []
        ssm = FakeSsm(
            {"/agentic-ctf/mcp-url": "https://mcp.example/mcp", "/ctf-decider/endpoint-name": "ctf-decider"}
        )
        env = {"CTF_DECIDER_ENDPOINT_PARAM": "/ctf-decider/endpoint-name"}
        runner = build_runner(
            App(),
            recording_play(seen),
            runtime="x",
            env=env,
            token_provider=StaticTokenProvider("t"),
            ssm_client=ssm,
            needs_decider=True,
        )
        assert run(runner)["status"] == "started"
        assert seen[0].config.mcp_url == "https://mcp.example/mcp"
        assert seen[0].config.decider_endpoint_name == "ctf-decider"
        assert ssm.asked == ["/agentic-ctf/mcp-url", "/ctf-decider/endpoint-name"]

    def test_decider_runtime_without_the_endpoint_param_fails_the_start_naming_the_variable(self):
        runner = build_runner(
            App(),
            lambda ctx: None,
            runtime="x",
            env={"CTF_MCP_URL": "http://m"},
            token_provider=StaticTokenProvider("t"),
            ssm_client=FakeSsm({}),
            needs_decider=True,
        )
        resp = runner.start(request())
        assert resp["status"] == "failed" and "CTF_DECIDER_ENDPOINT_PARAM" in resp["error"]

    def test_local_overrides_work_without_any_ssm_client(self):
        seen = []
        env = {"CTF_MCP_URL": "http://m/mcp", "CTF_DECIDER_URL": "http://127.0.0.1:8099"}
        runner = build_runner(
            App(),
            recording_play(seen),
            runtime="x",
            env=env,
            token_provider=StaticTokenProvider("t"),
            ssm_client_factory=lambda: pytest.fail("SSM must not be used with local overrides"),
            needs_decider=True,
        )
        assert run(runner)["status"] == "started"
        assert seen[0].config.decider_url == "http://127.0.0.1:8099"

    def test_the_join_timeout_is_a_runner_setting_with_a_bounded_default(self):
        from ctf_common.join import JOIN_TIMEOUT_S

        default = build_runner(App(), lambda ctx: None, runtime="x", env={})
        custom = build_runner(App(), lambda ctx: None, runtime="x", env={}, join_timeout_s=2.5)
        assert default._join_timeout_s == JOIN_TIMEOUT_S == 15.0  # noqa: SLF001
        assert custom._join_timeout_s == 2.5  # noqa: SLF001

    def test_the_token_provider_comes_from_the_environment_when_not_injected(self):
        runner = build_runner(App(), lambda ctx: None, runtime="x", env={"CTF_MCP_URL": "http://m"})
        resp = runner.start(request())
        assert resp["status"] == "failed" and "CTF_OAUTH_PROVIDER" in resp["error"]

    def test_the_preflight_token_is_handed_to_the_play_once(self):
        seen = []
        runner = build_runner(
            App(),
            recording_play(seen),
            runtime="x",
            env={"CTF_MCP_URL": "http://m"},
            token_provider=StaticTokenProvider("tok-1"),
        )
        run(runner)
        assert seen[0].tokens.get_token() == "tok-1"
