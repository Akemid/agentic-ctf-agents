"""MCP connection factory (design A3).

The Authorization header is built when the transport is OPENED, so one connection costs
exactly one token fetch, however many tool calls it carries. A 401 fails the match with an
auth error: there is deliberately no refresh or reconnect-on-401 logic (spec mcp-m2m-auth).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
from mcp.client.streamable_http import streamable_http_client
from strands.tools.mcp import MCPClient

from .auth import AuthError, TokenProvider

DEFAULT_SERVER_NAME = "ctf-game"

Connect = Callable[[str, dict[str, str]], AbstractAsyncContextManager[Any]]
ClientFactory = Callable[..., httpx.AsyncClient]


# Same values as the MCP SDK's own default client: a server may hold an SSE stream open, so reads are long.
HTTP_TIMEOUT = httpx.Timeout(30.0, read=300.0)


def default_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Build the httpx client for the MCP transport (replaces the SDK's private helper).

    Redirects stay at the httpx default (off): the streamable HTTP transport follows redirects
    within the endpoint origin itself, and a client-level follow could replay the bearer token
    wherever a redirect points.
    """
    return httpx.AsyncClient(headers=headers, timeout=timeout or HTTP_TIMEOUT, auth=auth)


class McpAuthError(AuthError):
    """The MCP server rejected the bearer token (HTTP 401). The match fails; no refresh is attempted."""


def _http_connect(client_factory: ClientFactory) -> Connect:
    @asynccontextmanager
    async def connect(url: str, headers: dict[str, str]):
        async with client_factory(headers=headers) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                yield streams

    return connect


def open_mcp_client(
    url: str,
    tokens: TokenProvider,
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    prefix_tools: bool = True,
    connect: Connect | None = None,
    client_factory: ClientFactory = default_http_client,
) -> MCPClient:
    """Build an MCPClient whose transport factory fetches a fresh token per connection.

    Tool names carry the `server_name` prefix unless `prefix_tools` is False (a code-owned loop
    that calls and filters tools by their raw names). `connect` and `client_factory` are test
    seams; production uses streamable HTTP.
    """
    open_transport = connect or _http_connect(client_factory)

    def transport() -> AbstractAsyncContextManager[Any]:
        headers = {"Authorization": f"Bearer {tokens.get_token()}"}
        return open_transport(url, headers)

    return MCPClient(transport, prefix=server_name if prefix_tools else None)


def _find_unauthorized(exc: BaseException | None, seen: set[int]) -> bool:
    if exc is None or id(exc) in seen:
        return False
    seen.add(id(exc))
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return True
    children: list[BaseException | None] = [exc.__cause__, exc.__context__]
    if isinstance(exc, BaseExceptionGroup):
        children.extend(exc.exceptions)
    return any(_find_unauthorized(child, seen) for child in children)


@contextlib.contextmanager
def raise_on_auth_failure() -> Iterator[None]:
    """Turn a 401 anywhere in the error chain into McpAuthError. Other errors pass through."""
    try:
        yield
    except Exception as exc:
        if _find_unauthorized(exc, set()):
            raise McpAuthError("MCP server rejected the bearer token (401); not refreshing") from exc
        raise
