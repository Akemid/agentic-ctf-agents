# Fleet Operations Specification

## Purpose

Operating the agent fleet: cross-stack configuration through SSM only, secret hydration, vendor sync, the fleet
launcher, status and stop, the workshop runbook, the lobby contract (runtime ARN publication), and rollback safety.
Merged from change `agentcore-ctf-fleet` on 2026-10-16. Amendments from `decider-optional.md` (persistent stack owns
the endpoint-name parameter), `lobby-wake.md` (ARN publication and caller IAM) and the slice 4 review (safe deploy
wrapper, missing-value behavior) are merged in.

## Requirements

### Requirement: Cross-stack values only via SSM
The AgentCore stack, scripts and runtimes MUST obtain cross-stack values (MCP URL, Cognito URLs, client ids, secret ARNs, decider endpoint name) only from SSM Parameter Store. This project MUST author no CloudFormation exports and MUST NOT use `Fn::ImportValue` (D4). "No exports" means no exports authored by this project. The alpha L3 construct emits its own stack outputs with `Export` (runtime id, runtime ARN, role ARN per agent); those are not authored here and are accepted. Values from SSM that `agentcore.json` cannot hold MUST be injected by CDK in `agentcore/cdk/lib/fleet-wiring.ts`. Wiring only sets parameter NAMES in the environment. A missing SSM value fails at runtime preflight, not in CDK wiring.

#### Scenario: No imports and no exports of our own
- GIVEN the synthesized AgentCore and Decider templates
- WHEN scanned
- THEN no `Fn::ImportValue` exists
- AND the Decider template has no `Export`
- AND the only `Export`s in the AgentCore template are the alpha L3's (`<stack>-<agent>-RuntimeId|RuntimeArn|RoleArn`)
- AND no template parameter of type `AWS::SSM::Parameter::Value` exists (other than the CDK bootstrap version)

#### Scenario: Env injection
- GIVEN SSM values present
- WHEN `agentcore deploy` synthesizes
- THEN each runtime receives the needed env vars and IAM grants from fleet-wiring

#### Scenario: Wiring never reads SSM
- GIVEN any SSM parameter missing or present
- WHEN `agentcore deploy` synthesizes
- THEN wiring only sets parameter NAMES in the environment and never reads or fails on a value

#### Scenario: Missing SSM value at match start
- GIVEN a required parameter missing when a match starts
- WHEN the runtime preflight runs
- THEN `start` is rejected and the error names the parameter

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

#### Scenario: Deploy without hydration
- GIVEN `agentcore deploy` runs with a credential provider lacking a client id or secret in `.env.local`
- WHEN the CLI reports the provider as `Skipped`
- THEN the deploy still reports success, so the safe deploy wrapper (see Workshop runbook) is the guard, not the placeholder `discoveryUrl`

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
A runbook MUST document deploy order (`AgenticCtfStack`, `CtfPersistentStack`, `DeciderStack`, AgentCore stack), secret hydration, launch, monitoring, and teardown (reverse order, including removing the endpoint), plus the quota request and Budget alert prerequisites. The safe deploy path is `make deploy YES=1` (`scripts/deploy_fleet.sh`), which runs the sync check, hydration and its check, `agentcore validate`, `agentcore deploy --yes`, and hydration cleanup in an EXIT trap. Without `YES=1` it only prints the plan.

#### Scenario: Order and teardown
- GIVEN the runbook
- WHEN read
- THEN it lists deploy order, teardown order, endpoint quota and budget alert steps

#### Scenario: Safe deploy cleans up
- GIVEN a deploy that fails or is interrupted (Ctrl-C or SIGTERM)
- WHEN the wrapper exits
- THEN the EXIT trap runs the hydration cleanup and no secret file is left behind

### Requirement: Runtime ARN publication for the lobby
The ARN of each of the runtimes `harness`, `harness_sdk`, `strand_decider` and `decider_loop` MUST be published to SSM as a plain String parameter `/ctfagents/runtimes/<runtime>/arn`, holding the runtime ARN of the default endpoint. These parameters are the contract for the web lobby to wake agents. No CloudFormation exports are used.

#### Scenario: ARN parameters present
- GIVEN the four runtimes are deployed
- WHEN SSM is read
- THEN four parameters `/ctfagents/runtimes/<runtime>/arn` exist, each holding a runtime ARN

#### Scenario: Lobby start contract
- GIVEN the lobby invokes `start` with `runtimeUserId` set
- WHEN the caller is authorized
- THEN the caller role has `InvokeAgentRuntime`, `InvokeAgentRuntimeForUser` and `StopRuntimeSession` on `runtime/<id>` and `runtime/<id>/runtime-endpoint/*`

### Requirement: Rollback safety
Operations in this change MUST NOT mutate agentic-ctf resources; SSM access is read-only for the runtimes.

#### Scenario: IAM
- GIVEN runtime roles
- WHEN inspected
- THEN SSM permissions are read-only (`GetParameter`): `/agentic-ctf/mcp-url` for every runtime, plus exactly `/ctf-decider/endpoint-name` (no wildcard) for `strand_decider` and `decider_loop` only
