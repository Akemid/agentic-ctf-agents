"""SageMaker adapter HTTP surface: pass-through, FIFO queue, bounds, /ping (decider-endpoint spec)."""

import asyncio
import gc
import json
import time

import httpx
import pytest

from adapter.app import create_app
from adapter.config import AdapterConfig

ANSWER = {"answers": {"threat": {"label": "false"}}, "usage": {"input_tokens": 7}, "latency_ms": 12.5}


class FakeSupervisor:
    def __init__(self, ready=True, state="ready"):
        self.ready = ready
        self.state = state
        self.unhealthy_reports: list[str] = []

    async def run(self):
        await asyncio.Event().wait()

    async def stop(self):
        pass

    async def report_unhealthy(self, reason):
        self.unhealthy_reports.append(reason)
        self.ready = False
        self.state = "starting"


class FakeDecider:
    """The upstream `strands-decider serve`. Records what it sees; a request blocks until released."""

    def __init__(self):
        self.seen: list[bytes] = []
        self.paths: list[str] = []
        self.active = 0
        self.max_active = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self.hold: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}
        self.delay = 0.0
        self.respond = None
        self.events: list[tuple[str, str]] = []
        self.linger_next = 0.0  # keep computing this long after the client gave up (a timeout)
        self.health_calls = 0
        self.health_script: list[bool] = []
        self._background: set[asyncio.Task] = set()

    def block(self, name: str) -> asyncio.Event:
        self.hold[name] = asyncio.Event()
        self.entered[name] = asyncio.Event()
        return self.hold[name]

    def _health(self, request: httpx.Request) -> httpx.Response:
        """`/health`: answers only when idle (the real decider cannot answer while it computes)."""
        self.health_calls += 1
        healthy = self.health_script.pop(0) if self.health_script else self.active == 0
        if not healthy:
            raise httpx.ReadTimeout("busy", request=request)
        return httpx.Response(200, json={"status": "ok"})

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return self._health(request)
        body = request.content
        name = json.loads(body).get("name", "") if body else ""
        self.seen.append(body)
        self.paths.append(request.url.path)
        self.started.append(name)
        self.events.append(("start", name))
        if self.linger_next:
            seconds, self.linger_next = self.linger_next, 0.0
            self.active += 1
            self.max_active = max(self.max_active, self.active)

            async def keep_computing() -> None:
                await asyncio.sleep(seconds)
                self.active -= 1
                self.finished.append(name)
                self.events.append(("end", name))

            task = asyncio.ensure_future(keep_computing())
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            raise httpx.ReadTimeout("slow", request=request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if name in self.entered:
                self.entered[name].set()
            if name in self.hold:
                await self.hold[name].wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.respond is not None:
                return self.respond(request)
            return httpx.Response(200, json=ANSWER)
        finally:
            self.active -= 1
            self.finished.append(name)
            self.events.append(("end", name))


def build(decider: FakeDecider, supervisor=None, **overrides):
    config = AdapterConfig(**{"queue_wait_s": 2.0, "upstream_timeout_s": 5.0, **overrides})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(decider))
    app = create_app(config, supervisor or FakeSupervisor(), upstream)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://adapter")
    return app, client


def invoke(client, name="a", **extra):
    return client.post("/invocations", content=json.dumps({"name": name, **extra}))


@pytest.fixture
def decider():
    return FakeDecider()


class TestPassThrough:
    async def test_forwards_the_body_unchanged_to_systemone(self, decider):
        _, client = build(decider)
        raw = b'{"state": "tick: 1", "model": "hobson-latest", "questions": {"q": {"type": "noul"}}, "n": 1}'
        response = await client.post("/invocations", content=raw)
        assert response.status_code == 200
        assert decider.seen == [raw]
        assert decider.paths == ["/v1/systemone"]

    async def test_returns_the_upstream_json_plus_queue_wait(self, decider):
        _, client = build(decider)
        payload = (await invoke(client)).json()
        assert payload["answers"] == ANSWER["answers"]
        assert payload["usage"] == ANSWER["usage"]
        assert payload["latency_ms"] == ANSWER["latency_ms"]
        assert set(payload["adapter"]) == {"queue_wait_ms"}
        assert payload["adapter"]["queue_wait_ms"] >= 0

    async def test_queue_wait_reflects_time_spent_behind_another_request(self, decider):
        decider.delay = 0.15
        _, client = build(decider)
        first, second = await asyncio.gather(invoke(client, "a"), _later(0.02, invoke(client, "b")))
        assert first.json()["adapter"]["queue_wait_ms"] < 50
        assert second.json()["adapter"]["queue_wait_ms"] >= 100

    async def test_invalid_json_is_a_400_and_never_reaches_the_decider(self, decider):
        _, client = build(decider)
        for body in (b"{not json", b"", b"\xff\xfe"):
            response = await client.post("/invocations", content=body)
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_json"
        assert decider.seen == []

    async def test_not_ready_is_a_503_and_never_reaches_the_decider(self, decider):
        _, client = build(decider, FakeSupervisor(ready=False, state="starting"))
        response = await invoke(client)
        assert response.status_code == 503
        assert response.json()["error"] == "not_ready"
        assert decider.seen == []


class TestUpstreamFailures:
    async def test_upstream_client_error_is_passed_through(self, decider):
        decider.respond = lambda request: httpx.Response(422, json={"detail": "bad question"})
        _, client = build(decider)
        response = await invoke(client)
        assert response.status_code == 422
        assert response.json() == {"detail": "bad question"}

    async def test_upstream_server_error_with_html_body_is_wrapped(self, decider):
        decider.respond = lambda request: httpx.Response(500, text="<html>boom</html>")
        _, client = build(decider)
        response = await invoke(client)
        assert response.status_code == 500
        assert response.json() == {"error": "upstream_error", "status": 500}

    async def test_unreachable_upstream_is_a_502(self, decider):
        def refuse(request):
            raise httpx.ConnectError("refused")

        decider.respond = refuse
        _, client = build(decider)
        response = await invoke(client)
        assert response.status_code == 502
        assert response.json()["error"] == "upstream_unavailable"

    async def test_slow_upstream_is_a_504(self, decider):
        def too_slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        decider.respond = too_slow
        _, client = build(decider)
        response = await invoke(client)
        assert response.status_code == 504
        assert response.json()["error"] == "upstream_timeout"

    async def test_the_upstream_call_carries_the_configured_timeout(self, decider):
        timeouts = []

        def record(request):
            timeouts.append(request.extensions["timeout"])
            return httpx.Response(200, json=ANSWER)

        decider.respond = record
        _, client = build(decider, upstream_timeout_s=3.5)
        await invoke(client)
        assert timeouts[0]["read"] == 3.5

    async def test_a_200_that_is_not_a_json_object_is_a_502(self, decider):
        for text in ("plain", "[1, 2]"):
            decider.respond = lambda request, t=text: httpx.Response(200, text=t)
            _, client = build(decider)
            response = await invoke(client)
            assert response.status_code == 502
            assert response.json()["error"] == "bad_upstream_response"

    async def test_a_failed_request_does_not_wedge_the_queue(self, decider):
        decider.respond = lambda request: httpx.Response(500, text="x")
        _, client = build(decider)
        await invoke(client)
        decider.respond = None
        assert (await invoke(client)).status_code == 200


async def _later(delay, awaitable):
    await asyncio.sleep(delay)
    return await awaitable


class TestSerialization:
    async def test_requests_reach_the_decider_one_at_a_time_in_arrival_order(self, decider):
        decider.delay = 0.03
        _, client = build(decider)
        tasks = [asyncio.create_task(invoke(client, str(i))) for i in range(5)]
        for _ in range(3):  # let every request reach the gate in creation order
            await asyncio.sleep(0)
        results = await asyncio.gather(*tasks)
        assert [r.status_code for r in results] == [200] * 5
        assert decider.max_active == 1
        assert decider.started == ["0", "1", "2", "3", "4"]
        assert decider.finished == decider.started

    async def test_a_disconnect_does_not_free_the_slot_while_the_decider_is_still_busy(self, decider):
        release = decider.block("a")
        _, client = build(decider)
        first = asyncio.create_task(invoke(client, "a"))
        await decider.entered["a"].wait()
        second = asyncio.create_task(invoke(client, "b"))
        await asyncio.sleep(0.05)
        first.cancel()  # the client went away; the decider is still computing "a"
        await asyncio.sleep(0.05)
        assert "b" not in decider.started
        release.set()
        assert (await second).status_code == 200
        assert decider.max_active == 1


class TestBoundedQueue:
    async def test_the_ninth_waiter_is_rejected_immediately_and_never_forwarded(self, decider):
        release = decider.block("hold")
        _, client = build(decider, queue_wait_s=30.0)
        in_flight = asyncio.create_task(invoke(client, "hold"))
        await decider.entered["hold"].wait()
        waiters = [asyncio.create_task(invoke(client, f"w{i}")) for i in range(8)]
        await asyncio.sleep(0.05)

        started = time.monotonic()
        rejected = await invoke(client, "overflow")
        assert time.monotonic() - started < 0.5
        assert rejected.status_code == 503
        assert rejected.json()["error"] == "queue_full"

        release.set()
        results = await asyncio.gather(in_flight, *waiters)
        assert [r.status_code for r in results] == [200] * 9
        assert "overflow" not in decider.started
        assert decider.started[1:] == [f"w{i}" for i in range(8)]  # FIFO
        assert (await invoke(client, "after")).status_code == 200  # the counter was released

    async def test_the_eighth_waiter_is_still_accepted(self, decider):
        release = decider.block("hold")
        _, client = build(decider, queue_wait_s=30.0)
        in_flight = asyncio.create_task(invoke(client, "hold"))
        await decider.entered["hold"].wait()
        waiters = [asyncio.create_task(invoke(client, f"w{i}")) for i in range(7)]
        await asyncio.sleep(0.05)
        eighth = asyncio.create_task(invoke(client, "w7"))
        await asyncio.sleep(0.05)
        assert not eighth.done()
        release.set()
        assert (await eighth).status_code == 200
        await asyncio.gather(in_flight, *waiters)

    async def test_a_request_that_waits_too_long_gets_503_and_is_never_forwarded_later(self, decider):
        release = decider.block("hold")
        _, client = build(decider, queue_wait_s=0.1)
        in_flight = asyncio.create_task(invoke(client, "hold"))
        await decider.entered["hold"].wait()

        started = time.monotonic()
        late = await invoke(client, "late")
        waited = time.monotonic() - started
        assert 0.08 <= waited < 1.0
        assert late.status_code == 503
        assert late.json()["error"] == "queue_timeout"

        release.set()
        assert (await in_flight).status_code == 200
        await asyncio.sleep(0.05)
        assert "late" not in decider.started
        assert (await invoke(client, "after")).status_code == 200

    async def test_a_timed_out_waiter_frees_its_place_in_the_queue(self, decider):
        release = decider.block("hold")
        _, client = build(decider, queue_wait_s=0.05, queue_max_waiting=2)
        in_flight = asyncio.create_task(invoke(client, "hold"))
        await decider.entered["hold"].wait()
        for i in range(4):  # each times out and must give its slot back, else the 3rd would be queue_full
            response = await invoke(client, f"t{i}")
            assert response.json()["error"] == "queue_timeout"
        release.set()
        await in_flight


class TestPing:
    async def test_is_200_when_ready(self, decider):
        _, client = build(decider)
        assert (await client.get("/ping")).status_code == 200

    async def test_is_503_with_the_state_until_warm(self, decider):
        _, client = build(decider, FakeSupervisor(ready=False, state="starting"))
        response = await client.get("/ping")
        assert response.status_code == 503
        assert response.json() == {"status": "starting"}

    async def test_is_503_for_good_once_the_decider_failed(self, decider):
        _, client = build(decider, FakeSupervisor(ready=False, state="failed"))
        assert (await client.get("/ping")).status_code == 503

    async def test_is_not_blocked_by_an_inference_in_flight_nor_by_a_full_queue(self, decider):
        release = decider.block("hold")
        _, client = build(decider, queue_wait_s=30.0)
        in_flight = asyncio.create_task(invoke(client, "hold"))
        await decider.entered["hold"].wait()
        waiters = [asyncio.create_task(invoke(client, f"w{i}")) for i in range(8)]
        await asyncio.sleep(0.05)
        started = time.monotonic()
        response = await asyncio.wait_for(client.get("/ping"), timeout=1.0)
        assert response.status_code == 200
        assert time.monotonic() - started < 0.2
        release.set()
        await asyncio.gather(in_flight, *waiters)


class TestDrainAfterUpstreamFailure:
    """After a timeout the decider may still be computing: the slot stays held until /health answers."""

    async def test_after_an_upstream_timeout_the_next_request_waits_for_the_decider_to_drain(self, decider):
        decider.linger_next = 0.3
        _, client = build(decider, drain_poll_s=0.02, drain_timeout_s=5.0, queue_wait_s=5.0)
        first = asyncio.create_task(invoke(client, "a"))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        second = await invoke(client, "b")
        assert (await first).status_code == 504
        assert second.status_code == 200
        assert time.monotonic() - started >= 0.2  # "b" sat behind the drain
        assert decider.events == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]
        assert decider.max_active == 1

    async def test_a_connection_error_also_holds_the_slot_until_health_answers(self, decider):
        health_calls_when_b_arrived = []

        def respond(request):
            name = json.loads(request.content)["name"]
            if name == "a":
                raise httpx.ConnectError("refused")
            health_calls_when_b_arrived.append(decider.health_calls)
            return httpx.Response(200, json=ANSWER)

        decider.respond = respond
        decider.health_script = [False, False, True]
        _, client = build(decider, drain_poll_s=0.01, drain_timeout_s=5.0)
        first = await invoke(client, "a")
        second = await invoke(client, "b")
        assert first.status_code == 502
        assert second.status_code == 200
        assert health_calls_when_b_arrived == [3]

    async def test_a_decider_that_never_drains_is_reported_unhealthy_and_nothing_is_forwarded_to_it(
        self, decider
    ):
        decider.linger_next = 5.0
        supervisor = FakeSupervisor()
        _, client = build(decider, supervisor, drain_poll_s=0.01, drain_timeout_s=0.15, queue_wait_s=5.0)
        first = asyncio.create_task(invoke(client, "a"))
        await asyncio.sleep(0.02)
        second = await invoke(client, "b")
        assert (await first).status_code == 504
        assert second.status_code == 503
        assert second.json()["error"] == "not_ready"
        assert len(supervisor.unhealthy_reports) == 1
        assert decider.started == ["a"]
        assert decider.max_active == 1

    async def test_a_request_error_that_is_not_an_upstream_failure_does_not_drain(self, decider):
        decider.respond = lambda request: httpx.Response(500, text="x")
        _, client = build(decider)
        await invoke(client)
        assert decider.health_calls == 0


class TestClientDisconnect:
    async def test_a_client_that_left_while_queued_is_never_forwarded(self, decider, caplog):
        release = decider.block("a")
        app, client = build(decider)
        first = asyncio.create_task(invoke(client, "a"))
        await decider.entered["a"].wait()

        body = json.dumps({"name": "b"}).encode()
        sent: list[dict] = []
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}  # the client is already gone

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/invocations",
            "raw_path": b"/invocations",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "server": ("adapter", 80),
            "client": ("test", 1),
            "scheme": "http",
        }
        with caplog.at_level("INFO", logger="adapter.app"):
            queued = asyncio.create_task(app(scope, receive, send))
            await asyncio.sleep(0.05)
            release.set()
            await queued
        assert (await first).status_code == 200
        assert decider.started == ["a"]  # "b" never reached the decider
        assert any("disconnected" in record.getMessage() for record in caplog.records)
        assert (await invoke(client, "after")).status_code == 200  # the slot was released


class TestUnretrievedTaskExceptions:
    async def test_a_call_that_fails_after_the_client_left_leaves_no_unretrieved_exception(self, decider):
        loop = asyncio.get_running_loop()
        problems: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: problems.append(context))
        release = decider.block("a")

        def explode(request):
            raise RuntimeError("upstream exploded")

        decider.respond = explode
        _, client = build(decider)
        first = asyncio.create_task(invoke(client, "a"))
        await decider.entered["a"].wait()
        first.cancel()
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.sleep(0.05)
        del first
        gc.collect()
        await asyncio.sleep(0)
        assert problems == []


class TestResponseShaping:
    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    async def test_non_finite_numbers_in_a_200_are_a_502_with_a_body(self, decider, literal):
        text = '{"answers": {}, "latency_ms": ' + literal + "}"
        decider.respond = lambda request: httpx.Response(200, text=text)
        _, client = build(decider)
        response = await invoke(client)
        assert response.status_code == 502
        assert response.json() == {"error": "bad_upstream_response"}

    async def test_non_finite_numbers_in_an_upstream_error_keep_the_upstream_status(self, decider):
        decider.respond = lambda request: httpx.Response(422, text='{"detail": NaN}')
        _, client = build(decider)
        response = await invoke(client)
        assert response.status_code == 422
        assert response.json() == {"error": "upstream_error", "status": 422}

    async def test_an_upstream_adapter_object_is_merged_not_overwritten(self, decider):
        decider.respond = lambda request: httpx.Response(
            200, json={**ANSWER, "adapter": {"build": "x1", "queue_wait_ms": 999}}
        )
        _, client = build(decider)
        payload = (await invoke(client)).json()
        assert payload["adapter"]["build"] == "x1"
        assert payload["adapter"]["queue_wait_ms"] != 999  # the adapter owns that key

    async def test_an_upstream_adapter_value_that_is_not_an_object_is_preserved_under_another_key(
        self, decider
    ):
        decider.respond = lambda request: httpx.Response(200, json={**ANSWER, "adapter": "legacy"})
        _, client = build(decider)
        payload = (await invoke(client)).json()
        assert payload["adapter"]["queue_wait_ms"] >= 0
        assert payload["upstream_adapter"] == "legacy"
