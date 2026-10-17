# Match Session Lifecycle Specification

## Purpose

Entrypoint contract of the four AgentCore runtimes: payload validation, one invocation per character, the
synchronous start up to the seat, status and stop on the same session, the double-start guard, and the code- or
model-owned seat join. Merged from change `agentcore-ctf-fleet` on 2026-10-16. Amendments from `lobby-wake.md`
(seat fields, synchronous start, join ownership) are merged in.

## Requirements

### Requirement: Payload validation
Every runtime entrypoint MUST validate the invocation payload before any model or MCP work. Required field: `action` (one of `start`, `status`, `stop`; default `start` when absent). For `start`: `match_id` is required and MUST match `^[A-Za-z0-9_-]{1,64}$`. Optional fields: `name` (sanitized to `^[a-z0-9_-]{1,32}$`; default `<runtime>-<last 6 chars of session id>`), `team` (`red`, `blue` or `auto`; default `auto`), `model` (`haiku`, `opus` or `nova-lite`; default `haiku`), `options` (an object whose keys among `window`, `threshold`, `threat_threshold`, `llm_budget_ms`, `safety_margin_ms` are validated per runtime), and the seat fields defined in Seat fields. A payload of the form `{"prompt": "<json string>"}` MUST be unwrapped first and then validated as above. Optional text prompt fields MUST be strings. The payload MUST NOT carry a caller-supplied message history; any `messages`, `tool_results`, `history` or equivalent key MUST be rejected, and unknown top-level keys MUST be rejected.

#### Scenario: Valid start payload
- GIVEN a runtime with no active match on the session
- WHEN invoked with `{"action":"start","match_id":"m1","name":"p1","team":"auto"}`
- THEN the entrypoint accepts it and returns a started acknowledgement once the join is decided

#### Scenario: Missing required field
- GIVEN any runtime
- WHEN invoked with a `start` payload lacking `match_id`
- THEN it returns a validation error naming the field and starts no work

#### Scenario: Non-string prompt
- GIVEN any runtime
- WHEN the payload has a prompt field that is not a string
- THEN it is rejected with a validation error

#### Scenario: Caller-supplied history rejected
- GIVEN any runtime
- WHEN the payload contains `messages` or `history`
- THEN it is rejected and the agent never receives that content

#### Scenario: Unknown action
- GIVEN any runtime
- WHEN `action` is `"restart"`
- THEN it is rejected with a validation error

#### Scenario: Unknown key rejected
- GIVEN any runtime
- WHEN the payload contains a key outside `action, match_id, team, name, model, options, agent_index, slot`
- THEN it is rejected with a validation error naming the key

#### Scenario: Prompt-wrapped payload
- GIVEN any runtime
- WHEN invoked with `{"prompt": "{\"action\":\"status\"}"}`
- THEN the inner JSON is unwrapped and handled as a `status` request

### Requirement: Seat fields
`agent_index` (integer 0..2, 0-based, the same name and range as `join_match`) and `slot` are equivalent keys for the seat. `slot` is the key the web lobby always sends. Both are fully supported and validated the same way. A seat request MUST carry an explicit `team` of `red` or `blue`; `team` `auto` together with either key is `rejected`. If both keys are sent with different values, the start is `rejected`. The seat is passed to the server unchanged as `agent_index`; `slot` is never forwarded to `join_match`, whose arguments are strict. `match_id`, `name`, `team` and `agent_index` are forced on the `join_match` call in all four runtimes. The character name shown to humans is 1-based: `<runtime-with-dashes>-<team>-<agent_index+1>`.

#### Scenario: Slot and agent_index agree
- GIVEN a start with `team` `red` and `slot` 1 and `agent_index` 1
- WHEN the entrypoint validates it
- THEN it is accepted and the seat requested is 1

#### Scenario: Seat with auto team
- GIVEN a start with `team` `auto` and `slot` 0
- WHEN validated
- THEN it is `rejected` and no join is attempted

#### Scenario: Disagreeing seat keys
- GIVEN a start with `slot` 0 and `agent_index` 1
- WHEN validated
- THEN it is `rejected` and no join is attempted

### Requirement: One invocation equals one character
Each `start` invocation MUST play exactly one character (one `name`) in exactly one AgentCore session. Payloads naming multiple characters MUST be rejected.

#### Scenario: Multiple characters rejected
- GIVEN any runtime
- WHEN `name` is a list of two names
- THEN it is rejected with a validation error

#### Scenario: Three characters need three invocations
- GIVEN the launcher is asked for 3 characters of one agent type
- WHEN it dispatches
- THEN it issues 3 invocations with 3 distinct session ids

### Requirement: Synchronous start up to the seat
`start` MUST wait for the outcome of `join_match`, bounded at 15 s (30 s for `harness`), and MUST answer only once the seat is decided. Answers:
- `started`, with `match_id`, `name`, `team`, `agent_index` (the seat the server assigned) and `slot` (the same value);
- `already_running`, unchanged (see Double-start guard);
- `rejected`, with `error: "join_match failed: <reason>"`, where the reason is `slot_taken`, `match_full`, `match_already_started`, `match_finished` or `unauthorized`;
- `failed`, with `error`, for preflight, connection, join timeout, or a malformed join result.

A requested seat is never traded for another one. After a successful join the match runs in the background as an async task (`add_async_task`) and progress is logged to CloudWatch. There MUST be no streaming response.

#### Scenario: Seat joined
- GIVEN a free seat 1 and a valid start payload
- WHEN `start` is handled
- THEN it returns `started` with `agent_index` 1 and `slot` 1, before the match ends

#### Scenario: Seat refused
- GIVEN the server refuses with `slot_taken`
- WHEN `start` is handled
- THEN it returns `rejected` with `join_match failed: slot_taken`, no other seat is taken, and the runner is not running

#### Scenario: Join timeout
- GIVEN the join is not answered within 15 s
- WHEN `start` is handled
- THEN the stop event is set, the runner stays `stopping` until the thread exits, and `start` returns `failed` with `join_match timed out after 15s`; a retry MUST use a new session id

#### Scenario: Retry after refusal
- GIVEN a session whose last start was refused
- WHEN `start` is invoked on the same session with another free seat
- THEN it is accepted

### Requirement: Async start, status and stop
`status` and `stop` MUST be invoked on the same session id as `start` and MUST report on or cancel that session's task. There MUST be no streaming response.

#### Scenario: Status on same session
- GIVEN a started match on session S
- WHEN `status` is invoked on S
- THEN it returns the state (`running`, `finished`, `stopping`, `stopped` or `failed`) with `match_id`, `name`, `team`, `started_at` and `ticks`, and `error` when failed

#### Scenario: Stop cancels the task
- GIVEN a running match on session S
- WHEN `stop` is invoked on S
- THEN the state becomes `stopping` and the response reports `stopping`; once the task is cancelled and the MCP connection closed, `status` reports `stopped`

#### Scenario: Status without a match
- GIVEN a fresh session
- WHEN `status` is invoked
- THEN it returns state `idle`, not an error

### Requirement: Seat join ownership
The match thread owns the MCP connection from open to close, and the connection that joins is the connection that plays, because the server binds a seat to one MCP session. Ownership per runtime:
- `harness_sdk` and `strand_decider`: code joins before the model runs; the join result (with the static `map`) goes into the first message.
- `decider_loop`: code joins before the decider warm-up, so a cold endpoint cannot hold `start`.
- `harness`: the join is the model's first tool call. `ForceJoinArgsHook` rewrites its arguments to exactly `{match_id, name, team[, agent_index]}`. A refusal ends the turn so the model cannot retry into another seat. With `team` `auto` the hook offers red, then blue only after `match_full`.

In the code-joined runtimes a model-initiated `join_match` is cancelled. A play function that ends without reporting a join outcome is a failed join (`the match ended before join_match completed`), so `start` does not wait for the full timeout.

#### Scenario: Model-initiated join in a code-joined runtime
- GIVEN `harness_sdk` has already joined in code
- WHEN the model issues `join_match`
- THEN the call is cancelled and does not reach the server

#### Scenario: Play thread ends before joining
- GIVEN the play function returns without reporting a join outcome
- WHEN `start` is waiting
- THEN `start` returns `failed` with `the match ended before join_match completed` without waiting for the timeout

### Requirement: Double-start guard
A session with a running match MUST NOT start a second match. A `start` with the same `(match_id, name)` MUST return `already_running`; a `start` with a different `(match_id, name)` MUST return `rejected`. In both cases the running match is unaffected. A session whose last start was `failed` or `rejected` is not running and MUST accept a new start. The full set of response statuses is `started`, `already_running`, `running`, `finished`, `failed`, `stopping`, `stopped`, `idle`, `rejected`.

#### Scenario: Second start rejected
- GIVEN a running match on session S
- WHEN `start` is invoked again on S
- THEN it returns `already_running` (same `match_id` and `name`) or `rejected` (a different pair) and the running match is unaffected

#### Scenario: Preflight does not block status or stop
- GIVEN a `start` whose preflight (SSM, token) is still in progress
- WHEN `status` or `stop` is invoked on S
- THEN each returns promptly (`running` and `stopping` respectively; there is no separate `starting` status), a second `start` follows the guard above, and a `stop` ends in `stopped` without registering an async task

#### Scenario: Restart after finish
- GIVEN the match on S finished or was stopped
- WHEN `start` is invoked on S
- THEN it is accepted
