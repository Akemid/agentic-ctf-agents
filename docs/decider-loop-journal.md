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

## 11. First win: 1–0 (2026-10-10)

**Match.** 200 ticks, deadline 5000 ms, decider warm-up 8.8 s (a cold start, paid before joining).

| Tick | Event |
|---|---|
| 16 | red-1 grabs the blue flag |
| 24 | blue-1 grabs our flag |
| 32 | **red-1 tags blue-1**, our first successful tag; blue-1 drops our flag and respawns |
| 35 | **red-1 scores** |

- **Zero deadline misses for us.** The opponent missed 6.
- The Decider handled every tick: 116 above the threshold, 84 through the budget path. The LLM
  never fit the 5 s budget, so the `end_turn` hook from entry 10 is still unverified live.
- **The process exited cleanly** with no `[exit]` line. Always stopping the MCP client in a
  `finally` block was enough, and the safety net never had to fire.

**New strategic stalemate (ticks 36–199).** After scoring, red-1 spent 164 ticks in 11
cells around the middle (x 4–8, row 7). The respawned bot holds the middle corridor:

- The direct route (7 steps) is either `BLOCKED (contention)` or carries 2 tag risks.
- The alternatives are 18–19-step detours that start by walking *backwards*.

Each tick the Decider flips between `direct`, `detour` and `safe` (110 / 41 / 13), so no
plan survives long enough to pay off. Plan memory shows the plan, but the Decider still
doesn't commit to an 18-step plan.

**Interpretation.** This is a strategy limit, not a bug, and exactly what the benchmark should
show. The options a better player has: commit to the long detour, or wait in its own
field for the bot to cross and tag it. The 2B Decider does neither.

---

## 12. First 1 vs 1: Strands harness vs. decider loop (2026-10-10)

**Setup.**

- One agent per team, no bots, 200 ticks, deadline 6000 ms.
- Red: `agent-harness.py` (`create_harness`, Haiku).
- Blue: `agent-decider-loop.py` (the Decider, with Haiku as fallback).

**Result: decider loop 4, harness 0.** The decider loop had four flag captures, each scored
about 16 ticks later.

| | Harness (red) | Decider loop (blue) |
|---|---|---|
| Score | 0 | 4 |
| Deadline misses | 4 (early ticks) | 0 |
| Tool calls / decisions | 15 tool calls in total | 201 decisions (161 Decider, 39 LLM) |
| Latency | — | Decider ~575 ms; LLM median 3.2 s, max 5.1 s |

**The surprising part: the harness stopped reasoning per tick.**

- After missing early deadlines ("los ticks expiran muy rápido"), the harness used its
  built-in `programmatic_tool_caller` to **write a script** that played the rest of the match.
- The script blindly followed `enemy_flag_detour` and oscillated between (3,4) and (4,4),
  then between (2,7) and (2,8), for most of the game. It never captured a flag.
- The harness's model also described the routes as "recomendadas" ("recommended"), although
  the prompt and the rules say they are not.
- So the harness made one strategic decision in the whole match, to hand play over to code,
  and the code it wrote had no strategy.

**`end_turn` hook validated (entry 10).** LLM ticks now take a median of 3.2 s, versus about
7.5 s per timed-out call before the hook. All 39 LLM ticks acted, with zero timeouts and zero
"no action" warnings.

**Benchmark-design decision.** `programmatic_tool_caller` **stays enabled**. It is part of
what the harness is, and the benchmark measures the harness as shipped. The plain-SDK agent
(`agent-harness-sdk.py`) does not have it, so a later match covers that case.

**Deeper analysis.**

- **The harness never left its own field.** Its maximum x was 4 (its field ends at x 6), and
  it spent 154 of 200 ticks on just two cells, (2,7) and (2,8), next to its own flag at (1,7).
  It defended by accident, never on purpose: it was within 2 cells of the invader in 72
  ticks, and the invader stood in red's field, so it was taggable. **The harness never
  tagged.** All 4 of its deadline misses came in ticks 2–5, before the script took over.
- **The harness's oscillation body-blocked the decider loop.** From tick 88 to about 115,
  blue-1 stood at (2,6) trying to move down into (2,7), while red's script kept moving into
  that same cell. The result was `move_blocked (contention)` for 30 of the 48 ticks between
  scores 2 and 3. The longest gap (48 ticks, versus about 22 for the other cycles) is
  entirely this jam.
- **The decider loop's weakness shows up again.** During that jam it chose
  `enemy_flag_direct` while the route was `BLOCKED (contention)` tick after tick, and took a
  detour only once in the whole match (134 `enemy_flag_direct`, 61 `own_base_direct`,
  4 `own_base_detour`, 1 `enemy_flag_detour`). Plan memory labels the blocked route as
  "continues your current path", which reinforces the insistence (the risk flagged in
  entry 6). It still won because the opponent was harmless, not because it handled the block.
- **The Haiku fallback** acted in all 39 of its ticks, always with `move`; it never chose
  `tag`, `wait` or `grab_flag`.

**What the match does and does not prove.**

- It proves that, at a 6 s deadline, an architecture that decides every tick in under a
  second beats a harness that answers latency by delegating to a one-shot script.
- It does not prove the Decider's strategy is good. Its opponent never left home and never
  tagged, so the Decider's known weaknesses (ignoring threats, insisting on blocked
  routes) were never punished.

---

## 13. Plain SDK agent vs. decider loop (2026-10-10, stopped at tick ~107)

**Setup.**

- One agent per team, deadline 6000 ms.
- Red: `agent-harness-sdk.py` (a plain Strands `Agent`, Haiku, no `programmatic_tool_caller`).
- Blue: the decider loop.
- The user stopped the server by force after ~100 ticks. Score: 0–0.

**The SDK agent was effectively frozen.**

- It had **100 `action_rejected reason=deadline` events in about 108 ticks** and sat on
  (4,8) for 101 ticks.
- It reasons in a single growing conversation (`wait_for_turn` → think → act, all in one
  `Agent` call), so every tick costs a full model turn over an ever-longer history. At a
  6 s deadline it almost never acts in time.
- Its own log is nearly empty: it was launched without `PYTHONUNBUFFERED=1` through
  `uv run … | tee`, so the buffered stdout was lost when it was killed. Next time, launch it
  unbuffered.

**The decider loop captured the flag at tick 16, then circled for 90 ticks without scoring.**

- Carrying the flag, it repeated a perfect 4-tick loop, (3,9) → (2,9) → (2,8) → (3,8) →
  (3,9), from about tick 20 until the stop.
- Every tick offered three routes home:
  - `own_base_direct`: 11–13 steps, 1–2 tag risks next to the frozen red-1 at (4,8).
  - `own_base_detour`: 13–15 steps, 1–2 tag risks.
  - `own_base_safe`: 13–15 steps, **zero risks**.
- The Decider alternated between `direct` (42) and `detour` (50) and **never chose
  `own_base_safe`**. Because each choice starts a new path, no plan ever continued. At most 2
  extra steps of safety would have scored.
- The "risk" was a frozen opponent that never acted, but the Decider could not know that;
  it read the risks and still picked a risky route.

**Fallback under load.** There were 60 LLM timeouts and only 6 completed LLM ticks (median
3.3 s), against 0 timeouts and a 3.2 s median in entry 12. The likely cause, not verified, is
Bedrock throttling: both agents were calling Haiku at the same time, and the SDK agent was
sending long prompts.

**Robustness check passed.** When the server was stopped, the decider loop logged 5
`session not found` errors and exited with `wait_for_turn kept failing`, instead of the
~14,500 retries from the earlier bug.

**What it shows.**

1. An LLM that reasons inside one long conversation cannot keep up with a 6 s tick; the
   architecture loses before strategy matters.
2. The Decider's weakness this time was not ignoring threats but **failing to trade length
   for safety**. A risk-free route, 2 steps longer, existed every tick.

---

## 14. Strands decider agent vs. decider loop (2026-10-10)

**Setup.**

- One agent per team, 50 ticks, deadline 5000 ms.
- Red: `agent-strand-decider.py`, a plain SDK `Agent` (Haiku) with a `before_tool_call`
  intervention. The Decider checks `args_grounded` / `stale_state` and `Guide`s bad calls.
- Blue: the decider loop.
- Both share **one Decider server and one cross-process lock**, an equal handicap for both.

**Result: decider loop 1, strands decider agent 0.**

- Blue grabbed at tick 14, scored at tick 26 and grabbed again at tick 40.

**The strands decider agent never moved.**

- It had **49 `action_rejected reason=deadline` in 50 ticks** and stayed on (0,7) the whole
  match.
- Its intervention reviewed 26 actions and proceeded with all of them (it guided 0 of 26).
  The gate was not the problem: by the time a reviewed action reached the server, the tick
  was gone.
- Each tick it pays for an LLM turn over a growing conversation, plus a Decider review that
  waits on the shared lock.
- At tick 14, blue grabbed red's flag from (1,7), right next to red-1 at (0,7). Red stood in
  its own field and could have tagged, but it was always one tick late.

**The agent's self-report was fiction.** Its final summary says "I tried to chase and tag
Blue-1 several times… Blue kept dodging my blocks." The game state shows red-1 never left
(0,7). The only true sentence is the one blaming deadlines and `stale_tick`.
**Lesson: never evaluate an agent by its own narrative; use the server's events.**

**Shared Decider cost.**

- Our wall time per Decider call had a median of 1450 ms, while the server-side latency
  median was 605 ms, so about 850 ms per tick went to waiting on the other agent's
  Decider calls.
- The warm-up took 22 s: a cold start plus waiting in the queue.
- With a 5 s deadline we still never missed a tick.

---

## Benchmark summary so far

| Match | Deadline | Winner | Loser's deadline misses | Why the loser lost |
|---|---|---|---|---|
| vs. bot (entry 11) | 5 s | decider loop 1–0 | — | stalemate after scoring; bot guarded the middle |
| vs. Strands harness (12) | 6 s | decider loop 4–0 | 4 | delegated play to a one-shot script that oscillated at home |
| vs. plain SDK agent (13) | 6 s | 0–0 (stopped at ~107) | 100 / ~108 | one growing conversation, too slow for the tick |
| vs. Strands decider agent (14) | 5 s | decider loop 1–0 | 49 / 50 | slow LLM turn plus a review gate on every action |

**Thesis supported by the data.** At sub-10 s ticks, **who owns the loop matters more than
how smart the model is.**

- If the agent owns the loop, every tick costs at least one full LLM turn, and the Decider
  can only add a check (entry 1). That never beat the clock here.
- If our code owns the loop, the Decider decides in under a second and the LLM is an
  optional fallback, bounded by a timeout.

**What is still unproven.** Whether a *fast-enough* LLM agent would out-strategise the
Decider. The Decider's known weaknesses (ignoring threats, insisting on blocked routes,
never trading length for safety) have not yet been punished by a competitor that acts in
time.

---

## 15. Refined agents: `agent/refined/` (2026-10-10)

**Decision.** Put the improvements in new files under `agent/refined/` and leave the
originals untouched, so every future match can compare the before and after directly.

**Changes, and the problem each one targets.**

| File | Change | Targets |
|---|---|---|
| `decider_loop.py` | **One Decider request, three questions:** `threat` (noul), `route_bad` (noul), `action` (choice). A pure policy, `combine_answers`, picks a tag or `defend_flag` on a threat, and a risk-free same-goal route when the chosen one is bad. | Ignored threats, insistence on blocked routes, never choosing `*_safe` (entries 5, 12, 13) |
| `decider_loop.py` | A blocked route is no longer labelled "continues your current path". It now reads "would continue your path but is BLOCKED now". | The plan memory reinforcing a blocked route (entry 12) |
| `harness_sdk.py`, `strand_decider.py` | **Bounded history:** `SlidingWindowConversationManager(window_size=12, per_turn=True, pin_first=1)`, about 3 ticks of tool calls with the instruction pinned. | Per-tick cost growing with the conversation (100/108 and 49/50 deadline misses) |
| same | **`StripStaticWallsHook`:** an `AfterToolCallEvent` hook that drops the static `walls` list from `wait_for_turn` results after the first turn. | Large tool results every tick |
| all LLM agents | **Act-fast prompt:** "call wait_for_turn, then immediately exactly one action; no text between tool calls; don't call get_rules again". | Long narration between tool calls |
| `strand_decider.py` | Decider review only for `tag` and `grab_flag`; `move` and `wait` proceed immediately. | One Decider round trip, plus a wait on the shared lock, on every move |

**Why the three-question design is cheap.** The client notes that the state is encoded once
and shared across all questions in a request. Two extra `noul` questions add almost no
latency, and they replace one broad choice with narrow, focused questions, which is what a
small classifier is good at.

**Fairness check.** None of these changes tells an LLM agent *which strategy* to use.

- The prompt change is about speed only, and the routes paragraph stays neutral.
- The Decider policy combines the Decider's own answers. The thresholds are explicit CLI
  flags (`--threat-threshold`).

**SDK facts verified (strands 1.59.0).**

- `window_size` counts messages, not tokens.
- `per_turn=True` is required, because by default trimming runs only at the end of an agent
  call, and a whole match is a single call.
- `AfterToolCallEvent.result` is writable, and the executor uses the rewritten value.
- `create_harness` has its own `context_manager`, which takes precedence over a
  conversation manager, so the refined harness only gets the prompt change.

**Status.** 200 tests pass (37 new). Not yet validated in a live match.

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
