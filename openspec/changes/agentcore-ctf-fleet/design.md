# Design: AgentCore CTF Fleet (4 runtimes + SageMaker decider)

> Inputs: proposal (engram #2918), exploration.md, D1-D6 (blog-notes/agentcore-ctf-technical-decisions.md, fixed).
> Size note: this exceeds the 800-word skill budget because the orchestrator required 12 explicit decision areas and 3 diagrams. Tables are used to keep it dense.
> Tags: `[UNVERIFIED]` = not confirmed against docs or source in this phase. Each one is assigned to a spike (S1-S5) or to an apply-time check.

## Technical Approach

There are four CodeZip HTTP runtimes. Each invocation runs one character. The entrypoint is sync. It validates the payload, runs a synchronous preflight (SSM config plus one M2M token), and starts the match in a background thread registered with `add_async_task`. Later invocations on the same session return `status` or perform `stop`. All shared logic lives in `shared/ctf_common/`, which is vendored into each `codeLocation` and checked for drift by a pytest test. The decider runs on a fixed-name SageMaker endpoint (`ctf-decider`). That endpoint is deployed by a separate CDK app (`infra/decider/`) through a FastAPI adapter that serializes requests. A persistent sibling stack holds the ECR repo and the AWS Budget. `agentcore/cdk/lib/fleet-wiring.ts` adds env vars and least-privilege IAM through a small, faked interface.

## Architecture Decisions

| # | Topic | Choice | Rejected (why) |
|---|---|---|---|
| A1 | Shared code | `shared/ctf_common/` is the source of truth. `scripts/sync_common.py` copies it to `app/<rt>/ctf_common/`, and the copies are **committed**. `tests/test_vendor_sync.py` fails on any hash drift. | Gitignored copies (a fresh-clone `agentcore deploy` hits an ImportError at runtime). uv path deps (the CLI `-r pyproject.toml` ignores them). Container build (ARM64 CodeBuild x4, slower). Copy-paste (4x drift). |
| A2 | Execution model | Async start plus `status`/`stop` actions on the same session. Sync `def` entrypoint (runs in a threadpool, so `/ping` is never blocked). | Sync blocking (15 min cap). Streaming (60 min cap, disconnect semantics unclear). |
| A3 | Token seam | `TokenProvider.get_token()` is called **once per MCP connection open**, inside the transport factory. The decorator wraps only the fetch function. | Decorating tools (D6: tokens == tool calls). Module-level decoration (needs env at import). |
| A4 | Decider failure policy | Bounded retry (1), deadline clamp, a 3-failure / 30 s cooldown breaker with a half-open probe, then LLM fallback. | Permanent `decider_down` (one SageMaker throttle kills the classifier for the whole match). |
| A5 | Adapter | FastAPI on 8080 supervises the `strands-decider serve` subprocess on 127.0.0.1:8099. Global FIFO `asyncio.Lock`, bounded wait. | In-process import (internal API unverified). No lock (upstream crashes on concurrency). |
| A6 | Weights | Baked into the image at build (`snapshot_download`, `HF_HUB_OFFLINE=1`). | Download at startup (HF outage during a workshop). S3 `ModelDataUrl` (extra bucket and upload step). |
| A7 | Image lifecycle | Persistent ECR repo `ctf-decider` (in `CtfPersistentStack`). `scripts/decider_image.sh` runs `docker buildx --platform linux/amd64` and pushes a content-hash tag. DeciderStack takes `-c imageTag=`. CodeBuild is the fallback if the emulated build exceeds ~45 min. | `DockerImageAsset` (re-hashes and rebuilds a ~10 GB image on every weekly deploy and bloats the bootstrap repo). |
| A8 | Stack placement | Separate CDK app `infra/decider/` with two stacks: `CtfPersistentStack` (deploy once: ECR, Budget, SNS email) and `DeciderStack` (weekly). | An extra stack in `agentcore/cdk/bin/cdk.ts` (CLI deploy scope is undocumented, so it risks silently starting a $0.736/h endpoint). |
| A9 | Identity secrets | **Path A** (hydrate `.env.local`) is final for this change, **if S3 passes**. Path B (`AWS::BedrockAgentCore::OAuth2CredentialProvider` with dynamic references) is the pre-designed fallback and the follow-up that removes laptop transit. | Path C (boto3 out-of-band, outside IaC). |
| A10 | Config source | Runtime reads SSM at **session preflight**. Env vars carry only parameter **names**. | CDK-time `valueForStringParameter` (stale MCP URL after the game stack is recreated, until a redeploy). |
| A11 | Python | `PYTHON_3_14` for all four runtimes if S2 passes, otherwise `PYTHON_3_13` for all four (uniform). The root dev env matches. | A mixed per-runtime version (vendored code is tested against one interpreter). |
| A12 | Infra constructs | L1 `CfnModel` / `CfnEndpointConfig` / `CfnEndpoint`. Only the **Endpoint** gets a fixed name. Model and EndpointConfig names are CFN-generated. | L2 alpha package. A fixed EndpointConfig name (immutable: an update needs replacement, and replacement with a fixed name fails). |

### Path A vs B gate (S3)

Choose **A** if all of the following hold:
- (a) `agentcore deploy` creates providers from `.env.local` with deterministic names equal to the credential `name`.
- (b) `.cli/deployed-state.json` exposes `credentialProviderArn` and `clientSecretArn`.
- (c) Synth shows the runtime role either already holds token access or accepts our statements.
- (d) The env-var normalization `AGENTCORE_CREDENTIAL_{project}{name}_CLIENT_ID/_SECRET` is confirmed.

Any failure means **B**: remove `credentials[]` from `agentcore.json`. `lib/ctf-credentials.ts` then creates 4 `CfnResource('AWS::BedrockAgentCore::OAuth2CredentialProvider')` with:
- `CredentialProviderVendor: CustomOauth2`
- `ClientId: {{resolve:ssm:/agentic-ctf/agents/<n>/client-id}}`
- `ClientSecret: {{resolve:secretsmanager:<secret-arn>:SecretString:client_secret}}` (the JSON key `client_secret` is `[UNVERIFIED]` and is owned by agentic-ctf)
- `OauthDiscovery.DiscoveryUrl` from SSM

`GetAtt CredentialProviderArn` and `ClientSecretArn` then feed `wireFleet`.

## Module Layout

```
shared/ctf_common/          # source of truth (tests import from here)
  prompts.py     SYSTEM_PROMPT, ACT_FAST_PROMPT, build_instruction(), MODEL_ALIASES
  payload.py     MatchRequest, Action, parse_payload()  (pure)
  config.py      RuntimeConfig, load_config(ssm, env)   (SSM at preflight; CTF_MCP_URL/CTF_DECIDER_URL local overrides)
  auth.py        TokenProvider (Protocol), AgentCoreM2MTokenProvider, PrefetchedTokenProvider, StaticTokenProvider
  mcp.py         open_mcp_client(url, tokens) -> MCPClient
  runner.py      MatchRunner, MatchState, PlayFn (Protocol)
  decider.py     question builders (noul/choice/score), DeciderPort, SageMakerDecider, HttpDecider (local), CircuitBreaker, errors
  hooks.py       StripStaticWallsHook, make_conversation_manager(), StopRequestedHook
app/<rt>/                   # per runtime: main.py (thin entrypoint), <rt-specific>.py, pyproject.toml, ctf_common/ (vendored)
  harness/harness_agent.py        create_harness wiring (S1 branch)
  harness_sdk/sdk_agent.py        Strands Agent + SlidingWindow + hooks   (replaces CLI template; mcp_client/, model/ deleted)
  strand_decider/gate.py          ToolCallReviewer InterventionHandler (fcntl lock removed)
  decider_loop/loop.py            code-owned loop + pure helpers (characterization-tested)
infra/decider/              # separate CDK app (cdk.json, package.json, jest)
  bin/decider.ts, lib/persistent-stack.ts, lib/decider-stack.ts, test/*.test.ts
  adapter/  Dockerfile, adapter/{app.py,supervisor.py,warmup.py,__main__.py}, requirements.txt
agentcore/cdk/lib/fleet-wiring.ts (+ test/fleet-wiring.test.ts); cdk-stack.ts gets a 2-line call
scripts/  sync_common.py, hydrate_credentials.py, decider_image.sh, fleet.py (start|status|stop)
tests/    root pytest suite; root pyproject.toml (uv dev group)
docs/workshop-runbook.md
```

Per-runtime only: the entrypoint and `PlayFn` implementation, runtime-specific deps (`strands-harness` only in `harness`, `boto3` sagemaker-runtime only in the decider runtimes), and the lifecycle config. Torch is in no runtime.

## Interfaces / Contracts

**Payload** (top-level object; `{"prompt": "<json string>"}` is unwrapped first; `messages`/`tool_results` are rejected, per AGENTS.md invariant 5; unknown keys are rejected):

```
{ "action": "start"|"status"|"stop"  (default "start"),
  "match_id": "^[A-Za-z0-9_-]{1,64}$"  (required for start),
  "name": sanitized "^[a-z0-9_-]{1,32}$" (default "<runtime>-<session_id[-6:]>"),
  "team": "red"|"blue"|"auto" (default auto),
  "model": "haiku"|"opus"|"nova-lite" (default haiku),
  "options": { window | threshold | threat_threshold | llm_budget_ms | safety_margin_ms }  # validated per runtime }
Response: { "status": "started"|"already_running"|"running"|"finished"|"failed"|"stopping"|"stopped"|"idle"|"rejected",
            "match_id", "name", "team", "started_at", "ticks", "error"? }
```

**MatchRunner state machine.** There is one runner per process, which is one microVM per session.

```
IDLE --start(ok preflight)--> RUNNING --play returns match_over--> FINISHED
  |  start(preflight error) -> FAILED          RUNNING --exception--> FAILED
  |                                             RUNNING --stop--> STOPPING --loop exits--> STOPPED
start while RUNNING: same (match_id,name) -> already_running ; different -> rejected
```

- `start` takes a `threading.Lock`, checks the state, then runs `preflight()`, which loads config and calls `PrefetchedTokenProvider(tokens.get_token())`. Errors are returned synchronously, which fails fast for the operator.
- Next, `task_id = app.add_async_task("match", {match_id, name})` runs. Then `threading.Thread(target=contextvars.copy_context().run, args=(self._run,), daemon=True)` starts the match.
- Copying the context propagates the request-scoped workload-identity ContextVar into the worker thread `[UNVERIFIED: the SDK stores it in a ContextVar; harmless if not]`.
- `_run` ends with `finally: app.complete_async_task(task_id)`. The session reports HealthyBusy until that call.
- `stop` sets a `threading.Event`. In `decider_loop`, the loop checks it each tick. In `harness_sdk`, `strand_decider`, and `harness`, `StopRequestedHook` cancels the next tool call and ends the event loop `[UNVERIFIED API: BeforeToolCallEvent.cancel_tool / request_state stop; verify on strands >=1.59]`. The hard stop is always `StopRuntimeSession` (`fleet.py stop --hard`).
- **lifecycleConfiguration** (all 4): `idleRuntimeSessionTimeout: 900` and `maxLifetime: 14400`. Four hours covers a 2-3 h workshop, equals the 4 h token validity, and caps runaway cost.

**MCP factory and token seam:**

```python
class AgentCoreM2MTokenProvider:            # provider_name from env CTF_OAUTH_PROVIDER, scopes from CTF_OAUTH_SCOPES
    def get_token(self) -> str:
        @requires_access_token(provider_name=self._provider, scopes=self._scopes, auth_flow="M2M", into="access_token")
        def _fetch(*, access_token: str) -> str: return access_token
        return _fetch()                      # 1 call == 1 GetResourceOauth2Token (no SDK cache, D6)

def open_mcp_client(url: str, tokens: TokenProvider) -> MCPClient:
    def transport():                         # MCPClient invokes this once per connection start
        return streamablehttp_client(url, headers={"Authorization": f"Bearer {tokens.get_token()}"})
    return MCPClient(transport)
```

If mcp 1.30 deprecates `streamablehttp_client`, use the replacement with an `httpx.AsyncClient(headers=...)`. Check this at apply time.

The **harness** path is S1-gated:
- **S1 pass**: `create_harness(mcp_servers={"ctf-game": {"type": "http", "url": url, "headers": {"Authorization": "Bearer <token>"}}})`. One token is fetched per harness creation.
- **Fallback 1**: pass the prebuilt `open_mcp_client(...)` through the harness's tools / `agent_kwargs` `[UNVERIFIED]`.
- **Fallback 2**: the `harness` runtime is descoped to a follow-up, and the fleet ships 3 runtimes (reported, not silent).
- Harness session and memory dirs move to `/tmp/.agent/<session>`.

**Decider client:**

```python
class SageMakerDecider:  # DeciderPort.ask(state, questions, *, deadline_s) -> answers
    # client = boto3 sagemaker-runtime, Config(connect_timeout=1, read_timeout=DECIDER_READ_S=2.5, retries={"total_max_attempts":1})
```

- **Request**: `invoke_endpoint(EndpointName=<ssm>, ContentType/Accept="application/json", Body={"state","model":"hobson-latest","questions"})`.
- **Response**: `{answers, usage, latency_ms, adapter:{queue_wait_ms}}`. The client returns `answers` and records latency and queue wait.
- **Deadline clamp**: skip the call (tick-local unavailable, not a breaker failure) if `remaining - safety_margin < 400 ms`. botocore cannot set a per-call read timeout, so the read timeout stays at a fixed 2.5 s ceiling. Retry once only if the error is transient and the budget still allows it.
- **Breaker**: CLOSED -> OPEN after 3 consecutive failures, for a 30 s cooldown -> HALF_OPEN with 1 probe. `AccessDenied` / `ResourceNotFound` / `ValidationError` on the endpoint open it for 300 s (endpoint missing or misconfigured).

Error mapping. All errors subclass `ServerUnavailable`, so the ported `except` paths keep working.

| Source | Mapped | Retry | Breaker |
|---|---|---|---|
| Read/Connect timeout, EndpointConnectionError | `DeciderTimeout` | 1x if budget | +1 |
| Throttling, ServiceUnavailable, InternalFailure, ModelNotReady | `DeciderTransient` | 1x if budget | +1 |
| ModelError, OriginalStatusCode 503 (adapter queue full) | `DeciderBusy` | no | +1 |
| ModelError 4xx | `DeciderRejected` (logged as bug) | no | +0 |
| AccessDenied, ResourceNotFound, Validation | `DeciderMisconfigured` | no | open 300 s |
| Non-JSON or missing `answers` | `DeciderProtocolError` | no | +1 |

**Adapter:**
- `POST /invocations` forwards the body 1:1 to `/v1/systemone` (httpx, 10 s timeout) and adds `adapter.queue_wait_ms` to the response.
- The lock is a global `asyncio.Lock` (FIFO waiters). If the waiting count is 8 or more, it returns 503 `queue_full` immediately. Otherwise `wait_for(acquire, 2.0 s)` runs, and a timeout returns 503.
- `/ping` never touches the lock. It returns 200 only when the state is READY, meaning upstream `/health` was OK **and** the warm-up questions (from `decider_loop.warmup_question` shapes) completed.
- A monitor runs every 5 s. If the subprocess has died or `/health` fails, it makes one restart attempt. If that fails, `/ping` returns 503 permanently and SageMaker replaces the instance.
- Exec-form `ENTRYPOINT ["python","-m","adapter"]` accepts `serve`.
- The base image is `pytorch/pytorch` with CUDA 12.x runtime, pinned by digest, linux/amd64. Set `InferenceAmiVersion` on the variant to pin the driver `[UNVERIFIED property/values; S4]`.
- **dtype**: T4 has no native bf16, so fp16 is primary and fp32 is the fallback (a 2B model in fp32 is about 9 GB, which fits in 16 GB). It is selected via `DECIDER_DTYPE` `[UNVERIFIED: strands-decider exposes a dtype flag; else a supervisor-side torch default]`. S4 decides.

**DeciderStack (weekly):**

| Resource | Settings |
|---|---|
| Execution role | ECR pull on the `ctf-decider` repo, plus logs |
| `CfnModel` | image = `<repo>:<imageTag>`; env `HF_HUB_OFFLINE=1`, `DECIDER_DTYPE` |
| `CfnEndpointConfig` | `ml.g4dn.xlarge` x1; `ContainerStartupHealthCheckTimeoutInSeconds: 1200` `[UNVERIFIED name; S4]` |
| `CfnEndpoint` | `EndpointName: "ctf-decider"` |
| SSM | `/ctf-decider/endpoint-name` |

There are **no exported Outputs**.

**CtfPersistentStack (once):**
- The ECR repo (keep the last 3 images, `RemovalPolicy.RETAIN`).
- An `AWS::Budgets::Budget` filtered to SageMaker: monthly $25, with a notification on actual >80% and on forecast >100% to an SNS/email subscriber taken from context.

**fleet-wiring.ts:**

```ts
export interface RuntimeEnvLike { readonly agentName: string; addEnvironmentVariable(k: string, v: string): void; addToRolePolicy(s: iam.PolicyStatement): void }
export interface FleetMember { runtime: string; credential: string; usesDecider: boolean }
export const FLEET: readonly FleetMember[]  // harness, harness_sdk (no decider); strand_decider, decider_loop (decider)
export interface FleetWiringProps { account: string; region: string; members?: readonly FleetMember[];
  credentials?: Record<string, { credentialProviderArn: string; clientSecretArn?: string }>;
  mcpUrlParam?: string /* /agentic-ctf/mcp-url */; deciderParamPrefix?: string /* /ctf-decider */; deciderEndpointName?: string /* ctf-decider */ }
export function adaptEnvironments(app: AgentCoreApplication): RuntimeEnvLike[]   // the only alpha-coupled code
export function wireFleet(envs: Iterable<RuntimeEnvLike>, props: FleetWiringProps): void  // throws on missing/unknown runtime
```

Env vars set on every member:
- `CTF_RUNTIME_NAME`
- `CTF_OAUTH_PROVIDER`
- `CTF_OAUTH_SCOPES=agentic-ctf/mcp`
- `CTF_MCP_URL_PARAM`
- `CTF_AGENT_STATE_DIR=/tmp/.agent`

Decider members also get `CTF_DECIDER_ENDPOINT_PARAM`.

**IAM per runtime role.** ARN formats are `[UNVERIFIED]`. S3 synth checks whether the L3 already grants them, and anything it already grants is skipped.

| Statement | Actions | Resources | Who |
|---|---|---|---|
| Token | `bedrock-agentcore:GetResourceOauth2Token` | `…:token-vault/default`, `…:token-vault/default/oauth2credentialprovider/<cred>`, `…:workload-identity-directory/default`, `…:workload-identity-directory/default/workload-identity/<runtime>-*` | all |
| Secret | `secretsmanager:GetSecretValue` | `credentials[cred].clientSecretArn`, else `…:secret:bedrock-agentcore-identity!default/oauth2/<cred>-*` | all |
| Config | `ssm:GetParameter` | `parameter/agentic-ctf/mcp-url`; plus `parameter/ctf-decider/*` for decider members | all / decider |
| Decider | `sagemaker:InvokeEndpoint` | `arn:aws:sagemaker:<r>:<a>:endpoint/ctf-decider` | strand_decider, decider_loop |

Notes:
- No runtime reads `agentic-ctf/agents/*` secrets.
- Bedrock `InvokeModel*` is assumed to come from the L3. Synth confirms this, and if it is missing, grant it on the `global.*` inference profiles and their foundation models.
- The deployer needs `iam:CreateServiceLinkedRole` (runtime identity SLR).

**Credentials (Path A):** `credentials[]` has 4 entries: `ctf_harness`, `ctf_harness_sdk`, `ctf_strand_decider`, `ctf_decider_loop`. Each has `vendor CustomOauth2`, `usage outbound`, `scopes ["agentic-ctf/mcp"]`, and `discoveryUrl` from SSM. `hydrate_credentials.py`:
- Reads `/agentic-ctf/agents/<runtime>/{client-id,secret-arn}` (ASSUMPTION: agentic-ctf agent names == runtime names).
- Writes `.env.local` with mode 0600, and `--clean` wipes it after deploy.
- Fails if `discoveryUrl` in `agentcore.json` differs from SSM, unless `--update-discovery-url` is passed.

## Data Flow / Sequences

```mermaid
sequenceDiagram
  title Workshop deploy order
  participant Op as Operator
  Op->>AgenticCtfStack: deploy (other repo) -> SSM /agentic-ctf/*
  Op->>CtfPersistentStack: deploy once (ECR, Budget)
  Op->>ECR: scripts/decider_image.sh (only when adapter/model changes)
  Op->>DeciderStack: cdk deploy -c imageTag=… (waits InService ~10-20 min) -> SSM /ctf-decider/endpoint-name
  Op->>Op: scripts/sync_common.py --check ; hydrate_credentials.py
  Op->>AgentCore: agentcore deploy (providers + 4 runtimes + fleet-wiring)
  Op->>Op: hydrate_credentials.py --clean ; fleet.py start --match M --chars …
```

```mermaid
sequenceDiagram
  title Character start
  participant L as fleet.py
  participant R as Runtime (microVM)
  participant SSM
  participant ID as AgentCore Identity
  participant MCP as agentic-ctf MCP
  participant D as SageMaker ctf-decider
  L->>R: InvokeAgentRuntime(sessionId>=33 chars, {action:start,…})
  R->>SSM: GetParameter mcp-url (+ decider endpoint)
  R->>ID: GetResourceOauth2Token (M2M) [1 per connection]
  R-->>L: {status: started} (add_async_task -> HealthyBusy)
  R->>MCP: connect (Bearer) ; join_match ; wait_for_turn…
  loop each tick (decider runtimes)
    R->>D: InvokeEndpoint (≤2.5 s, clamped) -> answers | breaker -> LLM fallback
    R->>MCP: action tool
  end
  R->>R: match_over -> complete_async_task -> Healthy -> idle 15 min
  L->>R: {action:status} same sessionId -> {status: finished, ticks}
```

```mermaid
sequenceDiagram
  title Teardown
  participant Op as Operator
  Op->>Op: fleet.py stop --all [--hard = StopRuntimeSession]
  Op->>DeciderStack: cdk destroy DeciderStack (endpoint billing stops)
  Op->>AgentCore: (optional) leave runtimes; idle cost is zero
  Op->>Op: verify: aws sagemaker list-endpoints == [] ; Budget remains
  Note over Op: AgenticCtfStack teardown is owned by the other repo; SSM is read-only here
```

## File Changes

| File | Action | Description |
|---|---|---|
| `shared/ctf_common/*.py` | Create | Shared modules (see layout) |
| `app/harness_sdk/main.py`, `sdk_agent.py`, `pyproject.toml` | Modify/Create | Replace the CLI template; delete `mcp_client/`, `model/` |
| `app/{harness,strand_decider,decider_loop}/` | Create | Ports plus `main.py`, `pyproject.toml`, vendored `ctf_common/` |
| `agentcore/agentcore.json` | Modify | 3 new runtimes (do not rename `harness_sdk`), lifecycleConfiguration, runtimeVersion (S2), 4 credentials (Path A) |
| `agentcore/cdk/lib/fleet-wiring.ts`, `test/fleet-wiring.test.ts` | Create | Wiring plus tests |
| `agentcore/cdk/lib/cdk-stack.ts` | Modify | `wireFleet(adaptEnvironments(this.application), {...})` |
| `infra/decider/**` | Create | CDK app (2 stacks), adapter, jest/pytest tests |
| `scripts/*.py`, `scripts/decider_image.sh` | Create | Sync, hydrate, image, fleet launcher |
| `pyproject.toml`, `tests/**` | Create | Root uv dev env (pytest, pytest-asyncio, httpx, fastapi, boto3, ruff); `pythonpath=["shared","app/…","infra/decider/adapter"]`, with `shared` first |
| `docs/workshop-runbook.md`, `.gitignore` | Create/Modify | Runbook; ignore `.env.local`, `.fleet/` |

## Testing Strategy (strict TDD)

| Seam | Test | Tool |
|---|---|---|
| `payload.parse_payload` | types, defaults, sanitize, prompt-unwrap, rejects messages/unknown keys | pytest (pure) |
| `runner.MatchRunner` | fake app + fake PlayFn: add/complete in `finally` (also on exception), double start, status, stop, preflight failure | pytest |
| `auth` + `mcp` | counting fake TokenProvider plus a fake transport: N tool calls means 1 token, and reconnect means +1 (D6 invariant) | pytest |
| `config.load_config` | botocore Stubber SSM; env override; missing param error | pytest |
| `decider.SageMakerDecider` / `CircuitBreaker` | Stubber: body shape, parse, every row of the error table, clamp skip, retry budget, injected-clock breaker transitions | pytest |
| `decider_loop/loop.py` pure fns | characterization tests first (combine_answers, build_options, choose_path, label_to_call, update_plan, continues_path), then the breaker-aware choose_path | pytest |
| play loops | fake MCP client + fake decider; stop event honored | pytest |
| adapter | FastAPI TestClient + httpx MockTransport: passthrough, max concurrency 1, queue_full 503, ping 503 before warm-up / after upstream death, ping not blocked by an in-flight request | pytest-asyncio |
| vendoring | hash(shared) == hash(each app copy) | pytest |
| `fleet-wiring.ts` | fake RuntimeEnvLike records calls: env vars, statements per member, decider grants only on decider members, throws on missing/unknown runtime, uses clientSecretArn when given | jest |
| DeciderStack / PersistentStack | `Template.fromStack`: instance type, fixed EndpointName, generated config name, startup timeout, SSM param, no `Export`, Budget only in Persistent | jest |
| E2E | the runbook run is the manual acceptance (no automated e2e layer) | manual |

## Spikes (order and exit criteria)

| Order | Spike | Exit criterion | Branches gated |
|---|---|---|---|
| 0 (day 0) | Quota request for `ml.g4dn.xlarge` endpoint usage | Quota ≥ 1 | S4, DeciderStack deploy |
| 1 | S5 CLI deploy scope | `agentcore deploy` touches only `AgentCore-ctfagents-*` | Confirms A8 (already safe by design) |
| 2 | S2 cp314 wheels | `uv pip install --python-platform aarch64-manylinux2014 --python-version 3.14 --only-binary=:all:` succeeds for each runtime's deps, including strands-harness | A11 (3.14 vs 3.13) |
| 3 | S3 identity | Criteria (a)-(d) in A9, plus role policy inspection via synth, plus one real token fetched in a deployed runtime | A9 (Path A vs B), IAM statement set |
| 4 | S1 harness headers | Harness lists the MCP tools against an auth-required endpoint using the header | Harness path / fallback 1 / descope |
| 5 | S4 decider on T4 | Endpoint InService within 20 min; p95 of `/invocations` ≤ 1.0 s; fp16 answers match fp32 within ±0.05 on 20 fixed states | dtype, DECIDER_READ_S, breaker thresholds, base image/AMI |

## Migration / Rollout

The `harness_sdk` runtime is updated in place: same name, entrypoint `main.py`. Rollout follows slice order: test scaffold and shared modules, then ports and decider client, then infra/decider, then agentcore.json, wiring, and scripts. Rollback:
- `cdk destroy DeciderStack` (the persistent stack stays).
- `agentcore remove` the new runtimes and credentials, then `agentcore deploy`.
- Revert the repo.

## Open Questions

- [ ] Does agentic-ctf `join_match` allow several characters per `sub` (shared client per runtime)? This is needed for more than 1 character per runtime.
- [ ] Is the Cognito pool persistent across weekly teardowns? If not, the hydrate script rewrites `discoveryUrl` weekly.
- [ ] What JSON key does agentic-ctf use in the client secret (`client_secret`)? This matters for Path B.
- [ ] Does the `wait_for_turn` server pending cap stay under the CloudFront origin timeout?

## Addendum: PR0 spike outcomes (2026-10-14)
Evidence in `spikes/FINDINGS.md`. These resolve [UNVERIFIED] markers; the rest of this document is unchanged.
- A8 confirmed (S5): `agentcore deploy` only runs the `agentcore/cdk` app with an explicit stack pattern.
- A11 selected (S2): `PYTHON_3_14` for all 4. CLI falls back to `aarch64-manylinux_2_28` automatically. Pin `strands-harness>=0.3.0` (the resolver otherwise picks 0.0.0).
- S1 selected: harness uses `mcp_servers={"ctf": {"url", "headers"}}`. Fallback 1 verified available; no descope.
- A9 Path A selected pending the real-token run. Corrections: env vars are `AGENTCORE_CREDENTIAL_<NAME>_CLIENT_ID/_CLIENT_SECRET` (no project prefix); the L3 already grants `GetResourceOauth2Token` (`workload-identity-directory/*`, `token-vault/*`) and `secretsmanager:GetSecretValue` on `bedrock-agentcore-identity!*` whenever `credentials[]` is non-empty, so fleet-wiring adds ONLY SSM and `sagemaker:InvokeEndpoint`. Runtime also receives `CREDENTIAL_<NAME>_NAME`.
- The workload access token is a per-request ContextVar (`BedrockAgentCoreContext`), confirming the `copy_context()` requirement for the background thread. The launcher must pass `runtimeUserId` on `InvokeAgentRuntime` [verify in the real run].
- PR0 found token validity of 1 h on agentic-ctf branch `feat/m2m-agent-clients`; the user requested the change to 4 h (decision D6, 2026-10-14). The gateway re-verifies the bearer on every request, so a match must end within 4 h of the preflight token fetch, which matches `maxLifetime` 14400. Re-verify the deployed value before the E2E match (task 4.5).
- agentic-ctf agent names are hyphen-only (`^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$`); the secret JSON keys are `client_id` and `client_secret`; the Cognito pool is destroyed with its stack (discoveryUrl drifts per redeploy). `hydrate_credentials.py` needs a runtime-name -> agentic-ctf-name mapping (`harness_sdk` -> `harness-sdk`).
- S4: `strands-decider serve` has NO dtype flag. dtype is `torch_dtype` in the checkpoint `strands_decider_config.json` (default bfloat16). The adapter must stage a checkpoint copy with the patched value from `DECIDER_DTYPE`. The T4 numbers (p95, fp16 vs fp32, InService) are pending user execution.
- Open questions resolved: multiple characters per sub are allowed (one MCP session = one join, names must be unique); `wait_for_turn` cap 25 s < CloudFront 120 s.

## Addendum: PR2 outcomes (2026-10-15)
- Added `shared/ctf_common/wiring.py` (runner wiring) and `strands_match.py` (Strands match loop shared by `harness_sdk` and `strand_decider`); `decider_loop` is split into `loop_logic.py` (pure) and `loop.py`; `strand_decider` has `gate.py` plus `strand_agent.py`.
- Decider client: `GuardedDecider(transport)` with `SageMakerDecider` and `HttpDecider`; errors carry their own retry/breaker policy. botocore has no per-call read timeout, so the transport pools clients by read timeout (rounded down to 100 ms) to make the tick clamp real.
- Harness MCP entry: no `type` key, `continue_on_error: false` (otherwise `create_harness` silently drops a server that fails to start).
- A dead MCP connection fails the match in every runtime (Strands returns an error tool result instead of raising): `ConnectionLostHook` for the Strands runtimes, `is_connection_lost` in the plain-Python loop.

## Addendum: slice 2 review fixes (2026-10-15)

- Breaker: `acquire()` returns an `Admission(allowed, owns_probe, epoch)`; only the probe owner releases it, and outcomes stamped with an older epoch (epoch increments on every open and close) are ignored. `allow()` stays as a boolean shortcut.
- Read-timeout buckets are 0.5 s steps (0.4 floor, 0.5 ... 2.5), rounded down: the SageMaker client pool holds at most 6 clients. Connect timeout is `min(1.0, read bucket)`.
- Strand gate: a malformed decider answer fails open (`Proceed("decider unavailable")`, warning log, not counted as a guide) and the handler sets `on_error = "proceed"`.
- TODO (revisit after the S4 benchmark): the `decider_loop` deadline split (`remaining - llm_budget`) was deliberately left unchanged in this round.

## Addendum: PR3 outcomes (2026-10-15)
- `infra/decider/` is its own CDK app (own `package.json`, `cdk.json`, jest). `lib/app.ts` builds a stack only when its input is present (`budgetEmail` / `CTF_BUDGET_EMAIL` for `CtfPersistentStack`, `imageTag` for `DeciderStack`), because `cdk deploy <name>` does not tell the app which stack was requested.
- `CtfPersistentStack`: ECR `ctf-decider` (IMMUTABLE tags, RETAIN, keep 3), CodeBuild image builder with an S3 asset source of `adapter/`, Budget $25 with direct EMAIL subscribers (no SNS), SSM `/ctf-decider/repo-uri` and `/ctf-decider/build-project`.
- `DeciderStack`: `containerStartupHealthCheckTimeoutInSeconds: 1200` is confirmed (CFN docs: 60 to 3600). `InferenceAmiVersion` pinned to `al2-ami-sagemaker-inference-gpu-3-1` (overridable by context) [UNVERIFIED on g4dn until the first deploy]. Model has `EnableNetworkIsolation: true` [UNVERIFIED]. The image is imported by repository name (no cross-stack reference, no exports).
- Adapter: dtype is patched into a staged checkpoint copy at start-up (`adapter/stage.py`); `adapter/bake.py` downloads the checkpoint and base model at pinned revisions at build time and fails if the dtype key is missing. The restart policy uses 3 consecutive failed health checks (a process exit restarts immediately).
- `tasks.md` 3.6 changed shape: `scripts/build_image.sh` (CodeBuild, `--yes` required) replaces `scripts/decider_image.sh`; the tag is a content hash of `adapter/` plus the model id. `Dockerfile.local` and `scripts/smoke_local.sh` were added for GPU-less local checks.

## Addendum: slice 3 review fixes (2026-10-15)
- Supersedes the PR3 note on `lib/app.ts`: BOTH stacks are always built. A stack whose input is missing (no `imageTag`, no budget email) falls back to an inert placeholder and carries an error annotation (`Annotations.addError`). The CDK CLI fails `deploy` and `synth <stack>` on error annotations but does not validate them in `destroy` (`selectStacksForDestroy`: "No validation"), so `cdk destroy DeciderStack` needs no inputs and a billing endpoint can always be stopped. `cdk deploy --ignore-errors` bypasses the guard. Makefile targets `deploy-persistent`, `deploy-decider`, `destroy-decider` wrap it.
- Base image: AWS Deep Learning Container `763104351884.dkr.ecr.us-east-2.amazonaws.com/pytorch-inference:2.6.0-gpu-py312-cu124-ubuntu22.04-sagemaker`, pinned by tag and digest `sha256:f5c4556668fe14fb9b7f97db0dd54cca324736c6ecd9cfa390b08c5c5aab0172` (verified read-only with `aws ecr describe-images`/`batch-get-image` on the registry; docs: https://github.com/aws/deep-learning-containers/blob/main/docs/src/data/pytorch-inference/2.6-gpu-sagemaker.yml and https://aws.github.io/deep-learning-containers/reference/available_images/). Chosen because it is the only inference line with CUDA 12.x: the newer `pytorch` lines (2.11 to 2.14, Amazon Linux 2023) are CUDA 13, which the driver-550 AMI cannot run. Trade-offs: the 2.6 line is past its published end of patching (2026-06-30); it ships torch 2.6.0 while `strands-decider==0.1.0` needs `torch>=2.7`, so the Dockerfile pins `torch==2.7.1` (the version of the previous base; unpinned pip would take a CUDA 13 wheel). The image's `LD_LIBRARY_PATH` does not include `/usr/local/cuda/lib64`, so the wheel's bundled CUDA 12.6 libraries are not shadowed by the DLC's 12.4 ones [UNVERIFIED on a GPU]. The DLC's `ENTRYPOINT` (`dockerd-entrypoint.py`) and `CMD` (`torchserve --start ...`) were read from the image config and are both overridden. CodeBuild logs in to that registry and its role gets `ecr:GetAuthorizationToken` plus read-only pull on that one repository. Fallback not needed: an ECR pull-through cache for Docker Hub (needs a Docker Hub credential secret).
- Failure handling (adapter): after an upstream timeout or connection error the slot is held and `/health` is polled until the decider answers (bounded by `ADAPTER_DRAIN_TIMEOUT_S`, default 30 s); past the limit the supervisor is told the decider is unhealthy and restarts it (counts against the single restart). A waiter that reaches the front while the decider is out of service gets 503 `not_ready`. This assumes `/health` cannot answer while the decider computes [UNVERIFIED against the real server; if it can, the drain returns early].
- Probes: skipped while the gate is busy; a probe that overlapped a request is discarded; a refused connection or a process exit restarts at once; timeouts and non-200 count only when idle (3 in a row, `ADAPTER_HEALTH_FAILURES`).
- `/invocations` shapes: NaN/Infinity in upstream JSON is 502 `bad_upstream_response`; an upstream `adapter` object is merged (the adapter owns `queue_wait_ms`), a non-object value is kept as `upstream_adapter`. `asyncio.shield` was replaced by `asyncio.wait` on the task: on Python 3.12+ shield logs "exception in shielded future" when the waiter was cancelled and the call fails later.
- IAM (execution role): `cloudwatch:PutMetricData` on `*` with `cloudwatch:namespace = /aws/sagemaker/Endpoints` (the endpoint instance-metrics namespace; taken from the SageMaker CloudWatch docs, not re-verified in this run) and `aws:SourceAccount` / `aws:SourceArn` (`arn:<partition>:sagemaker:<region>:<account>:*`) conditions on the trust policy.
- Build inputs: the model id lives in `lib/names.ts` only; `build_image.sh` reads it, has no `--model-id`, and treats only `ImageNotFoundException` as "build it"; the Dockerfile default is checked by a test and the `cdk.json` copy was removed. The buildspec pushes only when `CODEBUILD_BUILD_SUCCEEDING = 1`. A test proves `hash_source.py` gives the same hash for the adapter directory and for an unzipped copy of the S3 asset (parsed exclude globs).
- Known open item: the Budget cost-filter string `Amazon SageMaker` is still unverified (a Cost Explorer check is a billed API call; command in the README).

## Addendum: slice 3 base image switched from DLC 2.6 to ECR Public python:3.11-slim (2026-10-15)
- Supersedes the "Base image" bullet of the slice 3 review fixes. `adapter/Dockerfile` now builds on `public.ecr.aws/docker/library/python:3.11.17-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89` (read-only `docker buildx imagetools inspect`), and `Dockerfile.local` uses the same base.
- Why: the DLC 2.6 line is past its end of patching, we reinstalled torch 2.7.1 on top of it anyway, its ENTRYPOINT and CMD had to be overridden, and its CUDA 12.4 libraries were mixed with the 12.6 libraries of the wheel. A DLC only helps if you use what it ships. ECR Public avoids Docker Hub rate limits and needs no login.
- torch: `torch==2.7.1` from `https://download.pytorch.org/whl/cu126` (2.7.1 has no cu124 build; cu126 is the oldest published, run on the driver-550 AMI through CUDA 12.x minor-version compatibility). `strands-decider==0.1.0` and the adapter dependencies are installed with a constraint on `torch==2.7.1+cu126` so pip cannot replace it. The wheel bundles the CUDA runtime and libgomp [UNVERIFIED on a GPU].
- Removed: the buildspec login to the DLC registry, the CodeBuild pull grant on the DLC repository (the auth token stays, via `grantPullPush`, for our own ECR push), the `DLC_*` constants, and the hygiene allow-list entry. The jest test that matched the Dockerfile registry to the stack constants became a test that the base is ECR Public, tag- and digest-pinned, and that the buildspec logs in to no third-party registry.

