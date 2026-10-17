# Delta Spec: ctf-agent-runtimes

## ADDED Requirements

### Requirement: Four runtimes, one per agent
`agentcore.json` MUST declare four independent runtimes: `harness`, `harness_sdk`, `strand_decider`, `decider_loop`, each with its own `codeLocation`, ARN and IAM role (D1). Names MUST be CloudFormation-safe.

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
At match start (session preflight) each runtime MUST read the MCP URL from SSM parameter `/agentic-ctf/mcp-url`; `strand_decider` and `decider_loop` MUST also read the decider endpoint name from `/ctf-decider/endpoint-name`. Environment variables carry only the parameter NAMES, never the values. The Cognito parameters (`/agentic-ctf/cognito/*`) are consumed by deploy-time scripts, not by runtimes. Missing values MUST fail the start with an error naming the parameter.

#### Scenario: Missing parameter
- GIVEN `/agentic-ctf/mcp-url` is absent
- WHEN `start` is invoked
- THEN it fails with an error naming `/agentic-ctf/mcp-url` and no connection is attempted

### Requirement: Distinct player identity
Each runtime uses its own Cognito client so its token `sub` differs from other runtimes; the `name` MUST be distinct per runtime and character.

#### Scenario: Two types join
- GIVEN `harness` and `harness_sdk` both start
- WHEN both connect
- THEN their token `sub` claims differ
