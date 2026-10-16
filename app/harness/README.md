# harness runtime

One CTF character played by a `strands_harness` agent. Ported from `local/agent/refined/harness.py`.

| File | Purpose |
| --- | --- |
| `harness_agent.py` | `play()` (builds the harness with the bearer header, one token per build) and `build_runner()`. |
| `main.py` | Thin AgentCore entrypoint: validate payload, then start / status / stop. |
| `ctf_common/` | **Generated.** Vendored copy of `shared/ctf_common`. Do not edit; run `python scripts/sync_common.py`. |

## Invocation

```json
{"action": "start", "match_id": "m1", "name": "p1", "team": "auto", "model": "haiku", "options": {}}
{"action": "status"}
{"action": "stop"}
```

`start` answers after `join_match` (up to 30 s): `started` carries the `team` and `agent_index` the server assigned (also as `slot`, the lobby's key, same value), a refused
seat is `rejected` with `join_match failed: <reason>`, and the match then runs in the background. An optional `agent_index` (0..2, like `join_match`; `slot` is an equivalent key and the web lobby sends it)
needs an explicit `team` and is never traded for another one. `status` and `stop` must use the same AgentCore session id
as `start`. Caller-supplied message history is rejected. See `openspec/changes/agentcore-ctf-fleet/lobby-wake.md`.

`stop` takes effect at the next tick (or after the in-flight tool call returns, bounded by the
`wait_for_turn` long-poll of about 25 s), so `status` can report `stopping` for up to that long.
`StopRuntimeSession` is the hard stop.

## Environment

| Variable | Description |
| --- | --- |
| `CTF_MCP_URL_PARAM` | SSM parameter NAME holding the MCP URL (default `/agentic-ctf/mcp-url`). |
| `CTF_OAUTH_PROVIDER` | AgentCore Identity OAuth credential provider name. |
| `CTF_OAUTH_SCOPES` | Space or comma separated scopes (default `agentic-ctf/mcp`). |
| `CTF_MCP_URL` | Local override that skips SSM. |

## Tests

From the repository root: `uv run --with pytest pytest`.

The harness has its own context manager, so there is no `window` option. Session and memory files live under
`CTF_AGENT_STATE_DIR` (default `/tmp/.agent`). If the MCP server is unreachable the match fails; the harness is
configured with `continue_on_error: false` so it never plays without game tools.
