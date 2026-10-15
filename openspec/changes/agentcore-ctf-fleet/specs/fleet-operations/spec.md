# Delta Spec: fleet-operations

## ADDED Requirements

### Requirement: Cross-stack values only via SSM
The AgentCore stack, scripts and runtimes MUST obtain cross-stack values (MCP URL, Cognito URLs, client ids, secret ARNs, decider endpoint name) only from SSM Parameter Store. No CloudFormation exports or `Fn::ImportValue` MAY be introduced (D4). Values from SSM that `agentcore.json` cannot hold MUST be injected by CDK in `agentcore/cdk/lib/fleet-wiring.ts`.

#### Scenario: No imports
- GIVEN the synthesized AgentCore and Decider templates
- WHEN scanned
- THEN no `Fn::ImportValue` and no `Export` exist

#### Scenario: Env injection
- GIVEN SSM values present
- WHEN `agentcore deploy` synthesizes
- THEN each runtime receives the needed env vars and IAM grants from fleet-wiring

#### Scenario: Missing SSM value
- GIVEN a required parameter missing
- WHEN wiring runs
- THEN it fails with the parameter name

### Requirement: Secret hydration
A script MUST write gitignored `agentcore/.env.local` from SSM client ids and Secrets Manager secrets before `agentcore deploy` (interim path A). It MUST NOT print secrets and MUST fail if any of the four agents lacks a client id or secret.

#### Scenario: Hydrate
- GIVEN SSM and Secrets Manager entries for four agents
- WHEN the script runs
- THEN `.env.local` holds four id/secret pairs with file mode 0600

#### Scenario: Missing secret
- GIVEN one secret ARN is unreadable
- WHEN the script runs
- THEN it exits non-zero naming the agent, and leaves no partial file

### Requirement: Vendor sync
A script MUST copy `shared/ctf_common` into each runtime `codeLocation` and offer a check mode that fails on drift.

#### Scenario: Sync then check
- GIVEN the script ran
- WHEN check mode runs
- THEN it exits 0

### Requirement: Fleet launcher
A launcher MUST start N characters (up to 3 per agent type, up to 2 teams) for a `match_id`, one invocation and one distinct session id per character, and report per-character acknowledgement. Requests exceeding the limits MUST be rejected before any invocation.

#### Scenario: Launch
- GIVEN a `match_id` and 2 characters for each of 4 types
- WHEN launched
- THEN 8 invocations with 8 distinct session ids are made

#### Scenario: Limit exceeded
- GIVEN 4 characters requested for one type
- WHEN launched
- THEN it is rejected and nothing is invoked

#### Scenario: Partial failure
- GIVEN one invocation fails
- WHEN launched
- THEN the others proceed and the failure is reported with a non-zero exit

### Requirement: Status and stop
Scripts MUST query `status` and send `stop` for every session started by the launcher, using the same session ids.

#### Scenario: Stop all
- GIVEN 8 running sessions
- WHEN stop-all runs
- THEN `stop` is sent to each of the 8 session ids

### Requirement: Workshop runbook
A runbook MUST document deploy order (`AgenticCtfStack`, `DeciderStack`, AgentCore stack), secret hydration, launch, monitoring, and teardown (reverse order, including removing the endpoint), plus the quota request and Budget alert prerequisites.

#### Scenario: Order and teardown
- GIVEN the runbook
- WHEN read
- THEN it lists deploy order, teardown order, endpoint quota and budget alert steps

### Requirement: Rollback safety
Operations in this change MUST NOT mutate agentic-ctf resources; SSM access is read-only for the fleet.

#### Scenario: IAM
- GIVEN runtime roles
- WHEN inspected
- THEN SSM permissions are read-only (`GetParameter`): `/agentic-ctf/mcp-url` for every runtime, plus `/ctf-decider/*` for `strand_decider` and `decider_loop` only
