"""CTF agent where plain Python owns the game loop and a local strands-decider picks the action.

Unlike `agent-strand-decider.py` (an LLM agent loop whose tool calls the decider *reviews*),
here the code drives everything:

    wait_for_turn -> render state -> ONE decider `choice` question -> call the tool directly

Only when the decider is not confident enough (or is unreachable) does a strands `Agent`
run, once for that tick, restricted to the action tools. No LLM is involved on the fast path.

    uv run strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8099
    uv run python agent/agent-decider-loop.py --match llm-1 --threshold 0.85

STRANDS_DECIDER_URL overrides the decider endpoint.

Verified against the live servers (see comments marked VERIFIED / ASSUMPTION):

- VERIFIED  MCP tool names (raw, no prefix, because we call the MCPClient directly):
            get_rules, join_match, wait_for_turn, move, tag, grab_flag, wait.
- VERIFIED  A decider `choice` answer is
            {"type": "choice", "choice": <label>, "probabilities": {...}, "confidence": <float>}.
            Note `confidence` is NOT the top probability (0.31 next to 0.48 was observed), so the
            threshold applies to `confidence`.
- VERIFIED  `wait_for_turn` payload (from a real match): top-level `status`, `tick`, `events`, and
            the game state under `state` (grid, walls, agents, flags, score, fields, self,
            self_position, moves, teammates). `state.agents[].carrying` is non-null while an agent
            holds a flag; we only treat it as a boolean.
- VERIFIED  `state.routes` (from get_rules): advisory route options computed by the server, each
            with id, goal, first_step, length, path, blocked_now, blocked_reason, risks and
            min_enemy_distance. The server never recommends a route and neither does this client:
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
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

from _client import Decider, ServerUnavailable
from strands import Agent
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
TOOL_WAIT_FOR_TURN = "wait_for_turn"
ACTION_TOOLS = ("move", "tag", "grab_flag", "wait")

DIRECTIONS = ("up", "down", "left", "right")

DEFAULT_THRESHOLD = 0.85
MAX_CONSECUTIVE_TOOL_ERRORS = 5
DEFAULT_DEADLINE_MS = 5000
DEFAULT_LLM_BUDGET_MS = 4500
DEFAULT_SAFETY_MARGIN_MS = 300

DECIDER_LOCK_FILE = os.path.join(tempfile.gettempdir(), "strands-decider.lock")

DECIDER_INSTRUCTIONS = (
    "You are a player in a Capture the Flag game. Choose the single best action for this "
    "tick. Reach the enemy flag, grab it, and carry it back to your own base. The options "
    "are routes offered by the server: choose between them by weighing length, whether "
    "they are blocked right now, and the risk of being tagged along the way. Tag enemies "
    "inside your own field when it helps, and defend your flag when it is threatened. "
    "Your current plan, if any, is shown; switching routes every tick can undo your own "
    "progress. Keep or change it based on the current situation."
)

LLM_PLAN_NOTE = (
    "Your current plan, if any, is shown; switching routes every tick can undo your own "
    "progress. Keep or change it based on the current situation. When you call move or tag "
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

def _in_my_field(state: dict[str, Any]) -> bool:
    me = _my_position(state)
    field = (state.get("fields") or {}).get(_my_team(state) or "")
    if me is None or not isinstance(field, dict):
        return False
    return field.get("min_x", 1) <= me[0] <= field.get("max_x", 0)

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


@dataclass
class Plan:
    """Loop-owned memory of the route the agent last acted on. Informational only: the
    model still decides every tick and may keep or change it."""

    route_id: str | None = None
    goal: str | None = None
    since_tick: int | None = None
    last_action: str | None = None


def update_plan(plan: Plan, executed_route_id: str | None, executed_goal: str | None,
                action_text: str, tick: int) -> Plan:
    """New plan after an executed action (pure).

    A route action keeps `since_tick` while the goal is unchanged (the route id may change
    within a goal) and restarts it on a new goal. `grab_flag` clears the plan; any other
    non-route action (e.g. `wait`) only records `last_action`.
    """
    if executed_route_id is None:
        if action_text.split(" ", 1)[0] == "grab_flag":
            return Plan(last_action=action_text)
        return replace(plan, last_action=action_text)
    same_goal = plan.route_id is not None and plan.goal == executed_goal and plan.since_tick is not None
    since = plan.since_tick if same_goal else tick
    return Plan(executed_route_id, executed_goal, since, action_text)


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
    distance = route.get("min_enemy_distance")
    if isinstance(distance, int) and not isinstance(distance, bool):
        parts.append(f"nearest enemy {distance}")
    return ", ".join(parts)


def build_options(turn: dict[str, Any], plan: Plan | None = None) -> dict[str, str]:
    """Options for the decider `choice` question, built from the state. Always includes `wait`.

    One option per server route (label = route id). Without routes, falls back to one
    `move_<dir>` option per open direction (and adjacent tags). No option is ever recommended.
    With a `plan`, the matching route and other routes with the same goal get a neutral note.
    """
    state = _state(turn)
    me, team = _my_position(state), _my_team(state)

    options: dict[str, str] = {}
    routes = _routes(state)
    for route in routes:
        text = describe_route(route)
        if plan is not None and plan.route_id:
            if route["id"] == plan.route_id:
                text += f" | continues your current plan ({_plural(_plan_age(turn, plan), 'tick')})"
            elif plan.goal is not None and route.get("goal") == plan.goal:
                text += " | same goal as your current plan, different route"
        options[route["id"]] = text
    if not routes:
        for direction in _open_directions(state):
            options[f"move_{direction}"] = f"move one cell {direction}"

    enemy_flag = _flag(state, _enemy_team(team))
    if (me is not None and enemy_flag is not None and enemy_flag.get("carried_by") is None
            and _xy(enemy_flag.get("position")) == me):
        options["grab_flag"] = "pick up the enemy flag you are standing on"

    if not routes and me is not None and _in_my_field(state):
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
        enemy_id = _route_enemy_id(route)
        if enemy_id and route.get("length") == 0:
            return "tag", {"enemy_id": enemy_id, "tick": tick, "rationale": rationale}
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


def warmup_question() -> tuple[str, dict[str, dict[str, Any]]]:
    """A representative (state, questions) pair for the decider warm-up (pure, no server)."""
    def route(rid, goal, step, length, risks=(), blocked=False, dist=3):
        return {"id": rid, "goal": goal, "first_step": step, "length": length, "path": [],
                "blocked_now": blocked, "blocked_reason": "contention" if blocked else None,
                "risks": [{"step": st, "enemy_id": en} for st, en in risks], "min_enemy_distance": dist}

    def agent(aid, team, x, y):
        return {"id": aid, "team": team, "position": {"x": x, "y": y}, "carrying": None, "disconnected": False}

    turn = {
        "status": "turn", "tick": 12,
        "state": {
            "tick": 12,
            "agents": [agent("blue-1", "blue", 9, 7), agent("blue-2", "blue", 11, 4),
                       agent("red-1", "red", 5, 6), agent("red-2", "red", 4, 9), agent("red-3", "red", 2, 7)],
            "flags": [{"team": "red", "base": {"x": 1, "y": 7}, "position": {"x": 1, "y": 7}, "carried_by": None},
                      {"team": "blue", "base": {"x": 13, "y": 7}, "position": {"x": 13, "y": 7}, "carried_by": None}],
            "score": {"red": 1, "blue": 0},
            "fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7},
            "self": "blue-1", "self_position": {"x": 9, "y": 7},
            "teammates": [{"id": "blue-2", "position": {"x": 11, "y": 4}}],
            "routes": [route("enemy_flag_direct", "enemy_flag", "left", 8, [(3, "red-1")], dist=2),
                       route("enemy_flag_detour", "enemy_flag", "up", 11, dist=4),
                       route("tag:red-1", "tag", "left", 4, dist=4),
                       route("home_base", "own_base", "right", 4, blocked=True)],
        },
        "events": [{"type": "moved", "agent_id": "red-1", "from": {"x": 6, "y": 6}, "to": {"x": 5, "y": 6}}],
    }
    plan = Plan("enemy_flag_direct", "enemy_flag", 9, "move left")
    return render_state(turn, plan), {"action": Decider.choice(DECIDER_INSTRUCTIONS, build_options(turn, plan))}


def accept_choice(label: str | None, confidence: float, threshold: float) -> bool:
    return label is not None and confidence >= threshold


def sanitize_name(name: str) -> str:
    """join_match requires [a-z0-9_-]{1,32}."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return (cleaned or "amazingplayer")[:32]


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


def _plan_line(turn: dict[str, Any], plan: Plan) -> str:
    head = f"current plan: {plan.route_id} (goal {plan.goal}) since tick {plan.since_tick}"
    if plan.route_id not in {r["id"] for r in _routes(_state(turn))}:
        return f"{head} \u2014 that route is no longer offered"
    line = f"{head}, {_plural(_plan_age(turn, plan), 'tick')}"
    return f"{line}; last action: {plan.last_action}" if plan.last_action else line


def render_state(turn: dict[str, Any], plan: Plan | None = None) -> str:
    """Short, human-readable summary of a wait_for_turn result for the decider and the LLM fallback."""
    state = _state(turn)
    me = _my_position(state)
    lines = [f"tick: {extract_tick(turn)}"]

    if me is not None:
        if _in_my_field(state):
            where = "in your own field"
        elif me[0] == (state.get("fields") or {}).get("neutral_x"):
            where = "on the neutral line"
        else:
            where = "in the enemy field"
        lines.append(f"you: {state.get('self')} ({_my_team(state)}) at {me}, {where}")
    lines.append(f"carrying the flag: {'yes' if _carrying(state) else 'no'}")
    if plan is not None and plan.route_id:
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


class Stats:
    def __init__(self) -> None:
        self.decider_ticks = 0
        self.llm_ticks = 0
        self.budget_ticks = 0
        self.latencies_ms: list[float] = []

    def summary(self) -> str:
        avg = f"{sum(self.latencies_ms) / len(self.latencies_ms):.0f} ms" if self.latencies_ms else "n/a"
        return (
            f"ticks decided by decider: {self.decider_ticks}, by LLM fallback: {self.llm_ticks}, "
            f"by budget: {self.budget_ticks}, "
            f"decider avg latency: {avg}"
        )


def call_tool(client: MCPClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = client.call_tool_sync(f"{name}-{uuid.uuid4().hex[:8]}", name, arguments)
    parsed = parse_tool_result(result)
    # Tool errors set isError; transport errors (e.g. "session not found") only set status="error".
    parsed["_is_error"] = result.get("status") == "error" or bool(result.get("isError"))
    return parsed


def join(client: MCPClient, match: str, name: str) -> str:
    """Join as red first, then blue. Returns the team joined."""
    for team in ("red", "blue"):
        outcome = call_tool(client, TOOL_JOIN, {"match_id": match, "team": team, "name": name})
        if not outcome["_is_error"]:
            print(f"joined match {match} as {name} on team {team}")
            return team
        print(f"join as {team} failed: {outcome}")
    raise RuntimeError(f"could not join match {match} on either team")


def run_llm_tick(agent_factory, state: str, tick: int) -> tuple[int, tuple[str, dict[str, Any]] | None]:
    """Run the fallback Agent for one tick (a fresh agent each time).

    Returns (how many action tools it called, the (tool name, input) it executed or None).
    """
    prompt = (
        f"{state}\n\nThis is tick {tick}. Pick ONE of the listed routes or actions and call "
        f"exactly one action tool ({', '.join(ACTION_TOOLS)}) with tick={tick}: move with the "
        "route's first step, tag with the enemy id, grab_flag, or wait.\n" + LLM_PLAN_NOTE
    )
    agent = agent_factory()
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


def play(client: MCPClient, decider: Decider, args: argparse.Namespace) -> Stats:
    stats = Stats()
    decider_down = False
    errors = 0
    plan = Plan()

    action_tools = [t for t in client.list_tools_sync() if t.tool_name in ACTION_TOOLS]
    agent_factory = lambda: Agent(  # noqa: E731
        model=MODELS[args.model],
        tools=action_tools,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )

    if not args.no_warmup and not warm_up(decider):
        decider_down = True

    join(client, args.match, sanitize_name(args.name))

    while True:
        turn = call_tool(client, TOOL_WAIT_FOR_TURN, {})
        status = turn.get("status")
        if status == "match_over":
            print(f"match over: {json.dumps({k: v for k, v in turn.items() if not k.startswith('_')})}")
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
        state = render_state(turn, plan)
        options = build_options(turn, plan)
        if args.raw_turns:
            print(f"[tick {tick}] raw turn:\n{json.dumps(turn, indent=2, ensure_ascii=False)}")
            print(f"[tick {tick}] state sent to decider:\n{state}")
            print(f"[tick {tick}] options: {sorted(options)}")

        chosen: str | None = None
        confidence = 0.0
        if not decider_down:
            question = {"action": Decider.choice(DECIDER_INSTRUCTIONS, options)}
            started = time.monotonic()
            try:
                answers = ask_serialized(decider, state, question)
            except (ServerUnavailable, OSError, http.client.HTTPException) as exc:
                decider_down = True
                print(f"[decider] {exc}\n    -> LLM fallback for the rest of the match")
            else:
                chosen, confidence = parse_choice(answers["action"])
                if decider.last_latency_ms is not None:
                    stats.latencies_ms.append(float(decider.last_latency_ms))
                print(f"[tick {tick}] timing: decider wall {(time.monotonic() - started) * 1000:.0f} ms "
                      f"(server {decider.last_latency_ms} ms), deadline_remaining at receipt "
                      f"{turn.get('deadline_remaining_ms')} ms")

        remaining = turn_remaining_ms(turn, (time.monotonic() - turn_received) * 1000)
        path = choose_path(chosen, confidence, args.threshold, options, remaining,
                           args.llm_budget_ms, args.safety_margin_ms, decider_down)

        if path in ("decider", "budget"):
            tool, arguments = label_to_call(chosen, tick, turn)
            outcome = call_tool(client, tool, arguments)
            route_id = chosen if route_goal(turn, chosen) is not None else None
            plan = update_plan(plan, route_id, route_goal(turn, route_id), describe_action(tool, arguments), tick)
            if path == "decider":
                stats.decider_ticks += 1
                print(f"[tick {tick}] decider -> {chosen} (confidence {confidence:.2f}){plan_suffix(plan)}")
            else:
                stats.budget_ticks += 1
                print(f"[tick {tick}] budget: {remaining:.0f} ms left < "
                      f"{args.llm_budget_ms + args.safety_margin_ms:.0f} ms needed for LLM -> "
                      f"executing decider choice {chosen} ({confidence:.2f}){plan_suffix(plan)}")
            if outcome["_is_error"]:
                print(f"    action error: {outcome}")
            continue

        if path == "wait":
            outcome = call_tool(client, "wait", {"tick": tick})
            stats.budget_ticks += 1
            plan = update_plan(plan, None, None, "wait", tick)
            why = "decider unavailable" if decider_down else f"no valid decider choice ({chosen}, {confidence:.2f})"
            print(f"[tick {tick}] budget: {remaining:.0f} ms left < "
                  f"{args.llm_budget_ms + args.safety_margin_ms:.0f} ms needed for LLM, {why} -> wait")
            if outcome["_is_error"]:
                print(f"    action error: {outcome}")
            continue

        reason = "decider unavailable" if decider_down else f"low confidence ({chosen}, {confidence:.2f})"
        print(f"[tick {tick}] LLM fallback: {reason}")
        stats.llm_ticks += 1
        llm_started = time.monotonic()
        calls, call = run_llm_tick(agent_factory, state, tick)
        print(f"[tick {tick}] timing: LLM wall {(time.monotonic() - llm_started) * 1000:.0f} ms, "
              f"total since turn {(time.monotonic() - turn_received) * 1000:.0f} ms")
        if calls == 0:
            print("    warning: the LLM made no action call; the server will count this tick as wait")
        if call is not None:
            route_id = infer_executed_route(call, turn, plan)
            plan = update_plan(plan, route_id, route_goal(turn, route_id), describe_action(*call), tick)
            print(f"    LLM executed {describe_action(*call)}{plan_suffix(plan)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AmazingPlayer CTF agent with a code-owned loop and strands-decider.")
    parser.add_argument("--model", choices=sorted(MODELS.keys()), default="haiku",
                        help="Model used for the LLM fallback (default: haiku).")
    parser.add_argument("--match", default="llm-1", help="Match id to join (default: llm-1).")
    parser.add_argument("--name", default="AmazingPlayer",
                        help="Agent name used to join the match (default: AmazingPlayer; lowercased to fit [a-z0-9_-]).")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Minimum decider confidence to act without the LLM (default: {DEFAULT_THRESHOLD}).")
    parser.add_argument("--raw-turns", action="store_true",
                        help="Print each raw wait_for_turn payload, the state sent to the decider and its options.")
    parser.add_argument("--llm-budget-ms", type=float, default=DEFAULT_LLM_BUDGET_MS,
                        help=f"Expected LLM fallback time; skipped when less time is left (default: {DEFAULT_LLM_BUDGET_MS}).")
    parser.add_argument("--safety-margin-ms", type=float, default=DEFAULT_SAFETY_MARGIN_MS,
                        help=f"Extra margin added to the LLM budget (default: {DEFAULT_SAFETY_MARGIN_MS}).")
    parser.add_argument("--no-warmup", action="store_true", help="Skip the decider warm-up before joining the match.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    decider = Decider()
    try:
        print(decider.banner(), "\n")
    except ServerUnavailable as exc:
        print(f"{exc}\n    -> starting in LLM-fallback mode")
        decider_down_at_start = True
    else:
        decider_down_at_start = False

    client = MCPClient.load_servers(MCP_SERVERS)[0]
    with client:
        if decider_down_at_start:
            # Point the decider at nothing useful so the first ask fails open immediately.
            decider.timeout = 1.0
        stats = play(client, decider, args)

    print(f"\n{stats.summary()}; {decider}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
