"""The adapter with its real supervisor around a REAL child process (a fake decider: no GPU, no model),
plus the contract with the client in shared/ctf_common/decider.py."""

import io
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapter.config import AdapterConfig
from adapter.runtime import build_app
from adapter.warmup import load_warmup_body
from ctf_common.decider import SageMakerDecider, noul

ADAPTER_DIR = Path(__file__).resolve().parents[1] / "infra" / "decider" / "adapter"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def served(monkeypatch):
    """(client, supervisor) over a live fake decider on a free port."""
    port = free_port()
    monkeypatch.setenv("PYTHONPATH", str(ADAPTER_DIR))
    monkeypatch.setenv("DECIDER_UPSTREAM_PORT", str(port))
    config = AdapterConfig.from_env(
        {
            "DECIDER_UPSTREAM_PORT": str(port),
            "DECIDER_UPSTREAM_COMMAND": f"{sys.executable} -m adapter.fake_decider",
            "ADAPTER_HEALTH_INTERVAL_S": "0.1",
            "ADAPTER_STARTUP_TIMEOUT_S": "20",
        }
    )
    app, supervisor = build_app(config)
    with TestClient(app) as client:
        yield client, supervisor


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def ping(client):
    return client.get("/ping").status_code


class TestWithARealChildProcess:
    def test_ping_turns_200_after_warmup_and_invocations_are_answered(self, served):
        client, _ = served
        wait_for(lambda: ping(client) == 200)
        response = client.post("/invocations", content=load_warmup_body())
        assert response.status_code == 200
        payload = response.json()
        assert "answers" in payload
        assert payload["adapter"]["queue_wait_ms"] >= 0

    def test_invocations_before_ready_are_503(self, monkeypatch):
        port = free_port()
        monkeypatch.setenv("PYTHONPATH", str(ADAPTER_DIR))
        monkeypatch.setenv("DECIDER_UPSTREAM_PORT", str(port))
        monkeypatch.setenv("FAKE_STARTUP_S", "30")
        config = AdapterConfig.from_env(
            {
                "DECIDER_UPSTREAM_PORT": str(port),
                "DECIDER_UPSTREAM_COMMAND": f"{sys.executable} -m adapter.fake_decider",
                "ADAPTER_STARTUP_TIMEOUT_S": "60",
            }
        )
        app, _ = build_app(config)
        with TestClient(app) as client:
            assert ping(client) == 503
            assert client.post("/invocations", content=b"{}").status_code == 503

    def test_a_killed_decider_is_restarted_once_then_ping_stays_503(self, served):
        client, supervisor = served
        wait_for(lambda: ping(client) == 200)

        os.kill(supervisor.process.pid, signal.SIGKILL)
        wait_for(lambda: supervisor.restarts == 1 and ping(client) == 200)
        assert client.post("/invocations", content=load_warmup_body()).status_code == 200

        os.kill(supervisor.process.pid, signal.SIGKILL)
        wait_for(lambda: supervisor.state == "failed")
        time.sleep(0.3)
        assert ping(client) == 503
        assert supervisor.restarts == 1


class FakeSageMakerRuntime:
    """boto3 `sagemaker-runtime` whose `invoke_endpoint` calls the adapter, as SageMaker would."""

    def __init__(self, client: TestClient):
        self._client = client
        self.bodies: list[bytes] = []

    def invoke_endpoint(self, *, EndpointName, ContentType, Accept, Body):  # noqa: N803 - boto3 names
        assert (ContentType, Accept) == ("application/json", "application/json")
        self.bodies.append(Body)
        response = self._client.post("/invocations", content=Body, headers={"content-type": ContentType})
        if response.status_code != 200:
            from botocore.exceptions import ClientError

            raise ClientError(
                {
                    "Error": {"Code": "ModelError", "Message": response.text},
                    "OriginalStatusCode": response.status_code,
                    "ResponseMetadata": {"HTTPStatusCode": 424},
                },
                "InvokeEndpoint",
            )
        return {"Body": io.BytesIO(response.content)}


class TestClientContract:
    def test_sagemaker_decider_round_trips_through_the_adapter(self, served):
        client, _ = served
        wait_for(lambda: ping(client) == 200)
        runtime = FakeSageMakerRuntime(client)
        decider = SageMakerDecider("ctf-decider", client_factory=lambda timeout: runtime)

        questions = {"threat": noul("Is there a threat?", {"true": "yes", "false": "no"})}
        answers = decider.ask("tick: 1", questions)

        assert answers  # the fake decider answered every question id it was given
        sent = json.loads(runtime.bodies[0])
        assert sent["state"] == "tick: 1"
        assert set(sent) == {"state", "model", "questions"}
        assert decider.last_queue_wait_ms is not None  # the additive adapter key reached the client
        assert decider.last_latency_ms is not None

    def test_the_warmup_body_is_what_the_client_sends_for_the_loops_warmup_question(self):
        """Drift guard: warmup_body.json is generated from decider_loop's warmup_question()."""
        from loop_logic import warmup_question

        state, questions = warmup_question()
        expected = {"state": state, "model": "hobson-latest", "questions": dict(questions)}
        assert json.loads(load_warmup_body()) == expected


class TestSpawnFailures:
    """A decider that cannot even be started is a terminal 503 on /ping, not a crashed task."""

    def build(self, tmp_path, **env):
        config = AdapterConfig.from_env(
            {
                "DECIDER_CHECKPOINT_DIR": str(tmp_path / "checkpoint"),
                "DECIDER_STAGED_DIR": str(tmp_path / "staged"),
                "ADAPTER_HEALTH_INTERVAL_S": "0.05",
                **env,
            }
        )
        return build_app(config)

    def test_a_missing_decider_binary_leaves_ping_at_503_with_state_failed(self, tmp_path, monkeypatch):
        checkpoint = tmp_path / "checkpoint"
        checkpoint.mkdir()
        (checkpoint / "strands_decider_config.json").write_text('{"torch_dtype": "bfloat16"}')
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no `strands-decider` anywhere
        app, supervisor = self.build(tmp_path)
        with TestClient(app) as client:
            wait_for(lambda: supervisor.state == "failed", timeout=5)
            response = client.get("/ping")
            assert response.status_code == 503
            assert response.json() == {"status": "failed"}
            assert client.post("/invocations", content=b"{}").status_code == 503

    def test_a_missing_checkpoint_is_the_same_terminal_failure(self, tmp_path):
        app, supervisor = self.build(tmp_path)  # DECIDER_CHECKPOINT_DIR does not exist
        with TestClient(app) as client:
            wait_for(lambda: supervisor.state == "failed", timeout=5)
            assert client.get("/ping").status_code == 503

    def test_the_checkpoint_is_staged_through_the_threaded_helper(self, tmp_path, monkeypatch):
        import adapter.runtime as runtime

        calls = []

        async def fake_stage(source, destination, dtype):
            calls.append((str(source), str(destination), dtype))
            raise OSError("stop here")

        monkeypatch.setattr(runtime, "stage_checkpoint_async", fake_stage)
        app, supervisor = self.build(tmp_path)
        with TestClient(app):
            wait_for(lambda: supervisor.state == "failed", timeout=5)
        assert calls == [(str(tmp_path / "checkpoint"), str(tmp_path / "staged"), "float16")]
