# strand_decider runtime

One CTF character played by a plain Strands Agent. `tag` and `grab_flag` calls are reviewed by the SageMaker decider (two yes/no questions); `move` and `wait` proceed immediately. Ported from `local/agent/refined/strand_decider.py`.

| File | Purpose |
| --- | --- |
| `strand_agent.py` | `play()` (Strands match loop plus the reviewer) and `build_runner()`. |
| `gate.py` | `ToolCallReviewer`: the intervention handler that guides or lets a gated call through. |
| `main.py` | Thin AgentCore entrypoint: validate payload, then start / status / stop. |
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
as `start`. Caller-supplied message history is rejected. See `openspec/changes/archive/2026-10-16-agentcore-ctf-fleet/lobby-wake.md`.

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
| `CTF_DECIDER_ENDPOINT_PARAM` | SSM parameter NAME holding the SageMaker decider endpoint name (`/ctf-decider/endpoint-name`). |
| `CTF_DECIDER_URL` | Local override: a `strands-decider serve` URL. Skips SageMaker and SSM for the decider. |

## Tests

From the repository root: `uv run --with pytest pytest`.
