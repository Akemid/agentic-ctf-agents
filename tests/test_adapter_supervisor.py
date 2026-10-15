"""Upstream supervision: warm-up gating, restart exactly once, permanent failure (spec decider-endpoint)."""

import asyncio
import contextlib

import pytest

from adapter.supervisor import Probe, Supervisor


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()

    def die(self, code=1):
        self.returncode = code
        self._done.set()

    def terminate(self):
        self.terminated = True
        self.die(-15)

    def kill(self):
        self.killed = True
        self.die(-9)

    async def wait(self):
        await self._done.wait()
        return self.returncode


class Harness:
    """Scripted spawn / health / warm-up so each test states only what differs."""

    def __init__(self):
        self.processes: list[FakeProcess] = []
        self.healthy = True
        self.health_calls = 0
        self.warmups = 0
        self.warmup_ok = True
        self.warmup_gate: asyncio.Event | None = None
        self.health_script: list[bool] = []
        self.busy = False  # the request gate's busy flag
        self.probe_result: Probe | None = None  # overrides `healthy` with a typed probe outcome
        self.on_probe = None  # called at the start of every probe

    async def spawn(self):
        process = FakeProcess()
        self.processes.append(process)
        return process

    async def health(self):
        self.health_calls += 1
        if self.on_probe is not None:
            self.on_probe()
        if self.probe_result is not None:
            return self.probe_result
        if self.health_script:
            return self.health_script.pop(0)
        return self.healthy

    async def warmup(self):
        self.warmups += 1
        if self.warmup_gate is not None:
            await self.warmup_gate.wait()
        return self.warmup_ok

    def supervisor(self, **overrides) -> Supervisor:
        options = {
            "health_interval_s": 0.01,
            "startup_poll_s": 0.005,
            "startup_timeout_s": 0.2,
            "health_failures": 3,
            "max_restarts": 1,
            **overrides,
        }
        return Supervisor(self.spawn, self.health, self.warmup, busy=lambda: self.busy, **options)


async def until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


@pytest.fixture
def harness():
    return Harness()


async def start(supervisor):
    task = asyncio.create_task(supervisor.run())
    return task


async def finish(supervisor, task):
    await supervisor.stop()
    await asyncio.wait_for(task, timeout=2.0)


class TestStartup:
    async def test_is_starting_then_ready_only_after_health_and_warmup(self, harness):
        harness.warmup_gate = asyncio.Event()
        supervisor = harness.supervisor()
        assert supervisor.state == "starting"
        task = await start(supervisor)
        await until(lambda: harness.warmups == 1)
        assert supervisor.ready is False  # healthy but not warm
        assert supervisor.state == "starting"
        harness.warmup_gate.set()
        await until(lambda: supervisor.ready)
        assert supervisor.state == "ready"
        await finish(supervisor, task)

    async def test_waits_for_health_before_warming_up(self, harness):
        harness.health_script = [False, False, False, True]
        supervisor = harness.supervisor(startup_timeout_s=2.0)
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        assert harness.health_calls >= 4
        assert harness.warmups == 1
        await finish(supervisor, task)

    async def test_a_failed_warmup_counts_as_a_failed_start(self, harness):
        harness.warmup_ok = False
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.state == "failed")
        assert len(harness.processes) == 2  # one restart, then permanent
        assert harness.warmups == 2
        await asyncio.wait_for(task, timeout=2.0)

    async def test_never_healthy_means_restart_once_then_failed(self, harness):
        harness.healthy = False
        supervisor = harness.supervisor(startup_timeout_s=0.05)
        task = await start(supervisor)
        await until(lambda: supervisor.state == "failed")
        assert len(harness.processes) == 2
        assert all(p.terminated for p in harness.processes)
        assert harness.warmups == 0
        assert supervisor.ready is False
        await asyncio.wait_for(task, timeout=2.0)

    async def test_a_process_that_dies_while_starting_does_not_wait_for_the_timeout(self, harness):
        harness.healthy = False
        supervisor = harness.supervisor(startup_timeout_s=30.0)
        task = await start(supervisor)
        await until(lambda: len(harness.processes) == 1)
        harness.processes[0].die(1)
        await until(lambda: len(harness.processes) == 2)
        harness.processes[1].die(1)
        await until(lambda: supervisor.state == "failed")
        await asyncio.wait_for(task, timeout=2.0)


class TestRestartPolicy:
    async def test_a_dead_process_is_restarted_once_and_warmed_up_again(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.processes[0].die(139)
        await until(lambda: len(harness.processes) == 2)
        await until(lambda: supervisor.ready)
        assert supervisor.restarts == 1
        assert harness.warmups == 2
        await finish(supervisor, task)

    async def test_not_ready_while_restarting(self, harness):
        harness.warmup_gate = asyncio.Event()
        harness.warmup_gate.set()
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.warmup_gate.clear()
        harness.processes[0].die(1)
        await until(lambda: harness.warmups == 2)
        assert supervisor.ready is False
        harness.warmup_gate.set()
        await until(lambda: supervisor.ready)
        await finish(supervisor, task)

    async def test_a_second_death_is_permanent(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.processes[0].die(1)
        await until(lambda: supervisor.ready and len(harness.processes) == 2)
        harness.processes[1].die(1)
        await until(lambda: supervisor.state == "failed")
        await asyncio.sleep(0.05)
        assert len(harness.processes) == 2  # no third spawn
        assert supervisor.ready is False
        await asyncio.wait_for(task, timeout=2.0)

    async def test_the_old_process_is_stopped_before_the_replacement(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.healthy = False
        await until(lambda: len(harness.processes) == 2)
        assert harness.processes[0].terminated
        harness.healthy = True
        await until(lambda: supervisor.ready)
        await finish(supervisor, task)

    async def test_three_consecutive_health_failures_trigger_the_restart(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.health_script = [False, False, False]
        await until(lambda: len(harness.processes) == 2)
        await until(lambda: supervisor.ready)
        await finish(supervisor, task)

    async def test_fewer_failures_than_the_threshold_are_forgiven(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.health_script = [False, False, True, False, False, True]
        await until(lambda: not harness.health_script)
        await asyncio.sleep(0.03)
        assert len(harness.processes) == 1
        assert supervisor.ready is True
        await finish(supervisor, task)


class TestProbeDuringInference:
    """A /health probe cannot be answered while the decider computes: that is not a failure."""

    async def test_a_busy_gate_and_a_slow_health_endpoint_cause_no_restart(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        calls_before = harness.health_calls
        harness.busy = True
        harness.probe_result = Probe.TIMEOUT
        await asyncio.sleep(0.15)  # many intervals, far more than 3 "failures"
        assert len(harness.processes) == 1
        assert supervisor.ready is True
        assert harness.health_calls == calls_before  # not even probed while busy
        await finish(supervisor, task)

    async def test_an_idle_decider_that_times_out_three_times_in_a_row_is_restarted(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.probe_result = Probe.TIMEOUT
        await until(lambda: len(harness.processes) == 2)
        assert harness.health_calls >= 3
        harness.probe_result = None
        await until(lambda: supervisor.ready)
        await finish(supervisor, task)

    async def test_two_idle_timeouts_are_forgiven(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.health_script = [
            Probe.TIMEOUT,
            Probe.TIMEOUT,
            Probe.OK,
            Probe.TIMEOUT,
            Probe.TIMEOUT,
            Probe.OK,
        ]
        await until(lambda: not harness.health_script)
        await asyncio.sleep(0.03)
        assert len(harness.processes) == 1
        await finish(supervisor, task)

    async def test_a_dead_process_is_restarted_at_once_even_while_a_request_is_in_flight(self, harness):
        supervisor = harness.supervisor(health_failures=100)
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.busy = True
        harness.processes[0].die(139)
        await until(lambda: len(harness.processes) == 2)
        await finish(supervisor, task)

    async def test_connection_refused_while_idle_is_a_hard_failure_without_waiting_for_three(self, harness):
        supervisor = harness.supervisor(health_failures=100)
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.probe_result = Probe.REFUSED
        await until(lambda: len(harness.processes) == 2)
        await finish(supervisor, task)

    async def test_a_probe_that_overlapped_a_request_is_discarded(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)

        def request_arrives_mid_probe():
            harness.busy = True  # the gate was free when the probe started, busy when it timed out

        harness.on_probe = request_arrives_mid_probe
        harness.probe_result = Probe.TIMEOUT
        await asyncio.sleep(0.12)
        assert len(harness.processes) == 1
        await finish(supervisor, task)

    async def test_booleans_from_a_plain_probe_still_work(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.health_script = [True, False, False, False]
        await until(lambda: len(harness.processes) == 2)
        await finish(supervisor, task)


class TestFailureSurfaces:
    async def test_a_spawn_error_is_a_terminal_failed_state_not_a_crash(self, harness):
        async def missing_binary():
            raise FileNotFoundError("strands-decider")

        supervisor = Supervisor(missing_binary, harness.health, harness.warmup, health_interval_s=0.01)
        task = await start(supervisor)
        await asyncio.wait_for(task, timeout=2.0)  # run() returned instead of raising
        assert task.exception() is None
        assert supervisor.state == "failed"
        assert supervisor.ready is False

    async def test_a_spawn_error_on_the_restart_is_terminal_too(self, harness):
        spawns = 0

        async def second_spawn_fails():
            nonlocal spawns
            spawns += 1
            if spawns == 2:
                raise OSError("no more processes")
            return await harness.spawn()

        supervisor = Supervisor(
            second_spawn_fails, harness.health, harness.warmup, health_interval_s=0.01, startup_poll_s=0.005
        )
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        harness.processes[0].die(1)
        await until(lambda: supervisor.state == "failed")
        await asyncio.wait_for(task, timeout=2.0)
        assert harness.processes[0].returncode is not None

    async def test_stop_during_warmup_terminates_the_child_without_waiting_for_the_warmup(self, harness):
        harness.warmup_gate = asyncio.Event()  # never set: the warm-up hangs
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: harness.warmups == 1)
        await supervisor.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert harness.processes[0].terminated

    async def test_cancelling_run_during_warmup_still_terminates_the_child(self, harness):
        harness.warmup_gate = asyncio.Event()
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: harness.warmups == 1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert harness.processes[0].terminated

    async def test_report_unhealthy_takes_the_decider_out_of_service_and_restarts_it(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        await supervisor.report_unhealthy("drain exceeded")
        assert supervisor.ready is False  # immediately, before the restart completes
        await until(lambda: len(harness.processes) == 2)
        assert harness.processes[0].terminated
        await until(lambda: supervisor.ready)
        assert supervisor.restarts == 1
        await finish(supervisor, task)

    async def test_report_unhealthy_after_the_restart_budget_is_spent_is_permanent(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        await supervisor.report_unhealthy("first")
        await until(lambda: supervisor.ready and len(harness.processes) == 2)
        await supervisor.report_unhealthy("second")
        await until(lambda: supervisor.state == "failed")
        await asyncio.wait_for(task, timeout=2.0)


class TestShutdown:
    async def test_stop_terminates_the_upstream_and_ends_the_loop(self, harness):
        supervisor = harness.supervisor()
        task = await start(supervisor)
        await until(lambda: supervisor.ready)
        await finish(supervisor, task)
        assert harness.processes[0].terminated
        assert supervisor.ready is False
        assert supervisor.restarts == 0  # a clean stop is not a failure

    async def test_stop_before_run_is_harmless(self, harness):
        await harness.supervisor().stop()
