"""decider_loop runtime: plain Python owns the game loop and the decider picks the action.

    wait_for_turn -> render state -> ONE decider request (3 questions) -> call the tool directly

Only when the decider is not confident enough (or cannot answer in time) does a Strands `Agent`
run, once for that tick, restricted to the action tools. No LLM is involved on the fast path.

Ported from `local/agent/refined/decider_loop.py`. The pure decision logic is in `loop_logic.py`
(characterization-tested against the original). Differences from the local script:

- the static board map is captured once per match (`join_match.map`, else one `get_map` call) and threaded
  into the pure functions as a `MapInfo`: the v2 turn state no longer carries it;

- the decider is a `DeciderPort` (SageMaker in AgentCore, HTTP locally) with a deadline clamp, one
  bounded retry and a circuit breaker. There is no permanent "decider down" state: a failed call
  only sends THAT tick to the LLM, and the breaker decides when the endpoint is tried again;
- the `ctx.stop` event is checked every tick, and a dead MCP connection fails the match
  (the loop never retries a connection that cannot recover);
- the per-process file lock and the CLI are gone (serialization lives in the SageMaker adapter).
"""

from __future__ import annotations

import functools
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from strands import Agent
from strands.hooks import AfterToolCallEvent, AfterToolsEvent
from strands.interventions import Deny, InterventionHandler, Proceed

from ctf_common import wiring
from ctf_common.auth import TokenProvider
from ctf_common.decider import DeciderPort, ServerUnavailable, make_decider
from ctf_common.hooks import is_connection_lost
from ctf_common.join import JoinSignal, seat_in_code
from ctf_common.mcp import open_mcp_client, raise_on_auth_failure
from ctf_common.payload import MatchRequest, require_match_id
from ctf_common.prompts import MODEL_ALIASES
from ctf_common.runner import AsyncTaskApp, MatchContext, MatchRunner, PlayFn
from loop_logic import (
    ACTION_TOOLS,
    DEFAULT_LLM_BUDGET_MS,
    DEFAULT_SAFETY_MARGIN_MS,
    DEFAULT_THRESHOLD,
    LLM_PLAN_NOTE,
    LLM_SYSTEM_PROMPT,
    MAX_CONSECUTIVE_TOOL_ERRORS,
    THREAT_YES,
    TOOL_GET_MAP,
    TOOL_JOIN,
    TOOL_WAIT_FOR_TURN,
    MapInfo,
    Plan,
    Stats,
    _my_position,
    _state,
    action_rejection,
    build_options,
    build_questions,
    choose_path,
    combine_answers,
    describe_action,
    describe_match_over,
    extract_action_call,
    extract_tick,
    format_decision,
    infer_executed_route,
    label_to_call,
    llm_timeout_s,
    parse_map,
    parse_noul,
    parse_tool_result,
    plan_suffix,
    render_state,
    route_goal,
    route_path,
    run_with_timeout,
    timeout_action,
    turn_remaining_ms,
    update_plan,
    warmup_question,
)

RUNTIME = "decider_loop"

log = logging.getLogger(RUNTIME)

McpFactory = Callable[[str, TokenProvider], Any]


def raw_name_mcp_factory(url: str, tokens: TokenProvider) -> Any:
    """The loop calls and filters tools by their raw MCP names, so tool names are not prefixed."""
    return open_mcp_client(url, tokens, prefix_tools=False)


class McpConnectionLost(RuntimeError):
    """The MCP connection is dead. It cannot recover (no reconnect, no refresh), so the match ends."""


# --------------------------------------------------------------------------- LLM fallback guards


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
        name = (getattr(event, "tool_use", None) or {}).get("name")
        if name in ACTION_TOOLS and not getattr(event, "cancel_message", None):
            self.acted = True

    def after_tools(self, event: Any) -> None:
        if self.acted:
            event.end_turn = "action sent"


# --------------------------------------------------------------------------- MCP calls


def call_tool(client: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = client.call_tool_sync(f"{name}-{uuid.uuid4().hex[:8]}", name, arguments)
    if is_connection_lost(result):
        raise McpConnectionLost(f"MCP connection lost during {name}")
    parsed = parse_tool_result(result)
    # Tool errors set isError; transport errors (e.g. "session not found") only set status="error".
    parsed["_is_error"] = result.get("status") == "error" or bool(result.get("isError"))
    return parsed


class Joined(NamedTuple):
    team: str
    map_info: MapInfo | None  # None when the join result carried no usable `map`


def join(
    client: Any, request: MatchRequest, signal: JoinSignal, stop: threading.Event | None = None
) -> Joined | None:
    """Seat the character (code-owned) and report the outcome to `start` through `signal`.

    Only the requested team is tried (`auto`: red, then blue after a `match_full`), and a requested slot is
    sent as `agent_index` and never traded for another one. Returns the team the server assigned and the
    static map; None when `stop` was set before a join call; raises when the seat was refused or errored.
    """
    match = require_match_id(request)
    payload = seat_in_code(client, request, signal, stop)
    outcome = signal.outcome
    if outcome is not None and outcome.kind == "cancelled":
        return None
    if payload is None:
        reason = outcome.reason if outcome is not None else "unknown"
        if outcome is not None and outcome.kind == "error":
            raise McpConnectionLost(f"MCP connection lost during {TOOL_JOIN}: {reason}")
        log.warning("join_match refused for %s: %s", request.name, reason)
        raise RuntimeError(f"could not join match {match} on {request.team}: {reason}")
    log.info(
        "joined match %s as %s on team %s (agent_index %s)",
        match,
        request.name,
        payload["team"],
        payload["agent_index"],
    )
    return Joined(str(payload["team"]), parse_map(payload.get("map")))


def fetch_map(client: Any) -> MapInfo | None:
    """The static map from `get_map`: the fallback when `join_match` did not carry it. None on failure."""
    outcome = call_tool(client, TOOL_GET_MAP, {})
    if outcome["_is_error"]:
        log.warning("get_map failed: %s", outcome)
        return None
    return parse_map(outcome)


def log_outcome(outcome: dict[str, Any]) -> None:
    """Log a tool error or a rejected action ack (a normal result with a reason, not an MCP error)."""
    if outcome["_is_error"]:
        log.warning("    action error: %s", outcome)
    elif (reason := action_rejection(outcome)) is not None:
        log.warning("    action rejected: %s", reason)


def run_llm_tick(
    agent_factory: Callable[[InterventionHandler | None], Any],
    state: str,
    tick: int,
    handler: InterventionHandler | None = None,
) -> tuple[int, tuple[str, dict[str, Any]] | None]:
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


def warm_up(decider: DeciderPort) -> None:
    """Send one throwaway question. A failure is logged only: the breaker owns the availability policy."""
    state, questions = warmup_question()
    started = time.monotonic()
    try:
        decider.ask(state, questions)
    except ServerUnavailable as exc:
        log.warning("decider warm-up failed (%s): %s", type(exc).__name__, exc)
        return
    log.info(
        "decider warm-up: %.0f ms (server %s ms)",
        (time.monotonic() - started) * 1000,
        decider.last_latency_ms,
    )


# --------------------------------------------------------------------------- the loop


def build_decider(ctx: MatchContext) -> DeciderPort:
    """A fresh decider (and circuit breaker) per match, from the config resolved at preflight."""
    margin_ms = float(ctx.request.options.get("safety_margin_ms", DEFAULT_SAFETY_MARGIN_MS))
    return make_decider(ctx.config, safety_margin_s=margin_ms / 1000)


def play(
    ctx: MatchContext,
    *,
    mcp_factory: McpFactory = raw_name_mcp_factory,
    model: Any = None,
    decider: DeciderPort | None = None,
) -> None:
    """Play one character until match_over, a stop request, or a failure.

    The MCP connection is opened here (one token). A dead connection ends the loop; the 401 that
    killed it surfaces when the client closes, where `raise_on_auth_failure` maps it to `McpAuthError`.
    """
    decider = decider or build_decider(ctx)
    client = mcp_factory(ctx.config.mcp_url, ctx.tokens)
    lost = False
    with raise_on_auth_failure():
        with client:
            try:
                stats = run_match(ctx, client, decider, model)
            except McpConnectionLost as exc:
                log.error("%s", exc)
                lost = True
            else:
                log.info("%s; %s", stats.summary(), decider_summary(decider))
    if lost:
        raise RuntimeError("MCP connection lost mid-match")


def decider_summary(decider: DeciderPort) -> str:
    return f"{decider.calls} decisions, {decider.input_tokens} input tokens"


def run_match(ctx: MatchContext, client: Any, decider: DeciderPort, model: Any) -> Stats:
    request, options = ctx.request, ctx.request.options
    threshold = float(options.get("threshold", DEFAULT_THRESHOLD))
    threat_threshold = float(options.get("threat_threshold", THREAT_YES))
    llm_budget_ms = float(options.get("llm_budget_ms", DEFAULT_LLM_BUDGET_MS))
    margin_ms = float(options.get("safety_margin_ms", DEFAULT_SAFETY_MARGIN_MS))

    stats = Stats()
    errors = 0
    plan = Plan()

    action_tools = [t for t in client.list_tools_sync() if t.tool_name in ACTION_TOOLS]
    llm_model = model if model is not None else MODEL_ALIASES[request.model]
    agent_factory = lambda handler: Agent(  # noqa: E731
        model=llm_model,
        tools=action_tools,
        system_prompt=LLM_SYSTEM_PROMPT,
        callback_handler=None,
        interventions=[handler] if handler is not None else [],
        hooks=[StopAfterActionHook()],  # end the turn after the action: no wasted 2nd model call
    )

    if ctx.stop.is_set():  # stopped before taking a seat: do not join
        return stats
    # Join BEFORE the warm-up: `start` is waiting for the seat, and a cold decider endpoint must not hold it.
    joined = join(client, request, ctx.join, ctx.stop)
    if joined is None:  # stopped between opening the client and the join call
        return stats
    warm_up(decider)
    map_info = joined.map_info or fetch_map(client)  # an older server: fetch the static map once instead
    if map_info is None:
        log.warning("no static map (join_match.map / get_map): field-dependent hints are off for this match")

    while not ctx.stop.is_set():
        turn = call_tool(client, TOOL_WAIT_FOR_TURN, {})
        status = turn.get("status")
        if status == "match_over":
            log.info("%s", describe_match_over(turn))
            return stats
        if status != "turn":  # 'pending' (nothing arrived within the wait cap) or an error
            if turn["_is_error"]:
                errors += 1
                if errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                    raise RuntimeError(f"wait_for_turn kept failing: {turn}")
            continue
        errors = 0

        tick = extract_tick(turn)
        ctx.tick()
        if ctx.stop.is_set():  # stop arrived while waiting: do not act on this turn
            break
        turn_received = time.monotonic()
        state = render_state(turn, plan, map_info)
        options_map = build_options(turn, plan, map_info)

        chosen: str | None = None
        confidence = 0.0
        reason, threat_p, route_bad_p = "choice", 0.0, 0.0
        unavailable = False  # tick-local: the next tick asks again (the breaker owns the cooldown)
        started = time.monotonic()
        remaining_ms = turn_remaining_ms(turn, 0.0)
        try:
            answers = decider.ask(state, build_questions(options_map), deadline_s=remaining_ms / 1000)
        except ServerUnavailable as exc:
            unavailable = True
            log.warning(
                "[tick %s] decider unavailable (%s) -> LLM fallback for this tick", tick, type(exc).__name__
            )
        else:
            chosen, confidence, reason = combine_answers(
                answers, options_map, turn, plan, threat_threshold=threat_threshold
            )
            threat_p, route_bad_p = parse_noul(answers.get("threat")), parse_noul(answers.get("route_bad"))
            if decider.last_latency_ms is not None:
                stats.latencies_ms.append(float(decider.last_latency_ms))
            log.info(
                "[tick %s] timing: decider wall %.0f ms (server %s ms), deadline_remaining at receipt %s ms",
                tick,
                (time.monotonic() - started) * 1000,
                decider.last_latency_ms,
                turn.get("deadline_remaining_ms"),
            )

        remaining = turn_remaining_ms(turn, (time.monotonic() - turn_received) * 1000)
        path = choose_path(
            chosen, confidence, threshold, options_map, remaining, llm_budget_ms, margin_ms, unavailable
        )

        if path in ("decider", "budget"):
            try:
                tool, arguments = label_to_call(chosen or "", tick, turn)
            except ValueError as exc:
                # Never crash mid-match on an option we cannot map: a wait beats a dead agent.
                log.warning("[tick %s] %s -> wait", tick, exc)
                tool, arguments = "wait", {"tick": tick}
            outcome = call_tool(client, tool, arguments)
            route_id = chosen if route_goal(turn, chosen) is not None else None
            plan = update_plan(
                plan,
                route_id,
                route_goal(turn, route_id),
                describe_action(tool, arguments),
                tick,
                route_path(turn, route_id),
                _my_position(_state(turn)),
            )
            if path == "decider":
                stats.decider_ticks += 1
                log.info(
                    format_decision(tick, chosen, confidence, reason, threat_p, route_bad_p)
                    + plan_suffix(plan)
                )
            else:
                stats.budget_ticks += 1
                log.info(
                    "[tick %s] budget: %.0f ms left < %.0f ms needed for LLM -> executing %s (%.2f)%s",
                    tick,
                    remaining,
                    llm_budget_ms + margin_ms,
                    chosen,
                    confidence,
                    plan_suffix(plan),
                )
            log_outcome(outcome)
            continue

        if path == "wait":
            outcome = call_tool(client, "wait", {"tick": tick})
            stats.budget_ticks += 1
            plan = update_plan(plan, None, None, "wait", tick)
            why = (
                "decider unavailable"
                if unavailable
                else f"no valid decider choice ({chosen}, {confidence:.2f})"
            )
            log.info(
                "[tick %s] budget: %.0f ms left < %.0f ms needed for LLM, %s -> wait",
                tick,
                remaining,
                llm_budget_ms + margin_ms,
                why,
            )
            log_outcome(outcome)
            continue

        why = "decider unavailable" if unavailable else f"low confidence ({chosen}, {confidence:.2f})"
        log.info("[tick %s] LLM fallback: %s", tick, why)
        stats.llm_ticks += 1
        llm_started = time.monotonic()
        limit_ms = (
            llm_timeout_s(turn_remaining_ms(turn, (llm_started - turn_received) * 1000), margin_ms) * 1000
        )
        llm_state = render_state(turn, plan, map_info, include_last_action=True)  # fallback-only text
        handler = TickCancelHandler(threading.Event())
        timed_out, llm_result = run_with_timeout(
            functools.partial(run_llm_tick, agent_factory, llm_state, tick, handler), limit_ms / 1000
        )
        if timed_out:
            already_acted = handler.cancel_tick()  # BEFORE any fallback action
            stats.llm_ticks -= 1
            stats.llm_timeouts += 1
            waited = (
                f"[tick {tick}] LLM timeout after {(time.monotonic() - llm_started) * 1000:.0f} ms "
                f"(limit {limit_ms:.0f} ms)"
            )
            action = timeout_action(chosen, options_map, unavailable, already_acted)
            if action == "none":
                log.info("%s -> LLM already executed an action, not sending another", waited)
                continue
            if action == "decider":
                try:
                    tool, arguments = label_to_call(chosen or "", tick, turn)
                except ValueError as exc:
                    log.warning("[tick %s] %s -> wait", tick, exc)
                    action, tool, arguments = "wait", "wait", {"tick": tick}
            else:
                tool, arguments = "wait", {"tick": tick}
            outcome = call_tool(client, tool, arguments)
            route_id = chosen if action == "decider" and route_goal(turn, chosen) is not None else None
            plan = update_plan(
                plan,
                route_id,
                route_goal(turn, route_id),
                describe_action(tool, arguments),
                tick,
                route_path(turn, route_id),
                _my_position(_state(turn)),
            )
            verdict = (
                f"executing decider choice {chosen} ({confidence:.2f})" if action == "decider" else "wait"
            )
            log.info("%s -> %s%s", waited, verdict, plan_suffix(plan))
            log_outcome(outcome)
            continue
        calls, call = llm_result
        log.info(
            "[tick %s] timing: LLM wall %.0f ms, total since turn %.0f ms",
            tick,
            (time.monotonic() - llm_started) * 1000,
            (time.monotonic() - turn_received) * 1000,
        )
        if calls == 0:
            log.warning("    the LLM made no action call; the server will count this tick as wait")
        if call is not None:
            route_id = infer_executed_route(call, turn, plan)
            plan = update_plan(
                plan,
                route_id,
                route_goal(turn, route_id),
                describe_action(*call),
                tick,
                route_path(turn, route_id),
                _my_position(_state(turn)),
            )
            log.info("    LLM executed %s%s", describe_action(*call), plan_suffix(plan))
    return stats


def build_runner(
    app: AsyncTaskApp,
    env: Mapping[str, str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    ssm_client: Any | None = None,
    play: PlayFn = play,
) -> MatchRunner:
    """Wire the runner. The preflight also resolves the decider endpoint name (SSM)."""
    return wiring.build_runner(
        app,
        play,
        runtime=RUNTIME,
        env=env,
        token_provider=token_provider,
        ssm_client=ssm_client,
        needs_decider=True,
    )
