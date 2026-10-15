"""The HTTP surface SageMaker expects: `POST /invocations` and `GET /ping`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, TypeVar

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import AdapterConfig
from .supervisor import Probe
from .warmup import SYSTEMONE_PATH, upstream_healthy

log = logging.getLogger("adapter.app")

T = TypeVar("T")


class Readiness(Protocol):
    @property
    def ready(self) -> bool: ...

    @property
    def state(self) -> str: ...

    async def run(self) -> None: ...
    async def stop(self) -> None: ...
    async def report_unhealthy(self, reason: str) -> None: ...


class QueueFull(Exception):
    """Too many requests are already waiting."""


class QueueTimeout(Exception):
    """The request waited longer than the bounded wait."""


class NotReady(Exception):
    """The decider went out of service while the request waited for its turn."""


class ClientGone(Exception):
    """The client disconnected before its turn came."""


Recover = Callable[[Exception], Awaitable[None]]


class Gate:
    """One request at a time, FIFO, with a bounded number of waiters and a bounded wait.

    The decider crashes on concurrent requests, so the slot is released only when the upstream call has
    really finished: if the client disconnects mid-flight the call keeps running (shielded), and the next
    request still waits for it. `recover` runs while the slot is still held when the call failed, so a
    failure can be cleaned up (see `create_app`) before anyone else gets the decider.
    """

    def __init__(self, max_waiting: int, max_wait_s: float) -> None:
        self._lock = asyncio.Lock()  # asyncio.Lock wakes waiters in FIFO order
        self._max_waiting = max_waiting
        self._max_wait_s = max_wait_s
        self._waiting = 0

    @property
    def busy(self) -> bool:
        """True while a request holds the slot (computing, or being drained after a failure)."""
        return self._lock.locked()

    async def run(self, work: Callable[[], Awaitable[T]], recover: Recover | None = None) -> tuple[T, float]:
        """Run `work` in its turn. Returns its result and the queue wait in milliseconds."""
        arrived = time.monotonic()
        if self._waiting >= self._max_waiting:
            raise QueueFull
        self._waiting += 1
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._max_wait_s)
        except TimeoutError:
            raise QueueTimeout from None
        finally:
            self._waiting -= 1
        waited_ms = (time.monotonic() - arrived) * 1000
        try:
            task = asyncio.ensure_future(self._attempt(work, recover))
        except BaseException:
            self._lock.release()
            raise
        task.add_done_callback(self._finished)
        # Not asyncio.shield: when this waiter is cancelled (client gone) the call must keep running, and
        # shield would report its eventual failure as an unhandled error. `wait` leaves the task alone.
        await asyncio.wait({task})
        return task.result(), waited_ms

    @staticmethod
    async def _attempt(work: Callable[[], Awaitable[T]], recover: Recover | None) -> T:
        try:
            return await work()
        except Exception as exc:
            if recover is not None:
                try:
                    await recover(exc)
                except Exception:
                    log.exception("recovery after an upstream failure failed")
            raise

    def _finished(self, task: asyncio.Future[Any]) -> None:
        if not task.cancelled():
            exc = task.exception()  # retrieved here: nobody awaits the task when the client left
            if exc is not None:
                log.info(
                    "upstream call ended with %s (handled by the waiting request, if any)", type(exc).__name__
                )
        self._lock.release()


def _error(http_status: int, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=http_status)


def create_app(
    config: AdapterConfig, supervisor: Readiness, upstream: httpx.AsyncClient, gate: Gate | None = None
) -> FastAPI:
    gate = gate or Gate(config.queue_max_waiting, config.queue_wait_s)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(supervisor.run())
        try:
            yield
        finally:
            await supervisor.stop()
            try:
                await asyncio.wait_for(task, timeout=15.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task  # lets the supervisor stop the child before we close the client
            await upstream.aclose()

    app = FastAPI(title="decider-adapter", lifespan=lifespan)

    async def drain() -> None:
        """Hold the slot until the decider answers /health again (it is done with the abandoned call).

        Bounded: past `drain_timeout_s` the decider is reported unhealthy, so it is restarted instead of
        being handed another request while it may still be computing.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.drain_timeout_s
        while True:
            if await upstream_healthy(upstream, config.upstream_url) is Probe.OK:
                return
            if loop.time() >= deadline:
                await supervisor.report_unhealthy(
                    f"not idle {config.drain_timeout_s:g} s after a failed call"
                )
                return
            await asyncio.sleep(config.drain_poll_s)

    async def recover(exc: Exception) -> None:
        if isinstance(exc, httpx.TransportError):  # includes every timeout
            log.warning("upstream %s: holding the slot until it is idle again", type(exc).__name__)
            await drain()

    def attempt(request: Request, body: bytes) -> Callable[[], Awaitable[httpx.Response]]:
        async def call() -> httpx.Response:
            # The decider may have gone out of service (drain failed, crash) while this request queued.
            if not supervisor.ready:
                raise NotReady
            if await request.is_disconnected():
                log.info("client disconnected while queued; not forwarding")
                raise ClientGone
            return await forward(body)

        return call

    async def forward(body: bytes) -> httpx.Response:
        return await upstream.post(
            f"{config.upstream_url}{SYSTEMONE_PATH}",
            content=body,
            headers={"content-type": "application/json"},
            timeout=config.upstream_timeout_s,
        )

    @app.get("/ping")
    async def ping() -> JSONResponse:
        # Never touches the gate: SageMaker must get an answer even while an inference is running.
        return JSONResponse({"status": supervisor.state}, status_code=200 if supervisor.ready else 503)

    @app.post("/invocations")
    async def invocations(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            json.loads(body)
        except ValueError:  # includes UnicodeDecodeError and JSONDecodeError
            return _error(400, "invalid_json")
        if not supervisor.ready:
            return _error(503, "not_ready", state=supervisor.state)
        try:
            response, waited_ms = await gate.run(attempt(request, body), recover)
        except QueueFull:
            return _error(503, "queue_full")
        except QueueTimeout:
            return _error(503, "queue_timeout")
        except NotReady:
            return _error(503, "not_ready", state=supervisor.state)
        except ClientGone:
            return _error(499, "client_disconnected")
        except httpx.TimeoutException:
            return _error(504, "upstream_timeout")
        except httpx.HTTPError as exc:
            log.warning("upstream call failed: %s", type(exc).__name__)
            return _error(502, "upstream_unavailable")
        return _answer(response, waited_ms)

    return app


def _answer(response: httpx.Response, waited_ms: float) -> JSONResponse:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200:
        if isinstance(payload, dict):
            try:
                return JSONResponse(payload, status_code=response.status_code)
            except ValueError:  # NaN / Infinity: valid for Python's parser, not for a JSON response
                pass
        return _error(response.status_code, "upstream_error", status=response.status_code)
    if not isinstance(payload, dict):
        return _error(502, "bad_upstream_response")
    try:
        return JSONResponse(_with_adapter_metadata(payload, waited_ms))
    except ValueError:  # NaN / Infinity
        return _error(502, "bad_upstream_response")


def _with_adapter_metadata(payload: dict[str, Any], waited_ms: float) -> dict[str, Any]:
    """Add `adapter.queue_wait_ms` without discarding anything the upstream put under `adapter`."""
    ours = {"queue_wait_ms": round(waited_ms, 1)}
    if "adapter" not in payload:
        return {**payload, "adapter": ours}
    theirs = payload["adapter"]
    if isinstance(theirs, dict):
        return {**payload, "adapter": {**theirs, **ours}}  # the adapter owns queue_wait_ms
    return {**payload, "adapter": ours, "upstream_adapter": theirs}
