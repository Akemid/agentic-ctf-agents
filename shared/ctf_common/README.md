# ctf_common

Code shared by the four CTF agent runtimes. This directory is the **source of truth**.
`python scripts/sync_common.py` copies it into `app/<runtime>/ctf_common/` (committed, generated: never edit those).
`python scripts/sync_common.py --check` (and `tests/test_vendor_sync.py`) fail on drift.

| Module | Responsibility |
| --- | --- |
| `payload.py` | Validate invocation payloads (`parse_payload`). Rejects caller-supplied history and unknown keys. |
| `runner.py` | `MatchRunner`: one match per process, async `start` / `status` / `stop`, preflight, `handle_invocation`. |
| `config.py` | `load_config`: SSM parameters read at preflight; `CTF_MCP_URL` / `CTF_DECIDER_URL` local overrides. |
| `auth.py` | `TokenProvider` implementations. One `get_token()` call is one AgentCore Identity fetch (no cache). |
| `mcp.py` | `open_mcp_client`: fetches the token when a connection opens. `raise_on_auth_failure` maps 401 to an error. |
| `prompts.py` | System prompt, instruction builder, model aliases. |
| `hooks.py` | Strands hooks (static-wall stripping, stop request, tick counter) and the sliding-window manager. |

## Rules

- Edit here, run the sync script, commit both. Tests import from `shared/`, never from a vendored copy.
- No runtime-specific code in this package. A runtime provides its own `PlayFn` (see `app/harness_sdk/sdk_agent.py`).
- Tokens are secrets: never log them, never put them in error messages.

## Stop semantics

`stop` is cooperative. It sets the stop flag and the agent loop ends once the tool call that is already in
flight returns (no further game action is sent). That call is usually `wait_for_turn`, a long-poll the server
caps at about 25 s, so a stop can take up to ~25 s to reach `stopped`. `StopRuntimeSession` is the hard stop.
A `stop` that arrives while `start` is still in its preflight ends in `stopped` without registering a task.

## Tests

From the repository root: `uv run --with pytest pytest`.
