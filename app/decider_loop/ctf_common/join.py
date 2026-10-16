"""The synchronous join: how a runtime tells `start` whether the character is seated.

`start` answers only after `join_match`, and the join runs on the match thread because the server binds the
seat to ONE MCP session (one session is one join) and that session must be the one the match keeps playing
on. The thread therefore reports the outcome through a `JoinSignal` and `start` waits on it with a bound.

Outcomes (first one wins, later ones are ignored):
    joined     the server seated us: the REAL team and `agent_index` it assigned
    refused    the server answered with a tool error `{reason}` (slot_taken, match_full, ...): `rejected`
    error      the join could not be attempted (connection, token, timeout, malformed result): `failed`
    cancelled  an operator `stop` arrived while joining
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from .payload import MatchRequest
from .toolresult import is_connection_lost, result_is_error, result_texts

JOIN_TIMEOUT_S = 15.0
TOOL_JOIN = "join_match"
AUTO_TEAMS = ("red", "blue")
RETRY_OTHER_TEAM_REASON = "match_full"  # the only refusal after which `team: auto` tries the other team
_DETAIL_MAX = 120

Kind = Literal["joined", "refused", "error", "cancelled"]


@dataclass(frozen=True)
class JoinOutcome:
    kind: Kind
    team: str | None = None
    agent_index: int | None = None
    reason: str | None = None

    @property
    def joined(self) -> bool:
        return self.kind == "joined"


class JoinSignal:
    """A thread-safe, first-writer-wins one-shot carrying the join outcome."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._outcome: JoinOutcome | None = None

    @property
    def outcome(self) -> JoinOutcome | None:
        return self._outcome

    def succeed(self, team: str, agent_index: int) -> bool:
        return self._set(JoinOutcome("joined", team=team, agent_index=agent_index))

    def refuse(self, reason: str) -> bool:
        return self._set(JoinOutcome("refused", reason=reason))

    def error(self, reason: str) -> bool:
        return self._set(JoinOutcome("error", reason=reason))

    def cancel(self) -> bool:
        return self._set(JoinOutcome("cancelled"))

    def expire(self, reason: str) -> JoinOutcome:
        """Resolve a still-pending join as an error and return the FINAL outcome (a late win is kept)."""
        self.error(reason)
        assert self._outcome is not None
        return self._outcome

    def wait(self, timeout: float | None) -> JoinOutcome | None:
        self._done.wait(timeout)
        return self._outcome

    def _set(self, outcome: JoinOutcome) -> bool:
        with self._lock:
            if self._outcome is not None:
                return False
            self._outcome = outcome
        self._done.set()
        return True


def join_reason(result: Any) -> str:
    """The reason of a failed `join_match` tool result: the server's `reason`, else a short summary."""
    if is_connection_lost(result):
        return "connection_lost"
    for text in result_texts(result):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("reason"), str):
            return payload["reason"]
        if text.strip():
            return text.strip()[:_DETAIL_MAX]
    return "unknown"


def _success_payload(result: Any) -> dict[str, Any] | None:
    for text in result_texts(result):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def report_join_result(signal: JoinSignal, result: Any) -> None:
    """Translate a `join_match` tool result into the signal. Used by the code path and by the hook."""
    if result_is_error(result):
        if is_connection_lost(result):
            signal.error(f"join_match failed: {join_reason(result)}")
        else:
            signal.refuse(join_reason(result))
        return
    payload = _success_payload(result)
    team = payload.get("team") if payload else None
    index = payload.get("agent_index") if payload else None
    if not isinstance(team, str) or isinstance(index, bool) or not isinstance(index, int):
        signal.error("join_match returned no team/agent_index")
        return
    signal.succeed(team, index)


def seat_in_code(
    client: Any, request: MatchRequest, signal: JoinSignal, stop: threading.Event | None = None
) -> dict[str, Any] | None:
    """Call `join_match` from code on the connection the match will keep using, and report the outcome.

    The arguments are exactly the requested ones; `agent_index` is only sent when one was requested, so
    the server picks the lowest free one otherwise. A requested agent_index is NEVER traded for another. With
    `team="auto"` red is tried first and blue only after a `match_full`. Returns the join result body on
    success, None otherwise (the signal then carries the refusal or the error).

    `stop` is checked before every `join_match` call (the client is already open when this runs): an operator
    stop takes no seat, and with `team="auto"` never tries the next team. The signal is then `cancelled`.
    """
    teams = AUTO_TEAMS if request.team == "auto" else (request.team,)
    for index, team in enumerate(teams):
        if stop is not None and stop.is_set():
            signal.cancel()
            return None
        arguments: dict[str, Any] = {"match_id": request.match_id, "name": request.name, "team": team}
        if request.agent_index is not None:
            arguments["agent_index"] = request.agent_index
        try:
            result = client.call_tool_sync(f"join-{uuid.uuid4().hex[:8]}", TOOL_JOIN, arguments)
        except Exception as exc:  # noqa: BLE001 - reported through the signal, never lost
            signal.error(f"join_match failed: {type(exc).__name__}: {exc}")
            return None
        more_teams = index + 1 < len(teams)
        if result_is_error(result) and more_teams and join_reason(result) == RETRY_OTHER_TEAM_REASON:
            continue
        report_join_result(signal, result)
        outcome = signal.outcome
        return _success_payload(result) if outcome is not None and outcome.joined else None
    return None
