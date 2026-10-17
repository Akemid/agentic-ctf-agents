# CTF Agent Runtimes Specification

## Purpose

The four CTF player agents deployed as AgentCore CodeZip runtimes: `harness`, `harness_sdk`, `strand_decider` and
`decider_loop`. Covers their count and isolation, lifecycle, dependencies, vendored shared code, behavior parity,
configuration read at preflight, the game contract v2, and identity. Merged from change `agentcore-ctf-fleet` on
2026-10-16. Amendments from `contract-v2.md` (game contract), `decider-optional.md` (optional decider parameter),
`lobby-wake.md` and the slice 4 review (credential isolation is by convention only) are merged in.

## Requirements

### Requirement: Four runtimes, one per agent
`agentcore.json` MUST declare four independent runtimes: `harness`, `harness_sdk`, `strand_decider`, `decider_loop`, each with its own `codeLocation`, ARN and IAM role. Names MUST be CloudFormation-safe.

#### Scenario: Config validates
- GIVEN the project
- WHEN `agentcore validate` runs
- THEN it succeeds with four runtimes and four OAuth credentials

#### Scenario: Isolated roles
- GIVEN the deployed stack
- WHEN IAM is inspected
- THEN each runtime has its own role and only `strand_decider` and `decider_loop` can call `sagemaker:InvokeEndpoint`, restricted to the decider endpoint ARN
- AND every runtime role can read only SSM parameter `/agentic-ctf/mcp-url`, and only `strand_decider` and `decider_loop` can additionally read exactly `/ctf-decider/endpoint-name`

### Requirement: Runtime lifecycle configuration
All four runtimes MUST set `lifecycleConfiguration` with `idleRuntimeSessionTimeout` 900 seconds and `maxLifetime` 14400 seconds.

#### Scenario: Lifecycle values
- GIVEN `agentcore.json`
- WHEN each runtime is read
- THEN `idleRuntimeSessionTimeout` is 900 and `maxLifetime` is 14400 for all four

### Requirement: No torch in runtimes
Agent runtime dependencies MUST NOT include `torch` or any package that transitively requires it. The classifier runs only on the SageMaker endpoint.

#### Scenario: Dependency audit
- GIVEN each runtime's resolved dependency set
- WHEN scanned for `torch`
- THEN none is found

#### Scenario: Package size
- GIVEN a CodeZip build
- WHEN packaged
- THEN it stays within AgentCore CodeZip limits

### Requirement: Shared code vendored
`shared/ctf_common` (prompt, client, auth seams) MUST be the single source of truth and be vendored into every runtime `codeLocation` by a sync script; the vendored copies MUST be identical to the source.

#### Scenario: Drift detection
- GIVEN a vendored copy edited by hand
- WHEN the sync check runs
- THEN it fails and reports the differing file

### Requirement: Agent behavior parity
Each ported agent MUST preserve its original behavior: `harness` (strands_harness), `harness_sdk` (Strands Agent with MCP and sliding window), `strand_decider` (tag and grab_flag gated by classifier), `decider_loop` (Python-owned loop, classifier picks actions, LLM fallback). Each MUST play one character through the MCP server for a full match.

#### Scenario: Gated actions
- GIVEN `strand_decider` and a classifier verdict denying `tag`
- WHEN the agent attempts `tag`
- THEN the call is blocked and the agent continues

#### Scenario: Loop fallback
- GIVEN `decider_loop` and an unavailable decider
- WHEN a tick occurs
- THEN the LLM chooses the action (see decider-client)

#### Scenario: Fresh history per start
- GIVEN any runtime
- WHEN a match starts
- THEN agent message history begins empty

### Requirement: Runtime configuration from SSM
At match start (session preflight, at runtime) each runtime MUST read the MCP URL from SSM parameter `/agentic-ctf/mcp-url`; `strand_decider` and `decider_loop` MUST also read the decider endpoint name from `/ctf-decider/endpoint-name`. Environment variables carry only the parameter NAMES, never the values. The Cognito parameters (`/agentic-ctf/cognito/*`) are consumed by deploy-time scripts, not by runtimes. A missing value MUST fail the start at runtime preflight with an error naming the parameter. CDK wiring never reads or fails on a value (see fleet-operations).

#### Scenario: Missing parameter
- GIVEN `/agentic-ctf/mcp-url` is absent
- WHEN `start` is invoked
- THEN it fails with an error naming `/agentic-ctf/mcp-url` and no connection is attempted

#### Scenario: Missing decider endpoint parameter
- GIVEN `/ctf-decider/endpoint-name` is absent in a decider runtime
- WHEN `start` is invoked
- THEN preflight fails with an error naming `/ctf-decider/endpoint-name`

### Requirement: Game contract v2
The runtimes MUST follow the game server's MCP turn contract v2:
- The static map is not repeated in every turn. It comes from the `join_match` result (`map`) or from `get_map` (no arguments, joined sessions only). Turns from `wait_for_turn` carry no `grid`, `walls`, `fields` or `flags[].base`.
- `routes[].min_enemy_distance` no longer exists. `routes[].closest_threat` is `{enemy_id, step, distance}` or `null` (BFS distance, only enemies that can tag). A null or absent `closest_threat` renders no clause at all, never "no threat".
- Turns carry a top-level `last_action` (`null` on the first turn, else `{action, tick, result, reason?}`). The decider question, the warm-up state and the `/v1/systemone` state never include it. The LLM fallback renders it, labelled `outcome of your previous action`.
- `match_over` is `{status, score, winner, reason}`; `reason` is `completed` or `stopped`, and `winner` is `null` when stopped.
- Action acks can be rejected with `match_over`, `match_not_started` or `not_joined`. Joining a finished match is the tool error `match_finished`.
- The Strands runtimes do not pin the map in their context. Their system prompt says where the layout comes from and that `get_map` can be called when needed.

#### Scenario: Closest threat present
- GIVEN a route whose `closest_threat` is `{enemy_id, step, distance}`
- WHEN the state is rendered for the LLM
- THEN the route text includes the closest threat id, step and distance

#### Scenario: Closest threat absent
- GIVEN a route whose `closest_threat` is `null`
- WHEN the state is rendered
- THEN the route text has no threat clause

#### Scenario: Map missing from turns
- GIVEN a `wait_for_turn` turn with no `grid` or `walls`
- WHEN the loop needs the layout and the join result did not carry `map`
- THEN the loop fetches it once with `get_map`

#### Scenario: Stopped match result
- GIVEN a `match_over` with `reason` `stopped`
- WHEN logged
- THEN `winner` is `null` in the log record

### Requirement: Distinct player identity
Each runtime uses its own Cognito client so its token `sub` differs from other runtimes; the `name` MUST be distinct per runtime and character.

#### Scenario: Two types join
- GIVEN `harness` and `harness_sdk` both start
- WHEN both connect
- THEN their token `sub` claims differ
