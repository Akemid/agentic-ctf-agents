"""MatchRunner state machine (spec: match-session-lifecycle, async start/status/stop + double-start guard)."""

import contextvars
import json
import threading

import pytest

from ctf_common.auth import AuthError
from ctf_common.config import RuntimeConfig
from ctf_common.payload import Action, MatchRequest
from ctf_common.runner import MatchContext, MatchRunner, Preflight, handle_invocation, make_preflight

TIMEOUT = 5


class FakeApp:
    """Records add_async_task/complete_async_task like BedrockAgentCoreApp."""

    def __init__(self):
        self.added: list[tuple[str, dict]] = []
        self.completed: list[int] = []
        self.fail_add: Exception | None = None

    def add_async_task(self, name, metadata=None):
        if self.fail_add:
            raise self.fail_add
        self.added.append((name, metadata))
        return len(self.added)

    def complete_async_task(self, task_id):
        self.completed.append(task_id)
        return True


class FakeTokens:
    def get_token(self):
        return "tok"


def request(match_id="m1", name="p1", action=Action.START, team="auto"):
    return MatchRequest(action=action, match_id=match_id, name=name, team=team, model="haiku", options={})


def ok_preflight(_req):
    return Preflight(config=RuntimeConfig(mcp_url="https://mcp.example/mcp"), tokens=FakeTokens())


class Gate:
    """A play function that blocks until released, so the test controls when the match ends."""

    def __init__(self, error: Exception | None = None, ticks: int = 0):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error = error
        self.ticks = ticks
        self.contexts: list[MatchContext] = []

    def __call__(self, ctx: MatchContext) -> None:
        self.contexts.append(ctx)
        for _ in range(self.ticks):
            ctx.tick()
        self.entered.set()
        self.release.wait(TIMEOUT)
        if self.error:
            raise self.error


@pytest.fixture
def app():
    return FakeApp()


def make_runner(app, play, preflight=ok_preflight):
    return MatchRunner(app, play, preflight, runtime="harness_sdk")


class TestStart:
    def test_start_returns_immediately_while_match_runs(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        resp = runner.start(request())
        assert resp["status"] == "started"
        assert (resp["match_id"], resp["name"], resp["team"]) == ("m1", "p1", "auto")
        assert resp["ticks"] == 0 and resp["started_at"]
        assert gate.entered.wait(TIMEOUT)
        assert app.added == [("match", {"match_id": "m1", "name": "p1"})]
        assert app.completed == []  # session stays HealthyBusy while the match runs
        gate.release.set()
        assert runner.wait(TIMEOUT)

    def test_complete_async_task_runs_after_play_returns(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        runner.start(request())
        gate.release.set()
        assert runner.wait(TIMEOUT)
        assert app.completed == [1]
        assert runner.status()["status"] == "finished"

    def test_play_receives_validated_context(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        runner.start(request(match_id="m7", name="zed", team="red"))
        assert gate.entered.wait(TIMEOUT)
        ctx = gate.contexts[0]
        assert (ctx.request.match_id, ctx.request.name, ctx.request.team) == ("m7", "zed", "red")
        assert ctx.config.mcp_url == "https://mcp.example/mcp"
        assert ctx.tokens.get_token() == "tok"
        assert not ctx.stop.is_set()
        gate.release.set()
        runner.wait(TIMEOUT)

    def test_context_variables_propagate_to_worker_thread(self, app):
        seen = []
        var = contextvars.ContextVar("workload_token", default="missing")

        def play(ctx):
            seen.append(var.get())

        runner = make_runner(app, play)
        var.set("per-request-value")
        runner.start(request())
        assert runner.wait(TIMEOUT)
        assert seen == ["per-request-value"]


class TestDoubleStart:
    def test_same_match_and_name_is_already_running(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        runner.start(request())
        assert gate.entered.wait(TIMEOUT)
        resp = runner.start(request())
        assert resp["status"] == "already_running"
        assert len(app.added) == 1  # no second task
        gate.release.set()
        runner.wait(TIMEOUT)

    @pytest.mark.parametrize("other", [request(match_id="m2"), request(name="p2")])
    def test_different_pair_is_rejected_and_match_is_unaffected(self, app, other):
        gate = Gate()
        runner = make_runner(app, gate)
        runner.start(request())
        assert gate.entered.wait(TIMEOUT)
        resp = runner.start(other)
        assert resp["status"] == "rejected"
        assert (resp["match_id"], resp["name"]) == ("m1", "p1")  # reports the running match
        assert runner.status()["status"] == "running"
        assert len(app.added) == 1
        gate.release.set()
        runner.wait(TIMEOUT)

    def test_restart_after_finish_is_accepted(self, app):
        runner = make_runner(app, lambda ctx: None)
        runner.start(request())
        assert runner.wait(TIMEOUT)
        resp = runner.start(request(match_id="m2", name="p2"))
        assert resp["status"] == "started" and resp["match_id"] == "m2"
        assert runner.wait(TIMEOUT)
        assert len(app.added) == 2 and app.completed == [1, 2]

    def test_restart_after_failure_is_accepted(self, app):
        calls = iter([RuntimeError("boom"), None])

        def play(ctx):
            err = next(calls)
            if err:
                raise err

        runner = make_runner(app, play)
        runner.start(request())
        runner.wait(TIMEOUT)
        assert runner.status()["status"] == "failed"
        assert runner.start(request())["status"] == "started"
        runner.wait(TIMEOUT)
        assert runner.status()["status"] == "finished"


class TestStatus:
    def test_idle_before_any_start_is_not_an_error(self, app):
        status = make_runner(app, Gate()).status()
        assert status == {"status": "idle"}

    def test_running_status_reports_ticks_and_identity(self, app):
        gate = Gate(ticks=3)
        runner = make_runner(app, gate)
        runner.start(request())
        assert gate.entered.wait(TIMEOUT)
        status = runner.status()
        assert status["status"] == "running" and status["ticks"] == 3
        assert (status["match_id"], status["name"], status["team"]) == ("m1", "p1", "auto")
        assert "error" not in status
        gate.release.set()
        runner.wait(TIMEOUT)

    def test_finished_status_keeps_final_tick_count(self, app):
        gate = Gate(ticks=2)
        runner = make_runner(app, gate)
        runner.start(request())
        gate.release.set()
        runner.wait(TIMEOUT)
        status = runner.status()
        assert status["status"] == "finished" and status["ticks"] == 2

    def test_failed_status_carries_error_and_still_completes_the_task(self, app):
        gate = Gate(error=RuntimeError("model exploded"))
        runner = make_runner(app, gate)
        runner.start(request())
        gate.release.set()
        runner.wait(TIMEOUT)
        status = runner.status()
        assert status["status"] == "failed"
        assert "model exploded" in status["error"]
        assert app.completed == [1]  # finally: complete_async_task also on exception

    def test_auth_errors_are_reported_as_auth_failures(self, app):
        gate = Gate(error=AuthError("MCP rejected the bearer token (401)"))
        runner = make_runner(app, gate)
        runner.start(request())
        gate.release.set()
        runner.wait(TIMEOUT)
        status = runner.status()
        assert status["status"] == "failed" and "auth" in status["error"].lower()


class TestStop:
    def test_stop_signals_the_match_and_ends_stopped(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        runner.start(request())
        assert gate.entered.wait(TIMEOUT)
        resp = runner.stop()
        assert resp["status"] == "stopping"
        assert gate.contexts[0].stop.is_set()
        assert runner.status()["status"] == "stopping"
        gate.release.set()
        assert runner.wait(TIMEOUT)
        assert runner.status()["status"] == "stopped"
        assert app.completed == [1]

    def test_start_while_stopping_is_rejected(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        runner.start(request())
        assert gate.entered.wait(TIMEOUT)
        runner.stop()
        assert runner.start(request())["status"] == "rejected"
        gate.release.set()
        runner.wait(TIMEOUT)

    def test_stop_without_a_match_reports_current_state(self, app):
        runner = make_runner(app, Gate())
        assert runner.stop() == {"status": "idle"}

    def test_stop_after_finish_does_not_change_state(self, app):
        runner = make_runner(app, lambda ctx: None)
        runner.start(request())
        runner.wait(TIMEOUT)
        assert runner.stop()["status"] == "finished"

    def test_stop_while_play_fails_reports_failed_not_stopped(self, app):
        gate = Gate(error=RuntimeError("late failure"))
        runner = make_runner(app, gate)
        runner.start(request())
        gate.entered.wait(TIMEOUT)
        runner.stop()
        gate.release.set()
        runner.wait(TIMEOUT)
        assert runner.status()["status"] == "failed"


class TestPreflight:
    def test_preflight_error_is_synchronous_and_starts_no_work(self, app):
        played = []

        def bad_preflight(_req):
            raise RuntimeError("SSM parameter /agentic-ctf/mcp-url not found")

        runner = make_runner(app, lambda ctx: played.append(ctx), preflight=bad_preflight)
        resp = runner.start(request())
        assert resp["status"] == "failed"
        assert "/agentic-ctf/mcp-url" in resp["error"]
        assert app.added == [] and played == []
        assert runner.status()["status"] == "failed"

    def test_preflight_failure_does_not_block_a_later_start(self, app):
        attempts = iter([RuntimeError("no token"), None])

        def flaky_preflight(req):
            err = next(attempts)
            if err:
                raise err
            return ok_preflight(req)

        runner = make_runner(app, lambda ctx: None, preflight=flaky_preflight)
        assert runner.start(request())["status"] == "failed"
        assert runner.start(request())["status"] == "started"
        runner.wait(TIMEOUT)

    def test_add_async_task_failure_is_reported_and_no_thread_starts(self, app):
        app.fail_add = RuntimeError("cannot register")
        played = []
        runner = make_runner(app, lambda ctx: played.append(1))
        resp = runner.start(request())
        assert resp["status"] == "failed" and "cannot register" in resp["error"]
        assert played == []

    def test_make_preflight_loads_config_and_fetches_exactly_one_token(self):
        counter = {"tokens": 0, "loads": 0}

        class Counting:
            def get_token(self):
                counter["tokens"] += 1
                return f"t{counter['tokens']}"

        def load():
            counter["loads"] += 1
            return RuntimeConfig(mcp_url="https://m/mcp")

        result = make_preflight(load, Counting())(request())
        assert counter == {"tokens": 1, "loads": 1}
        assert result.config.mcp_url == "https://m/mcp"
        assert result.tokens.get_token() == "t1"  # first connection reuses the preflight token
        assert counter["tokens"] == 1

    def test_make_preflight_config_error_prevents_token_fetch(self):
        tokens_asked = []

        class Tokens:
            def get_token(self):
                tokens_asked.append(1)
                return "x"

        def load():
            raise RuntimeError("missing /agentic-ctf/mcp-url")

        with pytest.raises(RuntimeError, match="mcp-url"):
            make_preflight(load, Tokens())(request())
        assert tokens_asked == []


class TestHandleInvocation:
    def handle(self, runner, raw, session="session-abcdef-0123456789-0123456789-xyz999"):
        return handle_invocation(runner, raw, runtime="harness_sdk", session_id=session)

    def test_invalid_payload_is_rejected_before_any_work(self, app):
        runner = make_runner(app, Gate())
        resp = self.handle(runner, {"action": "start"})
        assert resp["status"] == "rejected" and "match_id" in resp["error"]
        assert app.added == []

    def test_caller_history_never_reaches_the_runner(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        resp = self.handle(runner, {"match_id": "m1", "messages": [{"role": "user", "content": []}]})
        assert resp["status"] == "rejected" and "messages" in resp["error"]
        assert gate.contexts == [] and app.added == []

    def test_prompt_wrapped_status(self, app):
        runner = make_runner(app, Gate())
        assert self.handle(runner, {"prompt": json.dumps({"action": "status"})}) == {"status": "idle"}

    def test_start_uses_default_name_from_session(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        resp = self.handle(runner, {"match_id": "m1"})
        assert resp["status"] == "started" and resp["name"] == "harness_sdk-xyz999"
        gate.release.set()
        runner.wait(TIMEOUT)

    def test_stop_dispatch(self, app):
        gate = Gate()
        runner = make_runner(app, gate)
        self.handle(runner, {"match_id": "m1"})
        gate.entered.wait(TIMEOUT)
        assert self.handle(runner, {"action": "stop"})["status"] == "stopping"
        gate.release.set()
        runner.wait(TIMEOUT)


class BlockingPreflight:
    """A preflight that blocks until released, to prove status/stop never wait on the network."""

    def __init__(self, error: Exception | None = None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error = error

    def __call__(self, req):
        self.entered.set()
        assert self.release.wait(TIMEOUT)
        if self.error:
            raise self.error
        return ok_preflight(req)


def in_thread(fn, *args):
    """Run fn in a thread; returns (thread, result-box)."""
    box: dict = {}

    def target():
        box["value"] = fn(*args)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


class TestPreflightDoesNotHoldTheLock:
    def test_status_and_stop_return_while_preflight_is_blocked(self, app):
        preflight = BlockingPreflight()
        runner = make_runner(app, Gate(), preflight)
        starter, box = in_thread(runner.start, request())
        assert preflight.entered.wait(TIMEOUT)

        status_thread, status_box = in_thread(runner.status)
        status_thread.join(1)
        assert not status_thread.is_alive(), "status() blocked on the preflight"
        assert status_box["value"]["status"] == "running"  # externally visible set stays spec-compliant

        stop_thread, stop_box = in_thread(runner.stop)
        stop_thread.join(1)
        assert not stop_thread.is_alive(), "stop() blocked on the preflight"
        assert stop_box["value"]["status"] == "stopping"

        preflight.release.set()
        starter.join(TIMEOUT)

    def test_stop_during_preflight_ends_stopped_without_starting_anything(self, app):
        preflight = BlockingPreflight()
        gate = Gate()
        runner = make_runner(app, gate, preflight)
        starter, box = in_thread(runner.start, request())
        assert preflight.entered.wait(TIMEOUT)
        runner.stop()
        preflight.release.set()
        starter.join(TIMEOUT)
        assert box["value"]["status"] == "stopped"
        assert runner.status()["status"] == "stopped"
        assert app.added == [] and app.completed == []
        assert gate.contexts == [] and runner.wait(0)

    def test_second_start_with_same_match_during_preflight_is_already_running(self, app):
        preflight = BlockingPreflight()
        runner = make_runner(app, Gate(), preflight)
        starter, _ = in_thread(runner.start, request())
        assert preflight.entered.wait(TIMEOUT)
        assert runner.start(request())["status"] == "already_running"
        preflight.release.set()
        starter.join(TIMEOUT)
        assert len(app.added) == 1

    def test_second_start_with_another_match_during_preflight_is_rejected(self, app):
        preflight = BlockingPreflight()
        runner = make_runner(app, Gate(), preflight)
        starter, _ = in_thread(runner.start, request())
        assert preflight.entered.wait(TIMEOUT)
        assert runner.start(request(match_id="m2"))["status"] == "rejected"
        preflight.release.set()
        starter.join(TIMEOUT)
        assert len(app.added) == 1

    def test_preflight_failure_after_unlock_is_failed(self, app):
        preflight = BlockingPreflight(error=AuthError("denied"))
        runner = make_runner(app, Gate(), preflight)
        starter, box = in_thread(runner.start, request())
        assert preflight.entered.wait(TIMEOUT)
        preflight.release.set()
        starter.join(TIMEOUT)
        assert box["value"]["status"] == "failed" and "denied" in box["value"]["error"]
        assert app.added == []

    def test_successful_start_after_slow_preflight_runs_the_match(self, app):
        preflight, gate = BlockingPreflight(), Gate()
        runner = make_runner(app, gate, preflight)
        starter, box = in_thread(runner.start, request())
        assert preflight.entered.wait(TIMEOUT)
        preflight.release.set()
        starter.join(TIMEOUT)
        assert box["value"]["status"] == "started" and gate.entered.wait(TIMEOUT)
        gate.release.set()
        assert runner.wait(TIMEOUT)


class TestThreadStartFailure:
    def test_thread_start_error_completes_the_task_and_fails(self, app, monkeypatch):
        runner = make_runner(app, Gate())

        def boom(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", boom)
        resp = runner.start(request())
        assert resp["status"] == "failed" and "can't start new thread" in resp["error"]
        assert app.added and app.completed == [1]
        assert runner.status()["status"] == "failed"
