"""Adapter configuration, checkpoint staging, source hash and warm-up body."""

import asyncio
import json
import re
import shlex
import threading
from pathlib import Path

import pytest

from adapter.config import AdapterConfig, ConfigError
from adapter.stage import StageError, stage_checkpoint, stage_checkpoint_async
from adapter.supervisor import Probe
from adapter.warmup import load_warmup_body, upstream_healthy
from hash_source import source_hash


class TestConfig:
    def test_defaults_match_the_design(self):
        config = AdapterConfig.from_env({})
        assert (config.port, config.upstream_port) == (8080, 8099)
        assert config.queue_max_waiting == 8
        assert config.queue_wait_s == 2.0
        assert config.upstream_timeout_s == 10.0
        assert config.dtype == "float16"
        assert config.upstream_url == "http://127.0.0.1:8099"
        assert config.upstream_command is None

    def test_reads_overrides_from_the_environment(self):
        env = {
            "ADAPTER_PORT": "9000",
            "DECIDER_UPSTREAM_PORT": "9100",
            "ADAPTER_QUEUE_MAX_WAITING": "3",
            "ADAPTER_QUEUE_WAIT_S": "0.5",
            "ADAPTER_UPSTREAM_TIMEOUT_S": "4",
            "DECIDER_DTYPE": "float32",
            "DECIDER_UPSTREAM_COMMAND": "python -m adapter.fake_decider --flag 'a b'",
        }
        config = AdapterConfig.from_env(env)
        assert (config.port, config.upstream_port) == (9000, 9100)
        assert config.queue_max_waiting == 3
        assert config.queue_wait_s == 0.5
        assert config.upstream_timeout_s == 4.0
        assert config.dtype == "float32"
        assert config.upstream_command == ("python", "-m", "adapter.fake_decider", "--flag", "a b")

    def test_the_health_warmup_and_drain_knobs_have_defaults_and_overrides(self):
        defaults = AdapterConfig.from_env({})
        assert defaults.health_failures == 3
        assert defaults.warmup_timeout_s == 120.0
        assert defaults.health_interval_s == 5.0
        assert defaults.startup_timeout_s == 1100.0
        assert defaults.drain_timeout_s == 30.0
        assert defaults.drain_poll_s == 0.5
        config = AdapterConfig.from_env(
            {
                "ADAPTER_HEALTH_FAILURES": "5",
                "ADAPTER_WARMUP_TIMEOUT_S": "30",
                "ADAPTER_DRAIN_TIMEOUT_S": "12",
                "ADAPTER_DRAIN_POLL_S": "0.2",
            }
        )
        assert config.health_failures == 5
        assert config.warmup_timeout_s == 30.0
        assert config.drain_timeout_s == 12.0
        assert config.drain_poll_s == 0.2

    def test_every_environment_variable_the_config_reads_is_documented_in_the_readme(self):
        decider = Path(__file__).resolve().parents[1] / "infra" / "decider"
        names = set(
            re.findall(
                r'"((?:ADAPTER|DECIDER)_[A-Z_]+)"',
                (decider / "adapter" / "adapter" / "config.py").read_text(),
            )
        )
        assert {"ADAPTER_HEALTH_FAILURES", "ADAPTER_WARMUP_TIMEOUT_S"} <= names
        readme = (decider / "README.md").read_text()
        assert [name for name in sorted(names) if name not in readme] == []

    @pytest.mark.parametrize(
        "env",
        [
            {"DECIDER_DTYPE": "int8"},
            {"ADAPTER_PORT": "eighty"},
            {"ADAPTER_QUEUE_WAIT_S": "-1"},
            {"ADAPTER_QUEUE_MAX_WAITING": "0"},
            {"ADAPTER_HEALTH_FAILURES": "0"},
            {"ADAPTER_WARMUP_TIMEOUT_S": "0"},
            {"ADAPTER_DRAIN_TIMEOUT_S": "-1"},
            {"ADAPTER_DRAIN_POLL_S": "0"},
        ],
    )
    def test_rejects_invalid_values_at_startup(self, env):
        with pytest.raises(ConfigError):
            AdapterConfig.from_env(env)

    def test_default_upstream_command_serves_the_staged_checkpoint_on_loopback(self):
        config = AdapterConfig.from_env({})
        argv = config.serve_command("/tmp/staged")
        assert argv[:3] == ("strands-decider", "serve", "/tmp/staged")
        assert argv[argv.index("--device") + 1] == "cuda"
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert argv[argv.index("--port") + 1] == "8099"

    def test_an_explicit_command_replaces_the_default(self):
        config = AdapterConfig.from_env({"DECIDER_UPSTREAM_COMMAND": shlex.join(["fake", "--x"])})
        assert config.serve_command("/ignored") == ("fake", "--x")


def checkpoint(tmp_path, **files):
    root = tmp_path / "checkpoint"
    root.mkdir()
    (root / "head.safetensors").write_bytes(b"weights")
    for name, content in files.items():
        (root / name.replace("__", ".")).write_text(json.dumps(content))
    return root


class TestStageCheckpoint:
    def test_patches_torch_dtype_in_a_copy_and_leaves_the_source_alone(self, tmp_path):
        src = checkpoint(
            tmp_path, strands_decider_config__json={"base_model": "m", "torch_dtype": "bfloat16"}
        )
        staged = stage_checkpoint(src, tmp_path / "staged", "float16")
        patched = json.loads((staged / "strands_decider_config.json").read_text())
        assert patched == {"base_model": "m", "torch_dtype": "float16"}
        assert json.loads((src / "strands_decider_config.json").read_text())["torch_dtype"] == "bfloat16"
        assert (staged / "head.safetensors").read_bytes() == b"weights"

    def test_patches_the_dtype_key_when_there_is_no_torch_dtype(self, tmp_path):
        src = checkpoint(tmp_path, strands_decider_config__json={"dtype": "bfloat16", "x": 1})
        staged = stage_checkpoint(src, tmp_path / "staged", "float32")
        assert json.loads((staged / "strands_decider_config.json").read_text()) == {
            "dtype": "float32",
            "x": 1,
        }

    def test_falls_back_to_config_json(self, tmp_path):
        src = checkpoint(
            tmp_path,
            strands_decider_config__json={"base_model": "m"},
            config__json={"torch_dtype": "bfloat16"},
        )
        staged = stage_checkpoint(src, tmp_path / "staged", "float16")
        assert json.loads((staged / "config.json").read_text())["torch_dtype"] == "float16"

    def test_fails_when_no_dtype_key_exists_anywhere(self, tmp_path):
        src = checkpoint(tmp_path, strands_decider_config__json={"base_model": "m"})
        with pytest.raises(StageError, match="torch_dtype"):
            stage_checkpoint(src, tmp_path / "staged", "float16")

    def test_fails_when_the_checkpoint_is_missing(self, tmp_path):
        with pytest.raises(StageError):
            stage_checkpoint(tmp_path / "nowhere", tmp_path / "staged", "float16")

    def test_restaging_replaces_a_previous_copy(self, tmp_path):
        src = checkpoint(tmp_path, strands_decider_config__json={"torch_dtype": "bfloat16"})
        stage_checkpoint(src, tmp_path / "staged", "float16")
        staged = stage_checkpoint(src, tmp_path / "staged", "float32")
        assert json.loads((staged / "strands_decider_config.json").read_text())["torch_dtype"] == "float32"


class TestStageCheckpointAsync:
    async def test_copies_off_the_event_loop_thread(self, tmp_path, monkeypatch):
        import shutil

        src = checkpoint(tmp_path, strands_decider_config__json={"torch_dtype": "bfloat16"})
        threads: list[int] = []
        real = shutil.copytree

        def spy(*args, **kwargs):
            threads.append(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(shutil, "copytree", spy)
        staged = await stage_checkpoint_async(src, tmp_path / "staged", "float16")
        assert threads
        assert threads[0] != threading.get_ident()
        assert json.loads((staged / "strands_decider_config.json").read_text())["torch_dtype"] == "float16"

    async def test_a_slow_copy_does_not_stall_the_event_loop(self, tmp_path, monkeypatch):
        import shutil
        import time

        src = checkpoint(tmp_path, strands_decider_config__json={"torch_dtype": "bfloat16"})
        real = shutil.copytree

        def slow(*args, **kwargs):
            time.sleep(0.3)
            return real(*args, **kwargs)

        monkeypatch.setattr(shutil, "copytree", slow)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await stage_checkpoint_async(src, tmp_path / "staged", "float16")
        task.cancel()
        assert ticks >= 10


class TestSourceHash:
    def populate(self, root, files):
        for name, content in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def test_is_stable_and_independent_of_creation_order(self, tmp_path):
        files = [("Dockerfile", "FROM x"), ("adapter/app.py", "a = 1"), ("adapter/z.py", "z = 1")]
        self.populate(tmp_path / "one", files)
        self.populate(tmp_path / "two", list(reversed(files)))
        assert source_hash(tmp_path / "one", "m") == source_hash(tmp_path / "two", "m")

    def test_changes_with_content_with_the_model_id_and_with_file_names(self, tmp_path):
        self.populate(tmp_path / "a", [("f.py", "1")])
        self.populate(tmp_path / "b", [("f.py", "2")])
        self.populate(tmp_path / "c", [("g.py", "1")])
        hashes = {
            source_hash(tmp_path / "a", "m"),
            source_hash(tmp_path / "b", "m"),
            source_hash(tmp_path / "c", "m"),
            source_hash(tmp_path / "a", "other-model"),
        }
        assert len(hashes) == 4

    def test_ignores_bytecode_caches(self, tmp_path):
        self.populate(tmp_path, [("f.py", "1")])
        before = source_hash(tmp_path, "m")
        self.populate(tmp_path, [("__pycache__/f.cpython-313.pyc", "junk"), ("adapter/x.pyc", "junk")])
        assert source_hash(tmp_path, "m") == before

    def test_is_a_short_hex_tag(self, tmp_path):
        self.populate(tmp_path, [("f.py", "1")])
        tag = source_hash(tmp_path, "m")
        assert len(tag) == 16
        int(tag, 16)

    def test_the_real_adapter_directory_hashes(self):
        from pathlib import Path

        import hash_source

        root = Path(hash_source.__file__).parent
        assert len(source_hash(root, "m")) == 16


class TestWarmupBody:
    def test_is_a_valid_systemone_request(self):
        body = json.loads(load_warmup_body())
        assert isinstance(body["state"], str)
        assert body["model"] == "hobson-latest"
        assert body["questions"]


class TestUpstreamHealthProbe:
    @staticmethod
    async def probe(handler):
        import httpx

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await upstream_healthy(client, "http://decider")

    async def test_200_is_ok(self):
        import httpx

        assert await self.probe(lambda request: httpx.Response(200)) is Probe.OK

    async def test_a_refused_connection_is_distinguished_from_a_timeout(self):
        import httpx

        def refuse(request):
            raise httpx.ConnectError("refused")

        def silence(request):
            raise httpx.ReadTimeout("silent", request=request)

        assert await self.probe(refuse) is Probe.REFUSED
        assert await self.probe(silence) is Probe.TIMEOUT

    async def test_a_connect_timeout_is_a_timeout_not_a_refusal(self):
        import httpx

        def slow_connect(request):
            raise httpx.ConnectTimeout("slow", request=request)

        assert await self.probe(slow_connect) is Probe.TIMEOUT

    async def test_any_other_answer_or_error_is_bad(self):
        import httpx

        def reset(request):
            raise httpx.RemoteProtocolError("reset")

        assert await self.probe(lambda request: httpx.Response(503)) is Probe.BAD
        assert await self.probe(reset) is Probe.BAD
