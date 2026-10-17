# infra/decider

Separate CDK app for the SageMaker decider endpoint. `agentcore deploy` never touches it (it only runs
`agentcore/cdk`), so the GPU endpoint can be created and destroyed on its own schedule.

| Stack | Deploy | Contains |
|---|---|---|
| `CtfPersistentStack` | once | ECR repo `ctf-decider` (RETAIN, keep last 3), CodeBuild image builder, SageMaker Budget ($25/month, alerts at 80% actual and 100% forecast), SSM `/ctf-decider/repo-uri`, `/ctf-decider/build-project` and `/ctf-decider/endpoint-name` (the constant `ctf-decider`, always present) |
| `DeciderStack` | weekly | `ml.g4dn.xlarge` x1 endpoint `ctf-decider`, execution role. No SSM parameter, no exports |

Both stacks are ALWAYS synthesized, with or without inputs. The inputs are only needed to DEPLOY:

| Stack | Needed to deploy | Without it |
|---|---|---|
| `CtfPersistentStack` | `CTF_BUDGET_EMAIL` env var (or `-c budgetEmail=`) | carries an error annotation; `cdk deploy` and `cdk synth CtfPersistentStack` fail |
| `DeciderStack` | `-c imageTag=<hash>` | carries an error annotation; `cdk deploy` and `cdk synth DeciderStack` fail |

`cdk destroy` does not validate error annotations, so `cdk destroy DeciderStack` works with no inputs: you
can always stop the billing endpoint without remembering a tag. The placeholder a stack falls back to (an
unusable image tag, an `.invalid` address) is inert, and the error annotation stops it from being deployed.
(`cdk deploy --ignore-errors` would bypass the guard; do not use it.)

## The decider is optional

`/ctf-decider/endpoint-name` lives in `CtfPersistentStack` and does not depend on the endpoint, so the
`strand_decider` and `decider_loop` runtimes always pass preflight, with or without `DeciderStack`. What they
do depends on the endpoint:

| Endpoint | Behaviour |
|---|---|
| up | the agents use it |
| down (stack destroyed) | the first `invoke_endpoint` fails with `ValidationError` ("Endpoint ctf-decider of account ... not found"); the client treats it as a misconfiguration and opens its circuit breaker for 300 s. `strand_decider` proceeds ungated; `decider_loop` plays every tick on the LLM. One INFO line, "decider endpoint unavailable; playing on the LLM fallback", marks the start of the outage |
| brought up mid-match | the next call after the 300 s window re-probes the endpoint; if it answers, the decider is used again. No restart needed |

`make destroy-decider` leaves the parameter in place on purpose.

**Deploy order.** The parameter used to belong to `DeciderStack`. A named SSM parameter cannot be owned by
two stacks, so deploy `CtfPersistentStack` only while `DeciderStack` is destroyed (or destroy `DeciderStack`
first). Otherwise CloudFormation fails with "parameter already exists". Once both are on the new templates the
order no longer matters.

## Workflow

Each `make` target below builds the TypeScript first and runs the CDK CLI, which asks for confirmation.

```bash
make install                                         # npm ci
# once (MUTATING; the budget email is read from the environment, never committed, never in argv)
CTF_BUDGET_EMAIL=<your-email> make deploy-persistent
# whenever the adapter or the model changes (MUTATING: runs CodeBuild, pushes to ECR)
make build-image YES=1                               # prints imageTag=<hash>
# weekly: creates the GPU endpoint (BILLING); wait for InService (start-up timeout is 600 s)
IMAGE_TAG=<hash> make diff-decider     # read-only: review IAM and resource changes
IMAGE_TAG=<hash> make deploy-decider   # passes --require-approval never; get the OK first (billing)
# teardown: stops billing; ECR, the Budget and the endpoint-name parameter stay. Needs no tag and no email
make destroy-decider
```

`make -n <target>` prints what a target would run without running it.

`CtfPersistentStack` uploads the adapter source as an S3 asset, so redeploy it after editing `adapter/`
before building the image (the build checks the source hash and fails on a stale upload).

## The adapter

`adapter/` is a FastAPI app on :8080 that SageMaker talks to, supervising `strands-decider serve` on
127.0.0.1:8099.

- `POST /invocations`: validates JSON, then forwards the body unchanged to `/v1/systemone`, one request at a
  time (FIFO). A request arriving with 8 already waiting gets 503 `queue_full`; one that waits more than 2 s
  gets 503 `queue_timeout` and is never forwarded. The answer is the upstream JSON plus
  `adapter: {queue_wait_ms}`.
- `GET /ping`: never waits on the queue. 200 only after upstream `/health` is OK and a warm-up question
  completed. If the decider dies, one automatic restart; after that `/ping` stays 503.
- dtype: `strands-decider serve` has no dtype flag, so at start-up the adapter stages a copy of the checkpoint
  with `torch_dtype` set from `DECIDER_DTYPE` (default `float16`; a T4 has no bf16).

Image tag = content hash of `adapter/` plus the model id (`adapter/hash_source.py`). CodeBuild recomputes it
on the unzipped S3 asset and fails on a mismatch; a test proves both sides agree, including the excluded
files (`__pycache__`, `*.pyc`, `.pytest_cache`). The model id is pinned in one place, `lib/names.ts`
(`DEFAULT_MODEL_ID`); `build_image.sh` reads it from there, has no `--model-id` option, and a test checks the
Dockerfile default against it.

### Failure handling

- After an upstream timeout or connection error the adapter keeps the request slot and polls `/health` until
  the decider answers again (it may still be computing the abandoned call), bounded by
  `ADAPTER_DRAIN_TIMEOUT_S`. Past the limit the decider is reported unhealthy and restarted. Requests that
  arrive meanwhile follow the queue rules (503 `queue_full` / `queue_timeout`), and a request that reaches
  the front of the queue while the decider is out of service gets 503 `not_ready`, never forwarded.
- Health probes are skipped while a request is in flight (the decider cannot answer `/health` while it
  computes). Only a process exit or a refused connection restarts immediately; timeouts and non-200 answers
  count only when idle, `ADAPTER_HEALTH_FAILURES` in a row.
- A client that disconnected while queued is never forwarded to the decider.
- If the decider cannot even be started (missing binary, unreadable checkpoint) `/ping` stays 503 with state
  `failed`. On shutdown the child is terminated even while the warm-up is running.
- Upstream JSON with `NaN`/`Infinity` is a 502 `bad_upstream_response`. An `adapter` object in the upstream
  answer is merged (the adapter owns `queue_wait_ms`); a non-object `adapter` value is kept as
  `upstream_adapter`.

### Environment variables

All optional; invalid values stop the container at start-up.

| Variable | Default | Meaning |
|---|---|---|
| `ADAPTER_PORT` | `8080` | port SageMaker talks to |
| `DECIDER_UPSTREAM_PORT` | `8099` | loopback port of `strands-decider serve` |
| `DECIDER_UPSTREAM_COMMAND` | unset | explicit upstream argv (tests use the fake decider); replaces the default command and skips checkpoint staging |
| `DECIDER_CHECKPOINT_DIR` | `/opt/decider/checkpoint` | baked checkpoint |
| `DECIDER_STAGED_DIR` | `/tmp/decider-checkpoint` | writable copy with the dtype patched |
| `DECIDER_DTYPE` | `float16` | `float16`, `float32` or `bfloat16` (a T4 has no bf16) |
| `DECIDER_DEVICE` | `cuda` | passed to `strands-decider serve --device` |
| `ADAPTER_QUEUE_MAX_WAITING` | `8` | requests allowed to wait; one more gets 503 `queue_full` |
| `ADAPTER_QUEUE_WAIT_S` | `2.0` | longest wait for the slot; then 503 `queue_timeout` |
| `ADAPTER_UPSTREAM_TIMEOUT_S` | `10.0` | per-call timeout to the decider (504 `upstream_timeout`) |
| `ADAPTER_HEALTH_INTERVAL_S` | `5.0` | seconds between idle health probes |
| `ADAPTER_HEALTH_FAILURES` | `3` | consecutive idle soft failures before a restart |
| `ADAPTER_STARTUP_TIMEOUT_S` | `550.0` | longest wait for the first healthy `/health` (under the 600 s endpoint start-up check) |
| `ADAPTER_WARMUP_TIMEOUT_S` | `120.0` | longest wait for the warm-up question |
| `ADAPTER_DRAIN_TIMEOUT_S` | `30.0` | longest wait for the decider to go idle after a failed call, then it is restarted |
| `ADAPTER_DRAIN_POLL_S` | `0.5` | seconds between `/health` polls while draining |

## Base image

`adapter/Dockerfile` builds on the official Python image mirrored on ECR Public:
`public.ecr.aws/docker/library/python:3.11.17-slim-bookworm`, pinned by tag and digest
(`sha256:0a310eee...f5f89`, the multi-arch index; `--platform=linux/amd64` selects the layer). ECR Public needs
no login and has no Docker Hub anonymous pull rate limit, so the buildspec logs in only to our own ECR repository
and the CodeBuild role has no pull grant on any other repository. `Dockerfile.local` uses the same base.

- Dependencies: `adapter/requirements.lock` is the full closure for linux/amd64 + Python 3.11, with a hash per file,
  generated from `adapter/requirements.in` (the command is in its header, with `uv pip compile`). Nothing is
  resolved at build time. `strands-decider==0.1.0` only asks for `torch>=2.7` and `transformers>=5.15,<6`; the first
  image left everything else open, pip took the newest transformers, and its model code imports
  `torch.distributed.checkpoint.hf_storage`, which torch 2.7.1 does not ship (it first appears in 2.8). The endpoint
  never became healthy. Details: `openspec/changes/archive/2026-10-16-agentcore-ctf-fleet/decider-image-deps.md`.
- torch: `torch==2.14.1+cu126` from the CUDA 12.6 wheel index (`https://download.pytorch.org/whl/cu126`), installed
  first and alone (`--no-deps --require-hashes -r torch.lock`; `torch.lock` is the torch entry of `requirements.lock`,
  verbatim, and a test keeps them equal, because pip skips torch's entry in the main install and would never verify its
  hash). cu126 is the newest CUDA 12.x index for this line (cu128 stops at 2.11, cu129 at
  2.13), and 2.14.1 plus `transformers==5.19.0` is the pair the decider was validated with locally. Driver check: the
  `al2-ami-sagemaker-inference-gpu-3-1` AMI has NVIDIA driver 550 (CUDA 12.4). CUDA 12.x wheels run on it through
  minor-version compatibility (any driver of the 12.x family, 525 or newer); CUDA 13 wheels need a newer driver and do
  NOT start. The default PyPI torch wheel line is CUDA 13, hence the pinned `+cu126` build from this index. The wheel
  bundles the CUDA runtime libraries; no NVIDIA driver is in the image. A T4 is sm_75, which cu126 builds include (torch 2.14
  is the last release with CUDA 12 wheels; 2.15 drops Maxwell, Pascal and Volta and stops publishing cu126).
- A second `pip install --require-hashes --no-deps --only-binary=:all: -r requirements.lock` installs the rest
  (torch is already there and is skipped). `tests/test_adapter_build.py` fails if any dependency is installed outside
  the lock, if the lock has an entry without a hash, a CUDA 13 or CPU wheel, or if torch is older than 2.8.
- Build-time smoke check: the last `RUN` is `python -m adapter.smoke --require-checkpoint`
  (strict: a missing checkpoint or config exits 1, so CodeBuild can never skip the meta-device build; without the flag
  or `SMOKE_REQUIRE_CHECKPOINT=1` a local run only prints "skipped"). No GPU and no weights loaded: it imports
  torch, transformers, peft, accelerate, strands_decider, fastapi and the rest, imports
  `strands_decider.modeling` (the module `strands-decider serve` builds its model with),
  `torch.distributed.checkpoint.hf_storage`, reads the baked checkpoint's `strands_decider_config.json`, resolves
  the base model's `AutoConfig` from the offline cache, builds `Qwen3_5ForCausalLM` on the meta device (shapes only)
  and prints the versions. A broken combination fails the CodeBuild build, not the endpoint.
- To change a version: edit `requirements.in`, regenerate the lock, run `make test-adapter`. The tag is a content
  hash of the adapter directory, so it changes; redeploy `CtfPersistentStack` (the CodeBuild source asset) before
  `make build-image`.
- No extra OS packages: the torch wheel bundles `libgomp`, and the slim image already has CA certificates.
  [UNVERIFIED on a GPU: the full image is not built locally.]
- The ENTRYPOINT/CMD are ours (`["python", "-m", "adapter"]`, `["serve"]`), nothing to override.
- Replaced the AWS Deep Learning Container 2.6 (past end of patching, torch reinstalled on top, entrypoint and CMD
  overridden, CUDA 12.4 libraries mixed with the wheel's 12.6).
- To refresh the pin (read-only): `docker buildx imagetools inspect
  public.ecr.aws/docker/library/python:3.11-slim-bookworm` and copy the `Digest` and the version tag.

## Testing

```bash
make test                             # jest: both stacks, the app guards, path resolution
make test-adapter                     # adapter pytest (fake decider, no GPU, no model)
make smoke-local                      # builds Dockerfile.local and curls /ping and /invocations (Docker only)
```

Run jest as `node_modules/.bin/jest` (not `npx`).

`Dockerfile` (GPU, weights baked in, `HF_HUB_OFFLINE=1`) is built in CodeBuild only. `Dockerfile.local` has
no torch and no weights and runs the same entrypoint against `adapter/adapter/fake_decider.py`.

## Budget cost filter (unverified)

The Budget filters on the cost-filter service name `Amazon SageMaker`. That string is NOT verified against
what Cost Explorer reports for this account: a wrong name would make the budget match nothing and never
alert. To check it, list the service names that contain "SageMaker" (read-only, but it is a Cost Explorer
API call and Cost Explorer API requests are BILLED, about USD 0.01 each):

```bash
aws ce get-dimension-values --dimension SERVICE --search-string SageMaker \
  --time-period Start=<first day of last month>,End=<today>
```

If the name differs, fix `costFilters` in `lib/persistent-stack.ts` and the matching jest assertion.
