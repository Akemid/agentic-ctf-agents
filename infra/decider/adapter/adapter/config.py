"""Adapter settings, read once from the environment at startup. Invalid values fail fast."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass

DTYPES = ("float16", "float32", "bfloat16")


class ConfigError(ValueError):
    """An environment variable holds a value the adapter cannot run with."""


def _number(env: Mapping[str, str], name: str, default, cast, *, minimum):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid {cast.__name__}") from exc
    if value < minimum:
        raise ConfigError(f"{name}={raw!r} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class AdapterConfig:
    port: int = 8080
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8099
    # None means "serve the staged checkpoint with strands-decider". Tests and the local smoke run
    # replace it with a fake decider, so no GPU and no model are needed.
    upstream_command: tuple[str, ...] | None = None
    checkpoint_dir: str = "/opt/decider/checkpoint"
    staged_dir: str = "/tmp/decider-checkpoint"  # noqa: S108 - container-local scratch space
    dtype: str = "float16"
    device: str = "cuda"
    queue_max_waiting: int = 8
    queue_wait_s: float = 2.0
    upstream_timeout_s: float = 10.0
    health_interval_s: float = 5.0
    health_failures: int = 3
    startup_timeout_s: float = 1100.0
    warmup_timeout_s: float = 120.0
    drain_timeout_s: float = 30.0
    drain_poll_s: float = 0.5

    @property
    def upstream_url(self) -> str:
        return f"http://{self.upstream_host}:{self.upstream_port}"

    def serve_command(self, checkpoint: str) -> tuple[str, ...]:
        """The argv that starts the upstream decider (an explicit command wins over the default)."""
        if self.upstream_command is not None:
            return self.upstream_command
        return (
            "strands-decider",
            "serve",
            checkpoint,
            "--device",
            self.device,
            "--host",
            self.upstream_host,
            "--port",
            str(self.upstream_port),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AdapterConfig:
        defaults = cls()
        dtype = env.get("DECIDER_DTYPE") or defaults.dtype
        if dtype not in DTYPES:
            raise ConfigError(f"DECIDER_DTYPE={dtype!r} must be one of {', '.join(DTYPES)}")
        command = env.get("DECIDER_UPSTREAM_COMMAND")
        return cls(
            port=_number(env, "ADAPTER_PORT", defaults.port, int, minimum=1),
            upstream_port=_number(env, "DECIDER_UPSTREAM_PORT", defaults.upstream_port, int, minimum=1),
            upstream_command=tuple(shlex.split(command)) if command else None,
            checkpoint_dir=env.get("DECIDER_CHECKPOINT_DIR") or defaults.checkpoint_dir,
            staged_dir=env.get("DECIDER_STAGED_DIR") or defaults.staged_dir,
            dtype=dtype,
            device=env.get("DECIDER_DEVICE") or defaults.device,
            queue_max_waiting=_number(
                env, "ADAPTER_QUEUE_MAX_WAITING", defaults.queue_max_waiting, int, minimum=1
            ),
            queue_wait_s=_number(env, "ADAPTER_QUEUE_WAIT_S", defaults.queue_wait_s, float, minimum=0.0),
            upstream_timeout_s=_number(
                env, "ADAPTER_UPSTREAM_TIMEOUT_S", defaults.upstream_timeout_s, float, minimum=0.1
            ),
            health_interval_s=_number(
                env, "ADAPTER_HEALTH_INTERVAL_S", defaults.health_interval_s, float, minimum=0.01
            ),
            health_failures=_number(env, "ADAPTER_HEALTH_FAILURES", defaults.health_failures, int, minimum=1),
            startup_timeout_s=_number(
                env, "ADAPTER_STARTUP_TIMEOUT_S", defaults.startup_timeout_s, float, minimum=1.0
            ),
            warmup_timeout_s=_number(
                env, "ADAPTER_WARMUP_TIMEOUT_S", defaults.warmup_timeout_s, float, minimum=1.0
            ),
            drain_timeout_s=_number(
                env, "ADAPTER_DRAIN_TIMEOUT_S", defaults.drain_timeout_s, float, minimum=0.0
            ),
            drain_poll_s=_number(env, "ADAPTER_DRAIN_POLL_S", defaults.drain_poll_s, float, minimum=0.01),
        )
