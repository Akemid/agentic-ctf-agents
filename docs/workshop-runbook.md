# Workshop runbook

How to stand up the CTF agent fleet for a workshop and tear it down again. Four AgentCore runtimes
(`harness`, `harness_sdk`, `strand_decider`, `decider_loop`) play against the agentic-ctf MCP server. The two
decider runtimes call a SageMaker endpoint (`ctf-decider`) that bills by the hour while it exists.

> Claude Code users: the skill `.claude/skills/ctf-fleet-ops/SKILL.md` encodes this runbook as an agent
> procedure with confirmation gates. This file is the human version; keep both in sync.

> **Cost warning.** The game stack bills 24/7 (ALB plus Fargate, about USD 25 per month), so destroy it between
> workshops too (step 10). The GPU endpoint (`ml.g4dn.xlarge`, 1 instance) costs about USD 0.74 per hour, roughly
> **USD 537 per month if it is forgotten**. Destroy it (step 10) the moment the workshop ends. A SageMaker
> budget alert is deployed in step 2, but an alert is not a stop: you still have to destroy the endpoint.

All commands run from the repository root unless a `cd` is shown. Replace `<...>` placeholders with your own
values; nothing account-specific is stored in this repository.

## Stacks at a glance

| Stack | Repo | Deploy | Bills |
|---|---|---|---|
| `AgenticCtfStack` (game) | agentic-ctf | per workshop | about USD 25/month while up |
| `CtfPersistentStack` | `infra/decider` | once | about nothing |
| `DeciderStack` (endpoint) | `infra/decider` | per workshop, OPTIONAL | USD 0.736/h |
| `AgentCore-ctfagents-default` (4 runtimes, 4 OAuth providers) | repo root | when runtimes change | per use, idle about nothing |

Region `us-east-2`. Order: game, then agents, then (optionally) the decider.

## Order at a glance

| # | Step | Where | Frequency | Mutates AWS |
|---|---|---|---|---|
| 0 | Prerequisites | - | once | no |
| 1 | `AgenticCtfStack` (game server, Cognito, SSM) | the agentic-ctf repo | per game stack | yes |
| 2 | Persistent stack (ECR, image builder, Budget) | `infra/decider` | once | yes |
| 3 | Build the decider image | `infra/decider` | when the adapter or model changes | yes (CodeBuild, ECR) |
| 4 | `DeciderStack` (GPU endpoint) | `infra/decider` | per workshop | yes, **billing starts** |
| 5 | Deploy the runtimes (`make deploy YES=1`: hydrate, check, validate, deploy, clean) | repo root | when runtimes change | yes |
| 6 | Verify the deploy (also after every game redeploy) | repo root | after step 5 | no |
| 7 | Launch the fleet | `scripts/fleet.py` | per match | yes |
| 8 | Play and monitor | - | - | no |
| 9 | Stop the fleet | `scripts/fleet.py` | per match | yes |
| 10 | Teardown | `infra/decider` | per workshop | yes, **billing stops** |

## 0. Prerequisites

- Tools: Node 22 with `agentcore` (the AgentCore CLI), `uv`, the AWS CLI, Docker is NOT needed (the image is
  built in CodeBuild).
- AWS credentials for the target account and region (`aws sts get-caller-identity` shows who you are).
  The AgentCore deploy also needs `iam:CreateServiceLinkedRole` once (runtime identity).
- `agentcore/aws-targets.json` holds your account and region. It is local to your checkout; keep it out of
  commits (for example `git update-index --skip-worktree agentcore/aws-targets.json`).
- **Quota**: SageMaker `ml.g4dn.xlarge for endpoint usage` must be at least 1 in the region (Service Quotas
  console or `aws service-quotas list-service-quotas --service-code sagemaker`). Request an increase days
  ahead; it is not instant.
- **Budget alert recipient**: decide the email address for step 2. It is read from an environment variable and
  never committed.
- Local checks before touching AWS (all offline):

```bash
uv run pytest                                   # whole Python suite
(cd agentcore/cdk && node_modules/.bin/jest)    # run `npm ci` there first on a fresh checkout
(cd infra/decider && node_modules/.bin/jest)    # run `make install` there first
uv run python scripts/sync_common.py --check    # vendored ctf_common copies match shared/
(cd agentcore && agentcore validate)
```

`make check` runs the same offline checks in one go.

## 1. AgenticCtfStack (other repo)

Deploy the game stack from the agentic-ctf repository, passing ALL four agent client names (without them the
stack does not create the clients and step 5 fails on missing SSM parameters):

```bash
pnpm infra:deploy -c agentClientNames=harness,harness-sdk,strand-decider,decider-loop
```

It owns the MCP server, the Cognito pool, 4 M2M clients (4 h tokens), the `/agentic-ctf/*` SSM parameters and the
`agentic-ctf/agents/*` secrets. This repository only READS them:

```bash
aws ssm get-parameter --name /agentic-ctf/mcp-url --query Parameter.Value --output text
aws ssm get-parameter --name /agentic-ctf/cognito/discovery-url --query Parameter.Value --output text
for a in harness harness-sdk strand-decider decider-loop; do
  aws ssm get-parameter --name "/agentic-ctf/agents/$a/client-id" --query Parameter.Name --output text
  aws ssm get-parameter --name "/agentic-ctf/agents/$a/secret-arn" --query Parameter.Name --output text
done
```

All of them must exist. The agent client names are defined in ONE place on our side,
`scripts/fleet_config.py` (`FLEET`); if the other team renames an agent, edit it there.

**Token lifetime.** The agent Cognito clients issue access tokens valid for 4 hours, so a match must finish
within 4 hours of the token fetch (there is no refresh or reconnect logic by design; a 401 fails the match).
The PR0 spike once found 1 hour on an older agentic-ctf branch; the current game stack deploys 4 hours. Verify
the DEPLOYED value (read-only) before the workshop:

```bash
DISCOVERY=$(aws ssm get-parameter --name /agentic-ctf/cognito/discovery-url --query Parameter.Value --output text)
POOL=$(echo "$DISCOVERY" | sed -E 's#https://cognito-idp\.[^/]+/([^/]+)/.*#\1#')
CLIENT=$(aws ssm get-parameter --name /agentic-ctf/agents/harness-sdk/client-id --query Parameter.Value --output text)
aws cognito-idp describe-user-pool-client --user-pool-id "$POOL" --client-id "$CLIENT" \
  --query 'UserPoolClient.[AccessTokenValidity,TokenValidityUnits.AccessToken]'
```

Expect `240 minutes` or `4 hours`. If it is shorter, keep matches shorter than that.

## 2. Persistent stack (once)

ECR repository, the CodeBuild image builder, the SageMaker budget ($25/month, alerts at 80% actual and
100% forecast) and the SSM parameter `/ctf-decider/endpoint-name`. It stays between workshops and costs almost
nothing.

**Deploy order.** The parameter `/ctf-decider/endpoint-name` used to belong to `DeciderStack`. Deploy this stack only while `DeciderStack` is
destroyed (or destroy `DeciderStack` first); a parameter owned by both stacks fails the deploy.

```bash
cd infra/decider
make install
CTF_BUDGET_EMAIL=<your-email> make deploy-persistent
```

`make -n deploy-persistent` shows the command without running it. **Unverified:** the budget filters on the cost
service name `Amazon SageMaker`; that string has not been checked against what Cost Explorer reports in your
account, so the alert could match nothing. How to check it (a billed Cost Explorer call, about USD 0.01) is in
`infra/decider/README.md`, section "Budget cost filter (unverified)".

## 3. Build the decider image (when the adapter or model changes)

```bash
cd infra/decider
make build-image YES=1          # CodeBuild builds and pushes; prints imageTag=<hash>
```

**Reuse the image when possible.** Check whether the current adapter source already has an image in ECR:

```bash
cd infra/decider
make build && scripts/build_image.sh      # no --yes: prints the computed "image tag", builds nothing
aws ecr describe-images --repository-name ctf-decider --query 'imageDetails[].imageTags[0]' --output text
```

If the tag is listed, skip this step and deploy it directly in step 4 (no rebuild, no persistent-stack redeploy).
Only when `adapter/` changed: redeploy the persistent stack first (the CodeBuild source is an S3 asset and the
build refuses a stale upload; do it only while `DeciderStack` is destroyed), then `make build-image YES=1`.
A build costs about USD 0.11.

The build ends with a strict smoke check (`python -m adapter.smoke --require-checkpoint`), so a dependency problem
fails the BUILD and never reaches the endpoint. All dependencies are hash-locked (`requirements.lock`,
`torch.lock`, torch 2.14.1+cu126): do not unpin them. Background: `openspec/changes/agentcore-ctf-fleet/decider-image-deps.md`.

## 4. DeciderStack (OPTIONAL, starts billing)

Get the user's explicit OK first: from the moment the endpoint is created it bills USD 0.74 per hour.

```bash
cd infra/decider
node_modules/.bin/cdk diff DeciderStack -c imageTag=<hash>        # review the IAM changes
node_modules/.bin/cdk deploy DeciderStack -c imageTag=<hash> --require-approval never
aws sagemaker describe-endpoint --endpoint-name ctf-decider --query EndpointStatus --output text
```

> `IMAGE_TAG=<hash> make deploy-decider` is equivalent: the Makefile passes `--require-approval never`, so it
> works in a non-interactive shell (CI, an agent). Always review `IMAGE_TAG=<hash> make diff-decider` first.

Watch the CloudWatch log group `/aws/sagemaker/Endpoints/ctf-decider`:

Measured timeline (7.5 GB image on `ml.g4dn.xlarge`):

| Event | Elapsed |
|---|---|
| `cdk deploy` start | t0 |
| `CreateEndpoint` | about t0 + 1 min |
| container start (`adapter.runtime: starting the decider`) | about 3.5-4 min after `CreateEndpoint` (SageMaker provisions the GPU and pulls the image) |
| `GET /ping 200` (model load + warm-up) | about 30 s after container start |
| `InService` | shortly after `/ping 200` |
| **Total** | **about 5-6 min** from deploy to usable |

For the first ~4 minutes the endpoint shows `Creating` and the container logs are EMPTY. That is normal. To wait,
poll every 30 s and stop at the first terminal signal: `filter-log-events` on the log group for
`GET /ping HTTP/1.1" 200`, `staying unhealthy` or `Traceback`, plus `describe-endpoint` (`InService` or `Failed`).

- Healthy: `GET /ping 200`, then status `InService`. Wait for `InService` before you continue.
- Failing (ONLY these signals): `decider failed again ... staying unhealthy`, a Traceback, or `Failed`. Delete the
  stack at once: `aws cloudformation delete-stack --stack-name DeciderStack`.

> **A `Creating` endpoint cannot be deleted.** SageMaker refuses until the start-up health-check timeout (600 s)
> expires, so a broken container bills for up to about 10 minutes even after you delete the stack. Confirm with
> `aws sagemaker list-endpoints` afterwards.

Validate before the workshop:

- One real inference: invoke `ctf-decider` (`sagemaker-runtime invoke-endpoint`, or the `SageMakerDecider` client)
  with the body `infra/decider/adapter/adapter/warmup_body.json`:

  ```python
  import boto3
  rt = boto3.client("sagemaker-runtime", region_name="us-east-2")
  body = open("infra/decider/adapter/adapter/warmup_body.json", "rb").read()
  r = rt.invoke_endpoint(EndpointName="ctf-decider", ContentType="application/json", Body=body)
  print(r["Body"].read().decode())
  ```

  A healthy answer has `answers.threat`, `answers.route_bad` and `answers.action` (with `choice` and
  `probabilities`), plus `adapter.queue_wait_ms`. The first call from a fresh client takes about 2 s (connection
  setup); after that about 585 ms round trip from a laptop, about 450 ms on the GPU.
- Latency gate S4 (p95 <= 1000 ms; measured p50 446 ms, p95 646 ms):

  ```bash
  uv run python spikes/s4_bench.py run --log <decider log> --endpoint ctf-decider --region us-east-2 --n 30 --out <file>
  ```

**The decider is optional.** The runtimes read `/ctf-decider/endpoint-name` (kept by the persistent stack) when a
match starts and pass preflight with or without this stack. With the endpoint down the first call fails
("endpoint not found"), the client's breaker opens for 300 s, `strand_decider` proceeds ungated and
`decider_loop` plays on the LLM. CloudWatch shows an INFO line, "decider endpoint unavailable ...; playing on the
LLM fallback". Bring the endpoint up mid-match and it is picked up within that 300 s window. Skip this whole step
for a workshop that does not need the trained decider.

## 5. Deploy the runtimes (safe path)

Use the wrapper. It is the only documented way to run `agentcore deploy` in this repository:

```bash
rm -rf agentcore/.cache  # gitignored; it held stale staging once, so clear it before every deploy
make deploy              # prints the plan, runs nothing
make deploy YES=1        # MUTATES AWS (same as: YES=1 scripts/deploy_fleet.sh)
```

Never read or print `agentcore/.env.local` (it holds the secrets).

In this order, and stopping at the first failure:

1. `sync_common.py --check`: vendored `ctf_common` copies match `shared/`.
2. `hydrate_credentials.py`: copies the client ids and secrets from SSM and Secrets Manager (read-only) into the
   gitignored `agentcore/.env.local` and writes the real `discoveryUrl` into the working copy. Nothing is printed.
3. `hydrate_credentials.py --check`: offline; fails if the placeholder URL or any variable is still missing.
4. `(cd agentcore && agentcore validate)`.
5. `agentcore deploy --yes`: the only mutating step. It only touches the `AgentCore-<project>-<target>` stack and
   can never create the GPU endpoint.
6. `hydrate_credentials.py --clean`: runs in a trap on EXIT, so it always happens, also when a step fails or you
   press Ctrl-C. It wipes the secrets and restores the placeholder, so `git status` is clean afterwards (no
   `skip-worktree` needed).

**Why not just `agentcore deploy`?** The CLI does NOT fail when an OAuth credential provider has no client id or
secret in `.env.local`: it logs `Skipped <provider>: ...` and reports success. A deploy without hydration
therefore "works", creates no providers, and the runtimes fail later at their first token request. The wrapper
makes the right order the only order. If you ever deploy by hand, read the output for `Skipped`.

- `agentcore.json` is committed with a **placeholder** `discoveryUrl` (a reserved `.invalid` host) so the public
  repository carries no Cognito pool id.
- **After EVERY game redeploy** (new Cognito pool and new clients) rotate the credentials. `discoveryUrl`
  drifts, so a plain deploy fails loudly; accept the new URL once you have confirmed the change is expected:

  ```bash
  rm -rf agentcore/.cache
  UPDATE_DISCOVERY_URL=1 make deploy YES=1
  ```

  The AgentCore CLI (0.31.1) UPDATES the existing OAuth providers (client id, secret and discovery URL) from
  `.env.local`. No delete or recreate is needed. Then run the checks in step 6.

- The hydrate script writes `agentcore.json` first and `.env.local` second. If it dies in between, `--check`
  reports the missing `.env.local`, and `--clean` restores the placeholder.

## 6. Verify the deploy

Do not trust a green CLI output alone. Check AWS (all read-only):

```bash
agentcore status
aws bedrock-agentcore-control list-agent-runtimes          # the 4 runtimes are READY
# OAuth providers: clientId and discoveryUrl must equal the game's SSM values
for rt in harness harness_sdk strand_decider decider_loop; do
  aws bedrock-agentcore-control get-oauth2-credential-provider --name "ctf_$rt"
done
aws ssm get-parameter --name /agentic-ctf/cognito/discovery-url --query Parameter.Value --output text
aws ssm get-parameter --name /agentic-ctf/agents/<name>/client-id --query Parameter.Value --output text   # per agent
grep -c discovery-url-not-hydrated agentcore/agentcore.json     # placeholder restored: 1 or more
git status --short                                              # clean
```

Compare the provider's `clientId`/`discoveryUrl` with the SSM values (<name> is `harness`, `harness-sdk`,
`strand-decider`, `decider-loop`). The SSM runtime ARNs are checked below.

The synthesized stack gives each runtime its environment (`CTF_*`, parameter names only) plus read-only
`ssm:GetParameter` on `/agentic-ctf/mcp-url`; the two decider runtimes also get `ssm:GetParameter` on the single
parameter `/ctf-decider/endpoint-name` and `sagemaker:InvokeEndpoint` on the `ctf-decider` endpoint.
`agentcore/.cli/deployed-state.json` is local and gitignored; the launcher reads the runtime ARNs from it.

The stack also publishes each runtime ARN as a plain SSM String parameter (no CloudFormation export):

```bash
for r in harness harness_sdk strand_decider decider_loop; do
  aws ssm get-parameter --name "/ctfagents/runtimes/$r/arn" --query Parameter.Value --output text
done
```

The value is the runtime ARN that `InvokeAgentRuntime` takes (default endpoint). `fleet.py` falls back to these
parameters when `deployed-state.json` has no ARN for a runtime (it needs `ssm:GetParameters` on
`/ctfagents/runtimes/*`; `--region` picks the SSM region), and so can any caller that is not on this machine,
such as the web lobby.

Note: the L3 grants `GetResourceOauth2Token` on `token-vault/*` to every runtime role, so a runtime uses only
its own credential by convention, not because IAM forbids the others.

## 7. Launch the fleet

`fleet.py` prints a plan and changes nothing unless you add `--yes`. Limits: at most 3 characters per agent
type and 2 teams, checked before the first invocation.

```bash
uv run python scripts/fleet.py start --match <match-id> \
  --agent harness=1 --agent harness_sdk=1 --agent strand_decider=2 --agent decider_loop=2      # dry run
uv run python scripts/fleet.py start --match <match-id> \
  --agent harness=1 --agent harness_sdk=1 --agent strand_decider=2 --agent decider_loop=2 --yes
```

Each character has its own session (id of 33+ characters) and its own MCP session, with unique names such as
`decider-loop-2`. A failed start does not stop the others; the command exits 1 and names the failures. Sessions
are recorded in `.fleet/<match-id>.json` (gitignored). Add `--teams 1` to put every character on one team.

### Waking one agent into an exact seat (what the web lobby does)

```bash
uv run python scripts/fleet.py start --match <match-id> --agent decider_loop=1 --team red --agent-index 1 --yes
#   decider-loop-red-2: ok (started) team red agent_index 1
```

`--agent-index N` (0..2, the same name and range as the server's `join_match`; `--slot` is an equivalent option,
the web lobby's key) needs `--team red|blue` and exactly one character; it names the character
`<runtime>-<team>-<agent_index+1>`. The payload key is `agent_index`; the runtimes also accept `slot` (equal values
are fine, different values are `rejected`) and `started` echoes both. `start` is synchronous up to the seat: the runtime calls `join_match`
before it answers, so `started` means the character is seated in that exact seat, and the printed team and
agent_index are what the server assigned. A refused seat is a failed start (exit 1) with the reason, for example
`join_match failed: slot_taken`; the runtime never falls back to another seat. After a refusal the runner is restartable at once (an immediate retry on another seat works). A start can therefore take up
to 15 s to answer (30 s for `harness`, whose model has to call `join_match` itself). See
`openspec/changes/agentcore-ctf-fleet/lobby-wake.md`.

## 8. Play and monitor

```bash
uv run python scripts/fleet.py status --match <match-id>
agentcore logs
aws sagemaker describe-endpoint --endpoint-name ctf-decider --query EndpointStatus --output text
```

**Decider or LLM fallback?** The runtime log groups are `/aws/bedrock-agentcore/runtimes/ctfagents_<runtime>-<id>-DEFAULT`:

```bash
aws logs filter-log-events --log-group-name <group> --start-time <epoch-ms> \
  --filter-pattern '"playing on the LLM fallback"'
```

A match means the endpoint was down then and the runtime played on the LLM. No match while matches ran with the
endpoint `InService` means it used the decider. The paginated CLI output can print several counts: use a boto3
paginator, or sum them. An agent woken while the endpoint is `InService` uses the decider from the first turn; one
already in a match picks it up within 300 s (breaker re-probe). For a cost audit, CloudTrail `lookup-events` with
`EventName=CreateEndpoint` / `DeleteEndpoint` gives the exact times, hence the billed duration.

`status` reports `running`, `finished`, `failed`, with the tick count, and stores the last status and the time
it was last seen in `.fleet/<match-id>.json`. It does NOT query a session that was stopped, whose start failed,
whose last status is `finished`/`failed`/`stopped`, or that is older than the 4 hour max lifetime: it prints
`ended (not queried)` with the last known status instead. The reason: invoking an ended session boots a fresh
empty microVM (a cost, and a misleading `idle`). `--wake` queries anyway. A `failed` match with an auth reason
means the token outlived its validity (see the token note in step 1).

## 9. Stop

```bash
uv run python scripts/fleet.py stop --match <match-id>            # dry run
uv run python scripts/fleet.py stop --match <match-id> --yes      # cooperative stop
uv run python scripts/fleet.py stop --all --hard --yes            # StopRuntimeSession for everything recorded
```

The cooperative stop is sent only to sessions that may still be alive. Ended or aged-out sessions are ended with
`StopRuntimeSession` alone (an already-gone session counts as stopped), so stopping never wakes anything.

Idle sessions end by themselves after 15 minutes and every session after 4 hours at most.

## 10. Teardown

Full teardown, in this order:

```bash
uv run python scripts/fleet.py stop --all --hard --yes          # 1. end every recorded session
cd infra/decider
node_modules/.bin/cdk destroy DeciderStack --force              # 2. stops the endpoint billing
aws sagemaker list-endpoints --query 'Endpoints[].EndpointName' --output text   # expect nothing
```

3. Destroy `AgenticCtfStack` from the agentic-ctf repo. It bills 24/7 (about USD 25 per month), so do not leave it
   up between workshops. This repository never writes to its resources.

`make destroy-decider` is equivalent to step 2 (it passes `--force`). It keeps the
`/ctf-decider/endpoint-name` parameter on purpose.

What stays, on purpose:

- The persistent stack (ECR, image builder, Budget): about nothing.
- The AgentCore runtimes: an idle runtime costs nothing. To remove them anyway, use
  `agentcore remove agent --name <runtime>` for each runtime, then deploy again with `make deploy YES=1` (the
  wrapper hydrates first).
- The OAuth credential providers can be removed too, if you want them gone: `agentcore remove credential
  --name <credential> [--yes]` for each of `ctf_harness`, `ctf_harness_sdk`, `ctf_strand_decider`,
  `ctf_decider_loop`, then `make deploy YES=1`. Removing the credentials from `agentcore.json` also means
  `fleet_config.py` and its tests no longer match it; restore them from git before the next workshop.
- Next workshop, after redeploying the game stack: `UPDATE_DISCOVERY_URL=1 make deploy YES=1` (step 5).

Costs (us-east-2): endpoint USD 0.736/h, image build about USD 0.11, ECR about USD 0.10/GB-month, game stack about
USD 25/month while up, idle runtimes about nothing.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `hydrate_credentials.py` names an agent and an SSM path | the game stack does not publish that agent | deploy or fix the agentic-ctf stack; check `FLEET` in `scripts/fleet_config.py` |
| `discovery URL drift` | the Cognito pool was recreated | confirm, then `--update-discovery-url` and redeploy (step 5) |
| the deploy printed `Skipped <provider>` | the credentials were not hydrated | never deploy by hand; use `make deploy YES=1` |
| `fleet.py` says no runtime ARN | `agentcore deploy` has not run on this machine and SSM has no `/ctfagents/runtimes/<runtime>/arn` | deploy, or pass `--runtime-arn RUNTIME=ARN` |
| a start is `rejected` with `join_match failed: slot_taken` (or `match_full`, `match_already_started`) | the seat is taken, the team is full or the match began | pick another `--agent-index`/`--team`; nothing was seated |
| a start fails with `join_match timed out` | the server or the model did not answer within the join timeout | check the MCP URL and the server; retry with a NEW session id (the old session stays HealthyBusy until its thread exits, bounded by the MCP HTTP read timeout of about 300 s; `StopRuntimeSession` forces the recovery) |
| `InvokeAgentRuntime` says `AccessDenied` for a caller that sends `runtimeUserId` | the role has `InvokeAgentRuntime` but not `InvokeAgentRuntimeForUser` | add `bedrock-agentcore:InvokeAgentRuntimeForUser` on the runtime and `runtime-endpoint/*` |
| a start reply says `rejected` with an SSM message | the MCP URL or decider parameter is missing | check steps 1 and 2 (`/ctf-decider/endpoint-name` is created by the persistent stack) |
| decider runtimes fall back to the LLM | endpoint absent (DeciderStack destroyed), not `InService`, throttled or over its latency budget | expected when the decider is down (the decider is optional); check the endpoint status and the runtime logs. A missing endpoint is retried after 300 s |
| deploying `CtfPersistentStack` fails because `/ctf-decider/endpoint-name` already exists | `DeciderStack` from before this change still owns it | destroy `DeciderStack`, then deploy the persistent stack |
| `cdk deploy` of the decider refuses to run | no `imageTag` context (intentional guard) | pass the tag printed by `make build-image` |
| A raw `cdk deploy` hangs with no output | the IAM approval prompt in a non-interactive shell | Ctrl-C; use `make deploy-decider` / `make deploy-persistent` (they pass `--require-approval never`) after the matching `make diff-*` |
| the endpoint never turns healthy (`No module named 'torch.distributed.checkpoint.hf_storage'`) | unpinned dependencies (fixed by the hash lock and the build-time smoke check) | rebuild the image; never unpin. See `decider-image-deps.md` |
| the endpoint is `Creating` and the container logs are empty for about 4 minutes | normal: SageMaker provisions the GPU and pulls the 7.5 GB image | wait; only `staying unhealthy`, a Traceback or `Failed` mean failure (step 4) |
| the endpoint still bills after you deleted a broken stack | a `Creating` endpoint cannot be deleted until the 600 s start-up check expires | wait about 10 minutes, then confirm `list-endpoints` is empty |
| runtimes run old code or staging | stale gitignored `agentcore/.cache` | `rm -rf agentcore/.cache`, redeploy |
| hydrate names an agent, or the game has no agent clients | the game stack was deployed without all four `agentClientNames` | redeploy the game with `-c agentClientNames=harness,harness-sdk,strand-decider,decider-loop`, then rotate (step 5) |
| `build-image` fails on the source hash | stale S3 asset | redeploy `CtfPersistentStack`, then rebuild |

The end-to-end acceptance run is described in `docs/e2e-acceptance.md`.
