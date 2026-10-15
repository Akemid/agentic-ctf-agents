"""Runtime configuration from SSM at preflight (spec: ctf-agent-runtimes)."""

import boto3
import pytest
from botocore.stub import Stubber

from ctf_common.config import ConfigError, RuntimeConfig, load_config

MCP_PARAM = "/agentic-ctf/mcp-url"
DECIDER_PARAM = "/ctf-decider/endpoint-name"


@pytest.fixture
def ssm():
    client = boto3.client("ssm", region_name="us-east-2", aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(client) as stubber:
        client.stubber = stubber  # type: ignore[attr-defined]
        yield client


def ok(name, value):
    return "get_parameter", {"Parameter": {"Name": name, "Value": value}}, {"Name": name}


def missing(name):
    return (
        "get_parameter",
        {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
        {"Name": name},
        True,
    )


def queue(ssm, *responses):
    for resp in responses:
        if len(resp) == 4:
            method, err, params, _ = resp
            ssm.stubber.add_client_error(
                method,
                service_error_code=err["Error"]["Code"],
                service_message=err["Error"]["Message"],
                expected_params=params,
            )
        else:
            method, out, params = resp
            ssm.stubber.add_response(method, out, params)


ENV = {"CTF_MCP_URL_PARAM": MCP_PARAM}
DECIDER_ENV = {**ENV, "CTF_DECIDER_ENDPOINT_PARAM": DECIDER_PARAM}


class TestLoadFromSsm:
    def test_mcp_url_is_read_from_the_parameter_named_in_env(self, ssm):
        queue(ssm, ok(MCP_PARAM, "https://game.example/mcp"))
        cfg = load_config(ssm, ENV)
        assert cfg == RuntimeConfig(mcp_url="https://game.example/mcp")
        ssm.stubber.assert_no_pending_responses()

    def test_a_different_parameter_name_is_honored(self, ssm):
        queue(ssm, ok("/other/url", "https://other.example/mcp"))
        assert load_config(ssm, {"CTF_MCP_URL_PARAM": "/other/url"}).mcp_url == "https://other.example/mcp"

    def test_parameter_name_defaults_when_env_is_unset(self, ssm):
        queue(ssm, ok(MCP_PARAM, "https://game.example/mcp"))
        assert load_config(ssm, {}).mcp_url == "https://game.example/mcp"

    def test_decider_runtimes_also_read_the_endpoint_name(self, ssm):
        queue(ssm, ok(MCP_PARAM, "https://game.example/mcp"), ok(DECIDER_PARAM, "ctf-decider"))
        cfg = load_config(ssm, DECIDER_ENV, needs_decider=True)
        assert cfg.mcp_url == "https://game.example/mcp"
        assert cfg.decider_endpoint_name == "ctf-decider"

    def test_decider_is_not_read_when_not_needed(self, ssm):
        queue(ssm, ok(MCP_PARAM, "https://game.example/mcp"))
        cfg = load_config(ssm, DECIDER_ENV, needs_decider=False)
        assert cfg.decider_endpoint_name is None
        ssm.stubber.assert_no_pending_responses()

    def test_values_are_stripped(self, ssm):
        queue(ssm, ok(MCP_PARAM, "  https://game.example/mcp\n"))
        assert load_config(ssm, ENV).mcp_url == "https://game.example/mcp"


class TestErrorsNameTheParameter:
    def test_missing_mcp_url_names_the_parameter(self, ssm):
        queue(ssm, missing(MCP_PARAM))
        with pytest.raises(ConfigError, match="/agentic-ctf/mcp-url"):
            load_config(ssm, ENV)

    def test_missing_decider_parameter_names_it(self, ssm):
        queue(ssm, ok(MCP_PARAM, "https://game.example/mcp"), missing(DECIDER_PARAM))
        with pytest.raises(ConfigError, match="/ctf-decider/endpoint-name"):
            load_config(ssm, DECIDER_ENV, needs_decider=True)

    def test_access_denied_also_names_the_parameter_and_code(self, ssm):
        ssm.stubber.add_client_error(
            "get_parameter", service_error_code="AccessDeniedException", expected_params={"Name": MCP_PARAM}
        )
        with pytest.raises(ConfigError, match=r"/agentic-ctf/mcp-url.*AccessDeniedException"):
            load_config(ssm, ENV)

    def test_blank_value_is_an_error(self, ssm):
        queue(ssm, ok(MCP_PARAM, "   "))
        with pytest.raises(ConfigError, match="/agentic-ctf/mcp-url"):
            load_config(ssm, ENV)

    def test_non_http_url_is_an_error(self, ssm):
        queue(ssm, ok(MCP_PARAM, "ftp://game.example/mcp"))
        with pytest.raises(ConfigError, match="http"):
            load_config(ssm, ENV)

    def test_missing_decider_param_name_is_a_config_error_naming_the_env_var(self, ssm):
        queue(ssm, ok(MCP_PARAM, "https://game.example/mcp"))
        with pytest.raises(ConfigError, match="CTF_DECIDER_ENDPOINT_PARAM"):
            load_config(ssm, ENV, needs_decider=True)

    def test_no_ssm_client_and_no_override_is_an_error(self):
        with pytest.raises(ConfigError, match="/agentic-ctf/mcp-url"):
            load_config(None, ENV)


class TestLocalOverrides:
    def test_mcp_override_skips_ssm_entirely(self, ssm):
        cfg = load_config(ssm, {"CTF_MCP_URL": "http://localhost:8080/mcp"})
        assert cfg.mcp_url == "http://localhost:8080/mcp"
        ssm.stubber.assert_no_pending_responses()  # nothing queued, nothing called

    def test_override_works_without_any_ssm_client(self):
        assert load_config(None, {"CTF_MCP_URL": "http://localhost:8080/mcp"}).mcp_url == (
            "http://localhost:8080/mcp"
        )

    def test_decider_url_override_for_local_runs(self):
        cfg = load_config(
            None,
            {"CTF_MCP_URL": "http://localhost:8080/mcp", "CTF_DECIDER_URL": "http://127.0.0.1:8099"},
            needs_decider=True,
        )
        assert cfg.decider_url == "http://127.0.0.1:8099"
        assert cfg.decider_endpoint_name is None

    def test_mcp_override_must_still_be_http(self):
        with pytest.raises(ConfigError, match="CTF_MCP_URL"):
            load_config(None, {"CTF_MCP_URL": "localhost:8080"})

    def test_mixed_override_and_ssm(self, ssm):
        queue(ssm, ok(DECIDER_PARAM, "ctf-decider"))
        cfg = load_config(
            ssm,
            {"CTF_MCP_URL": "http://localhost:8080/mcp", "CTF_DECIDER_ENDPOINT_PARAM": DECIDER_PARAM},
            needs_decider=True,
        )
        assert (cfg.mcp_url, cfg.decider_endpoint_name) == ("http://localhost:8080/mcp", "ctf-decider")
