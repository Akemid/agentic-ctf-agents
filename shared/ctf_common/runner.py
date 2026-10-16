"""One match per process: the start / status / stop state machine (design A2).

There is one runner per process, which is one microVM per AgentCore session.

    IDLE/FINISHED/FAILED/STOPPED --start--> STARTING (preflight + join, lock NOT held; reported as "running")
      STARTING --preflight error--> FAILED        STARTING --stop--> STOPPING --preflight done--> STOPPED
      STARTING --joined--> RUNNING --play returns--> FINISHED     RUNNING --exception--> FAILED
      STARTING --join refused/error--> FAILED at once (the old thread may still be unwinding)
      STARTING --join timeout--> STOPPING --play exits--> FAILED
      RUNNING --stop--> STOPPING --play exits--> STOPPED
      STARTING --stop during join--> STOPPING --play exits--> STOPPED
    start while STARTING/RUNNING: same (match_id, name) -> already_running; different -> rejected
    start while STOPPING -> rejected. Restart after a terminal state is allowed (spec: Restart after finish).

`start` is synchronous up to the seat: after the preflight it runs the match thread, which opens the MCP
connection and calls `join_match`, and `start` waits (bounded) for that outcome on a `JoinSignal` before
answering `started` (with the team/agent_index it assigned), `rejected` (the server refused the seat) or
`failed` (infrastructure error or timeout). The join runs on the match thread, not on `start`, because the
server binds the seat to one MCP session and the match must keep playing on that same session.

Every start bumps a run generation. A thread of an older run (for example one still unwinding after a
refusal) can only write state while its generation is still the current one, so `status` reports the refusal
at once and a restart is never overwritten by a thread that is finishing late.

`start` registers the match with `app.add_async_task` (the session reports HealthyBusy) and
runs it on a daemon thread. `complete_async_task` always runs in `finally`.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .auth import PrefetchedTokenProvider, TokenProvider
from .config import RuntimeConfig
from .join import JOIN_TIMEOUT_S, JoinOutcome, JoinSignal
from .payload import Action, MatchRequest, PayloadError, parse_payload

log = logging.getLogger("ctf_common.runner")

ASYNC_TASK_NAME = "match"


class MatchState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"  # internal: preflight in progress, reported externally as "running"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FINISHED = "finished"
    FAILED = "failed"


_TERMINAL = frozenset({MatchState.FINISHED, MatchState.FAILED, MatchState.STOPPED})
_ACTIVE = frozenset({MatchState.STARTING, MatchState.RUNNING, MatchState.STOPPING})


class AsyncTaskApp(Protocol):
    """The slice of BedrockAgentCoreApp the runner needs."""

    def add_async_task(self, name: str, metadata: dict | None = None) -> int: ...

    def complete_async_task(self, task_id: int) -> bool: ...


@dataclass(frozen=True)
class Preflight:
    config: RuntimeConfig
    tokens: TokenProvider


PreflightFn = Callable[[MatchRequest], Preflight]


@dataclass(frozen=True)
class MatchContext:
    """Everything a PlayFn gets. `stop` is set when an operator asks the match to end.

    `join` is how the play function answers `start`: it MUST report the outcome of `join_match` (the
    seat the server assigned, or why it refused) before playing. A play that ends without reporting is
    treated as a failed join.
    """

    request: MatchRequest
    config: RuntimeConfig
    tokens: TokenProvider
    stop: threading.Event
    tick: Callable[[], None]
    join: JoinSignal = field(default_factory=JoinSignal)


class PlayFn(Protocol):
    def __call__(self, ctx: MatchContext) -> None: ...


def make_preflight(load_config: Callable[[], RuntimeConfig], tokens: TokenProvider) -> PreflightFn:
    """Preflight = load config (SSM) then fetch one token. Config errors stop before any token call."""

    def preflight(_request: MatchRequest) -> Preflight:
        config = load_config()
        first = tokens.get_token()
        return Preflight(config=config, tokens=PrefetchedTokenProvider(first, tokens))

    return preflight


class MatchRunner:
    def __init__(
        self,
        app: AsyncTaskApp,
        play: PlayFn,
        preflight: PreflightFn,
        *,
        runtime: str,
        join_timeout_s: float = JOIN_TIMEOUT_S,
    ) -> None:
        self._app = app
        self._join_timeout_s = join_timeout_s
        self._join: JoinSignal | None = None
        self._seat: JoinOutcome | None = None
        self._play = play
        self._preflight = preflight
        self._runtime = runtime
        self._lock = threading.Lock()
        self._state = MatchState.IDLE
        self._request: MatchRequest | None = None
        self._started_at: str | None = None
        self._ticks = 0
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0  # bumped by every start: only the current run's thread may write state

    # ---- public API ------------------------------------------------------

    def start(self, request: MatchRequest) -> dict[str, Any]:
        """Run the (network) preflight WITHOUT the lock, launch the match thread, wait for its join.

        While the preflight and the join run the runner is in the internal STARTING state, which is
        reported externally as `running` (the spec's status set is unchanged). `status` and `stop` never
        wait on either, and neither holds the lock.
        """
        with self._lock:
            if self._state in _ACTIVE:
                current = self._request
                assert current is not None
                same = (current.match_id, current.name) == (request.match_id, request.name)
                if same and self._state in (MatchState.STARTING, MatchState.RUNNING):
                    return self._snapshot("already_running")
                return self._snapshot("rejected")
            self._request = request
            self._started_at = datetime.now(UTC).isoformat(timespec="seconds")
            self._ticks = 0
            self._error = None
            self._stop = threading.Event()
            self._thread = None
            self._join = None
            self._seat = None
            self._generation += 1
            generation = self._generation
            self._state = MatchState.STARTING

        try:
            prepared = self._preflight(request)
        except Exception as exc:  # noqa: BLE001 - reported to the operator, never swallowed silently
            return self._finish_start(MatchState.FAILED, error=exc)

        with self._lock:
            if self._stop.is_set():  # `stop` arrived during the preflight: nothing was registered yet
                self._state = MatchState.STOPPED
                log.info("[%s] stopped before the match began", self._runtime)
                return self._snapshot()
            try:
                task_id = self._app.add_async_task(
                    ASYNC_TASK_NAME, {"match_id": request.match_id, "name": request.name}
                )
            except Exception as exc:  # noqa: BLE001
                return self._fail_start_locked(exc)
            join = JoinSignal()
            context = MatchContext(
                request=request,
                config=prepared.config,
                tokens=prepared.tokens,
                stop=self._stop,
                tick=lambda: self._count_tick(generation),
                join=join,
            )
            # copy_context: the request-scoped workload-identity ContextVar must reach the worker thread.
            ctx = contextvars.copy_context()
            try:
                thread = threading.Thread(
                    target=ctx.run,
                    args=(self._run, context, task_id, generation),
                    name=f"match-{request.name}",
                    daemon=True,
                )
                thread.start()
            except Exception as exc:  # noqa: BLE001 - never leave the session HealthyBusy forever
                self._app.complete_async_task(task_id)
                return self._fail_start_locked(exc)
            self._thread = thread
            self._join = join  # the state stays STARTING until the join answers

        return self._await_join(join, generation)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state in (MatchState.STARTING, MatchState.RUNNING):
                self._state = MatchState.STOPPING
                self._stop.set()
                if self._join is not None:
                    self._join.cancel()  # wake a `start` that is waiting for a join nobody wants anymore
            return self._snapshot()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the background match thread exits (for tests and graceful shutdown)."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ---- internals -------------------------------------------------------

    def _await_join(self, join: JoinSignal, generation: int) -> dict[str, Any]:
        """Wait (bounded, lock NOT held) for `join_match`, then answer `start` from what the server said."""
        outcome = join.wait(self._join_timeout_s)
        timed_out = outcome is None
        if outcome is None:
            outcome = join.expire(f"join_match timed out after {self._join_timeout_s:g}s")
            if outcome.kind == "error":
                # Nobody answered in time: end the thread (the stop event) but keep the runner busy until
                # it exits, so a restart can never overlap a thread that may still hold a seat.
                with self._lock:
                    if self._state is MatchState.STARTING:
                        self._state = MatchState.STOPPING
                    self._stop.set()
        if outcome.joined:
            with self._lock:
                if generation != self._generation:
                    return self._snapshot()
                self._seat = outcome
                if self._state is MatchState.STARTING:
                    self._state = MatchState.RUNNING
                    return self._snapshot("started")
                return self._snapshot()  # a stop raced the join: report the stop, not a started match
        if outcome.kind == "cancelled":
            return self.status()  # an operator stop: report it now, the thread unwinds on its own
        # refused (the server said no) or error (could not join)
        status = "rejected" if outcome.kind == "refused" else "failed"
        with self._lock:
            if generation != self._generation:  # a newer run owns the runner: answer, do not touch it
                return {"status": status, "error": _join_error(outcome)}
            if timed_out:
                # The thread may be stuck holding the seat: the runner stays STOPPING (busy) until it exits.
                if not self._error:
                    self._error = outcome.reason
            else:
                # Refused or errored: no seat is held and the thread is only unwinding, so the runner is
                # terminal now (a restart is allowed; the generation keeps the old thread from writing).
                self._state = MatchState.FAILED
                self._error = _join_error(outcome)
            return self._snapshot(status)

    def _finish_start(self, state: MatchState, *, error: BaseException) -> dict[str, Any]:
        with self._lock:
            return self._fail_start_locked(error, state)

    def _fail_start_locked(
        self, error: BaseException, state: MatchState = MatchState.FAILED
    ) -> dict[str, Any]:
        log.warning("[%s] start failed before the match began: %s", self._runtime, _describe(error))
        self._state = state
        self._error = _describe(error)
        return self._snapshot()

    def _run(self, context: MatchContext, task_id: int, generation: int) -> None:
        outcome, error = MatchState.FINISHED, None
        try:
            self._play(context)
        except Exception as exc:  # noqa: BLE001 - a failed match is reported via status
            log.error("match failed: %s", _describe(exc))
            outcome, error = MatchState.FAILED, _describe(exc)
            context.join.error(_describe(exc))  # no-op when the join already answered
        else:
            if context.stop.is_set():
                outcome = MatchState.STOPPED
                context.join.cancel()
            else:
                context.join.error("the match ended before join_match completed")
        finally:
            seat = context.join.outcome
            if seat is not None and seat.kind in ("refused", "error"):
                # A failed join wins over a stop that raced it. The error is the thread's own exception when
                # there is one (a 401 after `connection_lost` is the real cause); else the join verdict.
                outcome = MatchState.FAILED
                if error is None or seat.kind == "refused":
                    error = _join_error(seat)
            with self._lock:
                if generation == self._generation:  # a newer run must never be overwritten by this thread
                    self._state, self._error = outcome, error
            self._app.complete_async_task(task_id)

    def _count_tick(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._ticks += 1

    def _snapshot(self, status: str | None = None) -> dict[str, Any]:
        if self._state is MatchState.IDLE and status is None:
            return {"status": MatchState.IDLE.value}
        request = self._request
        assert request is not None
        snap: dict[str, Any] = {
            "status": status or _external(self._state),
            "match_id": request.match_id,
            "name": request.name,
            "team": request.team if self._seat is None else self._seat.team,
            "started_at": self._started_at,
            "ticks": self._ticks,
        }
        if self._seat is not None:
            snap["agent_index"] = self._seat.agent_index
            snap["slot"] = self._seat.agent_index  # equivalent key, the web lobby reads it
        if self._error:
            snap["error"] = self._error
        return snap


def _join_error(seat: JoinOutcome) -> str | None:
    return f"join_match failed: {seat.reason}" if seat.kind == "refused" else seat.reason


def _external(state: MatchState) -> str:
    """The spec's status set has no `starting`: the preflight phase is a running match."""
    return (MatchState.RUNNING if state is MatchState.STARTING else state).value


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def handle_invocation(
    runner: MatchRunner, raw: Any, *, runtime: str, session_id: str | None
) -> dict[str, Any]:
    """Entrypoint glue: validate the payload, then dispatch. Invalid payloads do no work."""
    try:
        request = parse_payload(raw, runtime=runtime, session_id=session_id)
    except PayloadError as exc:
        return {"status": "rejected", "error": f"invalid payload: {exc}"}
    if request.action is Action.STATUS:
        return runner.status()
    if request.action is Action.STOP:
        return runner.stop()
    return runner.start(request)
