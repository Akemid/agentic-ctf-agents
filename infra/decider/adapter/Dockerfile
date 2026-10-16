# GPU image for the SageMaker decider endpoint. Build it with CodeBuild (scripts/build_image.sh): it is
# linux/amd64 and about 10 GB with the weights baked in. Do NOT build it on an Apple Silicon laptop; use
# Dockerfile.local (no torch, no weights) to exercise the adapter there.
#
# No NVIDIA driver is bundled: SageMaker mounts the host driver (see InferenceAmiVersion in DeciderStack).
#
# Base: the official Python image, mirrored on ECR Public (no login, no Docker Hub anonymous pull rate limit).
# Pinned by tag AND digest (the tags are mutable); the digest is the multi-arch index, resolved by --platform.
# Refresh (read-only): docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.11-slim-bookworm
# The AWS Deep Learning Container was dropped: its 2.6 line is past end of patching, we reinstalled torch on top
# of it, overrode its entrypoint and CMD, and mixed its CUDA 12.4 libraries with the wheel's. See the design addendum.
# Dependencies: requirements.lock (hash-pinned) and the build-time smoke check; see decider-image-deps.md in the change.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.11.17-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

ARG MODEL_ID=StrandsAgents/strands-decider-2B-hobson-v21
# Pinned revisions: the checkpoint repository and its base model (Qwen/Qwen3.5-2B-Base).
ARG MODEL_REVISION=2b52a6235c1b8306bbfa30b00b9d4b74b63a39f5
ARG BASE_MODEL_ID=Qwen/Qwen3.5-2B-Base
ARG BASE_MODEL_REVISION=b1485b2fa6dfa1287294f269f5fb618e03d52d7c

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/decider/hf \
    DECIDER_CHECKPOINT_DIR=/opt/decider/checkpoint

# Every runtime dependency is pinned and hash-checked in requirements.lock (generated from requirements.in, see the
# header there), the full closure for linux/amd64 + Python 3.11. Nothing is resolved at build time: an unpinned
# transitive dependency is how the first image shipped a transformers that needs a newer torch than the one pinned.
COPY requirements.lock torch.lock /tmp/

# torch comes first, alone, from the CUDA 12.6 wheel index. cu126 is the newest CUDA 12.x line for torch 2.14
# (cu128 stops at 2.11, cu129 at 2.13), and 2.14.1 is the build the decider was validated with. The wheel bundles the
# CUDA runtime; the SageMaker AMI mounts the host driver (550, CUDA 12.4), which runs CUDA 12.x wheels through
# minor-version compatibility. CUDA 13 wheels need a newer driver, and the default PyPI wheel line is CUDA 13, so a
# version pinned to the +cu126 build from this index is what keeps pip off it. --no-deps: the nvidia-* libraries
# below come from the lock, with their hashes.
# torch.lock is the torch entry of requirements.lock, verbatim (a test keeps them equal): pip skips that entry in the
# main install because torch is already present, so this is the one place torch's wheel hash is verified.
RUN pip install --require-hashes --no-deps --index-url https://download.pytorch.org/whl/cu126 \
        -r /tmp/torch.lock

# The rest of the lock. torch is already installed, so pip skips its entry; everything else is exact and hash-checked.
# The same two indexes as the `uv pip compile` that wrote the lock: a few packages (triton among them) are published on
# both with different file hashes, and pip keeps only the candidates whose hash is in the lock.
# --only-binary: no source build may slip in. --no-deps: the lock is the whole closure, pip resolves nothing.
RUN pip install --require-hashes --no-deps --only-binary=:all: \
        --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cu126 \
        -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock /tmp/torch.lock

WORKDIR /opt/decider
COPY adapter /opt/decider/adapter

# Download the checkpoint and the base model, and fail the build when the checkpoint has no
# torch_dtype/dtype key to patch (the dtype is patched at start-up from DECIDER_DTYPE).
RUN MODEL_ID="${MODEL_ID}" MODEL_REVISION="${MODEL_REVISION}" BASE_MODEL_ID="${BASE_MODEL_ID}" \
    BASE_MODEL_REVISION="${BASE_MODEL_REVISION}" python -m adapter.bake

# From here on nothing may reach the network: the weights are on disk.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    DECIDER_DTYPE=float16

# Fail the BUILD, not the endpoint, when the dependency set does not work together. No GPU, no weights loaded: it
# imports the model stack, resolves the checkpoint's architecture class and builds it on the meta device.
# --require-checkpoint: a missing checkpoint fails the build instead of silently skipping that meta-device build.
RUN python -m adapter.smoke --require-checkpoint

EXPOSE 8080

# SageMaker runs `docker run <image> serve`, which must reach OUR adapter on 8080.
# Exec form, so the `serve` argument reaches the adapter (and SIGTERM reaches python).
ENTRYPOINT ["python", "-m", "adapter"]
CMD ["serve"]
