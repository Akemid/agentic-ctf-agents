# decider_loop runtime

One CTF character driven by plain Python: `wait_for_turn`, one decider request with three questions, then the action tool. The LLM only runs for a tick when the decider is not confident or cannot answer in time. Ported from `local/agent/refined/decider_loop.py`.

| File | Purpose |
| --- | --- |
| `loop.py` | `play()` (the loop), the LLM fallback and `build_runner()`. |
| `loop_logic.py` | Pure decision logic, characterization-tested against the local original. |
| `main.py` | Thin AgentCore entrypoint: validate payload, then start / status / stop. |
| `ctf_common/` | **Generated.** Vendored copy of `shared/ctf_common`. Do not edit; run `python scripts/sync_common.py`. |

## Invocation

```json
{"action": "start", "match_id": "m1", "name": "p1", "team": "auto", "model": "haiku", "options": {"threshold": 0.85, "threat_threshold": 0.5, "llm_budget_ms": 4500, "safety_margin_ms": 300}}
{"action": "status"}
{"action": "stop"}
```

`start` answers after `join_match` (up to 15 s): `started` carries the `team` and `agent_index` the server assigned (also as `slot`, the lobby's key, same value), a refused
seat is `rejected` with `join_match failed: <reason>`, and the match then runs in the background. An optional `agent_index` (0..2, like `join_match`; `slot` is an equivalent key and the web lobby sends it)
needs an explicit `team` and is never traded for another one. `status` and `stop` must use the same AgentCore session id
as `start`. Caller-supplied message history is rejected. See `docs/lobby-wake.md`.

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

## Decider failure policy

Each tick asks the decider with the time left in the tick as the deadline. If the call is skipped (under 400 ms
left), fails, or the circuit breaker is open, THAT tick goes to the LLM and the match continues. There is no
permanent "decider down" state: the breaker opens after 3 consecutive failures for 30 s, then lets one probe
through (a misconfiguration such as AccessDenied opens it for 300 s).
