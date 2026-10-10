# Decider Loop: Engineering Journal

A running log of decisions, the evidence behind them, and what each change produced while
building `agent/agent-decider-loop.py`: a Capture the Flag agent where **our code owns the
loop** and a local [strands-decider](https://github.com/strands-labs/strands-decider) model
picks actions, with an LLM as fallback.

Each entry follows the same shape so it can be lifted into blog posts:
**Observation → Decision → Why → Trade-offs → Result.**

---

## Context

- **Game:** CTF on a grid with walls, played through an MCP server (`join_match`,
  `wait_for_turn`, `move`, `tag`, `grab_flag`, `wait`). One action per tick before a
  deadline; missing it counts as `wait`.
- **Goal of the project:** a benchmark that pits different AIs against each other — models,
  the Strands harness, the Strands harness SDK, and strands-decider versions — on
  **strategic reasoning**, not on pathfinding or plumbing.
- **The Decider:** a ~2B classifier (Qwen3.5-2B base with a pointer head). It does not
  generate text or call tools; given a state and typed questions (`noul`, `choice`, `score`)
  it returns labels with probabilities in one forward pass (~0.5–1 s on Apple MPS here).

---

## 1. Where the Decider can plug in (2026-10-09)

**Observation.** A previous research note claimed two integration points. We verified it
against the installed SDK and the official repo:

- `Agent(interventions=[...])` with `before_tool_call` is real; `Guide` cancels the call and
  sends feedback, so the model retries — **one more LLM call per correction**.
- The official example sends `event.agent.messages` in full, but its renderer keeps only
  text blocks, so tool results (where the game state lives) never reach the Decider.
- Two bugs in the note's sample code: MCP tools are prefixed (`ctf-game_move`) when
  `prefix_with_server_name=True`, so a filter on `"move"` never fires; and
  `Decider.choice` takes a `dict` of option → description, not a list.

**Decision.** Build a second agent where our code owns the loop instead of only filtering
the agent's loop.

**Why.** Inside the agent's loop the Decider can only veto (costing extra LLM calls under a
deadline). Owning the loop lets the Decider *decide* most ticks in ~150–600 ms and call the
MCP tool directly with `MCPClient.call_tool_sync`, with the LLM as fallback.

**Key clarification.** The Decider cannot call MCP itself — it only classifies. *Our code*
calls MCP; the Decider just picks the option.

---

## 2. First run: the Decider was blind (2026-10-09)

**Observation.** Every tick fell back to the LLM; the Decider picked `wait` at ~0.20
confidence (≈ 1/5, i.e. chance level over 5 options). A `--raw-turns` dump showed the state
sent to the Decider was literally `observation: {}`: the code read `observation`, but the
server sends everything under `state`.

**Decision.** Parse the real schema (`state.agents`, `state.flags`, `state.fields`,
`state.self`, `state.moves`) and send a short human-readable summary instead of raw JSON.

**Why.** Low confidence was a *symptom*. Lowering the threshold would just have executed
random `wait`s faster.

**Lesson.** Log the exact input your model sees before tuning anything about its output.

---

## 3. The Decider was copying our hint (2026-10-09)

**Observation.** With a real state and a `suggested direction` line, the Decider chose the
right move every tick (0.60–0.70). With `--threshold 0.6` it decided 14 of 15 ticks.
But it always matched the hint — then a contention deadlock appeared: red at (7,7) and a
bot at (8,6) both moving into (8,7), rejected with `reason: contention` for **42 of 50
ticks**. The Decider kept choosing `move_right` because the hint, the static `moves` map and
the trimmed event summary all still pointed right.

**Decision (interim).** Render full `move_blocked` events, track consecutive blocks, and
suggest detours.

**Result.** The greedy detour walked into a dead end and oscillated between (1,7) and (1,6)
for the whole match.

**Lesson.** A 2B classifier follows the strongest signal in its prompt. If your code writes
the hint, your code is the one making the decision.

---

## 4. Navigation belongs to the server, strategy to the agents (2026-10-09)

**Observation.** We were about to write BFS in the client. That raised the real question:
if navigation lives in each client, the benchmark measures who wrote the better BFS.

**Decision.** The server computes routes every tick and exposes them as advisory options in
`state.routes`: `enemy_flag_direct|safe|detour`, `own_base_*`, `tag:<enemy_id>`,
`defend_flag`, each with `first_step`, `length`, `path`, `blocked_now`, `risks`
(cells where you can be tagged) and `min_enemy_distance`. The client turns each route into a
`choice` option and executes `move(first_step)`.

**Why.** Pathfinding is deterministic and solved; it is not what we want to compare. Picking
*which* route — accept a tag risk or take two extra steps, chase an invader or go for the
flag — is the strategic decision we want to measure, and it is the same question for every
competitor.

**Fairness rules we set for the server:**

- Never mark a route as recommended; sort by id.
- Same algorithm and information for every agent.
- Keep it small (≤ 7 routes, paths truncated to 8 cells) — the Decider has a 4096-token window.

**Also.** All three other agents (`agent-harness.py`, `agent-harness-sdk.py`,
`agent-strand-decider.py`) got the same system-prompt paragraph about `state.routes`, so
the comparison is apples to apples.

---

## 5. Strategy failures become visible (2026-10-10)

**Observation.** With routes, navigation problems disappeared and strategy problems showed:

- `tag:blue-1` was offered in 32 ticks (6 of them "adjacent now: tag immediately") and
  `defend_flag` in 17 — neither the Decider nor Haiku ever tagged. Blue stole our flag and won 1–0.
- The agent oscillated between `enemy_flag_direct` (down) and `enemy_flag_detour` (right),
  undoing its own steps.

**Decision.** Do *not* fix threat handling in code — that is what the benchmark measures.
Fix the oscillation with memory.

---

## 6. Memory: inform, don't enforce (2026-10-10)

**Options considered.**

| Option | What it does | Who decides |
|---|---|---|
| A. Inform | Show `current plan: …` and mark the option that continues it | The model, every tick |
| B. Commit | Code keeps the plan and only re-asks on triggers (blocked, new risk, threat) | Code decides *when*, model decides *what* |
| C. Conversation memory | Reuse one LLM Agent for the whole match | The model, but context and latency grow per tick |

**Decision.** A.

**Why.** It keeps the benchmark pure: we measure whether a model sustains a plan when shown
one, instead of our code sustaining it.

**Result.** Steady progress from (0,3) to (6,4); for the first time the agent switched its
plan to `tag:blue-1` and chased an invader for 6 ticks.

---

## 7. Speed is part of the benchmark (2026-10-10)

**Observation.** The deadline was set to 5000 ms on purpose (to measure speed). Timing logs
per tick showed:

- Decider cold start: **8.9 s** on the first call (server-side, not lock wait); ~0.5–1.2 s warm.
- LLM fallback (Haiku): ~2.8–4.9 s. Decider + LLM ≈ 5.5 s > 5 s → every LLM tick missed
  the deadline, and the lag compounded (`deadline_remaining_ms` at receipt fell 2646 → 746 ms).

**Decisions.**

1. **Warm-up:** one representative Decider request before `join_match`.
2. **Time budget:** if the Decider is below threshold and there is not enough time left for
   the LLM (`--llm-budget-ms` + `--safety-margin-ms`), execute the Decider's choice anyway.

**Why.** A low-confidence action beats a forced `wait`. The warm-up only moves *when* the
cold-start cost is paid; it doesn't change the game.

**Result.** Warm-up measured 8 s cold, then tick 0 ran in 1.2 s. Zero deadline misses in 50
ticks; the LLM never fit the 5 s budget, so this agent was effectively "Decider only" at
~550 ms per decision. **First flag capture**; lost 0–1 by four ticks.

**Lesson.** Measure before you guess: the cold-start hypothesis was only half the story —
the real culprit was the LLM not fitting the deadline.

---

## 8. Route ids are not plans (2026-10-10)

**Observation.** The agent walked *backwards* for several ticks twice in one match,
(3,7) → (2,7) → (1,7) → (0,7). Cause: the server recomputes `enemy_flag_detour` each tick
as "shortest path avoiding the direct route's first cell", so the same id is a different
path every tick. Our "continues your current plan" label was matching by id.

**Decision.** Continuity by **path**: a route continues the plan if its path starts where the
previous path was going (after one step, or unchanged if we were blocked), regardless of id.

**Result.** A straight run from (0,7) to (13,7) with one continuous plan, a flag capture at
tick 14, and a continuous return plan. The match (20 ticks) ended 0–0 before we got home.

**Lesson.** Identifiers that a producer recomputes are labels, not identities. Compare the
thing itself.

---

## 9. Bounding the LLM, not just budgeting it (2026-10-10)

**Observation.** With the deadline raised to 10 s the LLM fit the budget again — until it
didn't: 3.1 s, 3.2 s, then **18.0 s, 10.5 s, 32.6 s**. The budget was checked only *before*
calling the LLM; nothing bounded the call itself.

**Decision.** Hard timeout on the LLM fallback (run it in a worker thread, wait at most the
remaining time minus the margin). On timeout, execute the Decider's choice. A Strands
`before_tool_call` intervention on the fallback Agent **denies** any action tool once the
tick has timed out, so a late LLM cannot submit a second action for the same tick.

**Why.** A slow thinker should lose the turn — that is what a speed benchmark should
show — but it must not corrupt the next one. Using an intervention to enforce it closes the
loop with where this journey started (entry 1).

**Implementation notes.**

- `run_with_timeout` runs the LLM tick in a **daemon thread per call**, not a one-worker
  executor: an abandoned call would otherwise keep the single worker busy and block the
  next tick.
- `TickCancelHandler` (a Strands `InterventionHandler`) returns `Deny("tick timed out")`
  once the tick's `threading.Event` is set. It also records whether the LLM already
  executed an action, so a timeout never sends a second one.
- Timeout outcomes: execute the Decider's choice, `wait`, or nothing (the LLM already acted).
  They are counted as `LLM timeouts` in the summary.
- Known limit: if the LLM's tool call is already in flight when the timeout fires, that
  call goes through. We accept this, because we never add a second action.

**Result (live match, deadline 9000 ms).**

- **Zero deadline misses for our agent** in 50 ticks. Every one of the 32
  `action_rejected reason=deadline` events belonged to the opponent (`blue-1`).
- **20 LLM timeouts** at about 7.5 s each. In 10 of them the LLM *had already acted*, and the
  guard correctly sent nothing more.
- One flag capture; the match ended 0–0.

**New finding.** "Already acted, but still running at the timeout" means the fallback Agent
makes a tool call and then keeps going: it spends a **second model call** writing a final
answer nobody reads. Roughly half of the LLM time per tick is wasted after the action is
already sent. Next step: stop the Agent's event loop right after the action tool runs.

---

## 10. One action, one model call (2026-10-10)

**Observation.** The fallback Agent called the action tool and then made a second model call
to write a final answer nobody reads (entry 9).

**Decision.** End the Agent's turn right after an action tool runs, using a Strands hook:
`AfterToolsEvent.end_turn` (`strands/hooks/events.py`), which the event loop honours by
exiting without another model call. `StopAfterActionHook` records on `AfterToolCallEvent`
that a real, non-cancelled action ran, and then sets `end_turn` on `AfterToolsEvent`.

**Alternatives ruled out (by reading the SDK, strands 1.59.0).**

- **Interventions:** `after_tool_call` only allows `Proceed` or `Transform`, and neither ends the turn.
- **`request_state["stop_event_loop"]`:** it works, but only from inside a tool. For MCP
  tools it would require monkeypatching.

**Why.** Hooks are the supported extension point for this. Interventions are for gating
tool calls, not for controlling the loop.

**Also fixed: the process never exited.**

- The MCP client is now always stopped in a `finally` block, including when `start()`
  fails and on Ctrl-C.
- Exceptions still print a traceback and exit non-zero.
- Safety net: if non-daemon threads are still alive 2 s after the summary, the process logs
  their names and calls `os._exit`. The culprit thread could not be identified from the code
  alone, so the next live run should name it in an `[exit]` log line.

**Status.** Implemented and unit-tested (163 tests). Expected in the next live run: `LLM wall`
drops to a single model call, and fewer LLM timeouts.

---

## Bugs worth a paragraph

- **Silent retry storm.** When the server stopped, strands returned transport errors as
  `status: "error"` (no `isError`). The client only checked `isError`, so the "5 consecutive
  errors" guard never fired: ~14,500 `session not found` retries and a 44 MB log. Fix: treat
  either flag as an error. Lesson: test the *failure* shape of every dependency, not just success.
- **Crash on arrival.** Standing on the enemy flag, the server sends the route with
  `length: 0, first_step: null`. Only `tag` routes handled length 0, so the agent crashed on
  the winning move. Fix: length-0 `enemy_flag` → `grab_flag`, `own_base`/`defend_flag` → `wait`,
  plus a guard so an unmappable option becomes `wait` instead of killing the match.
- **Process never exits (open).** After `match over`, both runs hung in
  `wait_for_thread_shutdown`: Python waits forever for a non-daemon thread (likely the MCP
  client's background thread or an abandoned LLM call). Found with `sample <pid>`.
- **Two runs, one log (operational).** Two runs started in parallel both used
  `tee logs/decider-loop.log`. The second truncated the first one's log, so the first run's
  traceback at tick 52 was lost. Lesson: give each run its own log file, named with a timestamp.
- **Possible server bug (open).** An enemy standing on the neutral column (which counts as its
  field) on our path produced `min_enemy_distance: 0` but `risks: []`.

---

## Blog angles

1. *"Your agent is only as smart as the state it sees"* — the `observation: {}` story.
2. *"When the classifier copies your hint"* — who is really deciding?
3. *"Put navigation on the server"* — designing a fair benchmark for strategy.
4. *"Inform vs. enforce"* — giving memory to an agent without stealing its decisions.
5. *"Speed is a feature"* — warm-ups, time budgets and hard timeouts under a 5 s deadline.
6. *"IDs are not identities"* — the backwards-walking agent.
