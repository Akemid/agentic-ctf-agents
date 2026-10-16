"""Pure helpers over a Strands / MCPClient tool result (`{"status", "content": [{"text"}], ...}`)."""

from __future__ import annotations

from typing import Any

# Strands turns a dead MCP connection into an ERROR tool result (it never raises into the agent loop),
# so the original cause (e.g. a mid-match HTTP 401) is only visible when the client context exits.
CONNECTION_LOST_MARKERS = (
    "Connection to the MCP server was closed",
    "the client session is not running",
)


def result_texts(result: Any) -> list[str]:
    """Every text block of a tool result, in order."""
    if not isinstance(result, dict):
        return []
    blocks = result.get("content") or []
    return [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]


def result_is_error(result: Any) -> bool:
    """True for a tool error: Strands' `status == "error"` or MCP's `isError` flag."""
    return isinstance(result, dict) and (result.get("status") == "error" or bool(result.get("isError")))


def is_connection_lost(result: Any) -> bool:
    """True for an error tool result that says the MCP connection is dead."""
    if not isinstance(result, dict) or result.get("status") != "error":
        return False
    return any(marker in text for text in result_texts(result) for marker in CONNECTION_LOST_MARKERS)
