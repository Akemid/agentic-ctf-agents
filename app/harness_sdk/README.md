# harness_sdk runtime

One CTF character played by a plain Strands `Agent` with a sliding-window conversation manager.
Ported from `local/agent/refined/harness_sdk.py`.

| File | Purpose |
| --- | --- |
| `main.py` | Thin AgentCore entrypoint: validate payload, then start / status / stop. |
| `sdk_agent.py` | `play()` (the agent loop) and `build_runner()` (wiring). |
| `ctf_common/` | **Generated.** Vendored copy of `shared/ctf_common`. Do not edit; run `python scripts/sync_common.py`. |

## Invocation

```json
{"action": "start", "match_id": "m1", "name": "p1", "team": "auto", "model": "haiku", "options": {"window": 12}}
{"action": "status"}
{"action": "stop"}
```

`start` answers after `join_match` (up to 15 s): `started` carries the `team` and `agent_index` the server assigned (also as `slot`, the lobby's key, same value), a refused
seat is `rejected` with `join_match failed: <reason>`, and the match then runs in the background. An optional `agent_index` (0..2, like `join_match`; `slot` is an equivalent key and the web lobby sends it)
needs an explicit `team` and is never traded for another one. `status` and `stop` must use the same AgentCore session id
as `start`. Caller-supplied message history is rejected. See `openspec/changes/agentcore-ctf-fleet/lobby-wake.md`.

`stop` takes effect after the in-flight tool call returns. That is bounded by the `wait_for_turn` long-poll
(about 25 s server cap), so `status` can report `stopping` for up to that long. `StopRuntimeSession` is the
hard stop.

## Environment

| Variable | Description |
| --- | --- |
| `CTF_MCP_URL_PARAM` | SSM parameter NAME holding the MCP URL (default `/agentic-ctf/mcp-url`). |
| `CTF_OAUTH_PROVIDER` | AgentCore Identity OAuth credential provider name. |
| `CTF_OAUTH_SCOPES` | Space or comma separated scopes (default `agentic-ctf/mcp`). |
| `CTF_MCP_URL` | Local override that skips SSM. |

## Tests

From the repository root: `uv run --with pytest pytest`.
