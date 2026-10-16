"""Invocation payload validation (pure, no I/O).

Contract (spec: match-session-lifecycle):

    {"action": "start"|"status"|"stop" (default "start"),
     "match_id": "^[A-Za-z0-9_-]{1,64}$" (required for start),
     "name": sanitized to "^[a-z0-9_-]{1,32}$" (default "<runtime>-<session_id[-6:]>"),
     "team": "red"|"blue"|"auto" (default "auto"),
     "agent_index": 0|1|2 (optional; the seat inside the team, the same name and range as the server's
                    `join_match`; needs an explicit team; `slot` is an equivalent, fully supported
                    key: it is what the web lobby sends),
     "model": "haiku"|"opus"|"nova-lite" (default "haiku"),
     "options": {window|threshold|threat_threshold|llm_budget_ms|safety_margin_ms}}

`{"prompt": "<json string>"}` is unwrapped first. Caller-supplied message history is always
rejected (AGENTS.md invariant 5), and so is every unknown top-level key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NAME_MAX_LEN = 32
_NAME_INVALID_RE = re.compile(r"[^a-z0-9_-]")

TEAMS = ("red", "blue", "auto")
MODELS = ("haiku", "opus", "nova-lite")

ALLOWED_KEYS = frozenset({"action", "match_id", "team", "agent_index", "slot", "name", "model", "options"})
AGENT_INDEX_MAX = 2  # the server's `agent_index` range is 0..2
HISTORY_KEYS = frozenset(
    {"messages", "tool_results", "history", "chat_history", "conversation", "message_history"}
)


class Action(StrEnum):
    START = "start"
    STATUS = "status"
    STOP = "stop"


class PayloadError(ValueError):
    """The invocation payload is invalid. The message names the offending field."""


@dataclass(frozen=True)
class OptionSpec:
    kind: type  # int or float
    minimum: float
    maximum: float


OPTION_SPECS: Mapping[str, OptionSpec] = {
    "window": OptionSpec(int, 1, 1000),
    "threshold": OptionSpec(float, 0.0, 1.0),
    "threat_threshold": OptionSpec(float, 0.0, 1.0),
    "llm_budget_ms": OptionSpec(int, 1, 600_000),
    "safety_margin_ms": OptionSpec(int, 0, 600_000),
}

# Which options each runtime understands. A runtime outside this table accepts none.
RUNTIME_OPTIONS: Mapping[str, frozenset[str]] = {
    "harness": frozenset(),
    "harness_sdk": frozenset({"window"}),
    "strand_decider": frozenset({"window"}),
    "decider_loop": frozenset({"threshold", "threat_threshold", "llm_budget_ms", "safety_margin_ms"}),
}


@dataclass(frozen=True)
class MatchRequest:
    action: Action
    match_id: str | None
    name: str
    team: str
    model: str
    options: Mapping[str, int | float] = field(default_factory=dict)
    agent_index: int | None = None  # forwarded to the server's `join_match` unchanged


def require_match_id(request: MatchRequest) -> str:
    """The match id of a request that is about to play. `start` always carries one (validated at parse)."""
    if request.match_id is None:
        raise PayloadError("match_id is required to play a match")
    return request.match_id


def sanitize_name(raw: str) -> str:
    """Lowercase, replace anything outside [a-z0-9_-] with '-', cap at 32 chars."""
    return _NAME_INVALID_RE.sub("-", raw.lower())[:NAME_MAX_LEN]


def parse_payload(
    raw: Any,
    *,
    runtime: str,
    session_id: str | None,
    allowed_options: frozenset[str] | None = None,
) -> MatchRequest:
    """Validate `raw` and return an immutable request. Raises PayloadError."""
    body = _unwrap_prompt(raw)
    _reject_unexpected_keys(body)

    action = _parse_action(body.get("action", Action.START.value))
    match_id = _parse_match_id(body.get("match_id"), required=action is Action.START)
    name = _parse_name(body.get("name"), runtime=runtime, session_id=session_id)
    team = _parse_choice("team", body.get("team", "auto"), TEAMS)
    model = _parse_choice("model", body.get("model", "haiku"), MODELS)
    if allowed_options is None:
        allowed_options = RUNTIME_OPTIONS.get(runtime, frozenset())
    agent_index = _parse_agent_index(body, team=team)
    options = _parse_options(body.get("options", {}), runtime=runtime, allowed=allowed_options)
    return MatchRequest(
        action=action,
        match_id=match_id,
        name=name,
        team=team,
        model=model,
        options=options,
        agent_index=agent_index,
    )


def _unwrap_prompt(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise PayloadError("payload must be a JSON object")
    if "prompt" not in raw:
        return raw
    extra = sorted(k for k in raw if k != "prompt")
    if extra:
        raise PayloadError(
            f"'prompt' must be the only key when wrapping a payload; found: {', '.join(extra)}"
        )
    prompt = raw["prompt"]
    if not isinstance(prompt, str):
        raise PayloadError("prompt must be a string")
    try:
        inner = json.loads(prompt)
    except json.JSONDecodeError as exc:
        raise PayloadError("prompt must contain a JSON object") from exc
    if not isinstance(inner, dict):
        raise PayloadError("prompt must contain a JSON object")
    if "prompt" in inner:
        raise PayloadError("prompt cannot be nested")
    return inner


def _reject_unexpected_keys(body: Mapping[str, Any]) -> None:
    for key in body:
        if key in HISTORY_KEYS:
            raise PayloadError(f"{key} is not accepted: message history cannot be supplied by the caller")
    unknown = sorted(str(k) for k in body if k not in ALLOWED_KEYS)
    if unknown:
        raise PayloadError(f"unknown key(s): {', '.join(unknown)}")


def _parse_action(value: Any) -> Action:
    if not isinstance(value, str) or value not in {a.value for a in Action}:
        raise PayloadError("action must be one of: start, status, stop")
    return Action(value)


def _parse_match_id(value: Any, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise PayloadError("match_id is required for action 'start'")
        return None
    if not isinstance(value, str) or not MATCH_ID_RE.fullmatch(value):
        raise PayloadError("match_id must match ^[A-Za-z0-9_-]{1,64}$")
    return value


def _parse_name(value: Any, *, runtime: str, session_id: str | None) -> str:
    if value is None:
        value = f"{runtime}-{(session_id or 'local')[-6:]}"
    if not isinstance(value, str):
        raise PayloadError("name must be a single string (one invocation plays one character)")
    name = sanitize_name(value)
    if not name:
        raise PayloadError("name must not be empty")
    return name


def _parse_choice(field_name: str, value: Any, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise PayloadError(f"{field_name} must be one of: {', '.join(choices)}")
    return value


def _parse_agent_index(body: Mapping[str, Any], *, team: str) -> int | None:
    """`agent_index` and `slot` (the web lobby's key) are equivalent, supported keys with one validation.

    Both present must agree. Internally the value is `agent_index`."""
    if "agent_index" not in body and "slot" not in body:
        return None
    values = [(key, body[key]) for key in ("agent_index", "slot") if key in body]
    for _key, value in values:
        # bool is an int subclass: `true` must not become seat 1. Errors always name the canonical key.
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= AGENT_INDEX_MAX:
            raise PayloadError(f"agent_index must be an integer between 0 and {AGENT_INDEX_MAX}")
    if len(values) == 2 and values[0][1] != values[1][1]:
        raise PayloadError(
            f"agent_index and slot differ ({values[0][1]} vs {values[1][1]}); "
            "they are the same field, send one of them"
        )
    if team == "auto":
        raise PayloadError(
            "agent_index requires an explicit team (red or blue); team 'auto' cannot target a seat"
        )
    return values[0][1]


def _parse_options(value: Any, *, runtime: str, allowed: frozenset[str]) -> dict[str, int | float]:
    if not isinstance(value, dict):
        raise PayloadError("options must be an object")
    parsed: dict[str, int | float] = {}
    for key, item in value.items():
        spec = OPTION_SPECS.get(key)
        if spec is None:
            raise PayloadError(f"unknown option: {key}")
        if key not in allowed:
            raise PayloadError(f"option {key} is not supported by runtime {runtime}")
        parsed[key] = _parse_number(key, item, spec)
    return parsed


def _parse_number(key: str, item: Any, spec: OptionSpec) -> int | float:
    if isinstance(item, bool) or not isinstance(item, int | float):
        raise PayloadError(f"option {key} must be a number")
    if spec.kind is int and not isinstance(item, int):
        raise PayloadError(f"option {key} must be an integer")
    if not spec.minimum <= item <= spec.maximum:
        raise PayloadError(f"option {key} must be between {spec.minimum} and {spec.maximum}")
    return float(item) if spec.kind is float else int(item)
