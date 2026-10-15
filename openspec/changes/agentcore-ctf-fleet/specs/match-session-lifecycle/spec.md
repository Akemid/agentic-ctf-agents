# Delta Spec: match-session-lifecycle

## ADDED Requirements

### Requirement: Payload validation
Every runtime entrypoint MUST validate the invocation payload before any model or MCP work. Required field: `action` (one of `start`, `status`, `stop`; default `start` when absent). For `start`: `match_id` is required and MUST match `^[A-Za-z0-9_-]{1,64}$`. Optional fields: `name` (sanitized to `^[a-z0-9_-]{1,32}$`; default `<runtime>-<last 6 chars of session id>`), `team` (`red`, `blue` or `auto`; default `auto`), `model` (`haiku`, `opus` or `nova-lite`; default `haiku`) and `options` (an object whose keys among `window`, `threshold`, `threat_threshold`, `llm_budget_ms`, `safety_margin_ms` are validated per runtime). A payload of the form `{"prompt": "<json string>"}` MUST be unwrapped first and then validated as above. Optional text prompt fields MUST be strings. The payload MUST NOT carry a caller-supplied message history; any `messages`, `tool_results`, `history` or equivalent key MUST be rejected, and unknown top-level keys MUST be rejected.

#### Scenario: Valid start payload
- GIVEN a runtime with no active match on the session
- WHEN invoked with `{"action":"start","match_id":"m1","name":"p1","team":"auto"}`
- THEN the entrypoint accepts it and returns a started acknowledgement

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
- WHEN the payload contains a key outside `action, match_id, team, name, model, options`
- THEN it is rejected with a validation error naming the key

#### Scenario: Prompt-wrapped payload
- GIVEN any runtime
- WHEN invoked with `{"prompt": "{\"action\":\"status\"}"}`
- THEN the inner JSON is unwrapped and handled as a `status` request

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

### Requirement: Async start, status and stop
`start` MUST return immediately after registering the match work as an async task (`add_async_task`); the match runs in the background. Progress MUST be logged to CloudWatch. `status` and `stop` MUST be invoked on the same session id as `start` and MUST report on or cancel that session's task. There MUST be no streaming response.

#### Scenario: Start returns promptly
- GIVEN a valid start payload
- WHEN the runtime handles it
- THEN the response returns before the match ends and the session reports busy via ping status

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

### Requirement: Double-start guard
A session with a running match MUST NOT start a second match. A `start` with the same `(match_id, name)` MUST return `already_running`; a `start` with a different `(match_id, name)` MUST return `rejected`. In both cases the running match is unaffected. The full set of response statuses is `started`, `already_running`, `running`, `finished`, `failed`, `stopping`, `stopped`, `idle`, `rejected`.

#### Scenario: Second start rejected
- GIVEN a running match on session S
- WHEN `start` is invoked again on S
- THEN it returns `already_running` (same `match_id` and `name`) or `rejected` (a different pair) and the running match is unaffected

#### Scenario: Restart after finish
- GIVEN the match on S finished or was stopped
- WHEN `start` is invoked on S
- THEN it is accepted
