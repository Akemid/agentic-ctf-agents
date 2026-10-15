# Contract v2: adapting the agents to the game server's new MCP turn contract

Date: 2026-10-15. Branch: `feat/mcp-turn-contract-v2`. Source of truth: the game server repo
(`agentic-ctf`, `packages/server/src/mcp/tools.ts`, `docs/mcp-agents.md`). This note does not edit the archived
proposal, specs, design or tasks; it records what changed after them.

## What the server changed

- New tool `get_map` (no arguments, joined sessions only, tool error `{reason: "not_joined"}` otherwise). It returns
  `{grid, walls, fields, flag_bases, ascii, legend}`. `join_match` now returns `{agent_id, team, agent_index, ticks,
  deadline_ms, grid, min_tick_ms, map}` where `map` is the same object.
- A `wait_for_turn` turn no longer carries `grid`, `walls`, `fields` or `flags[].base` in `state`. It gains a top-level
  `last_action` (`null` on the first turn, else `{action, tick, result, reason?}`).
- `routes[].min_enemy_distance` is gone; `routes[].closest_threat` is `{enemy_id, step, distance} | null` (BFS distance,
  only enemies that can tag).
- `match_over` is `{status, score, winner, reason}`; `reason` is `completed` or `stopped`, and `winner` is `null` when
  stopped. Action acks can be rejected with `match_over`, `match_not_started` and `not_joined`. Joining a finished match is
  the tool error `match_finished`.

## Why it mattered

Nothing failed loudly. The decider loop kept running with no field information (it never knew it was in its own field, no
adjacent `tag_` fallback) and without the distance hint, and the LLM prompts named a route field that no longer exists. The
fake game server in the tests still spoke the old contract, so the suite stayed green: a fake that encodes an outdated
contract makes every other test pass while the real agents degrade silently.

## What changed

| Area | Change |
|------|--------|
| `tests/fake_mcp.py` | `FakeGame` mirrors v2 (`get_map`, `join_match.map`, no static keys, `last_action`, `closest_threat`, `match_over.reason`, accepted/rejected acks, `match_finished`) and refuses state from the old contract. `make_route()` builds v2 routes. |
| `tests/test_fake_mcp_contract.py` (new) | Pins the v2 shapes independently of the fake. |
| `app/decider_loop/loop_logic.py` | `MapInfo` + `parse_map`; `render_state`, `build_options`, `_in_my_field` take the map; `describe_route` renders `closest_threat` (nothing when it is null); `describe_last_action`, `describe_match_over`, `action_rejection`; `render_state(include_last_action=...)`; new `warmup_question`. |
| `app/decider_loop/loop.py` | `join()` returns `Joined(team, map_info)`; `fetch_map()` falls back to one `get_map`; logs `match_over.reason` / `winner: null` and rejected acks. |
| `shared/ctf_common/prompts.py` | `closest_threat` guidance, where the map comes from (`join_match.map` / `get_map`), `last_action`. Vendored copies regenerated with `scripts/sync_common.py`. |
| `shared/ctf_common/hooks.py`, `strands_match.py` | `StripStaticWallsHook` removed (a no-op now) together with its wiring and tests. |
| `local/agent/refined/*`, `local/agent/agent-*.py`, `local/tests/*` | Same fixes in lockstep (Python 3.10 compatible); the differential tests in `tests/test_loop_logic.py` compare both sides on the new state shape. |
| `infra/decider/adapter/adapter/warmup_body.json` | Regenerated from `warmup_question()` (a drift test guards it). The decider image must be rebuilt for the warm-up to use it. |

## Decision: the decider text changes only for `closest_threat`

The decider is a trained model and the new wording has not been evaluated, so its input text changes only where the
contract forces it:

- `closest_threat` is rendered (`closest threat <id> at step N, distance D`) because `min_enemy_distance` is gone.
- A null or absent `closest_threat` renders no clause at all (never "no threat"), which restores the old behavior for
  missing data.
- The server's `last_action` is fallback-only: `render_state(..., include_last_action=True)` adds it to the text given
  to the LLM fallback, labelled `outcome of your previous action: ...` so it does not clash with the plan line's
  `last action:`. The decider question, the warm-up state and `/v1/systemone` state never include it. Re-evaluate
  adding it to the decider text after the S4 benchmark.

## Decision: no map pinning for the Strands runtimes

The Strands runtimes keep 12 messages with `pin_first=1`, so the `join_match` result (with the map) leaves the context
after a few ticks. Options were (a) rely on `get_map` and say so in the system prompt, or (b) a hook that keeps a map
summary in the pinned first message. We chose (a): the system prompt is never windowed, the server routes already encode
walls and fields, and a re-pinning hook would mutate the history for data the model rarely needs. The cost is one extra
model round trip inside the tick when the model decides it needs the layout. Revisit if traces show the model guessing
positions or calling `get_map` on most ticks.

## Not changed on purpose

- `Plan.last_action` (the loop's own executed-action note) stays; the server's `last_action` is rendered (LLM fallback
  only), never used to decide. Replacing it would change plan continuity, which the differential tests pin.
- `agentcore/aws-targets.json` and the archived openspec artifacts.
