"""Decider client: question builders, circuit breaker and two transports (design A4, spec decider-client).

    GuardedDecider(transport)   deadline clamp + skip, one bounded retry, circuit breaker
      SageMakerDecider          boto3 `sagemaker-runtime invoke_endpoint` (AgentCore mode)
      HttpDecider               plain HTTP to `strands-decider serve` (local mode)

Every failure is a subclass of `ServerUnavailable`, so callers fall back to the LLM with one
`except ServerUnavailable`. There is no permanent "decider down" state: the breaker opens for a
cooldown and then lets one probe call through.

Logs carry latency, outcome and breaker state only. They never contain the state or the questions.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .config import ConfigError, RuntimeConfig

log = logging.getLogger("ctf_common.decider")

MODEL_ID = "hobson-latest"  # sent as `model`; the server answers as whatever checkpoint it loaded

DEFAULT_READ_TIMEOUT_S = 2.5  # SageMaker read-timeout ceiling (design: DECIDER_READ_S)
CONNECT_TIMEOUT_S = 1.0
MIN_CALL_BUDGET_S = 0.4  # below this there is no point in calling the endpoint
DEFAULT_SAFETY_MARGIN_S = 0.3
MAX_RETRIES = 1
FAILURE_THRESHOLD = 3
COOLDOWN_S = 30.0
MISCONFIGURED_COOLDOWN_S = 300.0
READ_BUCKET_STEP_S = 0.5  # read-timeout buckets: 0.5 s steps, so the client pool stays at about 6
LOCAL_READ_TIMEOUT_S = 120.0  # a local model may pay a one-off compile on its first request

# --------------------------------------------------------------------------- question builders


def noul(instructions: str, criteria: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Yes/no question. The answer is P(true)."""
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria:
        question["criteria"] = dict(criteria)
    return question


def choice(instructions: str, criteria: Mapping[str, str]) -> dict[str, Any]:
    """Pick one of N. `criteria` maps option name to what that option means."""
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, criteria: Sequence[str]) -> dict[str, Any]:
    """Rate against an ordered rubric, lowest level first."""
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


# --------------------------------------------------------------------------- errors


class Effect(StrEnum):
    """What a failure does to the circuit breaker."""

    COUNT = "count"  # +1 consecutive failure
    NONE = "none"  # nothing (the failure is ours, not the endpoint's)
    OPEN_LONG = "open_long"  # misconfiguration: open for MISCONFIGURED_COOLDOWN_S


class ServerUnavailable(RuntimeError):
    """The decider cannot answer this call. Callers fall back to the LLM."""

    retry = False
    effect = Effect.COUNT


class DeciderSkipped(ServerUnavailable):
    """Not enough time left in the tick to call. Tick-local: never touches the breaker."""

    effect = Effect.NONE


class DeciderCircuitOpen(ServerUnavailable):
    """The breaker is open (or a probe is already in flight): no call was made."""

    effect = Effect.NONE


class DeciderTimeout(ServerUnavailable):
    retry = True


class DeciderTransient(ServerUnavailable):
    retry = True


class DeciderBusy(ServerUnavailable):
    """The adapter queue is full (503 queue_full). Not retried: retrying would deepen the queue."""


class DeciderRejected(ServerUnavailable):
    """The model answered 4xx: a bug in our request. Logged, never counted against the endpoint."""

    effect = Effect.NONE


class DeciderMisconfigured(ServerUnavailable):
    """Access denied, endpoint not found or no credentials: opens the breaker for 300 s."""

    effect = Effect.OPEN_LONG


class DeciderProtocolError(ServerUnavailable):
    """The response was not the expected JSON."""


# --------------------------------------------------------------------------- circuit breaker


@dataclass(frozen=True)
class Admission:
    """The breaker's answer to one call. `owns_probe` is True only for the single half-open probe.

    `epoch` stamps the call: outcomes from an older epoch (the breaker opened or closed since the call
    started) are stale and ignored.
    """

    allowed: bool
    owns_probe: bool = False
    epoch: int = 0

    def __bool__(self) -> bool:
        return self.allowed


class CircuitBreaker:
    """closed -> open (K consecutive failures) -> half-open after the cooldown (one probe) -> closed.

    Thread-safe. `clock` is injectable for tests. The epoch increments on every open and close
    transition; an outcome carrying an older epoch is dropped.
    """

    def __init__(
        self,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_s: float = COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until: float | None = None
        self._probing = False
        self._epoch = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._open_until is None:
            return "closed"
        return "open" if self._clock() < self._open_until else "half_open"

    def acquire(self) -> Admission:
        """Admit a call. In half-open state exactly one probe is admitted at a time, and only its
        caller gets `owns_probe`."""
        with self._lock:
            state = self._state_locked()
            if state == "closed":
                return Admission(True, False, self._epoch)
            if state == "open" or self._probing:
                return Admission(False, False, self._epoch)
            self._probing = True
            return Admission(True, True, self._epoch)

    def allow(self) -> bool:
        """True when a call may go out (see `acquire` for probe ownership)."""
        return self.acquire().allowed

    def _stale(self, epoch: int | None) -> bool:
        return epoch is not None and epoch != self._epoch

    def record_success(self, epoch: int | None = None) -> None:
        with self._lock:
            if self._stale(epoch):
                return
            if self._open_until is not None:
                self._epoch += 1  # open/half-open -> closed
            self._failures = 0
            self._open_until = None
            self._probing = False

    def record_failure(self, epoch: int | None = None) -> None:
        with self._lock:
            if self._stale(epoch):
                return
            self._failures += 1
            if self._open_until is not None or self._failures >= self._threshold:
                self._open_locked(self._cooldown_s)  # a failed probe reopens; so does the K-th failure

    def open_for(self, seconds: float, epoch: int | None = None) -> None:
        with self._lock:
            if self._stale(epoch):
                return
            self._open_locked(seconds)

    def release_probe(self, epoch: int | None = None) -> None:
        """The probe owner ended without a verdict (e.g. a rejected request): let the next call probe."""
        with self._lock:
            if self._stale(epoch):
                return
            self._probing = False

    def _open_locked(self, seconds: float) -> None:
        self._open_until = self._clock() + seconds
        self._probing = False
        self._epoch += 1


# --------------------------------------------------------------------------- port and guard


class DeciderPort(Protocol):
    calls: int
    input_tokens: int
    last_latency_ms: float | None
    last_queue_wait_ms: float | None

    def ask(
        self, state: Any, questions: Mapping[str, dict[str, Any]], *, deadline_s: float | None = None
    ) -> dict[str, Any]:
        """Answers keyed like `questions`. `deadline_s` is the time left in the tick, when known."""
        ...


class Transport(Protocol):
    def invoke(self, body: bytes, timeout_s: float) -> bytes:
        """POST the body and return the raw response. Raises a `ServerUnavailable` subclass."""
        ...


def effective_timeout_s(configured_s: float, remaining_s: float | None, margin_s: float) -> float | None:
    """`min(configured, remaining - margin)`, or None when less than MIN_CALL_BUDGET_S is left (pure)."""
    if remaining_s is None:
        return configured_s
    budget = remaining_s - margin_s
    if budget < MIN_CALL_BUDGET_S - 1e-9:
        return None
    return min(configured_s, budget)


class GuardedDecider:
    """Deadline clamp, bounded retry and circuit breaker around a `Transport`."""

    def __init__(
        self,
        transport: Transport,
        *,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        safety_margin_s: float = DEFAULT_SAFETY_MARGIN_S,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self._read_timeout_s = read_timeout_s
        self._margin_s = safety_margin_s
        self._max_retries = max_retries
        self.calls = 0
        self.input_tokens = 0
        self.last_latency_ms: float | None = None
        self.last_queue_wait_ms: float | None = None
        self._unavailable_announced = False  # one INFO line per outage, not one per probe

    def ask(
        self, state: Any, questions: Mapping[str, dict[str, Any]], *, deadline_s: float | None = None
    ) -> dict[str, Any]:
        started = self._clock()
        try:
            body = json.dumps({"state": state, "model": MODEL_ID, "questions": dict(questions)}).encode()
        except (TypeError, ValueError, RecursionError) as exc:
            raise self._outcome(DeciderRejected("state is not JSON-serializable"), started) from exc
        end = None if deadline_s is None else started + deadline_s
        attempt = 0
        last: ServerUnavailable | None = None
        admission = Admission(False)
        verdict = False  # becomes True when the breaker was told the outcome
        try:
            while True:
                remaining = None if end is None else end - self._clock()
                timeout = effective_timeout_s(self._read_timeout_s, remaining, self._margin_s)
                if timeout is None:
                    if attempt == 0:
                        raise self._outcome(DeciderSkipped("not enough time left in the tick"), started)
                    break  # no budget for the retry: report the failure of the first attempt
                if attempt == 0:
                    admission = self.breaker.acquire()
                    if not admission.allowed:
                        raise self._outcome(DeciderCircuitOpen("circuit breaker is open"), started)
                try:
                    answers = self._parse(self._transport.invoke(body, timeout))
                except ServerUnavailable as exc:
                    last = exc
                    if exc.retry and attempt < self._max_retries:
                        attempt += 1
                        continue
                    break
                self.breaker.record_success(admission.epoch)
                self._unavailable_announced = False
                verdict = True
                self._log(started, "ok")
                return answers
            verdict = True
            if last is None:  # unreachable: the loop only breaks out after a failed attempt
                raise DeciderProtocolError("decider call ended without an outcome")
            raise self._record_failure(last, started, admission)
        finally:
            # Only the probe's owner may release it: a denied or skipped caller never held it.
            if not verdict and admission.owns_probe:
                self.breaker.release_probe(admission.epoch)

    # ---- internals -------------------------------------------------------

    def _parse(self, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise DeciderProtocolError("decider response is not JSON") from exc
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            raise DeciderProtocolError("decider response has no 'answers' object")
        usage = payload.get("usage")
        adapter = payload.get("adapter")
        self.calls += 1
        self.input_tokens += _count(usage.get("input_tokens")) if isinstance(usage, dict) else 0
        self.last_latency_ms = _number(payload.get("latency_ms"))
        self.last_queue_wait_ms = _number(adapter.get("queue_wait_ms")) if isinstance(adapter, dict) else None
        return answers

    def _record_failure(
        self, exc: ServerUnavailable, started: float, admission: Admission
    ) -> ServerUnavailable:
        if exc.effect is Effect.COUNT:
            self.breaker.record_failure(admission.epoch)
        elif exc.effect is Effect.OPEN_LONG:
            self.breaker.open_for(MISCONFIGURED_COOLDOWN_S, admission.epoch)
            if not self._unavailable_announced:
                self._unavailable_announced = True
                log.info("decider endpoint unavailable (%s); playing on the LLM fallback", type(exc).__name__)
        elif admission.owns_probe:
            self.breaker.release_probe(admission.epoch)
        if isinstance(exc, DeciderRejected):
            log.warning("decider rejected the request (this is a bug in the request, not the endpoint)")
        return self._outcome(exc, started)

    def _outcome(self, exc: ServerUnavailable, started: float) -> ServerUnavailable:
        self._log(started, type(exc).__name__)
        return exc

    def _log(self, started: float, outcome: str) -> None:
        log.info(
            "decider outcome=%s latency_ms=%.0f breaker=%s",
            outcome,
            (self._clock() - started) * 1000,
            self.breaker.state,
        )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _count(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


# --------------------------------------------------------------------------- SageMaker transport

RuntimeClientFactory = Callable[[float], Any]

_TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "ServiceUnavailable",
        "InternalFailure",
        "InternalDependencyException",
        "ModelNotReadyException",
    }
)
_MISCONFIGURED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "ResourceNotFound",
        "ResourceNotFoundException",
        "ValidationError",
        "ValidationException",
    }
)


def default_runtime_client(read_timeout_s: float) -> Any:
    """Real sagemaker-runtime client (imported lazily so tests need no AWS setup)."""
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "sagemaker-runtime",
        config=Config(
            connect_timeout=min(CONNECT_TIMEOUT_S, read_timeout_s),  # never longer than the call budget
            read_timeout=read_timeout_s,
            retries={"total_max_attempts": 1},  # retries are ours: bounded by the tick deadline
        ),
    )


def map_sagemaker_error(exc: BaseException) -> ServerUnavailable:
    """Map a botocore failure to the design's error table. Every input maps to a ServerUnavailable."""
    from botocore import exceptions as bex  # noqa: PLC0415

    if isinstance(exc, bex.ClientError):
        return _map_client_error(exc)
    if isinstance(exc, bex.ReadTimeoutError | bex.ConnectTimeoutError | bex.EndpointConnectionError):
        return DeciderTimeout(type(exc).__name__)
    if isinstance(exc, bex.ConnectionError | bex.HTTPClientError):
        return DeciderTransient(type(exc).__name__)
    if isinstance(exc, bex.NoCredentialsError | bex.PartialCredentialsError | bex.NoRegionError):
        return DeciderMisconfigured(type(exc).__name__)
    if isinstance(exc, bex.ParamValidationError):
        return DeciderRejected(type(exc).__name__)
    return DeciderProtocolError(f"unexpected {type(exc).__name__}")


def _map_client_error(exc: Any) -> ServerUnavailable:
    response = exc.response if isinstance(exc.response, dict) else {}
    code = response.get("Error", {}).get("Code", "")
    if code == "ModelError":
        original = response.get("OriginalStatusCode")
        if original == 503:
            return DeciderBusy("adapter queue is full")
        if isinstance(original, int) and 400 <= original < 500:
            return DeciderRejected(f"model answered {original}")
        return DeciderTransient(f"model error {original}")
    if code in _TRANSIENT_CODES:
        return DeciderTransient(code)
    if code in _MISCONFIGURED_CODES:
        return DeciderMisconfigured(code)
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    if isinstance(status, int) and status >= 500:
        return DeciderTransient(code or f"HTTP {status}")
    return DeciderProtocolError(f"unexpected error {code or 'unknown'}")


def read_bucket(timeout_s: float) -> float:
    """Round DOWN to a 0.5 s bucket (0.5 ... 2.5), so a clamp is never exceeded.

    The call guard never asks for less than MIN_CALL_BUDGET_S, so anything under the first bucket maps
    to it (0.4). The pool is therefore {0.4, 0.5, 1.0, 1.5, 2.0, 2.5}: at most 6 clients.
    """
    stepped = math.floor(timeout_s / READ_BUCKET_STEP_S + 1e-9) * READ_BUCKET_STEP_S
    return stepped if stepped >= READ_BUCKET_STEP_S else MIN_CALL_BUDGET_S


class SageMakerTransport:
    """`invoke_endpoint` through a pool of clients keyed by read timeout.

    botocore cannot set a read timeout per call, so one client is created lazily per timeout
    bucket (rounded DOWN to 0.5 s, so a clamp is never exceeded). Most ticks use the ceiling.
    """

    def __init__(self, endpoint_name: str, client_factory: RuntimeClientFactory | None = None) -> None:
        self._endpoint = endpoint_name
        self._factory = client_factory or default_runtime_client
        self._clients: dict[float, Any] = {}
        self._lock = threading.Lock()

    def _client(self, timeout_s: float) -> tuple[Any, float]:
        bucket = read_bucket(timeout_s)
        with self._lock:
            if bucket not in self._clients:
                self._clients[bucket] = self._factory(bucket)
            return self._clients[bucket], bucket

    def invoke(self, body: bytes, timeout_s: float) -> bytes:
        client, _ = self._client(timeout_s)
        try:
            response = client.invoke_endpoint(
                EndpointName=self._endpoint,
                ContentType="application/json",
                Accept="application/json",
                Body=body,
            )
            return response["Body"].read()
        except ServerUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped; nothing escapes unmapped
            raise map_sagemaker_error(exc) from exc


class SageMakerDecider(GuardedDecider):
    """AgentCore mode: the decider runs on a SageMaker endpoint, called with the runtime role (SigV4)."""

    def __init__(
        self, endpoint_name: str, *, client_factory: RuntimeClientFactory | None = None, **kwargs: Any
    ) -> None:
        super().__init__(SageMakerTransport(endpoint_name, client_factory), **kwargs)


# --------------------------------------------------------------------------- HTTP transport (local)


class HttpTransport:
    """POST `/v1/systemone` on a local `strands-decider serve`."""

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")

    def invoke(self, body: bytes, timeout_s: float) -> bytes:
        request = urllib.request.Request(
            f"{self._url}/v1/systemone", data=body, headers={"content-type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - http(s) only
                return response.read()
        except urllib.error.HTTPError as exc:
            raise _map_http_status(exc.code) from exc
        except TimeoutError as exc:
            raise DeciderTimeout("timeout") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError | socket.timeout):
                raise DeciderTimeout("timeout") from exc
            raise DeciderTransient(f"no decider at {self._url}") from exc
        except OSError as exc:  # includes http.client.HTTPException subclasses raised as OSError
            raise DeciderTransient(type(exc).__name__) from exc
        except Exception as exc:  # noqa: BLE001 - http.client.HTTPException (RemoteDisconnected) is not an OSError
            raise DeciderTransient(type(exc).__name__) from exc


def _map_http_status(status: int) -> ServerUnavailable:
    if status == 503:
        return DeciderBusy("decider is busy")
    if 400 <= status < 500:
        return DeciderRejected(f"HTTP {status}")
    return DeciderTransient(f"HTTP {status}")


class HttpDecider(GuardedDecider):
    """Local mode: `strands-decider serve` over HTTP, with the same guard as the SageMaker path."""

    def __init__(self, url: str, *, read_timeout_s: float = LOCAL_READ_TIMEOUT_S, **kwargs: Any) -> None:
        super().__init__(HttpTransport(url), read_timeout_s=read_timeout_s, **kwargs)


# --------------------------------------------------------------------------- composition


def make_decider(config: RuntimeConfig, **kwargs: Any) -> GuardedDecider:
    """HTTP for a local `decider_url`, SageMaker for a resolved endpoint name."""
    if config.decider_url:
        return HttpDecider(config.decider_url, **kwargs)
    if config.decider_endpoint_name:
        return SageMakerDecider(config.decider_endpoint_name, **kwargs)
    raise ConfigError("no decider configured (neither a decider URL nor an endpoint name)")
