"""One match per process: the async start / status / stop state machine (design A2).

There is one runner per process, which is one microVM per AgentCore session.

    IDLE/FINISHED/FAILED/STOPPED --start--> STARTING (preflight, lock NOT held; reported as "running")
      STARTING --preflight error--> FAILED        STARTING --stop--> STOPPING --preflight done--> STOPPED
      STARTING --ok--> RUNNING --play returns--> FINISHED     RUNNING --exception--> FAILED
      RUNNING --stop--> STOPPING --play exits--> STOPPED
    start while STARTING/RUNNING: same (match_id, name) -> already_running; different -> rejected
    start while STOPPING -> rejected. Restart after a terminal state is allowed (spec: Restart after finish).

`start` registers the match with `app.add_async_task` (the session reports HealthyBusy) and
runs it on a daemon thread. `complete_async_task` always runs in `finally`.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .auth import PrefetchedTokenProvider, TokenProvider
from .config import RuntimeConfig
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
    """Everything a PlayFn gets. `stop` is set when an operator asks the match to end."""

    request: MatchRequest
    config: RuntimeConfig
    tokens: TokenProvider
    stop: threading.Event
    tick: Callable[[], None]


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
    def __init__(self, app: AsyncTaskApp, play: PlayFn, preflight: PreflightFn, *, runtime: str) -> None:
        self._app = app
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

    # ---- public API ------------------------------------------------------

    def start(self, request: MatchRequest) -> dict[str, Any]:
        """Register the match, run the (network) preflight WITHOUT the lock, then launch the thread.

        While the preflight runs the runner is in the internal STARTING state, which is reported
        externally as `running` (the spec's status set is unchanged). `status` and `stop` never wait
        on the preflight.
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
            context = MatchContext(
                request=request,
                config=prepared.config,
                tokens=prepared.tokens,
                stop=self._stop,
                tick=self._count_tick,
            )
            # copy_context: the request-scoped workload-identity ContextVar must reach the worker thread.
            ctx = contextvars.copy_context()
            try:
                thread = threading.Thread(
                    target=ctx.run,
                    args=(self._run, context, task_id),
                    name=f"match-{request.name}",
                    daemon=True,
                )
                thread.start()
            except Exception as exc:  # noqa: BLE001 - never leave the session HealthyBusy forever
                self._app.complete_async_task(task_id)
                return self._fail_start_locked(exc)
            self._thread = thread
            self._state = MatchState.RUNNING
            return self._snapshot("started")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state in (MatchState.STARTING, MatchState.RUNNING):
                self._state = MatchState.STOPPING
                self._stop.set()
            return self._snapshot()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the background match thread exits (for tests and graceful shutdown)."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ---- internals -------------------------------------------------------

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

    def _run(self, context: MatchContext, task_id: int) -> None:
        outcome, error = MatchState.FINISHED, None
        try:
            self._play(context)
        except Exception as exc:  # noqa: BLE001 - a failed match is reported via status
            log.error("match failed: %s", _describe(exc))
            outcome, error = MatchState.FAILED, _describe(exc)
        else:
            if context.stop.is_set():
                outcome = MatchState.STOPPED
        finally:
            with self._lock:
                self._state, self._error = outcome, error
            self._app.complete_async_task(task_id)

    def _count_tick(self) -> None:
        with self._lock:
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
            "team": request.team,
            "started_at": self._started_at,
            "ticks": self._ticks,
        }
        if self._error:
            snap["error"] = self._error
        return snap


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
