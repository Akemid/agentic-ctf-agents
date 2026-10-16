"""Strands hooks and conversation manager shared by the LLM-driven runtimes.

`make_conversation_manager` is extracted from `local/agent/refined/harness_sdk.py` (characterization-tested).
`StopRequestedHook`, `ConnectionLostHook`, `TickCounterHook` and `ForceJoinArgsHook` are new: they connect
the agent loop to the MatchRunner.

There is deliberately no hook that strips static board data from turns: since the game server's MCP turn
contract v2 the turn `state` no longer carries `grid`, `walls`, `fields` or `flags[].base`. The static map
comes from `join_match.map` / `get_map`; the system prompt tells the model to call `get_map` if it loses
track of it (with `pin_first=1` and a short window the join result leaves the history after a few ticks).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AfterToolCallEvent, AfterToolsEvent, BeforeToolCallEvent

from .join import AUTO_TEAMS, RETRY_OTHER_TEAM_REASON, JoinSignal, join_reason, report_join_result
from .toolresult import is_connection_lost, result_is_error

# One tick is 4 messages: assistant(wait_for_turn) + result + assistant(action) + result.
# 12 messages therefore keep about the last 3 ticks, enough to see where we are heading
# without paying for the whole match on every model call. The `window_size` limit counts
# messages (not tokens). `pin_first=1` protects the initial instruction. `per_turn=True`
# is REQUIRED: the whole match is ONE agent call, and by default management only runs at the
# end of a call, i.e. never. Verified in strands sliding_window_conversation_manager.py.
DEFAULT_WINDOW = 12

STOP_MESSAGE = "Match stopped by the operator."
CONNECTION_LOST_MESSAGE = "MCP connection lost; ending the match."
JOIN_FAILED_MESSAGE = "join_match failed; ending the match."
ALREADY_JOINED_REASON = "already_joined"  # the server's reason for a second join_match on one session
ALREADY_JOINED_MESSAGE = (
    "You are already seated in the match: do not call join_match again. Continue with wait_for_turn."
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


def _is_join_match(name: Any) -> bool:
    return isinstance(name, str) and (name == "join_match" or name.endswith("_join_match"))


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
        if is_connection_lost(getattr(event, "result", None)):
            self.lost = True

    def after_tools(self, event: Any) -> None:
        if self.lost:
            event.end_turn = CONNECTION_LOST_MESSAGE


class ForceJoinArgsHook:
    """The model never decides where the character sits: the runtime forces `join_match`'s arguments.

    Before the call, `match_id`, `name`, `team` and `agent_index` are overwritten with the requested values
    (and anything else the model passed is dropped; `agent_index` is removed when no slot was requested, so
    the server picks the lowest free one). A Strands `BeforeToolCallEvent` hook can mutate
    `event.tool_use["input"]`: the executor re-reads it after the hooks (verified on strands 1.61, and the
    programmatic tool caller's inner calls fire the same event).

    After the call, the outcome goes to the runner's `JoinSignal` (the seat the server assigned, or why it
    refused). A failed join ends the turn (`AfterToolsEvent.end_turn`) so the model can never retry into
    another slot. With `team="auto"` the hook offers red, then blue after a `match_full`, and nothing else.
    A second `join_match` after a successful join is cancelled here: the server would answer
    `already_joined` and that must not fail a match that is running.
    """

    def __init__(
        self, match_id: str, name: str, team: str, agent_index: int | None, join: JoinSignal
    ) -> None:
        self._match_id = match_id
        self._name = name
        self._team = team
        self._agent_index = agent_index
        self._join = join
        self._tried: list[str] = []
        self._offered: str | None = None
        self._last_refusal: str | None = None  # a `match_full` that left another team to try
        self.failed = False

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    def before_tool_call(self, event: Any) -> None:
        if not _is_join_match((getattr(event, "tool_use", None) or {}).get("name")):
            return
        outcome = self._join.outcome
        if outcome is not None:
            event.cancel_tool = ALREADY_JOINED_MESSAGE if outcome.joined else JOIN_FAILED_MESSAGE
            return
        team = self._next_team()
        arguments: dict[str, Any] = {"match_id": self._match_id, "name": self._name, "team": team}
        if self._agent_index is not None:
            arguments["agent_index"] = self._agent_index
        self._offered = team
        event.tool_use["input"] = arguments

    def after_tool_call(self, event: Any) -> None:
        if not _is_join_match((getattr(event, "tool_use", None) or {}).get("name")):
            return
        if getattr(event, "cancel_message", None) or self._join.outcome is not None:
            return  # a cancelled call never reached the server; a settled join is not reopened
        result = getattr(event, "result", None)
        if (
            result_is_error(result)
            and not is_connection_lost(result)
            and join_reason(result) == ALREADY_JOINED_REASON
        ):
            return  # a parallel duplicate in the same batch: the call that seated us reports, not this one
        if self._may_retry_other_team(result):
            self._last_refusal = join_reason(result)
            return
        report_join_result(self._join, result)
        outcome = self._join.outcome
        self.failed = outcome is not None and not outcome.joined

    def after_tools(self, event: Any) -> None:
        if self.failed:
            event.end_turn = JOIN_FAILED_MESSAGE

    def settle_unanswered(self) -> None:
        """Call when the agent turn has ended. `auto` waits for the model to try the other team; if it never
        did, the last refusal seen (`match_full`) is the outcome, not a generic "ended before join_match"."""
        if self._join.outcome is None and self._last_refusal is not None:
            self._join.refuse(self._last_refusal)

    def _next_team(self) -> str:
        if self._team != "auto":
            return self._team
        return next((t for t in AUTO_TEAMS if t not in self._tried), AUTO_TEAMS[-1])

    def _may_retry_other_team(self, result: Any) -> bool:
        """`auto` only: a `match_full` on one team leaves the other one to try."""
        if self._team != "auto" or self._offered is None or not result_is_error(result):
            return False
        if join_reason(result) != RETRY_OTHER_TEAM_REASON:
            return False
        self._tried.append(self._offered)
        return any(t not in self._tried for t in AUTO_TEAMS)


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
