"""Wires the real pieces: HTTP client, upstream process, supervisor, FastAPI app."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI

from .app import Gate, create_app
from .config import AdapterConfig
from .stage import stage_checkpoint_async
from .supervisor import Process, Supervisor
from .warmup import upstream_healthy, warm_up

log = logging.getLogger("adapter.runtime")


def build_app(config: AdapterConfig) -> tuple[FastAPI, Supervisor]:
    client = httpx.AsyncClient()
    gate = Gate(config.queue_max_waiting, config.queue_wait_s)  # shared: the supervisor must see "busy"

    async def spawn() -> Process:
        checkpoint = config.checkpoint_dir
        if config.upstream_command is None:
            staged = await stage_checkpoint_async(
                Path(config.checkpoint_dir), config.staged_dir, config.dtype
            )
            checkpoint = str(staged)
        argv = config.serve_command(checkpoint)
        log.info("starting the decider: %s", " ".join(argv))
        return await asyncio.create_subprocess_exec(*argv, env=dict(os.environ))

    supervisor = Supervisor(
        spawn,
        lambda: upstream_healthy(client, config.upstream_url),
        lambda: warm_up(client, config.upstream_url, config.warmup_timeout_s),
        busy=lambda: gate.busy,
        health_interval_s=config.health_interval_s,
        startup_poll_s=min(1.0, config.health_interval_s),
        startup_timeout_s=config.startup_timeout_s,
        health_failures=config.health_failures,
    )
    return create_app(config, supervisor, client, gate), supervisor
