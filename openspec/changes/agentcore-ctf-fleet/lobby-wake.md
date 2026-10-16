# Lobby wake: waking agents from the web lobby

Date: 2026-10-15. Branch: `feat/lobby-agent-wake`. Issue: Akemid/agentic-ctf-agents#8. This note does not edit the
archived proposal, specs, design or tasks; it records what changed after them (same convention as `contract-v2.md`).

## The contract (agreed in the issue)

- Runtime ARNs are published to SSM as plain String parameters `/ctfagents/runtimes/<runtime>/arn` for `harness`,
  `harness_sdk`, `strand_decider` and `decider_loop` (value: the runtime ARN, default endpoint). No exports.
- The start payload is `{"action":"start","match_id","name","team","agent_index"?,"model"}`. `agent_index` is an integer
  0..2 (0-based, the same name and range as `join_match`, so the game-server team needs no translation layer; issue #8,
  comment of 23:20). `slot` is an equivalent, fully supported key: the game server's web lobby always sends `slot`
  (agentic-ctf PRs #73-#77). Both need an explicit `team` (`red|blue`; `team:"auto"` plus either is `rejected`), have the
  same validation, and if both are sent with different values the start is `rejected`. Internally it is `agent_index`,
  passed to the server unchanged (`JoinArgs` is `.strict()`, a `slot` argument would be `invalid_arguments`). The
  character name stays human-facing 1-based: `<runtime-with-dashes>-<team>-<agent_index+1>`.
- `match_id`, `name`, `team` and `agent_index` are FORCED on the `join_match` call in all four runtimes.
- `start` is synchronous up to the seat. Answers: `started` (+ `match_id`, `name`, `team`, `agent_index`, the seat the server
  assigned; the same value is also sent as `slot`, which the lobby reads), `already_running` (unchanged), `rejected` + `error: "join_match failed: <reason>"` (`slot_taken`,
  `match_full`, `match_already_started`, `match_finished`, `unauthorized`), `failed` + `error` (preflight, connection, join
  timeout). A requested seat is never traded for another one.
- Caller IAM: `InvokeAgentRuntime` AND `InvokeAgentRuntimeForUser` (needed when `runtimeUserId` is sent) plus
  `StopRuntimeSession`, on `runtime/<id>` and `runtime/<id>/runtime-endpoint/*`.

## The synchronous join, and why it lives on the match thread

The server binds a seat to ONE MCP session, and one session is one join. So the connection that joins must be the
connection the match keeps playing on. Two designs were possible:

1. Preflight opens the MCP client, joins, and hands the open client to the thread. The strands `MCPClient` owns a
   background event loop and is a context manager; moving an entered client across threads and closing it on every path
   (join failure, start failure, stop during preflight, a thread that never starts) puts connection ownership in two
   places.
2. The match thread opens the connection and joins, and `start` waits for the outcome. This is what is implemented.

The thread owns the connection from the first byte to the last (`with client:` in the play function). `start` only waits
on a `JoinSignal` (`shared/ctf_common/join.py`): a first-writer-wins one-shot with four outcomes, `joined` (the REAL team
and `agent_index`), `refused` (the server's `reason`), `error` (could not ask: connection, token, timeout, malformed
result) and `cancelled` (an operator `stop`). `MatchContext.join` carries it into every play function.

How the runner (`shared/ctf_common/runner.py`) uses it:

- Preflight still runs without the lock, the double-start guard is unchanged (`already_running` during the join too), and
  `status`/`stop` never wait. The internal state stays `STARTING` (reported `running`) until the join answers.
- After the thread starts, `start` waits (bounded, lock NOT held): `joined` -> `RUNNING` and `started`; `refused` -> the runner is
  marked FAILED at once and `start` answers `rejected` (no wait for the unwinding thread); `error` -> FAILED at once and
  `failed`. Every start bumps a run generation: a thread of an older run (still unwinding after a refusal) may only write
  state while its generation is current, so `status` never shows a false `running`, an immediate retry on another seat
  works, and a late thread cannot overwrite the new run. If the play thread raises after the join was marked an error
  (for example `McpAuthError`/401 after `connection_lost`), its exception is the final `error`; the join reason is used
  only when there is no exception; `stop` during the join -> `cancelled`, the response is the stop state.
- Timeout (15 s; `harness` 30 s): `start` sets the stop event, the runner stays `STOPPING` until the thread exits (a restart can
  never overlap a thread that might still hold a seat), and the answer is `failed: join_match timed out after 15s`. A join
  that lands late is ignored (the signal is already resolved).
- A timed-out join with a STUCK thread: the session stays HealthyBusy until the thread exits. The MCP HTTP read timeout
  (about 300 s) bounds it, and `StopRuntimeSession` is the forced recovery. A timeout `failed` means retry with a NEW
  session id (the old runner is STOPPING, so the same session answers `rejected`).
- A play function that returns or raises without reporting is a failed join (`the match ended before join_match
  completed`), so `start` never waits the full timeout for a thread that is already gone.
- `add_async_task` / `complete_async_task` balance is unchanged: the task is added before the thread, completed in the
  thread's `finally`, and on the two start-failure paths. Tokens still equal connections (one token per MCP connection).

## Who joins: code or the model

| Runtime | Join | Why |
|---|---|---|
| `harness_sdk`, `strand_decider` (`strands_match.py`) | code, before the model runs | deterministic; the model cannot pick a seat or be too slow for the timeout. The join result (with the static `map`) goes into the first message. |
| `decider_loop` | code (`loop.join`) before the decider warm-up | already code-owned; the join comes BEFORE the warm-up so a cold decider endpoint cannot hold `start`. |
| `harness` | the model's first tool call, arguments forced by a hook | `create_harness` builds and owns the MCP client from `mcp_servers`; joining in code would need a second connection (a second session, a second seat) or a rewiring of the harness we cannot verify offline. |

`ForceJoinArgsHook` (`shared/ctf_common/hooks.py`) is on all three LLM runtimes. On `BeforeToolCallEvent` it rewrites
`tool_use["input"]` to exactly `{match_id, name, team[, agent_index]}` (a Strands hook can mutate it; the executor re-reads
it, verified on strands 1.61 and by a test with a real `Agent`). On `AfterToolCallEvent` it reports the result to the
signal; a refusal ends the turn (`AfterToolsEvent.end_turn`) so the model cannot retry into another seat. A second `join_match` that the server answers `already_joined` (parallel duplicates in one batch) is a no-op in the hook: the call that seated us reports. With `team:"auto"`, if red answered `match_full` and the turn ended without a second join, that refusal is reported (`rejected: join_match failed: match_full`). In the
code-joined runtimes it cancels any model-initiated `join_match` (the server would answer `already_joined`, which must not
fail a running match). With `team:"auto"` the hook offers red, then blue after a `match_full`, and nothing else.

## What did not change in `local/`

The local scripts have no runner and no lobby. The differential and prompt-equality tests keep comparing `SYSTEM_PROMPT`
and the pure decider logic, which did not change; only `build_instruction` (not compared for seats) and the join call
site in `app/decider_loop/loop.py` changed.

## Operational notes

- A `start` blocks until the seat is decided (up to 15 s, 30 s for `harness`). The caller must allow for that.
- While a character waits in the lobby the LLM runtimes make a model call about every 25 s (`wait_for_turn` returns
  `pending`); `decider_loop` waits for free. Session survival beyond 15 minutes of lobby wait relies on HealthyBusy (we hold
  an async task); not verified live.
- Not verified live: the whole path against the real server and AgentCore (offline tests use a fake that mirrors the
  server's tool-error shape and session-bound seats).
