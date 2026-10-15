"""Token seam and MCP connection (spec: mcp-m2m-auth). Invariant D6: tokens == MCP connections."""

import logging

import httpx
import pytest

from ctf_common import auth, mcp
from ctf_common.auth import (
    AgentCoreM2MTokenProvider,
    AuthError,
    PrefetchedTokenProvider,
    StaticTokenProvider,
)
from ctf_common.mcp import McpAuthError, open_mcp_client, raise_on_auth_failure
from fake_mcp import FakeGame, in_memory_transport

SECRET = "super-secret-token-value"


class CountingTokens:
    def __init__(self):
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return f"{SECRET}-{self.calls}"


class FakeIdentity:
    """Stands in for `requires_access_token`: records every decoration and every fetch."""

    def __init__(self, token=SECRET, error=None):
        self.decorations: list[dict] = []
        self.fetches = 0
        self.token = token
        self.error = error

    def __call__(self, **kwargs):
        self.decorations.append(kwargs)

        def decorate(fn):
            def wrapper(*args, **kw):
                self.fetches += 1
                if self.error:
                    raise self.error
                return fn(*args, **{**kw, kwargs["into"]: self.token})

            return wrapper

        return decorate


class TestAgentCoreProvider:
    def provider(self, identity, **kw):
        return AgentCoreM2MTokenProvider("ctf_harness_sdk", ["agentic-ctf/mcp"], decorator=identity, **kw)

    def test_get_token_returns_the_fetched_token(self):
        assert self.provider(FakeIdentity()).get_token() == SECRET

    def test_decorator_is_configured_for_m2m_with_provider_and_scopes(self):
        identity = FakeIdentity()
        self.provider(identity).get_token()
        assert identity.decorations == [
            {
                "provider_name": "ctf_harness_sdk",
                "scopes": ["agentic-ctf/mcp"],
                "auth_flow": "M2M",
                "into": "access_token",
            }
        ]

    def test_every_call_fetches_again_and_nothing_is_cached(self):
        identity = FakeIdentity()
        provider = self.provider(identity)
        provider.get_token()
        provider.get_token()
        assert identity.fetches == 2

    def test_nothing_is_decorated_at_construction_or_import_time(self):
        identity = FakeIdentity()
        self.provider(identity)
        assert identity.decorations == [] and identity.fetches == 0

    def test_fetch_failure_becomes_auth_error_without_leaking_the_token(self):
        identity = FakeIdentity(error=RuntimeError("identity service down"))
        with pytest.raises(AuthError, match="ctf_harness_sdk") as info:
            self.provider(identity).get_token()
        assert SECRET not in str(info.value)

    @pytest.mark.parametrize("token", ["", None, 123])
    def test_empty_or_non_string_token_is_an_auth_error(self, token):
        with pytest.raises(AuthError):
            self.provider(FakeIdentity(token=token)).get_token()

    def test_from_env_reads_provider_and_scopes(self):
        identity = FakeIdentity()
        env = {"CTF_OAUTH_PROVIDER": "ctf_decider_loop", "CTF_OAUTH_SCOPES": "agentic-ctf/mcp other/scope"}
        AgentCoreM2MTokenProvider.from_env(env, decorator=identity).get_token()
        assert identity.decorations[0]["provider_name"] == "ctf_decider_loop"
        assert identity.decorations[0]["scopes"] == ["agentic-ctf/mcp", "other/scope"]

    def test_from_env_default_scope_and_comma_separator(self):
        identity = FakeIdentity()
        AgentCoreM2MTokenProvider.from_env({"CTF_OAUTH_PROVIDER": "p"}, decorator=identity).get_token()
        assert identity.decorations[0]["scopes"] == ["agentic-ctf/mcp"]
        identity = FakeIdentity()
        env = {"CTF_OAUTH_PROVIDER": "p", "CTF_OAUTH_SCOPES": "a/x,b/y"}
        AgentCoreM2MTokenProvider.from_env(env, decorator=identity).get_token()
        assert identity.decorations[0]["scopes"] == ["a/x", "b/y"]

    def test_from_env_without_provider_names_the_variable(self):
        with pytest.raises(AuthError, match="CTF_OAUTH_PROVIDER"):
            AgentCoreM2MTokenProvider.from_env({}, decorator=FakeIdentity())

    def test_default_decorator_is_the_agentcore_identity_one(self):
        from bedrock_agentcore.identity import requires_access_token

        assert AgentCoreM2MTokenProvider("p", ["s"])._decorator is requires_access_token


class TestStaticAndPrefetched:
    def test_static_always_returns_the_same_token(self):
        provider = StaticTokenProvider(SECRET)
        assert provider.get_token() == provider.get_token() == SECRET

    def test_static_rejects_blank_token(self):
        with pytest.raises(AuthError):
            StaticTokenProvider("  ")

    def test_prefetched_returns_the_preflight_token_once_then_delegates(self):
        source = CountingTokens()
        provider = PrefetchedTokenProvider("first", source)
        assert provider.get_token() == "first"
        assert source.calls == 0
        assert provider.get_token() == f"{SECRET}-1"
        assert source.calls == 1

    def test_prefetched_without_fallback_cannot_reconnect(self):
        provider = PrefetchedTokenProvider("first")
        provider.get_token()
        with pytest.raises(AuthError):
            provider.get_token()

    def test_reprs_never_contain_the_token(self):
        for provider in (StaticTokenProvider(SECRET), PrefetchedTokenProvider(SECRET)):
            assert SECRET not in repr(provider) and SECRET not in str(provider)


class TestOneTokenPerConnection:
    """D6: AgentCore Identity has no client-side cache, so the token count must equal the connection count."""

    def make_client(self, tokens, headers_seen=None):
        game = FakeGame(turns=500)

        def connect(url, headers):
            if headers_seen is not None:
                headers_seen.append((url, dict(headers)))
            return in_memory_transport(game.server)

        return game, open_mcp_client("https://mcp.example/mcp", tokens, connect=connect)

    def test_many_tool_calls_cost_one_token(self):
        tokens = CountingTokens()
        game, client = self.make_client(tokens)
        with client:
            names = [t.tool_name for t in client.list_tools_sync()]
            assert {"ctf-game_wait_for_turn", "ctf-game_move"} <= set(names)
            for i in range(200):
                result = client.call_tool_sync(f"id-{i}", "wait_for_turn", {})
                assert result["status"] == "success"
        assert game.tick == 200
        assert tokens.calls == 1

    def test_two_connections_cost_two_tokens(self):
        tokens = CountingTokens()
        _, client = self.make_client(tokens)
        with client:
            client.list_tools_sync()
        with client:  # reconnect
            client.list_tools_sync()
        assert tokens.calls == 2

    def test_creating_the_client_does_not_fetch_a_token(self):
        tokens = CountingTokens()
        self.make_client(tokens)
        assert tokens.calls == 0

    def test_bearer_header_is_sent_with_the_connection_token(self):
        seen = []
        tokens = CountingTokens()
        _, client = self.make_client(tokens, seen)
        with client:
            client.list_tools_sync()
        assert seen == [("https://mcp.example/mcp", {"Authorization": f"Bearer {SECRET}-1"})]

    def test_tool_names_are_prefixed_with_the_server_name(self):
        _, client = self.make_client(CountingTokens())
        with client:
            names = {t.tool_name for t in client.list_tools_sync()}
        assert "ctf-game_wait_for_turn" in names

    def test_no_decorator_outside_the_token_provider(self):
        import inspect

        assert "requires_access_token" not in inspect.getsource(mcp)
        assert inspect.getsource(auth).count("requires_access_token") >= 1


class TestUnauthorizedFailsTheMatch:
    def unauthorized_client(self, tokens):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_token"})

        def client_factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)

        return open_mcp_client("https://mcp.example/mcp", tokens, client_factory=client_factory)

    def test_401_on_connect_becomes_mcp_auth_error_and_requests_no_new_token(self):
        tokens = CountingTokens()
        client = self.unauthorized_client(tokens)
        with pytest.raises(McpAuthError, match="401"):
            with raise_on_auth_failure():
                with client:
                    client.list_tools_sync()
        assert tokens.calls == 1  # no refresh, no retry

    def test_mcp_auth_error_is_an_auth_error(self):
        assert issubclass(McpAuthError, AuthError)

    def test_non_auth_errors_pass_through_untouched(self):
        with pytest.raises(ValueError, match="boom"):
            with raise_on_auth_failure():
                raise ValueError("boom")

    def test_http_500_is_not_an_auth_error(self):
        request = httpx.Request("POST", "https://mcp.example/mcp")
        error = httpx.HTTPStatusError(
            "server error", request=request, response=httpx.Response(500, request=request)
        )
        with pytest.raises(httpx.HTTPStatusError):
            with raise_on_auth_failure():
                raise error

    def test_401_nested_in_exception_group_and_cause_chain_is_found(self):
        request = httpx.Request("POST", "https://mcp.example/mcp")
        inner = httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request))
        wrapped = RuntimeError("client initialization failed")
        wrapped.__cause__ = ExceptionGroup("tg", [ValueError("x"), inner])
        with pytest.raises(McpAuthError):
            with raise_on_auth_failure():
                raise wrapped


class TestNoSecretLogging:
    def test_token_never_appears_in_logs_during_a_connection(self, caplog):
        caplog.set_level(logging.DEBUG)
        tokens = CountingTokens()
        game = FakeGame(turns=2)
        client = open_mcp_client(
            "https://mcp.example/mcp", tokens, connect=lambda url, headers: in_memory_transport(game.server)
        )
        with client:
            client.list_tools_sync()
            client.call_tool_sync("a", "wait_for_turn", {})
        assert SECRET not in caplog.text


class TestDefaultHttpClientFactory:
    """The default factory is ours (no private mcp imports) and carries the bearer header."""

    def test_factory_sets_the_authorization_header_and_mcp_timeouts(self):
        client = mcp.default_http_client(headers={"Authorization": "Bearer abc"})
        assert client.headers["Authorization"] == "Bearer abc"
        assert client.timeout == httpx.Timeout(30.0, read=300.0)

    def test_no_private_mcp_imports(self):
        import inspect

        assert "_httpx_utils" not in inspect.getsource(mcp)

    def test_open_mcp_client_uses_it_by_default(self):
        import inspect

        default = inspect.signature(open_mcp_client).parameters["client_factory"].default
        assert default is mcp.default_http_client
