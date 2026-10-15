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

# torch comes first, alone, from the CUDA 12.6 wheel index: 2.7.1 is published for cu118/cu126/cu128 (not cu124),
# and cu126 is the oldest of them, closest to the driver-550 (CUDA 12.4) AMI, which runs it through CUDA 12.x
# minor-version compatibility. The wheel bundles the CUDA runtime libraries; the host driver is mounted by SageMaker.
# A pinned version from this index also keeps pip from taking the default PyPI wheel line, which moves to CUDA 13.
RUN pip install --index-url https://download.pytorch.org/whl/cu126 "torch==2.7.1"

# strands-decider needs torch>=2.7: the constraint makes pip keep the wheel above instead of replacing it.
# The rest comes from PyPI.
RUN printf 'torch==2.7.1+cu126\n' > /tmp/constraints.txt \
    && pip install -c /tmp/constraints.txt "strands-decider==0.1.0" "fastapi==0.143.1" "uvicorn==0.54.0" "httpx==0.28.1" \
    && rm /tmp/constraints.txt

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

EXPOSE 8080

# SageMaker runs `docker run <image> serve`, which must reach OUR adapter on 8080.
# Exec form, so the `serve` argument reaches the adapter (and SIGTERM reaches python).
ENTRYPOINT ["python", "-m", "adapter"]
CMD ["serve"]
