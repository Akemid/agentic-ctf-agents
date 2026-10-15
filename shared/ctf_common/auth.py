"""Token seam (design A3, spec mcp-m2m-auth).

`TokenProvider.get_token()` is called once per MCP connection open, by the transport factory
in `mcp.py`. AgentCore Identity has no client-side cache (D6), so every call is one
`GetResourceOauth2Token`. The `requires_access_token` decorator is applied inside `get_token`
(not at import time): it builds its identity client at decoration time and needs the runtime
environment.

Tokens are bearer secrets: they are never logged, never put in error messages, and the
providers hide them from `repr`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

DEFAULT_SCOPES = ("agentic-ctf/mcp",)
ENV_OAUTH_PROVIDER = "CTF_OAUTH_PROVIDER"
ENV_OAUTH_SCOPES = "CTF_OAUTH_SCOPES"


class AuthError(RuntimeError):
    """Authentication with the MCP server failed (token fetch or rejected bearer)."""


class TokenProvider(Protocol):
    def get_token(self) -> str: ...


def _checked(token: Any, source: str) -> str:
    if not isinstance(token, str) or not token.strip():
        raise AuthError(f"{source} returned an empty or non-string token")
    return token


class AgentCoreM2MTokenProvider:
    """Fetches an OAuth M2M access token from AgentCore Identity (one call = one fetch)."""

    def __init__(
        self,
        provider_name: str,
        scopes: Sequence[str],
        *,
        decorator: Callable[..., Callable] | None = None,
    ) -> None:
        if decorator is None:
            from bedrock_agentcore.identity import requires_access_token  # noqa: PLC0415

            decorator = requires_access_token
        self._provider = provider_name
        self._scopes = list(scopes)
        self._decorator = decorator

    @classmethod
    def from_env(
        cls, env: Mapping[str, str], *, decorator: Callable[..., Callable] | None = None
    ) -> AgentCoreM2MTokenProvider:
        provider = env.get(ENV_OAUTH_PROVIDER, "").strip()
        if not provider:
            raise AuthError(f"{ENV_OAUTH_PROVIDER} is not set (name of the OAuth credential provider)")
        scopes = env.get(ENV_OAUTH_SCOPES, "").replace(",", " ").split() or list(DEFAULT_SCOPES)
        return cls(provider, scopes, decorator=decorator)

    def get_token(self) -> str:
        @self._decorator(
            provider_name=self._provider, scopes=self._scopes, auth_flow="M2M", into="access_token"
        )
        def _fetch(*, access_token: str) -> str:
            return access_token

        try:
            token = _fetch()
        except Exception as exc:  # noqa: BLE001
            raise AuthError(
                f"could not obtain an M2M token from provider {self._provider}: {type(exc).__name__}"
            ) from exc
        return _checked(token, f"credential provider {self._provider}")

    def __repr__(self) -> str:
        return f"AgentCoreM2MTokenProvider(provider={self._provider!r})"


class StaticTokenProvider:
    """A fixed token (local runs and tests)."""

    def __init__(self, token: str) -> None:
        self._token = _checked(token, "StaticTokenProvider")

    def get_token(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return "StaticTokenProvider(<redacted>)"


class PrefetchedTokenProvider:
    """Hands out the token fetched at preflight once, then defers to `then`.

    The preflight proves identity works (fail fast) and its token is reused by the first MCP
    connection, so a match costs exactly one fetch per connection. Without `then`, a second
    connection cannot be opened (there is no refresh logic by design).
    """

    def __init__(self, token: str, then: TokenProvider | None = None) -> None:
        self._first: str | None = _checked(token, "PrefetchedTokenProvider")
        self._then = then
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            token, self._first = self._first, None
        if token is not None:
            return token
        if self._then is None:
            raise AuthError("the preflight token was already used and there is no token source to reconnect")
        return self._then.get_token()

    def __repr__(self) -> str:
        return "PrefetchedTokenProvider(<redacted>)"
