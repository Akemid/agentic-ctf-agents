---
name: ctf-fleet-ops
description: "Trigger: deploy/redeploy agents or runtimes, decider up/down, workshop setup/teardown, rotate credentials, costs; despliega los agentes, levanta el decider, destruye el endpoint, prepara el workshop. Operates the CTF agent fleet."
license: Apache-2.0
metadata:
  author: ctfagents
  version: "1.0"
---

## Activation Contract

Load this skill when the user wants to operate the deployed CTF fleet in region `us-east-2`:

- deploy or redeploy the 4 AgentCore runtimes ("despliega los agentes");
- bring the SageMaker decider up, validate it, or destroy it ("levanta el decider", "destruye el endpoint");
- set up or tear down a workshop ("prepara el workshop");
- rotate credentials after the game stack was redeployed;
- check what is billing.

Do NOT use it for changing agent code or tests (see `AGENTS.md`) or for the game server (separate repo `agentic-ctf`).
Human version of every procedure: `docs/workshop-runbook.md`. Read it when a step here is not enough.

## Stack Map

| Stack | Owner | Deploy | Holds | Bills |
|---|---|---|---|---|
| `AgenticCtfStack` | repo `agentic-ctf` | per workshop | Cognito pool, 4 M2M clients (4 h tokens), `/agentic-ctf/*` SSM, `agentic-ctf/agents/*` secrets, ALB + Fargate | 24/7, about $25/month. Destroy between workshops |
| `CtfPersistentStack` | `infra/decider` | once | ECR, CodeBuild image build, $25 SageMaker budget, SSM `/ctf-decider/{repo-uri,build-project,endpoint-name}` | about $0 |
| `DeciderStack` | `infra/decider` | per workshop | SageMaker endpoint `ctf-decider`, `ml.g4dn.xlarge` | $0.736/h |
| `AgentCore-ctfagents-default` | `agentcore/` (AgentCore CLI) | when runtimes change | 4 runtimes, 4 OAuth providers `ctf_<runtime>`, SSM `/ctfagents/runtimes/<runtime>/arn` | per use, idle about $0 |

Runtimes: `harness`, `harness_sdk`, `strand_decider`, `decider_loop`. Game client names (hyphens): `harness`, `harness-sdk`, `strand-decider`, `decider-loop`.

`/ctf-decider/endpoint-name` lives in `CtfPersistentStack` on purpose: the decider runtimes start while the endpoint is down.

Order: game, then agents (they hydrate from the game's SSM and secrets), then optionally the decider.
The decider is OPTIONAL. Without it, `strand_decider` and `decider_loop` play on the LLM fallback: the breaker opens for 300 s on "endpoint not found", then re-probes. CloudWatch shows INFO "decider endpoint unavailable ...; playing on the LLM fallback".

## Hard Rules

- Run commands from the repo root unless a `cd` is shown.
- Never read or print `agentcore/.env.local` (secrets; reads are permission-blocked).
- `agentcore/aws-targets.json` holds the real account locally under `skip-worktree`. Never commit it. Never write account ids, emails, pool ids, client ids or account ARNs into any tracked file; use `<ACCOUNT_ID>`, `<email>`.
- Deploy agents ONLY with `make deploy YES=1` (never bare `agentcore deploy`: it silently skips OAuth providers without hydrated credentials).
- Delete the gitignored `agentcore/.cache` before every agents deploy (it held stale staging once).
- Do not unpin decider dependencies (`requirements.lock`, `torch.lock`, torch 2.14.1+cu126).
- Verify against AWS. Never trust a CLI success mark alone.
- zsh does not word-split unquoted variables: list paths explicitly in loops.
- The rtk hook can mangle `aws`, `cdk` and `git grep` output. Prefix with `rtk proxy` when output looks empty or wrong.

## Confirmation Gates

Ask the user and WAIT before any step marked MONEY or MUTATES. Read-only steps need no gate.

| Step | Gate |
|---|---|
| Game stack deploy or destroy (other repo) | MUTATES, bills 24/7 |
| `make deploy YES=1` (agents) | MUTATES (no GPU, per-use billing only) |
| `deploy-persistent`, `make build-image YES=1` | MUTATES, build about $0.11 |
| `cdk deploy DeciderStack` | MONEY: $0.736/h from the first minute. ALWAYS get explicit OK, after reviewing `cdk diff` |
| `cdk destroy DeciderStack`, game stack destroy | MUTATES. Destroying the endpoint stops billing: do not delay it |

## Procedures

### A. Workshop setup

1. Game: from the `agentic-ctf` repo run `pnpm infra:deploy -c agentClientNames=harness,harness-sdk,strand-decider,decider-loop` (ALL 4 names).
2. Agents: procedure B (use C when the game stack is new or was recreated).
3. Optional decider: procedure D.
4. Launch with `scripts/fleet.py` (see runbook steps 7-9, lobby wake below).

### B. Agents deploy

```bash
rm -rf agentcore/.cache
make deploy              # prints the plan only
make deploy YES=1        # after the gate
```

The wrapper `scripts/deploy_fleet.sh` runs, in order: `sync_common --check`, hydrate, `hydrate --check`, `agentcore validate`, `agentcore deploy --yes`, `hydrate --clean`. The clean step ALWAYS runs, also on failure or Ctrl-C. Watch the output for `Skipped <provider>`.

### C. Game redeploy: credential rotation

Every game redeploy creates a new Cognito pool and new clients. Run:

```bash
rm -rf agentcore/.cache
UPDATE_DISCOVERY_URL=1 make deploy YES=1
```

AgentCore CLI 0.31.1 UPDATES the existing OAuth providers (client id, secret, discovery URL) from `.env.local`. No delete or recreate is needed. Then run the OAuth verification below.

### D. Decider up, validate, down

Up:

1. If anything under `infra/decider/adapter/` changed: `cd infra/decider`, redeploy `CtfPersistentStack` first (the CodeBuild source is an S3 asset; the tag is a content hash of the adapter dir plus the model id). Do this only while `DeciderStack` is destroyed.
2. `cd infra/decider && make build-image YES=1`. It prints `imageTag=<tag>`. The image ends with a strict smoke check (`python -m adapter.smoke --require-checkpoint`), so dependency breakage fails the BUILD, never the endpoint. Skip if the tag already exists in ECR.
3. `cd infra/decider && node_modules/.bin/cdk diff DeciderStack -c imageTag=<tag>`. Review IAM changes.
4. GATE (money). Then:

```bash
cd infra/decider && node_modules/.bin/cdk deploy DeciderStack -c imageTag=<tag> --require-approval never
```

`IMAGE_TAG=<tag> make deploy-decider` is equivalent (it passes `--require-approval never`); review `IMAGE_TAG=<tag> make diff-decider` first.

Watch CloudWatch log group `/aws/sagemaker/Endpoints/ctf-decider`:

- success: `GET /ping 200` (about 4 min);
- failure: "decider failed again ... staying unhealthy", or a Traceback. Delete at once: `aws cloudformation delete-stack --stack-name DeciderStack`. A `Creating` endpoint CANNOT be deleted until the 600 s startup health-check expires, so a broken container bills up to about 10 min.

Validate:

- one real inference through `SageMakerDecider` or `sagemaker-runtime invoke-endpoint` with `infra/decider/adapter/adapter/warmup_body.json`;
- `uv run python spikes/s4_bench.py run --log <decider log> --endpoint ctf-decider --region us-east-2 --n 30 --out <file>`. S4 gate: p95 <= 1000 ms (measured p50 446 ms, p95 646 ms).

Down:

```bash
cd infra/decider && node_modules/.bin/cdk destroy DeciderStack --force
aws sagemaker list-endpoints --query 'Endpoints[].EndpointName' --output text   # expect empty
```

`make destroy-decider` is equivalent (it passes `--force`) and keeps `/ctf-decider/endpoint-name` on purpose.

### E. Full teardown

1. Stop matches: `uv run python scripts/fleet.py stop --all --hard --yes`.
2. Decider down (D). Confirm `list-endpoints` is empty.
3. Game stack destroy from the `agentic-ctf` repo (stops the 24/7 bill).
4. Keep `CtfPersistentStack` and idle runtimes (about $0). Runtimes removal: runbook step 10.

## Verification Checklists

After agents deploy or rotation (verify in AWS, not in CLI output):

- [ ] Runtimes READY: `aws bedrock-agentcore-control list-agent-runtimes`
- [ ] OAuth providers match the game: for each `ctf_<rt>`, `aws bedrock-agentcore-control get-oauth2-credential-provider --name ctf_<rt>` `clientId` and `discoveryUrl` equal SSM `/agentic-ctf/agents/<name>/client-id` and `/agentic-ctf/cognito/discovery-url`
- [ ] SSM ARNs exist: `/ctfagents/runtimes/<runtime>/arn` for all 4
- [ ] Placeholder restored: `agentcore/agentcore.json` contains `discovery-url-not-hydrated`, and `git status` is clean (no `.env.local` secrets, no `aws-targets.json` change)

After decider down: `aws sagemaker list-endpoints` is empty.
After decider up: `GET /ping 200` in the log, one real inference OK, S4 p95 <= 1000 ms.

## Known Failures

| Symptom | Cause | Fix |
|---|---|---|
| Endpoint never healthy: `No module named 'torch.distributed.checkpoint.hf_storage'` | Unpinned deps: torch 2.7.1 with a newer transformers (image-deps incident) | Hash-locked closure plus strict build-time smoke are already in place. Rebuild the image, never unpin. See `decider-image-deps.md` |
| `cdk deploy` hangs, no output | IAM approval prompt in a non-interactive shell (raw `cdk deploy` without `--require-approval never`) | Ctrl-C; use the Makefile targets (`make deploy-decider`, `make deploy-persistent`), which pass `--require-approval never`, after the matching `make diff-*` |
| Runtimes behave on old staging or old code | Stale `agentcore/.cache` | `rm -rf agentcore/.cache`, redeploy |
| Billing continues after a failed endpoint | `Creating` endpoints cannot be deleted until the 600 s startup check ends | `delete-stack` immediately, wait up to about 10 min, confirm `list-endpoints` is empty |
| `CtfPersistentStack` deploy fails: endpoint-name param already exists | An old-template `DeciderStack` still owns `/ctf-decider/endpoint-name` | Destroy `DeciderStack` first, then deploy the persistent stack |
| Hydrate names an agent and an SSM path, or tokens are rejected | Game deployed without all `agentClientNames` | Redeploy the game with all 4 names, then procedure C |
| `discovery URL drift` after a game redeploy | New Cognito pool | `UPDATE_DISCOVERY_URL=1 make deploy YES=1` |
| `Skipped <provider>` in deploy output | Credentials not hydrated (deployed by hand) | Use `make deploy YES=1` only |
| `build-image` fails on source hash | Stale S3 asset | Redeploy `CtfPersistentStack`, then rebuild |
| Decider runtimes log "playing on the LLM fallback" | Endpoint down or not `InService` | Expected when the decider is off; re-probes every 300 s |
| `aws`/`cdk` output empty | rtk hook | `rtk proxy <cmd>` |

## Lobby Wake Contract (pointer)

Start payload takes `slot` or `agent_index` (0..2) with an explicit `team`. A synchronous `start` returns `started`, or `rejected` with `join_match failed: <reason>`. The game server needs `InvokeAgentRuntime` and `InvokeAgentRuntimeForUser`. Details: `openspec/changes/agentcore-ctf-fleet/lobby-wake.md`.

## Costs (us-east-2)

| Item | Cost |
|---|---|
| endpoint | $0.736/h (about $537/month if forgotten) |
| image build | about $0.11 |
| ECR | about $0.10/GB-month |
| game stack | about $25/month while up |
| runtimes | idle about $0 |

## Output Contract

After any procedure report: what ran, what was verified (checklist items with evidence), what still bills, and the next gate.

## References

- `docs/workshop-runbook.md` (human runbook), `infra/decider/README.md`, `infra/decider/Makefile`, `Makefile`
- `scripts/deploy_fleet.sh`, `scripts/hydrate_credentials.py`, `scripts/fleet.py`, `infra/decider/scripts/build_image.sh`
- `openspec/changes/agentcore-ctf-fleet/`: `decider-image-deps.md`, `decider-optional.md`, `lobby-wake.md`, `contract-v2.md`
- `AGENTS.md`
