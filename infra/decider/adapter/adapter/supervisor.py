"""Keeps the upstream decider alive: start, wait for health, warm up, watch, restart once.

The adapter is `ready` only between a successful warm-up and the next failure. One automatic restart is
allowed per container lifetime; after that `state` is `failed` for good, `/ping` keeps answering 503 and
SageMaker replaces the instance. Spawn, health and warm-up are injected, so tests need no process or GPU.

Health probes and inference compete for the same decider, which cannot answer `/health` while it computes.
So the watch loop never probes while a request is in flight (the `busy` callback is the request gate's flag),
and only counts a probe as a failure when it ran entirely while idle:

- the process exited, or the probe was refused: a hard failure, restart at once;
- the probe timed out or answered non-200: a soft failure, restart after `health_failures` in a row.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

log = logging.getLogger("adapter.supervisor")

STARTING, READY, FAILED = "starting", "ready", "failed"
TERMINATE_GRACE_S = 10.0


class Probe(enum.StrEnum):
    """Outcome of one `/health` probe."""

    OK = "ok"
    REFUSED = "refused"  # nothing is listening: the server is gone
    TIMEOUT = "timeout"  # listening but silent, which is normal while it computes
    BAD = "bad"  # answered, but not 200


class Process(Protocol):
    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...


Spawn = Callable[[], Awaitable[Process]]
Check = Callable[[], Awaitable[bool]]
HealthCheck = Callable[[], Awaitable[Probe | bool]]


def _as_probe(result: Probe | bool) -> Probe:
    """Plain booleans (True/False) are accepted: they map to OK/BAD."""
    if isinstance(result, Probe):
        return result
    return Probe.OK if result else Probe.BAD


class Supervisor:
    def __init__(
        self,
        spawn: Spawn,
        health: HealthCheck,
        warmup: Check,
        *,
        busy: Callable[[], bool] | None = None,
        health_interval_s: float = 5.0,
        startup_poll_s: float = 1.0,
        startup_timeout_s: float = 550.0,
        health_failures: int = 3,
        max_restarts: int = 1,
    ) -> None:
        self._spawn = spawn
        self._health = health
        self._warmup = warmup
        self._busy = busy or (lambda: False)
        self._interval = health_interval_s
        self._poll = startup_poll_s
        self._startup_timeout = startup_timeout_s
        self._failures_allowed = health_failures
        self._max_restarts = max_restarts
        self._stopping = asyncio.Event()
        self._unhealthy = asyncio.Event()
        self._process: Process | None = None
        self.state = STARTING
        self.restarts = 0

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def process(self) -> Process | None:
        return self._process

    async def run(self) -> None:
        try:
            await self._supervise()
        except Exception:
            log.exception("the supervisor failed unexpectedly; staying unhealthy")
            self.state = FAILED
        finally:
            # Also on cancellation, and even if a warm-up was in progress: never leave the child behind.
            await self._terminate(self._process)
            if self.state != FAILED:
                self.state = STARTING

    async def stop(self) -> None:
        self._stopping.set()

    async def report_unhealthy(self, reason: str) -> None:
        """Take the decider out of service now and restart it (counts against the restart budget)."""
        log.error("decider reported unhealthy: %s", reason)
        if self.state == READY:
            self.state = STARTING
        self._unhealthy.set()

    async def _supervise(self) -> None:
        while not self._stopping.is_set():
            self._unhealthy.clear()
            try:
                self._process = await self._spawn()
            except Exception as exc:  # missing binary, StageError, no memory...: nothing to supervise
                log.error("cannot start the decider: %s: %s", type(exc).__name__, exc)
                self.state = FAILED
                return
            if await self._bring_up(self._process):
                self.state = READY
                log.info("decider is ready")
                await self._watch(self._process)
            if self._stopping.is_set():
                return
            self.state = STARTING
            await self._terminate(self._process)
            if self.restarts >= self._max_restarts:
                self.state = FAILED
                log.error("decider failed again after %d restart(s); staying unhealthy", self.restarts)
                return
            self.restarts += 1
            log.warning("restarting the decider (%d/%d)", self.restarts, self._max_restarts)

    async def _sleep(self, seconds: float, *, until: asyncio.Event | None = None) -> None:
        """Sleep, but wake early when stop() is called (or when `until` is set)."""
        events = [self._stopping] if until is None else [self._stopping, until]
        waiters = [asyncio.ensure_future(event.wait()) for event in events]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def _bring_up(self, process: Process) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout
        while not self._stopping.is_set():
            if process.returncode is not None:
                log.error("decider exited with %s while starting", process.returncode)
                return False
            if _as_probe(await self._health()) is Probe.OK:
                break
            if loop.time() >= deadline:
                log.error("decider was not healthy within %.0f s", self._startup_timeout)
                return False
            await self._sleep(self._poll)
        else:
            return False
        warmed = await self._warm_up()
        if not warmed and not self._stopping.is_set():
            log.error("decider warm-up failed")
        return warmed and not self._stopping.is_set()

    async def _warm_up(self) -> bool:
        """Run the warm-up, but give up the moment stop() is called: it may take minutes."""
        warmup = asyncio.ensure_future(self._warmup())
        stopped = asyncio.ensure_future(self._stopping.wait())
        try:
            await asyncio.wait({warmup, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not warmup.done():
                return False
            return warmup.result()
        finally:
            for task in (warmup, stopped):
                task.cancel()

    async def _watch(self, process: Process) -> None:
        failures = 0
        while not self._stopping.is_set():
            await self._sleep(self._interval, until=self._unhealthy)
            if self._stopping.is_set():
                return
            if self._unhealthy.is_set():
                return
            if process.returncode is not None:
                log.error("decider exited with %s", process.returncode)
                return
            if self._busy():
                failures = 0  # it is alive and working; /health cannot answer meanwhile
                continue
            probe = _as_probe(await self._health())
            if probe is Probe.OK:
                failures = 0
                continue
            if self._busy():
                continue  # a request arrived during the probe: the silence says nothing
            if probe is Probe.REFUSED:
                log.error("decider refused the health probe: nothing is listening")
                return
            failures += 1
            if failures >= self._failures_allowed:
                log.error("decider failed %d health checks in a row (%s)", failures, probe.value)
                return

    async def _terminate(self, process: Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
        except TimeoutError:
            process.kill()
            await process.wait()
