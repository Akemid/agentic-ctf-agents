"""Pure decision logic of the `decider_loop` runtime (no I/O, no MCP, no Strands).

Ported verbatim from `local/agent/refined/decider_loop.py` and characterization-tested against it
(`tests/test_loop_logic.py`). Only three things differ: the question builders come from
`ctf_common.decider`, the fcntl lock and CLI are gone, and the fallback system prompt is named
`LLM_SYSTEM_PROMPT` so it cannot be confused with `ctf_common.prompts.SYSTEM_PROMPT`.

The decider classifies, the code decides: `combine_answers` merges three answers (threat,
route_bad, action) into one option label, `choose_path` decides between decider, LLM fallback and
the time budget.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Literal

from ctf_common.decider import choice, noul

LLM_SYSTEM_PROMPT = (
    "You are AmazingPlayer, a Capture the Flag game agent competing as a member of a "
    "team. You are direct and tactical. You are given the current game state for ONE tick. "
    "Pick the single best action and call exactly one action tool with the tick number "
    "provided. Do not call any other tool and do not answer with text only."
)

# Raw tool names (VERIFIED via list_tools_sync). No server-name prefix on direct calls.
TOOL_JOIN = "join_match"
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
        a
        for a in state.get("agents") or []
        if isinstance(a, dict)
        and a.get("team") != team
        and a.get("team") is not None
        and not a.get("disconnected")
        and a.get("id") != state.get("self")
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
    return (
        [r for r in routes if isinstance(r, dict) and isinstance(r.get("id"), str)]
        if isinstance(routes, list)
        else []
    )


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
        if (
            not isinstance(cell, (list, tuple))
            or len(cell) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in cell)
        ):
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


def update_plan(
    plan: Plan,
    executed_route_id: str | None,
    executed_goal: str | None,
    action_text: str,
    tick: int,
    executed_path: Any = None,
    position: Cell | None = None,
) -> Plan:
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
    keeps = (
        plan.since_tick is not None
        and plan.goal is not None
        and plan.goal == executed_goal
        and continues_path(plan.path, plan.position, position, executed_path)
    )
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


def infer_executed_route(
    call: tuple[str, dict[str, Any]] | None, turn: dict[str, Any], plan: Plan
) -> str | None:
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
        matches = sorted(
            (r for r in routes if r.get("first_step") == arguments.get("direction")),
            key=lambda r: (r.get("goal") != plan.goal, r["id"]),
        )
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
        detail = "; ".join(
            f"can be tagged by {r.get('enemy_id', '?')} at step {r.get('step', '?')}" for r in risks
        )
        parts.append(f"{_plural(len(risks), 'risk')}: {detail}")
    distance = route.get("min_enemy_distance")
    if isinstance(distance, int) and not isinstance(distance, bool):
        parts.append(f"nearest enemy {distance}")
    return ", ".join(parts)


def build_options(turn: dict[str, Any], plan: Plan | None = None) -> dict[str, str]:
    """Options for the decider `choice` question, built from the state. Always includes `wait`.

    One option per server route (label = route id). Without routes, falls back to one
    `move_<dir>` option per open direction (and adjacent tags). No option is ever recommended.
    With a `plan`, every route whose path continues the plan's path gets a neutral note, and
    other routes with the same goal are marked as a different path.
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
    if (
        me is not None
        and enemy_flag is not None
        and enemy_flag.get("carried_by") is None
        and _xy(enemy_flag.get("position")) == me
    ):
        options["grab_flag"] = "pick up the enemy flag you are standing on"

    if not routes and me is not None and _in_my_field(state):
        for enemy in _enemies(state):
            pos = _xy(enemy.get("position"))
            if pos is not None and _is_adjacent(me, pos):
                options[f"tag_{enemy['id']}"] = (
                    f"tag the adjacent enemy {enemy['id']} (you are in your own field)"
                )

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
        "threat": noul(THREAT_INSTRUCTIONS, THREAT_CRITERIA),
        "route_bad": noul(ROUTE_BAD_INSTRUCTIONS, ROUTE_BAD_CRITERIA),
        "action": choice(DECIDER_INSTRUCTIONS, options),
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


def combine_answers(
    answers: dict[str, Any],
    options: dict[str, str],
    turn: dict[str, Any],
    plan: Plan | None = None,
    *,
    threat_threshold: float = THREAT_YES,
    route_bad_threshold: float = ROUTE_BAD_YES,
) -> tuple[str | None, float, str]:
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
        safe = [
            r
            for r in routes.values()
            if r["id"] in options
            and r["id"] != pick
            and r.get("goal") == picked.get("goal")
            and not _is_bad(r)
        ]
        if safe:
            best = min(
                safe,
                key=lambda r: (
                    not route_continues_plan(plan, me, r),
                    r["length"] if isinstance(r.get("length"), int) else 10**6,
                    r["id"],
                ),
            )
            return best["id"], route_bad, "avoid blocked/risky"

    return pick, pick_conf, "choice"


def format_decision(
    tick: int, label: str | None, confidence: float, reason: str, threat: float, route_bad: float
) -> str:
    """Per-tick log line for a decider decision."""
    return (
        f"[tick {tick}] decider -> {label} ({confidence:.2f}) reason={reason} "
        f"threat={threat:.2f} route_bad={route_bad:.2f}"
    )


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


def choose_path(
    chosen: str | None,
    confidence: float,
    threshold: float,
    options: dict[str, str],
    remaining: float,
    llm_budget_ms: float,
    margin_ms: float,
    decider_down: bool,
) -> Literal["decider", "budget", "llm", "wait"]:
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


def timeout_action(
    chosen: str | None, options: dict[str, str], decider_down: bool, already_acted: bool
) -> Literal["decider", "wait", "none"]:
    """What to send when the LLM fallback timed out (pure).

    none: the LLM already executed an action, never send a second one.
    decider: the decider's choice is a valid offered option. wait: nothing valid to run.
    """
    if already_acted:
        return "none"
    if not decider_down and chosen is not None and chosen in options:
        return "decider"
    return "wait"


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

    def route(rid, goal, step, length, risks=(), blocked=False, dist=3):
        return {
            "id": rid,
            "goal": goal,
            "first_step": step,
            "length": length,
            "path": [],
            "blocked_now": blocked,
            "blocked_reason": "contention" if blocked else None,
            "risks": [{"step": st, "enemy_id": en} for st, en in risks],
            "min_enemy_distance": dist,
        }

    def agent(aid, team, x, y):
        return {
            "id": aid,
            "team": team,
            "position": {"x": x, "y": y},
            "carrying": None,
            "disconnected": False,
        }

    turn = {
        "status": "turn",
        "tick": 12,
        "state": {
            "tick": 12,
            "agents": [
                agent("blue-1", "blue", 9, 7),
                agent("blue-2", "blue", 11, 4),
                agent("red-1", "red", 5, 6),
                agent("red-2", "red", 4, 9),
                agent("red-3", "red", 2, 7),
            ],
            "flags": [
                {"team": "red", "base": {"x": 1, "y": 7}, "position": {"x": 1, "y": 7}, "carried_by": None},
                {
                    "team": "blue",
                    "base": {"x": 13, "y": 7},
                    "position": {"x": 13, "y": 7},
                    "carried_by": None,
                },
            ],
            "score": {"red": 1, "blue": 0},
            "fields": {"red": {"min_x": 0, "max_x": 6}, "blue": {"min_x": 8, "max_x": 14}, "neutral_x": 7},
            "self": "blue-1",
            "self_position": {"x": 9, "y": 7},
            "teammates": [{"id": "blue-2", "position": {"x": 11, "y": 4}}],
            "routes": [
                route("enemy_flag_direct", "enemy_flag", "left", 8, [(3, "red-1")], dist=2),
                route("enemy_flag_detour", "enemy_flag", "up", 11, dist=4),
                route("tag:red-1", "tag", "left", 4, dist=4),
                route("home_base", "own_base", "right", 4, blocked=True),
            ],
        },
        "events": [{"type": "moved", "agent_id": "red-1", "from": {"x": 6, "y": 6}, "to": {"x": 5, "y": 6}}],
    }
    plan = Plan("enemy_flag_direct", "enemy_flag", 9, "move left")
    return render_state(turn, plan), build_questions(build_options(turn, plan))


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
    if _plan_active(plan):
        lines.append(_plan_line(turn, plan))

    score = state.get("score")
    if isinstance(score, dict):
        lines.append("score: " + ", ".join(f"{k} {v}" for k, v in score.items()))

    for enemy in _enemies(state):
        pos = _xy(enemy.get("position"))
        lines.append(
            f"enemy {enemy.get('id')} at {pos}, "
            f"carrying: {'yes' if enemy.get('carrying') is not None else 'no'}"
        )

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
