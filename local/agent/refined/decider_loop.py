"""REFINED CTF agent: plain Python owns the game loop and a local strands-decider picks the action.

This is the refined version of `agent/agent-decider-loop.py` (the original is untouched).
Everything is kept (warm-up, time budget, LLM timeout + TickCancelHandler, StopAfterActionHook,
plan memory by path, error handling, exit safety net, flags). Changes versus the original:

1. ONE decider request now carries THREE questions about the same encoded state (cheap, see
   `_client.Decider`): `threat` (noul), `route_bad` (noul) and `action` (choice over the server
   routes). A small pure policy, `combine_answers`, merges the three answers:
     - threat >= --threat-threshold and a `tag:<id>` / `defend_flag` option is offered -> pick it
       (reason "threat");
     - the choice picked a blocked/risky route, `route_bad` >= 0.5 and a same-goal route that is
       neither blocked nor risky is offered (e.g. `*_safe`) -> pick that (reason "avoid
       blocked/risky");
     - otherwise the choice's own pick (reason "choice").
   The existing threshold/budget/LLM logic then runs on the resulting label. For "choice" the
   confidence is the `action` confidence; for the rule-driven reasons it is the triggering noul
   probability.
2. A BLOCKED route is never annotated "continues your current path" (that reinforced insisting on
   blocked routes); it gets "would continue your path but is BLOCKED now" instead.
3. Imports `_client` from the parent `agent/` folder; default `--name` is `refined-decider`;
   new flag `--threat-threshold`.

Original description:

    wait_for_turn -> render state -> ONE decider request -> call the tool directly

Only when the decider is not confident enough (or is unreachable) does a strands `Agent`
run, once for that tick, restricted to the action tools. No LLM is involved on the fast path.

    uv run strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8099
    uv run python agent/refined/decider_loop.py --match llm-1 --threshold 0.85

STRANDS_DECIDER_URL overrides the decider endpoint.

`--llm-only` is an ablation mode: same loop, state rendering, plan continuity and route options,
but the LLM fallback agent decides every tick and the decider is never consulted (no server
needed). The LLM runs with whatever time is left (no "not enough time for the LLM" skip) and a
timed-out tick is a wait, since there is no decider choice to execute. `combine_answers` is unused.

Verified against the live servers (see comments marked VERIFIED / ASSUMPTION):

- VERIFIED  MCP tool names (raw, no prefix, because we call the MCPClient directly):
            get_rules, get_map, join_match, wait_for_turn, move, tag, grab_flag, wait.
- VERIFIED  A decider `choice` answer is
            {"type": "choice", "choice": <label>, "probabilities": {...}, "confidence": <float>}.
            Note `confidence` is NOT the top probability (0.31 next to 0.48 was observed), so the
            threshold applies to `confidence`.
- VERIFIED  `wait_for_turn` payload (MCP turn contract v2): top-level `status`, `tick`, `events`,
            `last_action` (the outcome of our previous action, null on the first turn) and the game
            state under `state` (agents, flags, score, self, self_position, moves, teammates, routes).
            `state` NO LONGER carries `grid`, `walls`, `fields` or `flags[].base`: the static map
            comes once from `join_match.map` (or `get_map`) and is kept as a `MapInfo`.
            `state.agents[].carrying` is non-null while an agent holds a flag; we only treat it as a
            boolean.
- VERIFIED  `state.routes` (from get_rules): advisory route options computed by the server, each
            with id, goal, first_step, length, path, blocked_now, blocked_reason, risks and
            closest_threat ({enemy_id, step, distance} or null). The server never recommends a route and neither does this client:
            the decider (or the LLM fallback) chooses between them. When `routes` is empty or
            missing, the agent falls back to plain per-direction `move_<dir>` options.
- ASSUMPTION `turn["observation"]` is only a fallback when `state` is missing.
"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

# `_client` lives in the parent `agent/` folder.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _client import Decider, ServerUnavailable  # noqa: E402
from strands import Agent
from strands.hooks import AfterToolCallEvent, AfterToolsEvent
from strands.interventions import Deny, InterventionHandler, Proceed
from strands.tools.mcp import MCPClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODELS = {
    "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "opus": "global.anthropic.claude-opus-4-6-v1",
    "nova-lite": "global.amazon.nova-2-lite-v1:0",
}

SYSTEM_PROMPT = (
    "You are AmazingPlayer, a Capture the Flag game agent competing as a member of a "
    "team. You are direct and tactical. You are given the current game state for ONE tick. "
    "Pick the single best action and call exactly one action tool with the tick number "
    "provided. Do not call any other tool and do not answer with text only."
)

MCP_SERVER_NAME = "ctf-game"
MCP_SERVERS = {
    MCP_SERVER_NAME: {
        "url": "http://localhost:8080/mcp",
        "transport": "streamable-http",
    },
}

# Raw tool names (VERIFIED via list_tools_sync). No server-name prefix on direct calls.
TOOL_JOIN = "join_match"
TOOL_GET_MAP = "get_map"
TOOL_WAIT_FOR_TURN = "wait_for_turn"
ACTION_TOOLS = ("move", "tag", "grab_flag", "wait")

DIRECTIONS = ("up", "down", "left", "right")

DEFAULT_THRESHOLD = 0.85
THREAT_YES = 0.5  # P(true) from the `threat` noul at which the threat rule fires
ROUTE_BAD_YES = 0.5  # P(true) from the `route_bad` noul at which the avoid rule fires
MAX_CONSECUTIVE_TOOL_ERRORS = 5
DEFAULT_DEADLINE_MS = 5000
DEFAULT_LLM_BUDGET_MS = 4500
DEFAULT_SAFETY_MARGIN_MS = 300
EXIT_GRACE_S = 2.0

DECIDER_LOCK_FILE = os.path.join(tempfile.gettempdir(), "strands-decider.lock")

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

LLM_PLAN_NOTE = (
    "Your current plan, if any, is shown, and routes that continue your current path are "
    "marked; route ids are recomputed every tick and do not mean the same path from one tick "
    "to the next. Keep or change the plan based on the current situation. When you call move or tag "
    "for a route, set `rationale` to `route <route_id>` (e.g. `route enemy_flag_direct`)."
)

# ---------------------------------------------------------------------------
# Pure logic (no I/O): state -> options, label -> tool call, answer parsing
# ---------------------------------------------------------------------------


def parse_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Decode an MCPToolResult into a dict. JSON text blocks are parsed; other text is wrapped."""
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if text is None:
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {}


def _state(turn: dict[str, Any]) -> dict[str, Any]:
    """The game state: `turn["state"]`, falling back to `turn["observation"]` if absent."""
    for key in ("state", "observation"):
        value = turn.get(key)
        if isinstance(value, dict):
            return value
    return {}

def extract_tick(turn: dict[str, Any]) -> int:
    """The tick number every action must echo back. Top-level first, then inside state."""
    for source in (turn, _state(turn)):
        tick = source.get("tick")
        if isinstance(tick, int) and not isinstance(tick, bool):
            return tick
    raise ValueError("no tick found in wait_for_turn result")

def _xy(value: Any) -> tuple[int, int] | None:
    """Coerce an {x, y} dict into a tuple."""
    if isinstance(value, dict) and isinstance(value.get("x"), int) and isinstance(value.get("y"), int):
        return value["x"], value["y"]
    return None

def _me(state: dict[str, Any]) -> dict[str, Any] | None:
    """The agent entry whose id equals `state.self`."""
    for agent in state.get("agents") or []:
        if isinstance(agent, dict) and agent.get("id") == state.get("self"):
            return agent
    return None

def _my_position(state: dict[str, Any]) -> tuple[int, int] | None:
    return _xy(state.get("self_position")) or _xy((_me(state) or {}).get("position"))

def _my_team(state: dict[str, Any]) -> str | None:
    team = (_me(state) or {}).get("team")
    return team if isinstance(team, str) else None

def _flag(state: dict[str, Any], team: str | None) -> dict[str, Any] | None:
    for flag in state.get("flags") or []:
        if isinstance(flag, dict) and flag.get("team") == team:
            return flag
    return None

def _enemy_team(team: str | None) -> str | None:
    return {"red": "blue", "blue": "red"}.get(team or "")

def _enemies(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Connected agents on the other team."""
    team = _my_team(state)
    return [
        a for a in state.get("agents") or []
        if isinstance(a, dict) and a.get("team") != team and a.get("team") is not None
        and not a.get("disconnected") and a.get("id") != state.get("self")
    ]

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class MapInfo:
    """The static board, captured once from `join_match.map` (or `get_map`). It never changes in a match.

    `fields` maps a team to its `(min_x, max_x)` columns, `neutral_x` is the neutral column, `flag_bases`
    maps a team to its base cell. `walls` is kept for completeness: movement and routes are server-computed.
    """

    fields: dict[str, tuple[int, int]]
    neutral_x: int | None
    walls: frozenset[tuple[int, int]]
    flag_bases: dict[str, tuple[int, int]]


def parse_map(raw: Any) -> MapInfo | None:
    """A `MapInfo` from a `join_match.map` / `get_map` payload; None when it has no usable `fields` (pure)."""
    raw_fields = raw.get("fields") if isinstance(raw, dict) else None
    if not isinstance(raw_fields, dict):
        return None
    fields: dict[str, tuple[int, int]] = {}
    for team in ("red", "blue"):
        bounds = raw_fields.get(team)
        if isinstance(bounds, dict) and _is_int(bounds.get("min_x")) and _is_int(bounds.get("max_x")):
            fields[team] = (bounds["min_x"], bounds["max_x"])
    if not fields:
        return None
    walls = raw.get("walls")
    bases = raw.get("flag_bases")
    return MapInfo(
        fields=fields,
        neutral_x=raw_fields["neutral_x"] if _is_int(raw_fields.get("neutral_x")) else None,
        walls=frozenset(c for c in map(_xy, walls if isinstance(walls, list) else []) if c is not None),
        flag_bases={
            team: cell
            for team, cell in (
                (t, _xy(bases.get(t) if isinstance(bases, dict) else None)) for t in ("red", "blue")
            )
            if cell is not None
        },
    )


def _in_my_field(state: dict[str, Any], map_info: MapInfo | None = None) -> bool:
    """Whether we stand in our own field. False without the map: the turn state no longer says."""
    me = _my_position(state)
    bounds = map_info.fields.get(_my_team(state) or "") if map_info is not None else None
    if me is None or bounds is None:
        return False
    return bounds[0] <= me[0] <= bounds[1]

def _is_adjacent(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1

def _carrying(state: dict[str, Any]) -> bool:
    return (_me(state) or {}).get("carrying") is not None

def _open_directions(state: dict[str, Any]) -> list[str]:
    moves = state.get("moves")
    moves = moves if isinstance(moves, dict) else {}
    return [d for d in DIRECTIONS if moves.get(d) == "open"]

def _routes(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Well-formed server routes (dicts with a string id), in server order."""
    routes = state.get("routes")
    return [r for r in routes if isinstance(r, dict) and isinstance(r.get("id"), str)] if isinstance(routes, list) else []


Cell = tuple[int, int]


@dataclass
class Plan:
    """Loop-owned memory of the path the agent last acted on. Informational only: the model
    still decides every tick and may keep or change it.

    Route ids are recomputed by the server every tick, so continuity is judged by `path` (the
    executed route's path when it was chosen) and `position` (where the agent stood then).
    `route_id` is kept for display and logging only.
    """

    route_id: str | None = None
    goal: str | None = None
    since_tick: int | None = None
    last_action: str | None = None
    path: tuple[Cell, ...] | None = None
    position: Cell | None = None


def _cells(path: Any) -> tuple[Cell, ...] | None:
    """Normalise a path (cells as tuples or {x, y} dicts) to a tuple of tuples; None if malformed/empty."""
    if not isinstance(path, (list, tuple)) or not path:
        return None
    cells: list[Cell] = []
    for cell in path:
        if isinstance(cell, dict):
            cell = _xy(cell)
        if (not isinstance(cell, (list, tuple)) or len(cell) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in cell)):
            return None
        cells.append((cell[0], cell[1]))
    return tuple(cells)


def _same_prefix(expected: tuple[Cell, ...], route: tuple[Cell, ...]) -> bool:
    """True when both agree on their overlapping prefix (both may be truncated); needs >= 1 cell."""
    overlap = min(len(expected), len(route))
    return overlap >= 1 and expected[:overlap] == route[:overlap]


def continues_path(prev_path: Any, prev_position: Any, my_position: Any, route_path: Any) -> bool:
    """Whether `route_path` (from `my_position`) continues the previously executed path (pure).

    Moved one step along the previous path: the route must start with the rest of that path.
    Did not move (e.g. move_blocked): the route must start with the previous path again.
    Anything else (respawned, tagged, moved elsewhere) or malformed input: False.
    """
    prev, route = _cells(prev_path), _cells(route_path)
    here, before = _cells([my_position]), _cells([prev_position])
    if prev is None or route is None or here is None:
        return False
    here_cell = here[0]
    if here_cell == prev[0] and (before is None or before[0] != here_cell):
        return _same_prefix(prev[1:], route)
    if before is not None and here_cell == before[0]:
        return _same_prefix(prev, route)
    return False


def route_continues_plan(plan: Plan | None, my_position: Cell | None, route: dict[str, Any]) -> bool:
    """Whether an offered route's path continues the plan's stored path."""
    return plan is not None and continues_path(plan.path, plan.position, my_position, route.get("path"))


def route_path(turn: dict[str, Any], route_id: str | None) -> tuple[Cell, ...] | None:
    """Path of the offered route with this id, if any."""
    route = next((r for r in _routes(_state(turn)) if r["id"] == route_id), None)
    return _cells(route.get("path")) if route else None


def update_plan(plan: Plan, executed_route_id: str | None, executed_goal: str | None,
                action_text: str, tick: int, executed_path: Any = None,
                position: Cell | None = None) -> Plan:
    """New plan after an executed action (pure).

    A route action keeps `since_tick` only while the goal is unchanged AND the executed route's
    path continues the plan's path (the route id is irrelevant: ids are recomputed every tick).
    Otherwise it starts a new plan at this tick. `position` is where the agent stood when
    acting. `grab_flag` clears the plan; any other non-route action (e.g. `wait`) only records
    `last_action`.
    """
    if executed_route_id is None:
        if action_text.split(" ", 1)[0] == "grab_flag":
            return Plan(last_action=action_text)
        return replace(plan, last_action=action_text)
    keeps = (plan.since_tick is not None and plan.goal is not None and plan.goal == executed_goal
             and continues_path(plan.path, plan.position, position, executed_path))
    since = plan.since_tick if keeps else tick
    return Plan(executed_route_id, executed_goal, since, action_text, _cells(executed_path), position)


def plan_suffix(plan: Plan) -> str:
    """Short per-tick log suffix, empty when there is no plan."""
    return f" [plan {plan.route_id} since {plan.since_tick}]" if plan.route_id else ""


def describe_action(tool: str, arguments: dict[str, Any]) -> str:
    """Short text for an executed action, e.g. `move down` or `tag blue-2`."""
    if tool == "move" and arguments.get("direction"):
        return f"move {arguments['direction']}"
    if tool == "tag" and arguments.get("enemy_id"):
        return f"tag {arguments['enemy_id']}"
    return tool


def route_goal(turn: dict[str, Any], route_id: str | None) -> str | None:
    """Goal of the offered route with this id, if any."""
    route = next((r for r in _routes(_state(turn)) if r["id"] == route_id), None)
    return route.get("goal") if route else None


def extract_action_call(messages: Any) -> tuple[str, dict[str, Any]] | None:
    """First (tool name, input) action toolUse block in an agent's messages, if any."""
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            use = block.get("toolUse") if isinstance(block, dict) else None
            if isinstance(use, dict) and use.get("name") in ACTION_TOOLS:
                tool_input = use.get("input")
                return use["name"], tool_input if isinstance(tool_input, dict) else {}
    return None


def infer_executed_route(call: tuple[str, dict[str, Any]] | None, turn: dict[str, Any],
                         plan: Plan) -> str | None:
    """Which offered route an LLM action call corresponds to, or None for non-route actions.

    Order: `rationale` of the form `route <id>` naming an offered route; `tag` -> `tag:<id>`;
    `move` -> route whose first step matches (current plan's goal preferred, then lowest id).
    """
    if call is None:
        return None
    tool, arguments = call
    routes = _routes(_state(turn))
    offered = {r["id"]: r for r in routes}

    match = re.search(r"route\s+(\S+)", str(arguments.get("rationale") or ""))
    if match:
        candidate = match.group(1).rstrip(".,;:!)\"'`")
        if candidate in offered:
            return candidate

    if tool == "tag":
        candidate = f"tag:{arguments.get('enemy_id')}"
        return candidate if candidate in offered else None
    if tool == "move":
        matches = sorted((r for r in routes if r.get("first_step") == arguments.get("direction")),
                         key=lambda r: (r.get("goal") != plan.goal, r["id"]))
        return matches[0]["id"] if matches else None
    return None


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _route_enemy_id(route: dict[str, Any]) -> str | None:
    """Enemy id of a `tag:<enemy_id>` route."""
    route_id = route["id"]
    return route_id[4:] if route_id.startswith("tag:") and len(route_id) > 4 else None


def describe_route(route: dict[str, Any]) -> str:
    """Neutral, fact-only description of one server route."""
    goal = str(route.get("goal", "unknown"))
    enemy_id = _route_enemy_id(route)
    parts = [f"goal {goal} {enemy_id}" if goal == "tag" and enemy_id else f"goal {goal}"]
    length, first_step = route.get("length"), route.get("first_step")
    if goal == "tag" and length == 0 and first_step is None:
        parts.append("adjacent now: tag immediately")
    else:
        if isinstance(length, int):
            parts.append(_plural(length, "step"))
        if first_step:
            parts.append(f"first step {first_step}")
    if route.get("blocked_now"):
        reason = route.get("blocked_reason")
        parts.append(f"BLOCKED now ({reason})" if reason else "BLOCKED now")
    risks = [r for r in route.get("risks") or [] if isinstance(r, dict)]
    if risks:
        detail = "; ".join(f"can be tagged by {r.get('enemy_id', '?')} at step {r.get('step', '?')}" for r in risks)
        parts.append(f"{_plural(len(risks), 'risk')}: {detail}")
    if "closest_threat" in route:
        threat = route["closest_threat"]
        if isinstance(threat, dict):
            parts.append(f"closest threat {threat.get('enemy_id', '?')} at step {threat.get('step', '?')}, "
                         f"distance {threat.get('distance', '?')}")
    return ", ".join(parts)


def build_options(turn: dict[str, Any], plan: Plan | None = None,
                  map_info: MapInfo | None = None) -> dict[str, str]:
    """Options for the decider `choice` question, built from the state. Always includes `wait`.

    One option per server route (label = route id). Without routes, falls back to one
    `move_<dir>` option per open direction (and adjacent tags). No option is ever recommended.
    With a `plan`, every route whose path continues the plan's path gets a neutral note, and
    other routes with the same goal are marked as a different path. The adjacent-`tag_` fallback needs
    `map_info` (to know we stand in our own field); without it no tag option is invented.
    """
    state = _state(turn)
    me, team = _my_position(state), _my_team(state)

    options: dict[str, str] = {}
    routes = _routes(state)
    for route in routes:
        text = describe_route(route)
        if _plan_active(plan):
            if route_continues_plan(plan, me, route):
                if route.get("blocked_now"):
                    text += " | would continue your path but is BLOCKED now"
                else:
                    text += f" | continues your current path ({_plural(_plan_age(turn, plan), 'tick')})"
            elif plan.goal is not None and route.get("goal") == plan.goal:
                text += " | same goal as your current plan, different path"
        options[route["id"]] = text
    if not routes:
        for direction in _open_directions(state):
            options[f"move_{direction}"] = f"move one cell {direction}"

    enemy_flag = _flag(state, _enemy_team(team))
    if (me is not None and enemy_flag is not None and enemy_flag.get("carried_by") is None
            and _xy(enemy_flag.get("position")) == me):
        options["grab_flag"] = "pick up the enemy flag you are standing on"

    if not routes and me is not None and _in_my_field(state, map_info):
        for enemy in _enemies(state):
            pos = _xy(enemy.get("position"))
            if pos is not None and _is_adjacent(me, pos):
                options[f"tag_{enemy['id']}"] = f"tag the adjacent enemy {enemy['id']} (you are in your own field)"

    options["wait"] = "do nothing this tick"
    return options

def label_to_call(label: str, tick: int, turn: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Map a chosen option label to (raw MCP tool name, arguments).

    Route labels need `turn` to look up the route's first step.
    """
    if label.startswith("move_") and label[5:] in DIRECTIONS:
        return "move", {"direction": label[5:], "tick": tick}
    if label.startswith("tag_") and len(label) > 4:
        return "tag", {"enemy_id": label[4:], "tick": tick}
    if label == "grab_flag":
        return "grab_flag", {"tick": tick}
    if label == "wait":
        return "wait", {"tick": tick}
    route = next((r for r in _routes(_state(turn or {})) if r["id"] == label), None)
    if route is not None:
        rationale = f"route {label}"
        first_step = route.get("first_step")
        if first_step in DIRECTIONS:
            return "move", {"direction": first_step, "tick": tick, "rationale": rationale}
        if route.get("length") == 0:  # already on the route's target (first_step is null)
            enemy_id = _route_enemy_id(route)
            if enemy_id:
                return "tag", {"enemy_id": enemy_id, "tick": tick, "rationale": rationale}
            if route.get("goal") == "enemy_flag":
                return "grab_flag", {"tick": tick, "rationale": rationale}
            return "wait", {"tick": tick, "rationale": rationale}  # own_base / defend_flag: stay put
    raise ValueError(f"unknown option label: {label!r}")


def parse_choice(answer: dict[str, Any]) -> tuple[str | None, float]:
    """(label, confidence) from a decider `choice` answer; (None, 0.0) if unusable.

    VERIFIED shape: {"type": "choice", "choice": str, "probabilities": {...}, "confidence": float}.
    Falls back to the chosen label's probability if `confidence` is absent.
    """
    if not isinstance(answer, dict):
        return None, 0.0
    label = answer.get("choice")
    if not isinstance(label, str):
        return None, 0.0
    confidence = answer.get("confidence")
    if not isinstance(confidence, (int, float)):
        probs = answer.get("probabilities")
        confidence = probs.get(label) if isinstance(probs, dict) else None
    return label, float(confidence) if isinstance(confidence, (int, float)) else 0.0


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


def build_questions(options: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The three questions sent in ONE decider request (the state is encoded once and shared)."""
    return {
        "threat": Decider.noul(THREAT_INSTRUCTIONS, THREAT_CRITERIA),
        "route_bad": Decider.noul(ROUTE_BAD_INSTRUCTIONS, ROUTE_BAD_CRITERIA),
        "action": Decider.choice(DECIDER_INSTRUCTIONS, options),
    }


def parse_noul(answer: Any) -> float:
    """P(true) from a decider `noul` answer ({"type": "noul", "noul": float}); 0.0 if unusable."""
    value = answer.get("noul") if isinstance(answer, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _is_bad(route: dict[str, Any]) -> bool:
    return bool(route.get("blocked_now")) or bool(route.get("risks"))


def _threat_candidates(options: dict[str, str], routes: dict[str, dict[str, Any]]) -> list[str]:
    """Offered, unblocked `tag:<id>` / `tag_<id>` / `defend_flag` labels."""
    found = []
    for label in options:
        if label.startswith(("tag:", "tag_")) or label == "defend_flag":
            if not (label in routes and routes[label].get("blocked_now")):
                found.append(label)
    return found


def _threat_rank(label: str, routes: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Fallback order: adjacent tag, then shortest tag, then defend_flag."""
    if label == "defend_flag":
        return 2, 0
    length = routes.get(label, {}).get("length")
    length = length if isinstance(length, int) else 0  # tag_<id> fallback options are adjacent
    return (0, 0) if length == 0 else (1, length)


def combine_answers(answers: dict[str, Any], options: dict[str, str], turn: dict[str, Any],
                    plan: Plan | None = None, *,
                    threat_threshold: float = THREAT_YES,
                    route_bad_threshold: float = ROUTE_BAD_YES) -> tuple[str | None, float, str]:
    """Merge the three decider answers into (label, confidence, reason) (pure).

    reason "threat": the threat noul fired and a tag/defend option is offered; confidence is the
    threat probability. reason "avoid blocked/risky": the choice picked a blocked or risky route,
    `route_bad` fired and a same-goal route that is not blocked and has no risks is offered;
    confidence is the `route_bad` probability. reason "choice": the choice's own pick with its
    own confidence. The decider's answers are combined; no strategy is hardcoded beyond this.
    """
    state = _state(turn)
    routes = {r["id"]: r for r in _routes(state)}
    action = answers.get("action") if isinstance(answers, dict) else None
    pick, pick_conf = parse_choice(action)
    threat = parse_noul(answers.get("threat") if isinstance(answers, dict) else None)
    route_bad = parse_noul(answers.get("route_bad") if isinstance(answers, dict) else None)

    if threat >= threat_threshold:
        candidates = _threat_candidates(options, routes)
        if candidates:
            probs = action.get("probabilities") if isinstance(action, dict) else None
            probs = probs if isinstance(probs, dict) else {}
            scored = [c for c in candidates if isinstance(probs.get(c), (int, float))]
            if scored:
                best = max(scored, key=lambda c: (probs[c], -_threat_rank(c, routes)[0]))
            else:
                best = min(candidates, key=lambda c: _threat_rank(c, routes))
            return best, threat, "threat"

    picked = routes.get(pick) if pick else None
    if picked is not None and _is_bad(picked) and route_bad >= route_bad_threshold:
        me = _my_position(state)
        safe = [r for r in routes.values()
                if r["id"] in options and r["id"] != pick and r.get("goal") == picked.get("goal")
                and not _is_bad(r)]
        if safe:
            best = min(safe, key=lambda r: (not route_continues_plan(plan, me, r),
                                            r["length"] if isinstance(r.get("length"), int) else 10**6,
                                            r["id"]))
            return best["id"], route_bad, "avoid blocked/risky"

    return pick, pick_conf, "choice"


def format_decision(tick: int, label: str | None, confidence: float, reason: str,
                    threat: float, route_bad: float) -> str:
    """Per-tick log line for a decider decision."""
    return (f"[tick {tick}] decider -> {label} ({confidence:.2f}) reason={reason} "
            f"threat={threat:.2f} route_bad={route_bad:.2f}")


def remaining_ms(deadline_remaining_ms: float | None, elapsed_ms: float) -> float:
    """Time left in the tick: the deadline remaining at receipt minus time elapsed since."""
    base = DEFAULT_DEADLINE_MS if deadline_remaining_ms is None else deadline_remaining_ms
    return base - elapsed_ms


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def turn_remaining_ms(turn: dict[str, Any], elapsed_ms: float) -> float:
    """`remaining_ms` for a turn: `deadline_remaining_ms`, else `deadline_ms`, else 5000."""
    base = _number(turn.get("deadline_remaining_ms"))
    if base is None:
        base = _number(turn.get("deadline_ms"))
    return remaining_ms(base, elapsed_ms)


def choose_path(chosen: str | None, confidence: float, threshold: float, options: dict[str, str],
                remaining: float, llm_budget_ms: float, margin_ms: float,
                decider_down: bool) -> Literal["decider", "budget", "llm", "wait"]:
    """How to act this tick (pure).

    decider: confident, offered choice. llm: not confident but there is time for the LLM.
    budget: no time for the LLM and the decider's choice is a valid option, so run it.
    wait: no time for the LLM and nothing valid to run.
    """
    valid = chosen is not None and chosen in options
    if not decider_down and valid and accept_choice(chosen, confidence, threshold):
        return "decider"
    if remaining >= llm_budget_ms + margin_ms:
        return "llm"
    if not decider_down and valid:
        return "budget"
    return "wait"


MIN_LLM_TIMEOUT_S = 0.05


def llm_timeout_s(remaining: float, margin_ms: float) -> float:
    """Seconds the LLM fallback may run: time left minus the safety margin, at least 50 ms (pure)."""
    return max(MIN_LLM_TIMEOUT_S, (remaining - margin_ms) / 1000)


def timeout_action(chosen: str | None, options: dict[str, str], decider_down: bool,
                   already_acted: bool) -> Literal["decider", "wait", "none"]:
    """What to send when the LLM fallback timed out (pure).

    none: the LLM already executed an action, never send a second one.
    decider: the decider's choice is a valid offered option. wait: nothing valid to run.
    """
    if already_acted:
        return "none"
    if not decider_down and chosen is not None and chosen in options:
        return "decider"
    return "wait"


class TickCancelHandler(InterventionHandler):
    """Blocks action tools once the tick's cancel event is set (the LLM timed out).

    An abandoned LLM thread may still try to act later; its action would be rejected as late
    or, worse, double the one we already sent. `executed_action` records that an action tool
    was allowed through before the cancel, so the timeout path can avoid a second action.
    """

    name = "tick-cancel"

    def __init__(self, cancel: threading.Event) -> None:
        self.cancel = cancel
        self.executed_action = False
        self._lock = threading.Lock()

    def before_tool_call(self, event: Any, **kwargs: Any) -> Proceed | Deny:
        with self._lock:  # check-and-record is atomic with respect to the timeout path
            if self.cancel.is_set():
                return Deny(reason="tick timed out")
            if (getattr(event, "tool_use", None) or {}).get("name") in ACTION_TOOLS:
                self.executed_action = True
            return Proceed()

    def cancel_tick(self) -> bool:
        """Set the cancel event; True when the LLM had already been allowed to act."""
        with self._lock:
            self.cancel.set()
            return self.executed_action


class StopAfterActionHook:
    """Ends the Agent's turn right after an action tool completes (no second model call).

    VERIFIED (strands 1.59): `AfterToolsEvent.end_turn` (hooks/events.py) makes the event loop halt
    after the tool batch without calling the model again (event_loop/event_loop.py, "Hook requested
    halt"). Interventions cannot do this (`after_tool_call` only allows Proceed/Transform), and the
    `request_state["stop_event_loop"]` flag is only reachable from inside a tool. `AfterToolsEvent`
    carries tool results without names, so `AfterToolCallEvent` records that an action tool ran.
    A cancelled call (denied by TickCancelHandler) does not count as an action.
    """

    def __init__(self) -> None:
        self.acted = False

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    def after_tool_call(self, event: Any) -> None:
        if (getattr(event, "tool_use", None) or {}).get("name") in ACTION_TOOLS and not getattr(event, "cancel_message", None):
            self.acted = True

    def after_tools(self, event: Any) -> None:
        if self.acted:
            event.end_turn = "action sent"


def run_with_timeout(fn, timeout_s: float) -> tuple[bool, Any]:
    """Run `fn` in a daemon thread and wait at most `timeout_s`. Returns (timed_out, result).

    On timeout the thread is abandoned (it cannot be killed) and (True, None) is returned.
    An exception raised by `fn` is re-raised in the caller. A fresh thread per call means an
    abandoned run never blocks the next tick.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True, name="llm-tick")
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return True, None
    if "error" in box:
        raise box["error"]
    return False, box.get("value")


def warmup_question() -> tuple[str, dict[str, dict[str, Any]]]:
    """A representative (state, questions) pair for the decider warm-up (pure, no server)."""
    def route(rid, goal, step, length, risks=(), blocked=False, threat=None):
        return {"id": rid, "goal": goal, "first_step": step, "length": length, "path": [],
                "blocked_now": blocked, "blocked_reason": "contention" if blocked else None,
                "risks": [{"step": st, "enemy_id": en} for st, en in risks],
                "closest_threat": (None if threat is None
                                   else {"enemy_id": threat[0], "step": threat[1], "distance": threat[2]})}

    def agent(aid, team, x, y):
        return {"id": aid, "team": team, "position": {"x": x, "y": y}, "carrying": None, "disconnected": False}

    turn = {
        "status": "turn", "tick": 12,
        "state": {
            "tick": 12,
            "agents": [agent("blue-1", "blue", 9, 7), agent("blue-2", "blue", 11, 4),
                       agent("red-1", "red", 5, 6), agent("red-2", "red", 4, 9), agent("red-3", "red", 2, 7)],
            "flags": [{"team": "red", "position": {"x": 1, "y": 7}, "carried_by": None},
                      {"team": "blue", "position": {"x": 13, "y": 7}, "carried_by": None}],
            "score": {"red": 1, "blue": 0},
            "self": "blue-1", "self_position": {"x": 9, "y": 7},
            "teammates": [{"id": "blue-2", "position": {"x": 11, "y": 4}}],
            "routes": [route("enemy_flag_direct", "enemy_flag", "left", 8, [(3, "red-1")], threat=("red-1", 6, 2)),
                       route("enemy_flag_detour", "enemy_flag", "up", 11, threat=("red-2", 4, 4)),
                       route("tag:red-1", "tag", "left", 4),
                       route("home_base", "own_base", "right", 4, blocked=True)],
        },
        "events": [{"type": "moved", "agent_id": "red-1", "from": {"x": 6, "y": 6}, "to": {"x": 5, "y": 6}}],
        "last_action": {"action": "move:left", "tick": 11, "result": "moved"},
    }
    plan = Plan("enemy_flag_direct", "enemy_flag", 9, "move left")
    map_info = parse_map({"fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14},
                                     "neutral_x": 7},
                          "walls": [], "flag_bases": {"red": {"x": 1, "y": 7}, "blue": {"x": 13, "y": 7}}})
    return render_state(turn, plan, map_info), build_questions(build_options(turn, plan, map_info))


def accept_choice(label: str | None, confidence: float, threshold: float) -> bool:
    return label is not None and confidence >= threshold


def sanitize_name(name: str) -> str:
    """join_match requires [a-z0-9_-]{1,32}."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return (cleaned or "refined-decider")[:32]


def _describe_event(event: Any) -> str:
    if not isinstance(event, dict):
        return str(event)
    etype = str(event.get("type", "event"))
    if etype == "move_blocked":
        parts = [etype, str(event.get("agent_id", "?")), str(event.get("direction", "?"))]
        target = _xy(event.get("target"))
        text = " ".join(parts) + (f" -> {target}" if target else "")
        return f"{text}: {event.get('reason', 'unknown')}"
    parts = [etype]
    if "agent_id" in event:
        parts.append(str(event["agent_id"]))
    src, dst = _xy(event.get("from")), _xy(event.get("to"))
    if src and dst:
        parts.append(f"{src}->{dst}")
    for key, value in event.items():
        if key in ("type", "agent_id", "from", "to"):
            continue
        if _xy(value):
            parts.append(f"{key}={_xy(value)}")
        elif isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={value}")
    return " ".join(parts)

def _plan_age(turn: dict[str, Any], plan: Plan) -> int:
    """Ticks elapsed since the plan started (0 when unknown)."""
    if plan.since_tick is None:
        return 0
    return max(0, extract_tick(turn) - plan.since_tick)


def _plan_active(plan: Plan | None) -> bool:
    return plan is not None and bool(plan.route_id or plan.goal)


def _plan_line(turn: dict[str, Any], plan: Plan) -> str:
    target = f", heading to {plan.path[-1]}" if plan.path else ""
    head = f"current plan: goal {plan.goal}{target} since tick {plan.since_tick}"
    me = _my_position(_state(turn))
    if not any(route_continues_plan(plan, me, r) for r in _routes(_state(turn))):
        return f"{head} \u2014 no offered route continues that path"
    line = f"{head}, {_plural(_plan_age(turn, plan), 'tick')}"
    return f"{line}; last action: {plan.last_action}" if plan.last_action else line


def describe_last_action(last_action: Any) -> str | None:
    """The server's `last_action` ({action, tick, result, reason?}) as one line, None when absent."""
    if not isinstance(last_action, dict) or not isinstance(last_action.get("action"), str):
        return None
    result = str(last_action.get("result", "unknown"))
    reason = last_action.get("reason")
    outcome = f"{result} ({reason})" if reason else result
    return f"outcome of your previous action: {last_action['action']} at tick {last_action.get('tick', '?')} -> {outcome}"


def render_state(
    turn: dict[str, Any],
    plan: Plan | None = None,
    map_info: MapInfo | None = None,
    include_last_action: bool = False,
) -> str:
    """Short, human-readable summary of a wait_for_turn result for the decider and the LLM fallback.

    Where we stand relative to the fields needs `map_info` (the turn no longer carries `fields`); without it
    the position is shown plainly rather than guessed.

    The server's `last_action` is rendered only with `include_last_action` (the LLM fallback): the decider is a
    trained model and its input text must not change until that wording has been evaluated.
    """
    state = _state(turn)
    me = _my_position(state)
    lines = [f"tick: {extract_tick(turn)}"]

    if me is not None:
        where = ""
        if map_info is not None:
            if _in_my_field(state, map_info):
                where = ", in your own field"
            elif me[0] == map_info.neutral_x:
                where = ", on the neutral line"
            else:
                where = ", in the enemy field"
        lines.append(f"you: {state.get('self')} ({_my_team(state)}) at {me}{where}")
    lines.append(f"carrying the flag: {'yes' if _carrying(state) else 'no'}")
    last_action = describe_last_action(turn.get("last_action")) if include_last_action else None
    if last_action:
        lines.append(last_action)
    if _plan_active(plan):
        lines.append(_plan_line(turn, plan))

    score = state.get("score")
    if isinstance(score, dict):
        lines.append("score: " + ", ".join(f"{k} {v}" for k, v in score.items()))

    for enemy in _enemies(state):
        pos = _xy(enemy.get("position"))
        lines.append(f"enemy {enemy.get('id')} at {pos}, carrying: {'yes' if enemy.get('carrying') is not None else 'no'}")

    mates = [t for t in state.get("teammates") or [] if t]
    if mates:
        lines.append(f"teammates: {json.dumps(mates)}")

    routes = _routes(state)
    if routes:
        lines.append("routes:")
        lines.extend(f"  {route['id']}: {describe_route(route)}" for route in routes)
    else:
        moves = state.get("moves") if isinstance(state.get("moves"), dict) else {}
        if moves:
            lines.append("moves: " + ", ".join(f"{d}: {moves.get(d, 'unknown')}" for d in DIRECTIONS))

    events = [_describe_event(e) for e in turn.get("events") or []]
    if events:
        lines.append("recent events: " + "; ".join(events))
    return "\n".join(lines)

def ask_serialized(decider: Decider, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Ask the decider holding a cross-process file lock.

    The server runs inference in a thread pool with no lock, so concurrent requests from
    the 3 team agents crash it (MPS). One request at a time across all processes.
    """
    with open(DECIDER_LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return decider.ask(state, questions)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def describe_match_over(turn: dict[str, Any]) -> str:
    """One log line for a `match_over` result. `winner` is null when the match was stopped early."""
    reason = turn.get("reason") if isinstance(turn.get("reason"), str) else "unknown"
    winner = turn.get("winner")
    verdict = f"winner {winner}" if isinstance(winner, str) and winner else "no winner"
    score = turn.get("score")
    tail = ", score " + ", ".join(f"{k} {v}" for k, v in score.items()) if isinstance(score, dict) else ""
    return f"match over ({reason}): {verdict}{tail}"


def action_rejection(outcome: dict[str, Any]) -> str | None:
    """The reason of a rejected action ack (`status: rejected`), `unknown` if it has none; None otherwise.

    Rejections are normal results, not MCP errors. New in v2: `match_over`, `match_not_started`, `not_joined`.
    """
    if outcome.get("status") != "rejected":
        return None
    reason = outcome.get("reason")
    return reason if isinstance(reason, str) and reason else "unknown"


def report_outcome(outcome: dict[str, Any]) -> None:
    """Print a tool error or a rejected action ack (a normal result with a reason, not an MCP error)."""
    if outcome["_is_error"]:
        print(f"    action error: {outcome}")
    elif action_rejection(outcome):
        print(f"    action rejected: {action_rejection(outcome)}")


class Stats:
    def __init__(self) -> None:
        self.decider_ticks = 0
        self.llm_ticks = 0
        self.budget_ticks = 0
        self.llm_timeouts = 0
        self.latencies_ms: list[float] = []

    def summary(self) -> str:
        avg = f"{sum(self.latencies_ms) / len(self.latencies_ms):.0f} ms" if self.latencies_ms else "n/a"
        return (
            f"ticks decided by decider: {self.decider_ticks}, by LLM fallback: {self.llm_ticks}, "
            f"by budget: {self.budget_ticks}, LLM timeouts: {self.llm_timeouts}, "
            f"decider avg latency: {avg}"
        )


def call_tool(client: MCPClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = client.call_tool_sync(f"{name}-{uuid.uuid4().hex[:8]}", name, arguments)
    parsed = parse_tool_result(result)
    # Tool errors set isError; transport errors (e.g. "session not found") only set status="error".
    parsed["_is_error"] = result.get("status") == "error" or bool(result.get("isError"))
    return parsed


def join(client: MCPClient, match: str, name: str) -> tuple[str, MapInfo | None]:
    """Join as red first, then blue. Returns the team joined and the static map (None if absent)."""
    for team in ("red", "blue"):
        outcome = call_tool(client, TOOL_JOIN, {"match_id": match, "team": team, "name": name})
        if not outcome["_is_error"]:
            print(f"joined match {match} as {name} on team {team}")
            return team, parse_map(outcome.get("map"))
        print(f"join as {team} failed: {outcome}")
    raise RuntimeError(f"could not join match {match} on either team")


def fetch_map(client: MCPClient) -> MapInfo | None:
    """The static map from `get_map`: the fallback when `join_match` did not carry it."""
    outcome = call_tool(client, TOOL_GET_MAP, {})
    if outcome["_is_error"]:
        print(f"get_map failed: {outcome}")
        return None
    return parse_map(outcome)


def run_llm_tick(agent_factory, state: str, tick: int,
                 handler: InterventionHandler | None = None) -> tuple[int, tuple[str, dict[str, Any]] | None]:
    """Run the fallback Agent for one tick (a fresh agent each time, guarded by `handler`).

    Returns (how many action tools it called, the (tool name, input) it executed or None).
    """
    prompt = (
        f"{state}\n\nThis is tick {tick}. Pick ONE of the listed routes or actions and call "
        f"exactly one action tool ({', '.join(ACTION_TOOLS)}) with tick={tick}: move with the "
        "route's first step, tag with the enemy id, grab_flag, or wait.\n" + LLM_PLAN_NOTE
    )
    agent = agent_factory(handler)
    result = agent(prompt)
    tool_metrics = getattr(getattr(result, "metrics", None), "tool_metrics", {}) or {}
    calls = sum(getattr(m, "call_count", 0) for m in tool_metrics.values())
    return calls, extract_action_call(getattr(agent, "messages", None))


def warm_up(decider: Decider) -> bool:
    """Send one throwaway question through the shared lock. False if the decider is unreachable."""
    state, questions = warmup_question()
    started = time.monotonic()
    try:
        ask_serialized(decider, state, questions)
    except (ServerUnavailable, OSError, http.client.HTTPException) as exc:
        print(f"[decider] warm-up failed: {exc}\n    -> LLM fallback for the rest of the match")
        return False
    print(f"decider warm-up: {(time.monotonic() - started) * 1000:.0f} ms (server {decider.last_latency_ms} ms)")
    return True


def build_agent_factory(model: str, action_tools: list[Any], system_prompt: str):
    """Return `handler -> Agent`, the per-tick LLM agent restricted to the action tools."""
    return lambda handler: Agent(
        model=model,
        tools=action_tools,
        system_prompt=system_prompt,
        callback_handler=None,
        interventions=[handler] if handler is not None else [],
        hooks=[StopAfterActionHook()],  # end the turn after the action: no wasted 2nd model call
    )


def play(client: MCPClient, decider: Decider | None, args: argparse.Namespace,
         system_prompt: str = SYSTEM_PROMPT) -> Stats:
    """Run the match loop. With `args.llm_only` the decider is never used (it may be None)."""
    llm_only = bool(getattr(args, "llm_only", False))
    stats = Stats()
    decider_down = False
    errors = 0
    plan = Plan()

    action_tools = [t for t in client.list_tools_sync() if t.tool_name in ACTION_TOOLS]
    agent_factory = build_agent_factory(MODELS[args.model], action_tools, system_prompt)

    if llm_only:
        print("llm-only mode: decider disabled (no warm-up, no per-tick decider request)")
    elif not args.no_warmup and not warm_up(decider):
        decider_down = True

    _, map_info = join(client, args.match, sanitize_name(args.name))
    if map_info is None:  # an older server: fetch the static map once instead
        map_info = fetch_map(client)
    if map_info is None:
        print("warning: no static map (join_match.map / get_map): field-dependent hints are off")

    while True:
        turn = call_tool(client, TOOL_WAIT_FOR_TURN, {})
        status = turn.get("status")
        if status == "match_over":
            print(describe_match_over(turn))
            return stats
        if status != "turn":  # 'pending' (nothing arrived within the wait cap) or an error
            if turn["_is_error"]:
                errors += 1
                if errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                    raise RuntimeError(f"wait_for_turn kept failing: {turn}")
            continue
        errors = 0

        tick = extract_tick(turn)
        turn_received = time.monotonic()
        state = render_state(turn, plan, map_info)
        options = build_options(turn, plan, map_info)
        if args.raw_turns:
            print(f"[tick {tick}] raw turn:\n{json.dumps(turn, indent=2, ensure_ascii=False)}")
            print(f"[tick {tick}] state sent to decider:\n{state}")
            print(f"[tick {tick}] options: {sorted(options)}")

        chosen: str | None = None
        confidence = 0.0
        reason, threat_p, route_bad_p = "choice", 0.0, 0.0
        if not decider_down and not llm_only:
            questions = build_questions(options)  # ONE request, three questions
            started = time.monotonic()
            try:
                answers = ask_serialized(decider, state, questions)
            except (ServerUnavailable, OSError, http.client.HTTPException) as exc:
                decider_down = True
                print(f"[decider] {exc}\n    -> LLM fallback for the rest of the match")
            else:
                chosen, confidence, reason = combine_answers(answers, options, turn, plan,
                                                             threat_threshold=args.threat_threshold)
                threat_p, route_bad_p = parse_noul(answers.get("threat")), parse_noul(answers.get("route_bad"))
                if decider.last_latency_ms is not None:
                    stats.latencies_ms.append(float(decider.last_latency_ms))
                print(f"[tick {tick}] timing: decider wall {(time.monotonic() - started) * 1000:.0f} ms "
                      f"(server {decider.last_latency_ms} ms), deadline_remaining at receipt "
                      f"{turn.get('deadline_remaining_ms')} ms")

        remaining = turn_remaining_ms(turn, (time.monotonic() - turn_received) * 1000)
        if llm_only:
            path = "llm"  # no decider choice to fall back on: the LLM always runs with the time left
        else:
            path = choose_path(chosen, confidence, args.threshold, options, remaining,
                               args.llm_budget_ms, args.safety_margin_ms, decider_down)

        if path in ("decider", "budget"):
            try:
                tool, arguments = label_to_call(chosen or "", tick, turn)
            except ValueError as exc:
                # Never crash mid-match on an option we cannot map: a wait beats a dead agent.
                print(f"[tick {tick}] {exc} -> wait")
                tool, arguments = "wait", {"tick": tick}
            outcome = call_tool(client, tool, arguments)
            route_id = chosen if route_goal(turn, chosen) is not None else None
            plan = update_plan(plan, route_id, route_goal(turn, route_id), describe_action(tool, arguments), tick,
                               route_path(turn, route_id), _my_position(_state(turn)))
            if path == "decider":
                stats.decider_ticks += 1
                print(format_decision(tick, chosen, confidence, reason, threat_p, route_bad_p) + plan_suffix(plan))
            else:
                stats.budget_ticks += 1
                print(f"[tick {tick}] budget: {remaining:.0f} ms left < "
                      f"{args.llm_budget_ms + args.safety_margin_ms:.0f} ms needed for LLM -> "
                      f"executing decider choice {chosen} ({confidence:.2f}){plan_suffix(plan)}")
            report_outcome(outcome)
            continue

        if path == "wait":
            outcome = call_tool(client, "wait", {"tick": tick})
            stats.budget_ticks += 1
            plan = update_plan(plan, None, None, "wait", tick)
            why = "decider unavailable" if decider_down else f"no valid decider choice ({chosen}, {confidence:.2f})"
            print(f"[tick {tick}] budget: {remaining:.0f} ms left < "
                  f"{args.llm_budget_ms + args.safety_margin_ms:.0f} ms needed for LLM, {why} -> wait")
            report_outcome(outcome)
            continue

        if llm_only:
            reason = "llm-only mode"
        else:
            reason = "decider unavailable" if decider_down else f"low confidence ({chosen}, {confidence:.2f})"
        print(f"[tick {tick}] LLM fallback: {reason}")
        stats.llm_ticks += 1
        llm_started = time.monotonic()
        limit_ms = llm_timeout_s(turn_remaining_ms(turn, (llm_started - turn_received) * 1000),
                                 args.safety_margin_ms) * 1000
        llm_state = render_state(turn, plan, map_info, include_last_action=True)  # fallback-only text
        handler = TickCancelHandler(threading.Event())
        timed_out, llm_result = run_with_timeout(
            lambda: run_llm_tick(agent_factory, llm_state, tick, handler), limit_ms / 1000)
        if timed_out:
            already_acted = handler.cancel_tick()  # BEFORE any fallback action
            stats.llm_ticks -= 1
            stats.llm_timeouts += 1
            waited = f"[tick {tick}] LLM timeout after {(time.monotonic() - llm_started) * 1000:.0f} ms (limit {limit_ms:.0f} ms)"
            # llm-only: no decider choice exists, so treat the decider as down (-> wait on timeout)
            action = timeout_action(chosen, options, decider_down or llm_only, already_acted)
            if action == "none":
                print(f"{waited} -> LLM already executed an action, not sending another")
                continue
            if action == "decider":
                try:
                    tool, arguments = label_to_call(chosen or "", tick, turn)
                except ValueError as exc:
                    print(f"[tick {tick}] {exc} -> wait")
                    action, tool, arguments = "wait", "wait", {"tick": tick}
            else:
                tool, arguments = "wait", {"tick": tick}
            outcome = call_tool(client, tool, arguments)
            route_id = chosen if action == "decider" and route_goal(turn, chosen) is not None else None
            plan = update_plan(plan, route_id, route_goal(turn, route_id), describe_action(tool, arguments), tick,
                               route_path(turn, route_id), _my_position(_state(turn)))
            verdict = f"executing decider choice {chosen} ({confidence:.2f})" if action == "decider" else "wait"
            print(f"{waited} -> {verdict}{plan_suffix(plan)}")
            report_outcome(outcome)
            continue
        calls, call = llm_result
        print(f"[tick {tick}] timing: LLM wall {(time.monotonic() - llm_started) * 1000:.0f} ms, "
              f"total since turn {(time.monotonic() - turn_received) * 1000:.0f} ms")
        if calls == 0:
            print("    warning: the LLM made no action call; the server will count this tick as wait")
        if call is not None:
            route_id = infer_executed_route(call, turn, plan)
            plan = update_plan(plan, route_id, route_goal(turn, route_id), describe_action(*call), tick,
                               route_path(turn, route_id), _my_position(_state(turn)))
            print(f"    LLM executed {describe_action(*call)}{plan_suffix(plan)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


DEFAULT_NAME = "refined-decider"


def parse_args(default_name: str = DEFAULT_NAME) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refined AmazingPlayer CTF agent with a code-owned loop and strands-decider.")
    parser.add_argument("--model", choices=sorted(MODELS.keys()), default="haiku",
                        help="Model used for the LLM fallback (default: haiku).")
    parser.add_argument("--match", default="llm-1", help="Match id to join (default: llm-1).")
    parser.add_argument("--name", default=default_name,
                        help=f"Agent name used to join the match (default: {default_name}; lowercased to fit [a-z0-9_-]).")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Minimum decider confidence to act without the LLM (default: {DEFAULT_THRESHOLD}).")
    parser.add_argument("--threat-threshold", type=float, default=THREAT_YES,
                        help=f"Minimum `threat` probability to prefer a tag/defend option (default: {THREAT_YES}).")
    parser.add_argument("--raw-turns", action="store_true",
                        help="Print each raw wait_for_turn payload, the state sent to the decider and its options.")
    parser.add_argument("--llm-budget-ms", type=float, default=DEFAULT_LLM_BUDGET_MS,
                        help=f"Expected LLM fallback time; skipped when less time is left (default: {DEFAULT_LLM_BUDGET_MS}).")
    parser.add_argument("--safety-margin-ms", type=float, default=DEFAULT_SAFETY_MARGIN_MS,
                        help=f"Extra margin added to the LLM budget (default: {DEFAULT_SAFETY_MARGIN_MS}).")
    parser.add_argument("--llm-only", action="store_true",
                        help="Ablation: the LLM fallback agent decides EVERY tick and the decider is never "
                             "consulted (no server needed). The LLM always runs with the time left, no budget "
                             "skip; on timeout the tick is a wait. combine_answers is not used.")
    parser.add_argument("--no-warmup", action="store_true", help="Skip the decider warm-up before joining the match.")
    return parser.parse_args()


def lingering_threads(threads: Any) -> list[Any]:
    """Alive, non-daemon threads other than the main thread: what blocks interpreter exit."""
    main = threading.main_thread()
    return [t for t in threads if t is not main and t.is_alive() and not t.daemon]


def describe_threads(threads: Any) -> str:
    return ", ".join(f"{t.name} (daemon={t.daemon})" for t in threads)


def exit_if_threads_linger(code: int, grace_s: float = EXIT_GRACE_S, *, exit_fn=os._exit,
                           enumerate_fn=threading.enumerate, sleep=time.sleep,
                           clock=time.monotonic, log=None) -> bool:
    """Safety net: if non-daemon threads outlive `grace_s`, log them and `exit_fn(code)` (os._exit).

    Python would otherwise wait forever for them at interpreter exit. Returns True when it forced
    the exit (only observable with a fake `exit_fn`).
    """
    log = log or (lambda message: print(message, file=sys.stderr))
    deadline = clock() + grace_s
    while True:
        remaining = lingering_threads(enumerate_fn())
        if not remaining:
            return False
        if clock() >= deadline:
            break
        sleep(0.05)
    log(f"[exit] non-daemon threads still alive after {grace_s:.1f}s grace, forcing exit {code}: "
        f"{describe_threads(remaining)}; all threads: {describe_threads(enumerate_fn())}")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - never block the forced exit
            pass
    exit_fn(code)
    return True


def run_guarded(fn) -> int:
    """Run `fn` and turn its outcome into an exit code, printing the traceback on failure."""
    try:
        return fn()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BaseException:  # noqa: BLE001 - reported, not swallowed: non-zero exit
        traceback.print_exc()
        return 1


def with_client(client: Any, body) -> Any:
    """Start the MCP client, run `body`, and ALWAYS stop it (even if start fails or on Ctrl-C)."""
    try:
        client.start()
        return body()
    finally:
        try:
            client.stop(None, None, None)
        except Exception as exc:  # noqa: BLE001 - a failing stop must not mask the real error
            print(f"[exit] MCP client stop failed: {exc}", file=sys.stderr)


def main(system_prompt: str = SYSTEM_PROMPT, default_name: str = DEFAULT_NAME) -> int:
    args = parse_args(default_name)

    decider: Decider | None = None
    decider_down_at_start = False
    if not args.llm_only:  # llm-only never touches the decider server
        decider = Decider()
        try:
            print(decider.banner(), "\n")
        except ServerUnavailable as exc:
            print(f"{exc}\n    -> starting in LLM-fallback mode")
            decider_down_at_start = True

    client = MCPClient.load_servers(MCP_SERVERS)[0]
    if decider_down_at_start:
        # Point the decider at nothing useful so the first ask fails open immediately.
        decider.timeout = 1.0
    stats = with_client(client, lambda: play(client, decider, args, system_prompt=system_prompt))

    print(f"\n{stats.summary()}; {decider if decider is not None else 'decider disabled (llm-only)'}")
    return 0


if __name__ == "__main__":
    exit_code = run_guarded(main)
    sys.stdout.flush()
    sys.stderr.flush()
    exit_if_threads_linger(exit_code)
    sys.exit(exit_code)
