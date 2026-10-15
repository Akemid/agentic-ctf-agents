# Workshop runbook

How to stand up the CTF agent fleet for a workshop and tear it down again. Four AgentCore runtimes
(`harness`, `harness_sdk`, `strand_decider`, `decider_loop`) play against the agentic-ctf MCP server. The two
decider runtimes call a SageMaker endpoint (`ctf-decider`) that bills by the hour while it exists.

> **Cost warning.** The GPU endpoint (`ml.g4dn.xlarge`, 1 instance) costs about USD 0.74 per hour, roughly
> **USD 537 per month if it is forgotten**. Destroy it (step 10) the moment the workshop ends. A SageMaker
> budget alert is deployed in step 2, but an alert is not a stop: you still have to destroy the endpoint.

All commands run from the repository root unless a `cd` is shown. Replace `<...>` placeholders with your own
values; nothing account-specific is stored in this repository.

## Order at a glance

| # | Step | Where | Frequency | Mutates AWS |
|---|---|---|---|---|
| 0 | Prerequisites | - | once | no |
| 1 | `AgenticCtfStack` (game server, Cognito, SSM) | the agentic-ctf repo | per game stack | yes |
| 2 | Persistent stack (ECR, image builder, Budget) | `infra/decider` | once | yes |
| 3 | Build the decider image | `infra/decider` | when the adapter or model changes | yes (CodeBuild, ECR) |
| 4 | `DeciderStack` (GPU endpoint) | `infra/decider` | per workshop | yes, **billing starts** |
| 5 | Deploy the runtimes (`make deploy YES=1`: hydrate, check, validate, deploy, clean) | repo root | when runtimes change | yes |
| 6 | Verify the deploy | repo root | after step 5 | no |
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

Deploy the game stack from the agentic-ctf repository. It owns the MCP server, the Cognito pool and the
per-agent OAuth clients. This repository only READS its SSM parameters:

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

**Token lifetime.** The design assumes the agent Cognito clients issue access tokens valid for 4 hours, so a
match must finish within 4 hours of the token fetch (there is no refresh or reconnect logic by design; a 401
fails the match). The PR0 spike found 1 hour on the agentic-ctf branch and a change to 4 hours was
requested. Verify the DEPLOYED value (read-only) before the workshop:

```bash
DISCOVERY=$(aws ssm get-parameter --name /agentic-ctf/cognito/discovery-url --query Parameter.Value --output text)
POOL=$(echo "$DISCOVERY" | sed -E 's#https://cognito-idp\.[^/]+/([^/]+)/.*#\1#')
CLIENT=$(aws ssm get-parameter --name /agentic-ctf/agents/harness-sdk/client-id --query Parameter.Value --output text)
aws cognito-idp describe-user-pool-client --user-pool-id "$POOL" --client-id "$CLIENT" \
  --query 'UserPoolClient.[AccessTokenValidity,TokenValidityUnits.AccessToken]'
```

Expect `240 minutes` or `4 hours`. If it is shorter, keep matches shorter than that.

## 2. Persistent stack (once)

ECR repository, the CodeBuild image builder and the SageMaker budget ($25/month, alerts at 80% actual and
100% forecast). It stays between workshops and costs almost nothing.

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

Redeploy the persistent stack first if you edited `adapter/` (the build refuses a stale source upload). Skip
this step when the tag from the previous workshop is still the right one.

## 4. DeciderStack (starts billing)

```bash
cd infra/decider
IMAGE_TAG=<hash> make deploy-decider
aws sagemaker describe-endpoint --endpoint-name ctf-decider --query EndpointStatus --output text
```

The deploy waits for the endpoint (start-up timeout 1200 s, typically 10 to 20 minutes). It must read
`InService` before you continue. The stack publishes `/ctf-decider/endpoint-name`, which the decider
runtimes read when a match starts.

## 5. Deploy the runtimes (safe path)

Use the wrapper. It is the only documented way to run `agentcore deploy` in this repository:

```bash
make deploy              # prints the plan, runs nothing
make deploy YES=1        # MUTATES AWS (same as: YES=1 scripts/deploy_fleet.sh)
```

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
- If the game stack was recreated, the Cognito pool changed and `discoveryUrl` drifts: hydrate fails loudly and
  tells you to accept the new URL once you have confirmed the change is expected:

  ```bash
  make deploy YES=1 UPDATE_DISCOVERY_URL=1
  ```

- The hydrate script writes `agentcore.json` first and `.env.local` second. If it dies in between, `--check`
  reports the missing `.env.local`, and `--clean` restores the placeholder.

## 6. Verify the deploy

```bash
agentcore status
```

The synthesized stack gives each runtime its environment (`CTF_*`, parameter names only) plus read-only
`ssm:GetParameter` on `/agentic-ctf/mcp-url`; the two decider runtimes also get `ssm:GetParameter` on the single
parameter `/ctf-decider/endpoint-name` and `sagemaker:InvokeEndpoint` on the `ctf-decider` endpoint.
`agentcore/.cli/deployed-state.json` is local and gitignored; the launcher reads the runtime ARNs from it.

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

## 8. Play and monitor

```bash
uv run python scripts/fleet.py status --match <match-id>
agentcore logs
aws sagemaker describe-endpoint --endpoint-name ctf-decider --query EndpointStatus --output text
```

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

```bash
cd infra/decider
make destroy-decider                                     # stops the endpoint billing; needs no tag or email
aws sagemaker list-endpoints --query 'Endpoints[].EndpointName' --output text   # expect nothing
```

- The persistent stack (ECR, image builder, Budget) stays on purpose.
- AgentCore runtimes are optional to keep: an idle runtime costs nothing. To remove them, use
  `agentcore remove agent --name <runtime>` for each runtime, then deploy again with `make deploy YES=1` (the
  wrapper hydrates first).
- The OAuth credential providers can be removed too, if you want them gone: `agentcore remove credential
  --name <credential> [--yes]` for each of `ctf_harness`, `ctf_harness_sdk`, `ctf_strand_decider`,
  `ctf_decider_loop`, then `make deploy YES=1`. Removing the credentials from `agentcore.json` also means
  `fleet_config.py` and its tests no longer match it; restore them from git before the next workshop.
- The Cognito pool is recreated every time the game stack is redeployed. After a game redeploy run
  `make deploy YES=1 UPDATE_DISCOVERY_URL=1` (step 5), which re-hydrates with `--update-discovery-url`.
- `AgenticCtfStack` is torn down from the agentic-ctf repo. This repository never writes to its resources.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `hydrate_credentials.py` names an agent and an SSM path | the game stack does not publish that agent | deploy or fix the agentic-ctf stack; check `FLEET` in `scripts/fleet_config.py` |
| `discovery URL drift` | the Cognito pool was recreated | confirm, then `--update-discovery-url` and redeploy (step 5) |
| the deploy printed `Skipped <provider>` | the credentials were not hydrated | never deploy by hand; use `make deploy YES=1` |
| `fleet.py` says no runtime ARN | `agentcore deploy` has not run on this machine | deploy, or pass `--runtime-arn RUNTIME=ARN` |
| a start reply says `rejected` with an SSM message | the MCP URL or decider parameter is missing | check steps 1 and 4 |
| decider runtimes fall back to the LLM | endpoint not `InService`, throttled or over its latency budget | check the endpoint status and the runtime logs |
| `cdk deploy` of the decider refuses to run | no `IMAGE_TAG` (intentional guard) | pass the tag printed by `make build-image` |

The end-to-end acceptance run is described in `docs/e2e-acceptance.md`.
