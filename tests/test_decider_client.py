"""Decider client: question builders, circuit breaker, SageMaker transport (Stubber), HTTP transport."""

import importlib.util
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
    ReadTimeoutError,
)
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from ctf_common.config import ConfigError, RuntimeConfig
from ctf_common.decider import (
    CircuitBreaker,
    DeciderBusy,
    DeciderCircuitOpen,
    DeciderMisconfigured,
    DeciderProtocolError,
    DeciderRejected,
    DeciderSkipped,
    DeciderTimeout,
    DeciderTransient,
    HttpDecider,
    SageMakerDecider,
    ServerUnavailable,
    choice,
    make_decider,
    noul,
    score,
)

LOCAL_CLIENT = Path(__file__).resolve().parents[1] / "local" / "agent" / "_client.py"
ENDPOINT = "ctf-decider"
QUESTIONS = {"threat": noul("Is there a threat?", {"true": "yes", "false": "no"})}
ANSWERS = {"threat": {"type": "noul", "noul": 0.9}}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def body_of(payload) -> StreamingBody:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


def ok_payload(answers=None, **extra):
    return {"answers": answers or ANSWERS, "usage": {"input_tokens": 7}, "latency_ms": 41.5, **extra}


def sm_client():
    return boto3.client(
        "sagemaker-runtime", region_name="us-east-2", aws_access_key_id="x", aws_secret_access_key="y"
    )


class Rig:
    """A SageMakerDecider wired to a Stubber-backed client and a fake clock."""

    def __init__(self, **kwargs):
        self.clock = FakeClock()
        self.client = sm_client()
        self.stubber = Stubber(self.client)
        self.stubber.activate()
        self.timeouts: list[float] = []
        kwargs.setdefault("read_timeout_s", 2.5)
        self.decider = SageMakerDecider(ENDPOINT, client_factory=self.factory, clock=self.clock, **kwargs)

    def factory(self, read_timeout_s):
        self.timeouts.append(read_timeout_s)
        return self.client

    def respond(self, payload=None, *, expected_body=None):
        params = {"EndpointName": ENDPOINT, "ContentType": "application/json", "Accept": "application/json"}
        params["Body"] = expected_body if expected_body is not None else ANY
        self.stubber.add_response(
            "invoke_endpoint",
            {
                "Body": body_of(payload if payload is not None else ok_payload()),
                "ContentType": "application/json",
            },
            expected_params=params,
        )

    def fail(self, code, status=400, **fields):
        self.stubber.add_client_error(
            "invoke_endpoint",
            service_error_code=code,
            service_message=code,
            http_status_code=status,
            modeled_fields=fields or None,
        )


# --------------------------------------------------------------------------- builders


def load_local_client():
    spec = importlib.util.spec_from_file_location("local_decider_client", LOCAL_CLIENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestQuestionBuilders:
    def test_noul_without_and_with_criteria(self):
        assert noul("q") == {"type": "noul", "instructions": "q"}
        assert noul("q", {"true": "t", "false": "f"}) == {
            "type": "noul",
            "instructions": "q",
            "criteria": {"true": "t", "false": "f"},
        }

    def test_choice_and_score_shapes(self):
        assert choice("pick", {"a": "first"}) == {
            "type": "choice",
            "instructions": "pick",
            "criteria": {"a": "first"},
        }
        assert score("rate", ["low", "high"]) == {
            "type": "score",
            "instructions": "rate",
            "criteria": ["low", "high"],
        }

    def test_builders_match_the_local_client_wire_shape(self):
        local = load_local_client().Decider
        criteria = {"true": "t", "false": "f"}
        assert noul("q", criteria) == local.noul("q", criteria)
        assert noul("q") == local.noul("q")
        assert choice("c", {"a": "x", "b": "y"}) == local.choice("c", {"a": "x", "b": "y"})
        assert score("s", ["l", "h"]) == local.score("s", ["l", "h"])


# --------------------------------------------------------------------------- breaker


class TestCircuitBreaker:
    def make(self):
        clock = FakeClock()
        return CircuitBreaker(failure_threshold=3, cooldown_s=30.0, clock=clock), clock

    def test_stays_closed_below_the_threshold(self):
        breaker, _ = self.make()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == "closed" and breaker.allow()

    def test_a_success_resets_the_consecutive_count(self):
        breaker, _ = self.make()
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == "closed"

    def test_opens_after_three_consecutive_failures_and_blocks_calls(self):
        breaker, _ = self.make()
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state == "open" and not breaker.allow()

    def test_after_the_cooldown_exactly_one_probe_is_allowed(self):
        breaker, clock = self.make()
        for _ in range(3):
            breaker.record_failure()
        clock.advance(29.9)
        assert not breaker.allow()
        clock.advance(0.2)
        assert breaker.state == "half_open"
        assert breaker.allow() is True
        assert breaker.allow() is False  # the probe is in flight

    def test_probe_success_closes_the_breaker(self):
        breaker, clock = self.make()
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31)
        assert breaker.allow()
        breaker.record_success()
        assert breaker.state == "closed" and breaker.allow()

    def test_probe_failure_reopens_for_a_new_cooldown(self):
        breaker, clock = self.make()
        for _ in range(3):
            breaker.record_failure()
        clock.advance(31)
        assert breaker.allow()
        breaker.record_failure()
        assert breaker.state == "open"
        clock.advance(29)
        assert not breaker.allow()
        clock.advance(2)
        assert breaker.allow()

    def test_open_for_uses_its_own_duration(self):
        breaker, clock = self.make()
        breaker.open_for(300)
        clock.advance(299)
        assert breaker.state == "open"
        clock.advance(2)
        assert breaker.state == "half_open"

    def test_release_probe_lets_another_probe_through(self):
        breaker, clock = self.make()
        breaker.open_for(10)
        clock.advance(11)
        assert breaker.allow() and not breaker.allow()
        breaker.release_probe()
        assert breaker.allow()


# --------------------------------------------------------------------------- SageMaker call


class TestSageMakerCall:
    def test_success_sends_the_systemone_body_and_returns_the_answers(self):
        rig = Rig()
        expected = json.dumps({"state": "S", "model": "hobson-latest", "questions": QUESTIONS}).encode()
        rig.respond(ok_payload(adapter={"queue_wait_ms": 12}), expected_body=expected)
        answers = rig.decider.ask("S", QUESTIONS)
        rig.stubber.assert_no_pending_responses()
        assert answers == ANSWERS
        assert rig.decider.calls == 1
        assert rig.decider.input_tokens == 7
        assert rig.decider.last_latency_ms == 41.5
        assert rig.decider.last_queue_wait_ms == 12

    def test_two_calls_accumulate_the_counters(self):
        rig = Rig()
        rig.respond()
        rig.respond()
        rig.decider.ask("S", QUESTIONS)
        rig.decider.ask("S", QUESTIONS)
        assert rig.decider.calls == 2 and rig.decider.input_tokens == 14

    @pytest.mark.parametrize(
        "payload",
        [b"not json at all", {"usage": {}}, {"answers": "nope"}, [1, 2]],
        ids=["non-json", "no-answers", "answers-not-a-mapping", "not-an-object"],
    )
    def test_protocol_errors_are_mapped_and_not_retried(self, payload):
        rig = Rig()
        rig.respond(payload)
        with pytest.raises(DeciderProtocolError):
            rig.decider.ask("S", QUESTIONS)
        rig.stubber.assert_no_pending_responses()  # exactly one call


# --------------------------------------------------------------------------- clamp and skip


class TestDeadlineClamp:
    def test_read_timeout_is_the_configured_ceiling_without_a_deadline(self):
        rig = Rig(read_timeout_s=4.5)
        rig.respond()
        rig.decider.ask("S", QUESTIONS)
        assert rig.timeouts == [4.5]

    def test_timeout_is_clamped_to_the_time_left_in_the_tick(self):
        rig = Rig(read_timeout_s=4.5, safety_margin_s=0.0)
        rig.respond()
        rig.decider.ask("S", QUESTIONS, deadline_s=2.0)
        assert rig.timeouts == [2.0]

    def test_the_safety_margin_is_subtracted_before_clamping(self):
        rig = Rig(read_timeout_s=4.5, safety_margin_s=0.3)
        rig.respond()
        rig.decider.ask("S", QUESTIONS, deadline_s=2.3)
        assert rig.timeouts == [2.0]

    def test_a_generous_deadline_keeps_the_configured_timeout(self):
        rig = Rig(read_timeout_s=2.5, safety_margin_s=0.3)
        rig.respond()
        rig.decider.ask("S", QUESTIONS, deadline_s=4.0)
        assert rig.timeouts == [2.5]

    def test_less_than_400ms_after_the_margin_skips_the_call_without_touching_the_breaker(self):
        rig = Rig(safety_margin_s=0.3)
        rig.decider.breaker.record_failure()
        rig.decider.breaker.record_failure()
        with pytest.raises(DeciderSkipped):
            rig.decider.ask("S", QUESTIONS, deadline_s=0.69)  # 0.69 - 0.3 = 0.39 < 0.4
        # nothing was stubbed, so a real call would have failed with a different error
        assert rig.decider.calls == 0
        # The skip left the failure count at 2: one more real failure opens the breaker.
        rig.fail("ServiceUnavailable", 503)
        rig.fail("ServiceUnavailable", 503)
        with pytest.raises(DeciderTransient):
            rig.decider.ask("S", QUESTIONS)
        assert rig.decider.breaker.state == "open"

    def test_exactly_400ms_of_budget_still_calls(self):
        rig = Rig(safety_margin_s=0.3)
        rig.respond()
        rig.decider.ask("S", QUESTIONS, deadline_s=0.7)
        assert rig.timeouts == [0.4]


# --------------------------------------------------------------------------- retry


class TestRetry:
    def test_throttled_once_then_success_returns_the_second_result(self):
        rig = Rig()
        rig.fail("ThrottlingException")
        rig.respond()
        assert rig.decider.ask("S", QUESTIONS) == ANSWERS
        rig.stubber.assert_no_pending_responses()
        assert rig.decider.breaker.state == "closed"

    def test_exhausted_retries_count_as_one_failure(self):
        rig = Rig()
        for _ in range(2):  # two asks, each fails twice (original + 1 retry)
            rig.fail("ServiceUnavailable", 503)
            rig.fail("ServiceUnavailable", 503)
            with pytest.raises(DeciderTransient):
                rig.decider.ask("S", QUESTIONS)
        assert rig.decider.breaker.state == "closed"  # two failures, not four
        rig.fail("ServiceUnavailable", 503)
        rig.fail("ServiceUnavailable", 503)
        with pytest.raises(DeciderTransient):
            rig.decider.ask("S", QUESTIONS)
        assert rig.decider.breaker.state == "open"
        rig.stubber.assert_no_pending_responses()

    def test_no_retry_when_the_tick_budget_is_spent(self):
        rig = Rig(safety_margin_s=0.3)
        rig.fail("ThrottlingException")
        rig.stubber.add_response("invoke_endpoint", {"Body": body_of(ok_payload())})  # must stay unused

        original = rig.client.invoke_endpoint

        def slow(**kwargs):
            rig.clock.advance(2.0)
            return original(**kwargs)

        rig.client.invoke_endpoint = slow
        with pytest.raises(DeciderTransient):
            rig.decider.ask("S", QUESTIONS, deadline_s=2.5)  # 2.5 - 2.0 - 0.3 = 0.2 < 0.4 after the first try
        with pytest.raises(AssertionError):
            rig.stubber.assert_no_pending_responses()  # the stubbed second response was never consumed

    def test_a_retry_is_clamped_to_what_is_left(self):
        rig = Rig(safety_margin_s=0.0, read_timeout_s=2.5)
        rig.fail("ThrottlingException")
        rig.respond()
        original = rig.client.invoke_endpoint

        def slow(**kwargs):
            rig.clock.advance(1.0)
            return original(**kwargs)

        rig.client.invoke_endpoint = slow
        rig.decider.ask("S", QUESTIONS, deadline_s=2.0)
        assert rig.timeouts == [2.0, 1.0]  # the retry only gets the remaining second


# --------------------------------------------------------------------------- error map


class FakeRuntime:
    """A runtime client whose invoke_endpoint raises scripted exceptions (connection-level failures)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke_endpoint(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"Body": body_of(outcome)}


def fake_rig(*outcomes, **kwargs):
    clock = FakeClock()
    runtime = FakeRuntime(*outcomes)
    decider = SageMakerDecider(ENDPOINT, client_factory=lambda timeout: runtime, clock=clock, **kwargs)
    return decider, runtime, clock


class TestErrorMap:
    @pytest.mark.parametrize(
        "code,status",
        [
            ("ThrottlingException", 400),
            ("ServiceUnavailable", 503),
            ("InternalFailure", 500),
            ("ModelNotReadyException", 429),
            ("InternalDependencyException", 500),
        ],
    )
    def test_transient_codes_are_retried_once_and_count_one_failure(self, code, status):
        rig = Rig()
        rig.fail(code, status)
        rig.fail(code, status)
        with pytest.raises(DeciderTransient) as info:
            rig.decider.ask("S", QUESTIONS)
        assert isinstance(info.value, ServerUnavailable)
        rig.stubber.assert_no_pending_responses()

    def test_adapter_queue_full_is_busy_not_retried_and_counts(self):
        rig = Rig()
        rig.fail("ModelError", 424, OriginalStatusCode=503, OriginalMessage="queue_full")
        with pytest.raises(DeciderBusy):
            rig.decider.ask("S", QUESTIONS)
        rig.stubber.assert_no_pending_responses()  # one call only
        rig.decider.breaker.record_failure()
        rig.decider.breaker.record_failure()
        assert rig.decider.breaker.state == "open"  # the busy answer was failure #1

    def test_model_4xx_is_rejected_logged_as_a_bug_and_never_counts(self, caplog):
        rig = Rig()
        for _ in range(4):
            rig.fail("ModelError", 424, OriginalStatusCode=422, OriginalMessage="bad question")
        with caplog.at_level(logging.WARNING, logger="ctf_common.decider"):
            for _ in range(4):
                with pytest.raises(DeciderRejected):
                    rig.decider.ask("S", QUESTIONS)
        assert rig.decider.breaker.state == "closed"  # 4 rejections > threshold, none counted
        assert "bug" in caplog.text.lower()

    @pytest.mark.parametrize(
        "code", ["AccessDeniedException", "AccessDenied", "ValidationError", "ResourceNotFound"]
    )
    def test_misconfiguration_opens_the_breaker_for_300_seconds(self, code):
        rig = Rig()
        rig.fail(code, 400)
        with pytest.raises(DeciderMisconfigured):
            rig.decider.ask("S", QUESTIONS)
        rig.stubber.assert_no_pending_responses()  # not retried
        assert rig.decider.breaker.state == "open"
        rig.clock.advance(299)
        with pytest.raises(DeciderCircuitOpen):
            rig.decider.ask("S", QUESTIONS)
        rig.clock.advance(2)
        assert rig.decider.breaker.state == "half_open"

    def test_a_missing_endpoint_is_a_misconfiguration_not_a_retried_transient_failure(self, caplog):
        """sagemaker-runtime answers a non-existent endpoint with ValidationError, HTTP 400."""
        rig = Rig()
        rig.stubber.add_client_error(
            "invoke_endpoint",
            service_error_code="ValidationError",
            service_message=f"Endpoint {ENDPOINT} of account 123456789012 not found.",
            http_status_code=400,
        )
        with caplog.at_level(logging.INFO, logger="ctf_common.decider"):
            with pytest.raises(DeciderMisconfigured) as info:
                rig.decider.ask("S", QUESTIONS)
        assert isinstance(info.value, ServerUnavailable)
        rig.stubber.assert_no_pending_responses()  # exactly one call: no retry burned the tick budget
        assert rig.decider.breaker.state == "open"
        rig.clock.advance(299)
        assert rig.decider.breaker.state == "open"
        rig.clock.advance(2)
        assert rig.decider.breaker.state == "half_open"
        unavailable = [r for r in caplog.records if "playing on the LLM fallback" in r.getMessage()]
        assert len(unavailable) == 1 and unavailable[0].levelno == logging.INFO

    def test_the_unavailable_notice_is_logged_once_per_outage_not_on_every_probe(self, caplog):
        rig = Rig()
        for _ in range(2):
            rig.fail("ValidationError", 400)
        with caplog.at_level(logging.INFO, logger="ctf_common.decider"):
            with pytest.raises(DeciderMisconfigured):
                rig.decider.ask("S", QUESTIONS)
            rig.clock.advance(301)
            with pytest.raises(DeciderMisconfigured):  # the re-probe fails too
                rig.decider.ask("S", QUESTIONS)
            rig.clock.advance(301)
            rig.respond()
            rig.decider.ask("S", QUESTIONS)  # recovered
            rig.fail("ValidationError", 400)
            rig.clock.advance(1)
            with pytest.raises(DeciderMisconfigured):  # a NEW outage is announced again
                rig.decider.ask("S", QUESTIONS)
        notices = [r for r in caplog.records if "playing on the LLM fallback" in r.getMessage()]
        assert len(notices) == 2

    @pytest.mark.parametrize(
        "error,expected",
        [
            (ReadTimeoutError(endpoint_url="u"), DeciderTimeout),
            (ConnectTimeoutError(endpoint_url="u"), DeciderTimeout),
            (EndpointConnectionError(endpoint_url="u"), DeciderTimeout),
            (ConnectionClosedError(endpoint_url="u"), DeciderTransient),
        ],
        ids=["read-timeout", "connect-timeout", "endpoint-connection", "connection-closed"],
    )
    def test_connection_failures_are_retried_once(self, error, expected):
        decider, runtime, _ = fake_rig(error, error)
        with pytest.raises(expected):
            decider.ask("S", QUESTIONS)
        assert runtime.calls == 2

    def test_missing_credentials_are_a_misconfiguration(self):
        decider, runtime, _ = fake_rig(NoCredentialsError())
        with pytest.raises(DeciderMisconfigured):
            decider.ask("S", QUESTIONS)
        assert runtime.calls == 1 and decider.breaker.state == "open"

    def test_a_request_we_built_wrongly_is_rejected_not_counted(self):
        decider, _, _ = fake_rig(ParamValidationError(report="bad"))
        with pytest.raises(DeciderRejected):
            decider.ask("S", QUESTIONS)
        assert decider.breaker.state == "closed"

    def test_anything_else_maps_to_a_protocol_error(self):
        decider, runtime, _ = fake_rig(RuntimeError("boom"))
        with pytest.raises(DeciderProtocolError):
            decider.ask("S", QUESTIONS)
        assert runtime.calls == 1

    def test_every_decider_error_is_a_server_unavailable(self):
        for cls in (
            DeciderTimeout,
            DeciderTransient,
            DeciderBusy,
            DeciderRejected,
            DeciderMisconfigured,
            DeciderProtocolError,
            DeciderSkipped,
            DeciderCircuitOpen,
        ):
            assert issubclass(cls, ServerUnavailable)


# --------------------------------------------------------------------------- breaker + fallback


class TestBreakerIntegration:
    def test_three_failed_asks_open_the_breaker_and_the_fourth_makes_no_call(self):
        rig = Rig()
        for _ in range(3):
            rig.fail("ModelError", 424, OriginalStatusCode=503)
            with pytest.raises(DeciderBusy):
                rig.decider.ask("S", QUESTIONS)
        with pytest.raises(DeciderCircuitOpen):
            rig.decider.ask("S", QUESTIONS)
        rig.stubber.assert_no_pending_responses()

    def test_probe_success_after_the_cooldown_restores_the_endpoint(self):
        rig = Rig()
        for _ in range(3):
            rig.fail("ModelError", 424, OriginalStatusCode=503)
            with pytest.raises(DeciderBusy):
                rig.decider.ask("S", QUESTIONS)
        rig.clock.advance(31)
        rig.respond()
        rig.respond()
        assert rig.decider.ask("S", QUESTIONS) == ANSWERS  # the probe
        assert rig.decider.breaker.state == "closed"
        assert rig.decider.ask("S", QUESTIONS) == ANSWERS

    def test_probe_failure_reopens(self):
        rig = Rig()
        for _ in range(3):
            rig.fail("ModelError", 424, OriginalStatusCode=503)
            with pytest.raises(DeciderBusy):
                rig.decider.ask("S", QUESTIONS)
        rig.clock.advance(31)
        rig.fail("ModelError", 424, OriginalStatusCode=503)
        with pytest.raises(DeciderBusy):
            rig.decider.ask("S", QUESTIONS)
        with pytest.raises(DeciderCircuitOpen):
            rig.decider.ask("S", QUESTIONS)

    def test_a_rejected_probe_does_not_wedge_the_breaker(self):
        rig = Rig()
        rig.decider.breaker.open_for(10)
        rig.clock.advance(11)
        rig.fail("ModelError", 424, OriginalStatusCode=422)
        with pytest.raises(DeciderRejected):
            rig.decider.ask("S", QUESTIONS)
        rig.respond()
        assert rig.decider.ask("S", QUESTIONS) == ANSWERS  # another probe is allowed

    def test_a_skipped_call_does_not_consume_the_probe(self):
        rig = Rig(safety_margin_s=0.3)
        rig.decider.breaker.open_for(10)
        rig.clock.advance(11)
        with pytest.raises(DeciderSkipped):
            rig.decider.ask("S", QUESTIONS, deadline_s=0.5)
        rig.respond()
        assert rig.decider.ask("S", QUESTIONS) == ANSWERS


class TestLogging:
    def test_logs_carry_latency_outcome_and_breaker_state_but_never_the_state_text(self, caplog):
        rig = Rig()
        rig.respond()
        rig.fail("ModelError", 424, OriginalStatusCode=503, OriginalMessage="SECRET-STATE leaked?")
        secret_questions = {"q": noul("SECRET-QUESTION text")}
        with caplog.at_level(logging.DEBUG, logger="ctf_common.decider"):
            rig.decider.ask("SECRET-STATE game dump", secret_questions)
            with pytest.raises(DeciderBusy):
                rig.decider.ask("SECRET-STATE game dump", secret_questions)
        text = caplog.text
        assert "outcome=ok" in text and "latency_ms=" in text and "breaker=closed" in text
        assert "outcome=DeciderBusy" in text
        assert "SECRET-STATE" not in text and "SECRET-QUESTION" not in text


# --------------------------------------------------------------------------- HTTP transport (local mode)


class _Handler(BaseHTTPRequestHandler):
    status = 200
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        type(self).seen.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
        data = json.dumps(ok_payload()).encode()
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    handler = type("H", (_Handler,), {"status": 200, "seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", handler
    server.shutdown()
    server.server_close()


class TestHttpDecider:
    def test_posts_the_same_systemone_body_the_local_client_sends(self, http_server):
        url, handler = http_server
        decider = HttpDecider(url)
        assert decider.ask("S", QUESTIONS) == ANSWERS
        assert handler.seen == [
            {
                "path": "/v1/systemone",
                "body": {"state": "S", "model": "hobson-latest", "questions": QUESTIONS},
            }
        ]
        assert decider.calls == 1 and decider.last_latency_ms == 41.5

    def test_server_503_is_busy(self, http_server):
        url, handler = http_server
        handler.status = 503
        with pytest.raises(DeciderBusy):
            HttpDecider(url).ask("S", QUESTIONS)

    def test_server_500_is_transient_and_retried_once(self, http_server):
        url, handler = http_server
        handler.status = 500
        with pytest.raises(DeciderTransient):
            HttpDecider(url).ask("S", QUESTIONS)
        assert len(handler.seen) == 2

    def test_connection_refused_is_server_unavailable_and_feeds_the_breaker(self):
        decider = HttpDecider("http://127.0.0.1:1", read_timeout_s=1.0)
        for _ in range(3):
            with pytest.raises(ServerUnavailable):
                decider.ask("S", QUESTIONS)
        assert decider.breaker.state == "open"
        with pytest.raises(DeciderCircuitOpen):
            decider.ask("S", QUESTIONS)


# --------------------------------------------------------------------------- composition root


class TestMakeDecider:
    def test_decider_url_selects_the_http_transport(self):
        assert isinstance(
            make_decider(RuntimeConfig(mcp_url="http://m", decider_url="http://d")), HttpDecider
        )

    def test_endpoint_name_selects_sagemaker(self):
        decider = make_decider(
            RuntimeConfig(mcp_url="http://m", decider_endpoint_name=ENDPOINT),
            client_factory=lambda timeout: None,
        )
        assert isinstance(decider, SageMakerDecider)

    def test_no_decider_in_the_config_is_an_error(self):
        with pytest.raises(ConfigError, match="decider"):
            make_decider(RuntimeConfig(mcp_url="http://m"))


# --------------------------------------------------------------------------- slice 2 review fixes


class BlockingTransport:
    """Blocks the first invoke until released; later invokes answer immediately."""

    def __init__(self, *, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail
        self.calls = 0

    def invoke(self, body, timeout_s):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        if self.fail:
            raise DeciderBusy("queue full")
        return json.dumps({"answers": ANSWERS}).encode()


def half_open_guard(transport):
    from ctf_common.decider import GuardedDecider

    clock = FakeClock()
    guard = GuardedDecider(transport, clock=clock)
    guard.breaker.open_for(10)
    clock.advance(11)
    assert guard.breaker.state == "half_open"
    return guard


def start_probe(guard):
    box = {}

    def run():
        try:
            box["result"] = guard.ask("S", QUESTIONS)
        except ServerUnavailable as exc:
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


class TestProbeOwnership:
    def test_a_denied_caller_does_not_release_the_probe_of_the_owner_and_success_closes(self):
        transport = BlockingTransport()
        guard = half_open_guard(transport)
        thread, box = start_probe(guard)
        assert transport.entered.wait(5)  # A owns the probe and is in flight
        with pytest.raises(DeciderCircuitOpen):
            guard.ask("S", QUESTIONS)  # B is turned away
        assert guard.breaker.allow() is False  # a third caller is still denied: A's probe survived
        transport.release.set()
        thread.join(5)
        assert box["result"] == ANSWERS
        assert guard.breaker.state == "closed"

    def test_a_denied_caller_does_not_release_the_probe_and_owner_failure_reopens(self):
        transport = BlockingTransport(fail=True)
        guard = half_open_guard(transport)
        thread, box = start_probe(guard)
        assert transport.entered.wait(5)
        with pytest.raises(DeciderCircuitOpen):
            guard.ask("S", QUESTIONS)
        assert guard.breaker.allow() is False
        transport.release.set()
        thread.join(5)
        assert isinstance(box["error"], DeciderBusy)
        assert guard.breaker.state == "open"

    def test_a_skipped_caller_does_not_release_a_probe_it_never_took(self):
        transport = BlockingTransport()
        guard = half_open_guard(transport)
        thread, _ = start_probe(guard)
        assert transport.entered.wait(5)
        with pytest.raises(DeciderSkipped):
            guard.ask("S", QUESTIONS, deadline_s=0.1)
        assert guard.breaker.allow() is False
        transport.release.set()
        thread.join(5)

    def test_acquire_reports_ownership_only_to_the_probe_caller(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        assert breaker.acquire().owns_probe is False  # closed: allowed, no probe
        breaker.open_for(10)
        assert breaker.acquire().allowed is False
        clock.advance(11)
        first, second = breaker.acquire(), breaker.acquire()
        assert (first.allowed, first.owns_probe) == (True, True)
        assert (second.allowed, second.owns_probe) == (False, False)


class TestBreakerEpoch:
    def test_a_stale_success_does_not_close_a_breaker_that_reopened(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_s=30.0, clock=clock)
        slow = breaker.acquire()  # a call that started while the breaker was closed
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state == "open"
        breaker.record_success(slow.epoch)  # the slow call finally answers
        assert breaker.state == "open"

    def test_a_stale_failure_does_not_reopen_a_breaker_that_closed(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=30.0, clock=clock)
        slow = breaker.acquire()
        breaker.record_failure()
        clock.advance(31)
        probe = breaker.acquire()
        breaker.record_success(probe.epoch)
        assert breaker.state == "closed"
        breaker.record_failure(slow.epoch)
        assert breaker.state == "closed"

    def test_a_current_epoch_outcome_still_counts(self):
        breaker = CircuitBreaker(failure_threshold=2, clock=FakeClock())
        admission = breaker.acquire()
        breaker.record_failure(admission.epoch)
        breaker.record_failure(admission.epoch)
        assert breaker.state == "open"


class TestConnectTimeout:
    def test_connect_timeout_never_exceeds_the_call_budget(self, monkeypatch):
        from ctf_common.decider import default_runtime_client

        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
        assert default_runtime_client(0.4).meta.config.connect_timeout == 0.4
        assert default_runtime_client(2.0).meta.config.connect_timeout == 1.0


class TestTimeoutBuckets:
    @pytest.mark.parametrize(
        ("timeout", "bucket"),
        [(0.4, 0.4), (0.49, 0.4), (0.5, 0.5), (0.99, 0.5), (1.0, 1.0), (1.7, 1.5), (2.3, 2.0), (2.5, 2.5)],
    )
    def test_the_read_timeout_rounds_down_to_a_coarse_bucket(self, timeout, bucket):
        rig = Rig(read_timeout_s=timeout, safety_margin_s=0.0)
        rig.respond()
        rig.decider.ask("S", QUESTIONS)
        assert rig.timeouts == [bucket]

    def test_the_pool_is_bounded_by_the_bucket_count(self):
        rig = Rig(read_timeout_s=2.5, safety_margin_s=0.0)
        for deadline in [x / 100 for x in range(40, 260, 3)]:
            rig.respond()
            rig.decider.ask("S", QUESTIONS, deadline_s=deadline)
        assert len(set(rig.timeouts)) <= 6
        assert len(rig.timeouts) <= 6  # one client per bucket, never one per call


class TestUnserializableState:
    def test_a_state_that_cannot_be_encoded_is_rejected_not_raised_raw(self):
        rig = Rig()
        with pytest.raises(DeciderRejected):
            rig.decider.ask({"bad": object()}, QUESTIONS)
        assert rig.decider.breaker.state == "closed"
