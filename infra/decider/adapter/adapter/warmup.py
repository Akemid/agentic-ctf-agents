"""Warm-up and health probes against the upstream decider."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .supervisor import Probe

log = logging.getLogger("adapter.warmup")

SYSTEMONE_PATH = "/v1/systemone"
HEALTH_PATH = "/health"
HEALTH_TIMEOUT_S = 2.0
_BODY = Path(__file__).with_name("warmup_body.json")


def load_warmup_body() -> bytes:
    """A representative `/v1/systemone` request: the first call pays the CUDA warm-up cost.

    Generated from `decider_loop.loop_logic.warmup_question()`; a test keeps the two in sync.
    """
    return _BODY.read_bytes()


async def upstream_healthy(client: httpx.AsyncClient, base_url: str) -> Probe:
    """One `/health` probe. The outcome says WHY it failed: a refusal means the server is gone, a timeout
    is what a busy decider looks like."""
    try:
        response = await client.get(f"{base_url}{HEALTH_PATH}", timeout=HEALTH_TIMEOUT_S)
    except httpx.TimeoutException:  # before ConnectError: ConnectTimeout is a timeout, not a refusal
        return Probe.TIMEOUT
    except httpx.ConnectError:
        return Probe.REFUSED
    except httpx.HTTPError:
        return Probe.BAD
    return Probe.OK if response.status_code == 200 else Probe.BAD


async def warm_up(client: httpx.AsyncClient, base_url: str, timeout_s: float) -> bool:
    """True when the decider answered a full question. It bypasses the request gate on purpose:
    the adapter is not ready (so it takes no traffic) while this runs."""
    try:
        response = await client.post(
            f"{base_url}{SYSTEMONE_PATH}",
            content=load_warmup_body(),
            headers={"content-type": "application/json"},
            timeout=timeout_s,
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("warm-up failed: %s", type(exc).__name__)
        return False
    ok = (
        response.status_code == 200 and isinstance(payload, dict) and isinstance(payload.get("answers"), dict)
    )
    if not ok:
        log.warning("warm-up answered HTTP %s without answers", response.status_code)
    return ok
