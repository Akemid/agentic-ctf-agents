# PR0 spike findings (2026-10-14)

Evidence for the design branches selected in `agentcore-ctf-fleet`. Status legend:
DONE = fully verified locally or by reading source; PREPARED = needs an AWS-mutating or GPU step, script provided.

| Task | Spike | Status | Outcome / design branch |
|------|-------|--------|-------------------------|
| 0.1 | Endpoint quota | DONE | Quota `L-B67CFA0C` in us-east-2: request `9cf62dab75704e41a9648bd55ce941ffvCArSmVu` APPROVED on 2026-10-14; current value 1.0. Check with `bash spikes/s0_request_quota.sh status`. |
| 0.2 | S5 deploy scope | DONE | A8 confirmed: `infra/decider/` is unreachable by `agentcore deploy`. |
| 0.3 | S2 cp314 wheels | DONE | PASS: `PYTHON_3_14` for all 4 (A11). |
| 0.4 | S3 identity | PARTIAL | Criteria a, b, c, d verified from source (d corrected); Path A selected. Real token PREPARED. |
| 0.5 | S1 harness headers | DONE | PASS: `create_harness(mcp_servers={"x": {"url", "headers"}})`. |
| 0.6 | Open questions | DONE | All four answered from `agentic-ctf` source (see below). |
| 0.7 | S4 T4 decider | PREPARED | Scripts ready; one design assumption refuted (no dtype flag). |

## S5: CLI deploy scope (0.2)
- `agentcore deploy` runs the CDK app declared by `agentcore/cdk/cdk.json` (`node dist/bin/cdk.js`) and calls
  `toolkit.deploy({stacks})` with an explicit stack name pattern (`PATTERN_MUST_MATCH`), see
  `@aws/agentcore@0.31.1 dist/cli/index.mjs` (`deploy({stacks:v})`). `bin/cdk.ts` only instantiates
  `AgentCore-<project>-<target>`.
- `infra/decider/` is a different CDK app with its own `cdk.json`, so no CLI command synthesizes it.
- Result: PASS. The endpoint can never be started by an agent deploy. Not run: `agentcore deploy --diff`
  (the pre-deploy step may touch AWS identity setup; source evidence is sufficient).

## S2: aarch64 wheels (0.3)
- The CLI installs with `uv pip install -r pyproject --target --python-version <v> --python-platform <p> --only-binary :all:`
  and retries platforms `aarch64-manylinux2014`, `aarch64-manylinux_2_28`, `aarch64-manylinux_2_34`
  (`dist/lib/packaging/python.js`). The retry triggers on "has no usable wheels" (`uv.js`).
- Resolved set for `strands-harness>=0.3.0, bedrock-agentcore[strands-agents]>=1.24.1, strands-agents>=1.59,
  mcp>=1.28.1,<2, boto3, botocore[crt], aws-opentelemetry-distro~=0.18.0, httpx`:
  - `aarch64-manylinux2014` FAILS for both 3.14 and 3.13: `pydantic-monty-client==0.0.23` (a dependency of
    `strands-harness 0.3.0`) ships only `manylinux_2_28` wheels.
  - `aarch64-manylinux_2_28` and `_2_34` PASS for both 3.14 and 3.13 (`cp314-cp314-manylinux_2_28_aarch64`
    exists). The CLI falls back to it automatically, so this is not a blocker.
  - Without `>=0.3.0` the resolver silently picks `strands-harness==0.0.0`. The harness pyproject MUST pin
    `strands-harness>=0.3.0`.
- Versions: strands-agents 1.61.0, mcp 1.30.0, bedrock-agentcore 1.24.1, boto3 1.43.x. No torch/nvidia package.
- Real install into a temp dir (3.14, manylinux_2_28): 141 MB unzipped, under the CodeZip limit.
- Decision: **`PYTHON_3_14` for all four runtimes.** Local dev venv may stay 3.13 (same wheels resolve).

## S1: harness MCP headers (0.5)
- `strands_harness.agent.create_harness(mcp_servers=...)` calls `MCPClient.load_servers(...)`; the
  `MCPServerConfig` TypedDict has `url` and `headers`; `strands_harness.config._python_mcp_servers` keeps them.
- Executed `spikes/s1_harness_headers.py` (local HTTP server): the server received `Authorization: Bearer spike-token`.
- Decision: **harness branch = `mcp_servers={"ctf": {"url": ..., "headers": {"Authorization": f"Bearer {token}"}}}`.**
  Fallback 1 also exists (`create_harness(tools=[MCPClient(url=..., headers=...)])`, a connected client is accepted;
  set `application_name`). No descope needed.
- Constraint: the header is fixed when the client is built, so the token is fetched once per harness build.
  That matches "1 connection = 1 token" but see the token validity finding below (found 1 h in PR0; 4 h requested (D6); re-verify before E2E (task 4.5)).

## S3: AgentCore Identity (0.4), criteria from CLI 0.31.1 + L3 `@aws/agentcore-cdk 0.1.0-alpha.54` source
- (a) PASS by source: provider creation uses `name: credential.name` unchanged (`CreateOauth2CredentialProvider`
  with `customOauth2ProviderConfig`). The runtime also receives `CREDENTIAL_<NAME>_NAME=<credential name>`
  (`AgentCoreApplication.wireCredentialsToAgents`, name upper-cased, `.`/`-` -> `_`).
- (b) PASS by source: `deployed-state.json` schema is `{credentialProviderArn, clientSecretArn?, callbackUrl?}`.
- (c) PASS by source: when `credentials[]` is non-empty the L3 calls `runtime.grantCredentialAccess()` on every runtime:
  `GetResourceOauth2Token`, `GetWorkloadAccessTokenForUserId`, `CreateWorkloadIdentity` on
  `workload-identity-directory/*` and `token-vault/*`, plus `secretsmanager:GetSecretValue` on
  `secret:bedrock-agentcore-identity!*`. **fleet-wiring MUST NOT re-add these**; it only adds SSM and SageMaker.
  Also available natively: runtime `additionalPolicies` (managed policy ARN or JSON file in `codeLocation`).
- (d) CORRECTED: env var names are `AGENTCORE_CREDENTIAL_<NAME>_CLIENT_ID` and `_CLIENT_SECRET` where
  `<NAME>` = credential name upper-cased with `-` -> `_`. There is NO project prefix. A credential name must not end
  in a reserved suffix (`...id`/`client...` family, see `sge()` in the CLI).
- Decision: **Path A (hydrate `.env.local`) is selected**, pending the real-token run.
- PREPARED: one real M2M token (`spikes/s3_identity_token.py`). Blocked on a prerequisite: no `/agentic-ctf/*` SSM
  parameters and no AgenticCtfStack exist in account <ACCOUNT_ID> / us-east-2 (checked read-only), so the Cognito
  agent clients (`agentClientNames`) must be deployed first.
- Gotcha for PR4 `fleet.py`: the SDK reads the workload access token from a ContextVar set per request by the
  runtime (`BedrockAgentCoreContext.get_workload_access_token`). Under SigV4 inbound auth the SDK error text says the
  caller must pass `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`; the CLI always sends it (default user id). The launcher
  MUST pass `runtimeUserId` in `InvokeAgentRuntime`. Verify in the real run.
- Gotcha: `@requires_access_token` builds `IdentityClient(region)` at DECORATION time, so decorate inside
  `get_token()` (A3 already does). The sync wrapper copes with a running loop (thread + `copy_context`).

## S4: decider on T4 (0.7)
- PREPARED: `spikes/s4_t4_host.sh` + `spikes/s4_bench.py` (+ tested pure helpers `spikes/s4_lib.py`).
- REFUTED assumption: `strands-decider 0.1.0 serve` has **no dtype flag** (options: `--host --port --device
  --no-prefix-cache --model-name`). dtype comes from `strands_decider_config.json` in the checkpoint
  (`torch_dtype`, default `"bfloat16"`, T4 has no native bf16). The adapter (PR3) must stage a local checkpoint copy
  with `torch_dtype` patched from `DECIDER_DTYPE` (the script does exactly this for the benchmark). Weights are
  fetched through `huggingface_hub.snapshot_download` (A6 stays valid).
- Risk to check in the run: Qwen3.5 uses linear-attention layers; on CUDA the fast kernels need extra packages
  (not verified). The benchmark will show it as p95.
- InService time (<= 20 min) can only be measured with a real endpoint (first PR3 deploy). Design pins (`dtype`,
  read timeout 2.5 s, breaker 3/30 s, image/AMI) stay at the design defaults until the numbers arrive.

## 0.6 Open questions (source: `agentic-ctf`, branch `feat/m2m-agent-clients`)
1. join_match multi-character per sub: ALLOWED. `lobby.join` only checks the `sub` when RECLAIMING a disconnected
   slot (`joinExplicit`/`joinLowestFree`); a fresh join takes the lowest free slot, or `agent_index`. Rules:
   one MCP session joins at most one match ("A session joins at most one match"), so 1 character = 1 MCP session
   (matches D-design), `name` MUST be unique per character, and a same-`name` rejoin by the same sub reconnects
   the old slot instead of taking a new one.
2. Cognito pool persistence: NOT persistent. `CognitoAuth` uses `RemovalPolicy.DESTROY` for the pool and the agent
   secrets, so every AgenticCtfStack redeploy changes the pool id and `discoveryUrl`; `hydrate_credentials.py`
   drift check (`--update-discovery-url`) is required, not optional.
3. Client-secret JSON key: `{"client_id", "client_secret"}` in secret `agentic-ctf/agents/<name>`
   (`CognitoAuth.addAgentClient`). Path B dynamic reference key: `client_secret`. SSM contract:
   `/agentic-ctf/agents/<name>/client-id`, `/agentic-ctf/agents/<name>/secret-arn`, `/agentic-ctf/mcp-url`,
   `/agentic-ctf/cognito/{user-pool-id,discovery-url,token-endpoint,mcp-scope}`. `<name>` is whatever is passed in
   `agentClientNames` (`^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$`: hyphens, NO underscores) so the runtime-name
   == agentic-ctf-name assumption needs a mapping (`harness_sdk` -> `harness-sdk`).
4. wait_for_turn vs CloudFront: `DEFAULT_MCP_WAIT_CAP_MS = 25_000` (server cap) < CloudFront origin read timeout
   120 s. No 504 risk from long-polling.

## Spec/design conflicts found
- **Access token validity: found 1 h in PR0; 4 h requested (D6); re-verify before E2E (task 4.5).** PR0 read
  `accessTokenValidity: Duration.hours(1)` for agent M2M clients in agentic-ctf branch `feat/m2m-agent-clients`
  (the 8 h figure belongs to the web client). Decision D6 sets 4 h and the change has been requested; the deployed
  value is NOT confirmed. The gateway re-verifies the bearer on EVERY request, so until 4 h is deployed a match must
  finish within 1 h of the token fetch (preflight). `maxLifetime=14400` stays as a session cap. Re-check the deployed
  value (`spikes/s3_identity_token.py` prints the lifetime: 14400 = D6 applied, 3600 = not yet) before the E2E match
  (task 4.5). Mitigation until then: keep matches short (ticks x deadline). Specs amended accordingly (`mcp-m2m-auth`).
- Hyphen-only agentic-ctf agent names (see 0.6.3) vs underscore runtime names: affects `hydrate_credentials.py`.
