"""Pure helpers for the S4 decider benchmark (no AWS, no network)."""
from __future__ import annotations

import math
import re
from typing import Any

MODEL_ID = "hobson-latest"

DECIDER_INSTRUCTIONS = (
    "You are a player in a Capture the Flag game. Choose the single best action for this "
    "tick. Reach the enemy flag, grab it, and carry it back to your own base. The options "
    "are routes offered by the server: choose between them by weighing length, whether "
    "they are blocked right now, and the risk of being tagged along the way. Tag enemies "
    "inside your own field when it helps, and defend your flag when it is threatened. "
    "Your current plan, if any, is shown, and routes that continue your current path are "
    "marked; route ids are recomputed every tick and do not mean the same path from one tick "
    "to the next. Keep or change the plan based on the current situation."
)
THREAT_INSTRUCTIONS = (
    "Is there a threat to your flag right now? Answer true when an enemy is inside your own "
    "field close to your flag, or an enemy is carrying your flag."
)
THREAT_CRITERIA = {
    "true": "an enemy is inside my own field and close to my flag, or is carrying my flag",
    "false": "no enemy is inside my own field close to my flag and none is carrying my flag",
}
ROUTE_BAD_INSTRUCTIONS = (
    "Is the route you would continue now a bad one? Answer true when it is blocked right now "
    "or passes next to an enemy who can tag you."
)
ROUTE_BAD_CRITERIA = {
    "true": "the route I would continue is blocked now, or passes next to an enemy who can tag me",
    "false": "the route I would continue is open and no enemy can tag me along it",
}

_STATE_HEADER = re.compile(r"^\[tick (\d+)\] state sent to decider:\s*$")
_OPTIONS = re.compile(r"^\[tick (\d+)\] options: (\[.*\])\s*$")
_ANY_TICK_LINE = re.compile(r"^\[tick \d+\] ")


def parse_states(log_text: str) -> list[dict[str, Any]]:
    """Extract (state text, options) per tick from a decider_loop log."""
    lines = log_text.splitlines()
    states: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    i = 0
    while i < len(lines):
        header = _STATE_HEADER.match(lines[i])
        if header:
            tick = int(header.group(1))
            body: list[str] = []
            i += 1
            while i < len(lines) and not _ANY_TICK_LINE.match(lines[i]):
                body.append(lines[i])
                i += 1
            states[tick] = {"state": "\n".join(body).strip("\n"), "options": []}
            order.append(tick)
            continue
        opts = _OPTIONS.match(lines[i])
        if opts and int(opts.group(1)) in states:
            states[int(opts.group(1))]["options"] = _parse_list(opts.group(2))
        i += 1
    return [states[t] for t in order]


def _parse_list(text: str) -> list[str]:
    return re.findall(r"'([^']+)'", text)


def build_request(state: str, options: list[str]) -> dict[str, Any]:
    """The same 3-question request decider_loop sends, as a /v1/systemone body."""
    return {
        "state": state,
        "model": MODEL_ID,
        "questions": {
            "threat": {"type": "noul", "instructions": THREAT_INSTRUCTIONS, "criteria": THREAT_CRITERIA},
            "route_bad": {"type": "noul", "instructions": ROUTE_BAD_INSTRUCTIONS, "criteria": ROUTE_BAD_CRITERIA},
            "action": {
                "type": "choice",
                "instructions": DECIDER_INSTRUCTIONS,
                "criteria": {name: name for name in options},
            },
        },
    }


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def max_prob_delta(a: Any, b: Any) -> float:
    """Largest absolute numeric difference between two answer trees.

    A differing non-numeric leaf (e.g. a different chosen option) counts as infinite,
    so a dtype change that flips a decision always fails the +-0.05 gate.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return 0.0 if a == b else float("inf")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b))
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return float("inf")
        return max((max_prob_delta(a[k], b[k]) for k in a), default=0.0)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return float("inf")
        return max((max_prob_delta(x, y) for x, y in zip(a, b)), default=0.0)
    return 0.0 if a == b else float("inf")


def select_usable_states(states: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Keep states that have routing options (at most n). Raise if none are usable."""
    usable = [s for s in states if s["options"]][:n]
    if not usable:
        raise ValueError("no usable states (with options) found in the log")
    return usable


def check_comparable(a: list[Any], b: list[Any]) -> None:
    """Raise unless both answer lists are non-empty and have the same length."""
    if not a or not b:
        raise ValueError("answers list is empty; nothing to compare")
    if len(a) != len(b):
        raise ValueError(f"answer lists have different lengths ({len(a)} vs {len(b)})")
