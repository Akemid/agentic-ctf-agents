# Decider image: unpinned dependencies kept the model from loading

Date: 2026-10-16. Branch: `fix/decider-image-deps`. This note does not edit the archived proposal, specs, design or
tasks; it records what changed after them (same convention as `contract-v2.md` and `lobby-wake.md`).

## Incident

The SageMaker endpoint (image tag `c7da0f2fedc1f9d7`, built from `infra/decider/adapter/Dockerfile`) never became
healthy. CloudWatch:

```
adapter.runtime: starting the decider: strands-decider serve /tmp/decider-checkpoint --device cuda ...
ModuleNotFoundError: No module named 'torch.distributed.checkpoint.hf_storage'
ModuleNotFoundError: Could not import module 'Qwen3_5ForCausalLM'. Are this ... the original import error.
adapter.supervisor: decider exited with 1 while starting  (restarted once, then stayed unhealthy)
```

## Root cause

The Dockerfile pinned only `torch==2.7.1` and the direct dependencies. `strands-decider==0.1.0` declares
`torch>=2.7` and `transformers>=5.15,<6`, so pip chose the newest transformers. Its Qwen3.5 code imports
`torch.distributed.checkpoint.hf_storage`, which torch 2.7.1 does not ship (it is in the 2.8.0 wheels). transformers
loads model classes lazily, so the failure surfaced as "Could not import module Qwen3_5ForCausalLM" only when the decider
started, on the endpoint. Nothing at build time imported the model class.

## Fix

- `adapter/requirements.in` + `adapter/requirements.lock`: the full closure for linux/amd64 and Python 3.11, hash-pinned,
  from `uv pip compile --generate-hashes`. The Dockerfile installs torch first and alone (`torch==2.14.1+cu126`, the
  cu126 index) and then the lock with `--require-hashes --no-deps --only-binary=:all:`.
- Torch hash: pip skips torch's entry in the main lock install because torch is already present, so with an inline
  `torch==...` its hash would never be verified. The first step is now `--require-hashes --no-deps -r torch.lock`;
  `adapter/torch.lock` is torch's entry copied verbatim from `requirements.lock` (all cu126 wheel hashes), and a test
  asserts the two hash sets are equal so they cannot drift. The lock's hashes were compared with the sha256 the index
  publishes (`https://download.pytorch.org/whl/cu126/torch/`): the 24 torch 2.14.1+cu126 hashes are identical, including
  the cp311 manylinux_2_28 x86_64 wheel (`acffcaa3...926f`).
- Versions: torch 2.14.1+cu126 and transformers 5.19.0 (the pair the decider was validated with locally), peft 0.21.2,
  accelerate 1.15.0, safetensors 0.8.0, tokenizers 0.23.3, huggingface-hub 2.2.0. cu126 is the newest CUDA 12.x index
  for torch 2.14 (cu128 stops at 2.11, cu129 at 2.13); driver 550 (CUDA 12.4) runs CUDA 12.x wheels through
  minor-version compatibility, CUDA 13 wheels would not start.
- `python -m adapter.smoke --require-checkpoint` as the last `RUN`: imports the stack, `strands_decider.modeling` (the
  module `strands-decider serve` uses to build the model; it must expose `StrandsDeciderModel`),
  `torch.distributed.checkpoint.hf_storage`, resolves the baked checkpoint's architecture and builds
  `Qwen3_5ForCausalLM` on the meta device. No GPU, no weight load.
- Strict smoke: without a flag the smoke skips the checkpoint check when `DECIDER_CHECKPOINT_DIR` or
  `strands_decider_config.json` is absent (fine for a local run). The image passes `--require-checkpoint` (or set
  `SMOKE_REQUIRE_CHECKPOINT=1`), so a missing checkpoint exits 1 with a clear message and CodeBuild can never skip the
  meta-device model build.
- `tests/test_adapter_build.py` audits that every runtime dependency comes from the lock, that the smoke `RUN` is last, and
  that torch comes from a cu12x index. The smoke module has unit tests with fake imports.

## Lesson: fail at build, not at deploy

A SageMaker endpoint takes minutes to create and its only failure signal is "unhealthy" plus a log you read afterwards.
Anything the container needs in order to start must be proven when the image is built: pin the whole dependency closure
and make the last build step import and resolve what the entrypoint will load. Lazy imports defeat a plain
`import transformers`; the check must touch the model class itself.

## Gotcha: a Creating endpoint cannot be deleted

A SageMaker endpoint cannot be deleted (or updated) while it is `Creating`. A container that never turns healthy is
therefore billed until `ContainerStartupHealthCheckTimeoutInSeconds` expires and the endpoint fails. With 1200 s we hit
it on the first incident: about 25 minutes of an `ml.g4dn.xlarge`, roughly $0.30. Loading the 2B model (weights are baked
into the image, so there is no S3 model data and no `ModelDataDownloadTimeoutInSeconds` to tune) plus the warm-up takes
about 2-4 minutes, so the timeout is now 600 s (`STARTUP_HEALTH_CHECK_SECONDS` in `lib/decider-stack.ts`, jest asserts
it) and the adapter's own `ADAPTER_STARTUP_TIMEOUT_S` default dropped from 1100 to 550 to stay under it. This supersedes
the 1200 s figure in `design.md` (assumption table and DeciderStack notes), which is archived and not edited.

## GPU compatibility note

sm_75 (the T4 in g4dn) is kept in the cu126 wheels. torch 2.14 is the last release with CUDA 12 wheels: from 2.15 on,
CUDA 12.6 wheels are no longer published and Maxwell, Pascal and Volta support is dropped
(https://dev-discuss.pytorch.org/t/notice-cuda-12-6-wheels-will-no-longer-be-published-from-pytorch-2-15-drops-maxwell-pascal-volta/3432).
Residual risk: the wheels bundle nvjitlink 12.9 while the host driver is 550 (CUDA 12.4). Minor-version compatibility
covers the normal path; only JIT-compiled paths (PTX/nvjitlink at runtime) could fail on that driver.

## Not verified here

The new image was not run on a GPU: the CUDA 12.6 wheel on driver 550 relies on documented minor-version compatibility.
The first endpoint deploy is the real test. Order: redeploy `CtfPersistentStack` (the CodeBuild source asset), then
`make build-image`, then deploy `DeciderStack` with the new tag (the tag is a content hash of the adapter directory and
changed).
