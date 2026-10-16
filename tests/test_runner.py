"""MatchRunner state machine (spec: match-session-lifecycle, async start/status/stop + double-start guard)."""

import contextvars
import json
import threading
import time

import pytest

from ctf_common.auth import AuthError
from ctf_common.config import RuntimeConfig
from ctf_common.mcp import McpAuthError
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


def request(match_id="m1", name="p1", action=Action.START, team="auto", agent_index=None):
    return MatchRequest(
        action=action,
        match_id=match_id,
        name=name,
        team=team,
        model="haiku",
        options={},
        agent_index=agent_index,
    )


def seat(ctx: MatchContext) -> None:
    """What a runtime does after `join_match`: report the seat the server assigned."""
    team = "red" if ctx.request.team == "auto" else ctx.request.team
    ctx.join.succeed(team, ctx.request.agent_index or 0)


def joins_then(play=lambda ctx: None):
    """Wrap a play function so it reports a successful join first (every runtime must)."""

    def wrapped(ctx: MatchContext) -> None:
        seat(ctx)
        play(ctx)

    return wrapped


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
        seat(ctx)
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
        assert (resp["match_id"], resp["name"], resp["team"]) == ("m1", "p1", "red")
        assert resp["agent_index"] == 0 and resp["ticks"] == 0 and resp["started_at"]
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
            seat(ctx)
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
        runner = make_runner(app, joins_then())
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
            seat(ctx)

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
        assert (status["match_id"], status["name"], status["team"]) == ("m1", "p1", "red")
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
        runner = make_runner(app, joins_then())
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

        runner = make_runner(app, joins_then(), preflight=flaky_preflight)
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


class Joiner:
    """A play function that decides the join outcome, optionally late, so the test controls the timing."""

    def __init__(self, outcome="joined", *, reason="slot_taken", team="blue", agent_index=2, delay=None):
        self.outcome, self.reason, self.team, self.agent_index = outcome, reason, team, agent_index
        self.delay = delay  # a threading.Event the play waits for before reporting
        self.entered = threading.Event()
        self.finish = threading.Event()  # keeps a joined match running until released
        self.calls = 0
        self.contexts: list[MatchContext] = []

    def __call__(self, ctx: MatchContext) -> None:
        self.calls += 1
        self.contexts.append(ctx)
        self.entered.set()
        if self.delay is not None:
            self.delay.wait(TIMEOUT)
        if self.outcome == "joined":
            ctx.join.succeed(self.team, self.agent_index)
            self.finish.wait(TIMEOUT)
        elif self.outcome == "refused":
            ctx.join.refuse(self.reason)
        elif self.outcome == "raise":
            raise RuntimeError(self.reason)
        elif self.outcome == "hang":
            ctx.stop.wait(TIMEOUT)  # never joins; ends when the runner stops it
        # "silent": return without reporting anything


def make_sync_runner(app, play, **kw):
    return MatchRunner(app, play, ok_preflight, runtime="harness_sdk", **kw)


class TestSynchronousJoin:
    def test_started_echoes_the_team_and_slot_the_server_assigned(self, app):
        joiner = Joiner(team="blue", agent_index=2)
        runner = make_sync_runner(app, joiner)
        resp = runner.start(request(team="blue", agent_index=2))
        assert resp["status"] == "started"
        assert (resp["match_id"], resp["name"], resp["team"], resp["agent_index"]) == ("m1", "p1", "blue", 2)
        assert resp["slot"] == 2  # the lobby's key: same value as agent_index
        assert runner.status()["status"] == "running"
        assert (runner.status()["team"], runner.status()["agent_index"]) == ("blue", 2)
        joiner.finish.set()
        assert runner.wait(TIMEOUT)

    def test_team_auto_reports_the_team_the_server_picked(self, app):
        joiner = Joiner(team="blue", agent_index=0)
        runner = make_sync_runner(app, joiner)
        resp = runner.start(request(team="auto"))
        assert (resp["team"], resp["agent_index"]) == ("blue", 0)
        joiner.finish.set()
        runner.wait(TIMEOUT)

    def test_start_does_not_answer_until_join_match_has_answered(self, app):
        release = threading.Event()
        joiner = Joiner(delay=release)
        runner = make_sync_runner(app, joiner)
        starter, box = in_thread(runner.start, request(team="blue", agent_index=2))
        assert joiner.entered.wait(TIMEOUT)
        starter.join(0.3)
        assert starter.is_alive() and "value" not in box, "start answered before the join finished"
        assert runner.status()["status"] == "running"  # reported externally while joining
        release.set()
        starter.join(TIMEOUT)
        assert box["value"]["status"] == "started"
        joiner.finish.set()
        runner.wait(TIMEOUT)

    def test_the_requested_slot_reaches_the_play_function_unchanged(self, app):
        joiner = Joiner()
        runner = make_sync_runner(app, joiner)
        runner.start(request(team="red", agent_index=1))
        assert (joiner.contexts[0].request.team, joiner.contexts[0].request.agent_index) == ("red", 1)
        joiner.finish.set()
        runner.wait(TIMEOUT)

    @pytest.mark.parametrize(
        "reason",
        ["slot_taken", "match_full", "match_already_started", "match_finished", "unauthorized"],
    )
    def test_every_join_refusal_is_rejected_with_its_reason_and_no_other_slot_is_tried(self, app, reason):
        joiner = Joiner("refused", reason=reason)
        runner = make_sync_runner(app, joiner)
        resp = runner.start(request(team="red", agent_index=1))
        assert resp["status"] == "rejected"
        assert resp["error"] == f"join_match failed: {reason}"
        assert joiner.calls == 1  # one join, no fallback to another slot
        assert runner.wait(TIMEOUT)
        assert runner.status()["status"] == "failed" and reason in runner.status()["error"]
        assert app.added == [("match", {"match_id": "m1", "name": "p1"})] and app.completed == [1]

    def test_a_refused_start_leaves_the_runner_ready_for_an_immediate_retry(self, app):
        outcomes = iter(["refused", "joined"])
        joiner = Joiner()

        def play(ctx):
            joiner.outcome = next(outcomes)
            joiner(ctx)

        runner = make_sync_runner(app, play)
        assert runner.start(request(team="red", agent_index=1))["status"] == "rejected"
        assert runner.start(request(team="red", agent_index=2))["status"] == "started"  # no wait, no busy
        joiner.finish.set()
        runner.wait(TIMEOUT)
        deadline = time.monotonic() + TIMEOUT
        while sorted(app.completed) != [1, 2] and time.monotonic() < deadline:
            time.sleep(0.01)  # the refused run's thread unwinds on its own
        assert sorted(app.completed) == [1, 2]

    def test_a_play_that_fails_before_joining_is_failed_not_rejected(self, app):
        runner = make_sync_runner(app, Joiner("raise", reason="token endpoint down"))
        resp = runner.start(request(team="red", agent_index=0))
        assert resp["status"] == "failed" and "token endpoint down" in resp["error"]
        assert runner.wait(TIMEOUT)  # start no longer waits for the unwinding thread
        assert app.completed == [1]

    def test_a_play_that_returns_without_joining_is_failed(self, app):
        runner = make_sync_runner(app, Joiner("silent"))
        resp = runner.start(request())
        assert resp["status"] == "failed" and "before join_match" in resp["error"]
        assert runner.wait(TIMEOUT) and app.completed == [1]

    def test_a_double_start_while_joining_is_already_running(self, app):
        release = threading.Event()
        joiner = Joiner(delay=release)
        runner = make_sync_runner(app, joiner)
        starter, _ = in_thread(runner.start, request(team="red", agent_index=0))
        assert joiner.entered.wait(TIMEOUT)
        assert runner.start(request(team="red", agent_index=0))["status"] == "already_running"
        assert runner.start(request(name="p2"))["status"] == "rejected"
        release.set()
        starter.join(TIMEOUT)
        assert len(app.added) == 1
        joiner.finish.set()
        runner.wait(TIMEOUT)


class TestJoinTimeout:
    def test_a_join_that_never_answers_ends_failed_with_a_clear_error(self, app):
        runner = make_sync_runner(app, Joiner("hang"), join_timeout_s=0.1)
        resp = runner.start(request(team="red", agent_index=0))
        assert resp["status"] == "failed"
        assert "join_match timed out after 0.1s" in resp["error"]

    def test_the_timed_out_match_is_stopped_and_its_task_completed(self, app):
        joiner = Joiner("hang")
        runner = make_sync_runner(app, joiner, join_timeout_s=0.1)
        runner.start(request())
        assert joiner.contexts[0].stop.is_set()
        assert runner.wait(TIMEOUT)
        assert runner.status()["status"] == "failed" and "timed out" in runner.status()["error"]
        assert app.added == [("match", {"match_id": "m1", "name": "p1"})] and app.completed == [1]

    def test_a_join_that_lands_after_the_timeout_is_ignored(self, app):
        late = threading.Event()
        joiner = Joiner(delay=late)
        runner = make_sync_runner(app, joiner, join_timeout_s=0.1)
        resp = runner.start(request(team="blue", agent_index=2))
        assert resp["status"] == "failed"
        late.set()
        joiner.finish.set()
        assert runner.wait(TIMEOUT)
        assert runner.status()["status"] == "failed"  # the late success did not resurrect the start
        assert app.completed == [1]

    def test_a_timed_out_runner_cannot_start_a_second_match_until_the_thread_exits(self, app):
        late = threading.Event()
        joiner = Joiner(delay=late)
        runner = make_sync_runner(app, joiner, join_timeout_s=0.1)
        runner.start(request())
        assert runner.start(request(match_id="m2"))["status"] == "rejected"
        late.set()
        joiner.finish.set()
        runner.wait(TIMEOUT)


class TestStopDuringJoin:
    def test_stop_while_joining_ends_stopped_and_start_never_says_started(self, app):
        release = threading.Event()
        joiner = Joiner(delay=release)
        runner = make_sync_runner(app, joiner)
        starter, box = in_thread(runner.start, request(team="red", agent_index=0))
        assert joiner.entered.wait(TIMEOUT)
        assert runner.stop()["status"] == "stopping"
        starter.join(TIMEOUT)
        assert not starter.is_alive(), "start kept waiting for a join nobody wants"
        assert box["value"]["status"] in ("stopping", "stopped")
        release.set()  # the join answers late: it must not turn the stop into a started match
        joiner.finish.set()
        assert runner.wait(TIMEOUT)
        assert runner.status()["status"] == "stopped"
        assert app.completed == [1]

    def test_stop_while_joining_sets_the_stop_event_for_the_play_function(self, app):
        release = threading.Event()
        joiner = Joiner(delay=release)
        runner = make_sync_runner(app, joiner)
        starter, _ = in_thread(runner.start, request())
        assert joiner.entered.wait(TIMEOUT)
        runner.stop()
        assert joiner.contexts[0].stop.is_set()
        release.set()
        joiner.finish.set()
        starter.join(TIMEOUT)
        runner.wait(TIMEOUT)


class TestFailureCauseIsKept:
    """MINOR 1: a play that raises after the join was already marked an error keeps ITS error."""

    def test_a_401_after_connection_lost_wins_over_the_join_reason(self, app):
        def play(ctx):
            ctx.join.error("join_match failed: connection_lost")
            raise McpAuthError("MCP server rejected the token (401)")

        runner = make_sync_runner(app, play)
        runner.start(request(team="red", agent_index=0))
        assert runner.wait(TIMEOUT)
        status = runner.status()
        assert status["status"] == "failed"
        assert "McpAuthError" in status["error"] and "401" in status["error"]

    def test_without_an_exception_the_join_reason_is_the_error(self, app):
        def play(ctx):
            ctx.join.error("join_match failed: connection_lost")

        runner = make_sync_runner(app, play)
        runner.start(request(team="red", agent_index=0))
        assert runner.wait(TIMEOUT)
        assert runner.status()["error"] == "join_match failed: connection_lost"


class TestRefusalUnwind:
    """MINOR 2: after a refusal the runner is terminal at once, even while the old thread still unwinds."""

    def slow_unwinding_play(self, release):
        calls = []

        def play(ctx):
            calls.append(ctx)
            if len(calls) == 1:
                ctx.join.refuse("slot_taken")
                release.wait(TIMEOUT)  # the old thread is still closing its MCP connection
            else:
                ctx.join.succeed("red", 2)
                release.wait(TIMEOUT)

        return play, calls

    def test_status_reports_the_refusal_not_running_during_the_unwind(self, app):
        release = threading.Event()
        play, _ = self.slow_unwinding_play(release)
        runner = make_sync_runner(app, play)
        started = time.monotonic()
        resp = runner.start(request(team="red", agent_index=1))
        assert time.monotonic() - started < 2, "start waited for the unwinding thread"
        assert resp["status"] == "rejected" and resp["error"] == "join_match failed: slot_taken"
        status = runner.status()
        assert status["status"] == "failed" and "slot_taken" in status["error"]
        release.set()
        assert runner.wait(TIMEOUT)

    def test_an_immediate_restart_is_not_already_running_and_the_old_thread_cannot_overwrite_it(self, app):
        release = threading.Event()
        play, _ = self.slow_unwinding_play(release)
        runner = make_sync_runner(app, play)
        assert runner.start(request(team="red", agent_index=1))["status"] == "rejected"
        second = runner.start(request(team="red", agent_index=2))
        assert second["status"] == "started"
        # the first thread (still alive) now finishes: it must not write FAILED over the second run
        first_thread = [t for t in threading.enumerate() if t.name == "match-p1"]
        assert first_thread
        release.set()
        for thread in first_thread:
            thread.join(TIMEOUT)
        assert runner.status()["status"] in ("running", "finished")
        assert "error" not in runner.status()
        assert runner.wait(TIMEOUT)
