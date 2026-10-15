"""Strands hooks and conversation manager shared by the LLM-driven runtimes.

`StripStaticWallsHook` and `make_conversation_manager` are extracted verbatim from
`local/agent/refined/harness_sdk.py` (characterization-tested). `StopRequestedHook` and
`TickCounterHook` are new: they connect the agent loop to the MatchRunner.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AfterToolCallEvent, AfterToolsEvent, BeforeToolCallEvent

# One tick is 4 messages: assistant(wait_for_turn) + result + assistant(action) + result.
# 12 messages therefore keep about the last 3 ticks, enough to see where we are heading
# without paying for the whole match on every model call. The `window_size` limit counts
# messages (not tokens). `pin_first=1` protects the initial instruction. `per_turn=True`
# is REQUIRED: the whole match is ONE agent call, and by default management only runs at the
# end of a call, i.e. never. Verified in strands sliding_window_conversation_manager.py.
DEFAULT_WINDOW = 12

STOP_MESSAGE = "Match stopped by the operator."
CONNECTION_LOST_MESSAGE = "MCP connection lost; ending the match."
# Strands turns a dead MCP connection into an ERROR tool result (it never raises into the agent loop),
# so the original cause (e.g. a mid-match HTTP 401) is only visible when the client context exits.
_CONNECTION_LOST_MARKERS = (
    "Connection to the MCP server was closed",
    "the client session is not running",
)


def make_conversation_manager(window: int = DEFAULT_WINDOW) -> SlidingWindowConversationManager:
    return SlidingWindowConversationManager(
        window_size=window,
        should_truncate_results=True,
        per_turn=True,
        pin_first=1,
    )


def _is_wait_for_turn(name: Any) -> bool:
    return isinstance(name, str) and (name == "wait_for_turn" or name.endswith("_wait_for_turn"))


class StripStaticWallsHook:
    """Removes the static `state.walls` list from `wait_for_turn` results after the first turn.

    The map never changes during a match, so repeating it every tick only grows the context and
    the input tokens. The first successful turn keeps it so the model sees the map once.
    Verified in strands hooks/events.py: `AfterToolCallEvent.result` is writable, and the
    executor uses the (possibly replaced) result. MCP tool names carry the server prefix
    (`ctf-game_wait_for_turn`), hence the suffix match.
    """

    def __init__(self) -> None:
        self.walls_seen = False

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    @staticmethod
    def _is_wait_for_turn(name: Any) -> bool:
        return _is_wait_for_turn(name)

    def after_tool_call(self, event: Any) -> None:
        if not self._is_wait_for_turn((getattr(event, "tool_use", None) or {}).get("name")):
            return
        result = getattr(event, "result", None)
        if not isinstance(result, dict) or result.get("status") == "error":
            return
        content = result.get("content")
        if not isinstance(content, list):
            return
        for index, block in enumerate(content):
            text = block.get("text") if isinstance(block, dict) else None
            if not isinstance(text, str):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return
            state = payload.get("state") if isinstance(payload, dict) else None
            if not isinstance(state, dict) or "walls" not in state:
                return
            if not self.walls_seen:
                self.walls_seen = True
                return
            del state["walls"]
            new_content = list(content)
            new_content[index] = {**block, "text": json.dumps(payload)}
            event.result = {**result, "content": new_content}
            return


class StopRequestedHook:
    """Ends the agent loop after an operator `stop`.

    Once `stop` is set, the next tool call is cancelled (so no further game action is sent) and
    the loop halts after the current tool batch via `AfterToolsEvent.end_turn` (verified on
    strands 1.61). The hard stop remains `StopRuntimeSession`.
    """

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    def before_tool_call(self, event: Any) -> None:
        if self._stop.is_set():
            event.cancel_tool = STOP_MESSAGE

    def after_tools(self, event: Any) -> None:
        if self._stop.is_set():
            event.end_turn = STOP_MESSAGE


class ConnectionLostHook:
    """Ends the agent loop as soon as the MCP connection is dead (e.g. a 401 after the handshake).

    There is deliberately no reconnect or token refresh (spec mcp-m2m-auth), so a dead connection
    can never recover: continuing would only make the model retry failing tools. The loop halts
    after the tool batch via `AfterToolsEvent.end_turn`; the caller then checks `lost` and lets the
    MCP client's close exception (which carries the 401) surface through `raise_on_auth_failure`.
    """

    def __init__(self) -> None:
        self.lost = False

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    def after_tool_call(self, event: Any) -> None:
        result = getattr(event, "result", None)
        if not isinstance(result, dict) or result.get("status") != "error":
            return
        for block in result.get("content") or []:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and any(marker in text for marker in _CONNECTION_LOST_MARKERS):
                self.lost = True
                return

    def after_tools(self, event: Any) -> None:
        if self.lost:
            event.end_turn = CONNECTION_LOST_MESSAGE


class TickCounterHook:
    """Calls `on_tick` for every successful `wait_for_turn` that delivers a turn (not match_over)."""

    def __init__(self, on_tick: Callable[[], None]) -> None:
        self._on_tick = on_tick

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: Any) -> None:
        if not _is_wait_for_turn((getattr(event, "tool_use", None) or {}).get("name")):
            return
        result = getattr(event, "result", None)
        if not isinstance(result, dict) or result.get("status") == "error":
            return
        for block in result.get("content") or []:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and _is_turn(text):
                self._on_tick()
                return


def _is_turn(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "turn"
