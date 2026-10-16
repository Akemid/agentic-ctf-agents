"""Build-time smoke check: the pinned dependency set must import and resolve the decider's model class.

Runs as the LAST `RUN` of the production Dockerfile, with no GPU and without loading any weights. A broken
combination (the incident: a transformers that needs a newer torch than the one pinned) fails the CodeBuild
build instead of the SageMaker endpoint, which would only show it minutes later as an unhealthy container.

    python -m adapter.smoke                       # checkpoint: DECIDER_CHECKPOINT_DIR; skipped if absent
    python -m adapter.smoke --require-checkpoint  # strict (or SMOKE_REQUIRE_CHECKPOINT=1): absent is an error
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

# Everything the image imports at run time, in the order the decider loads it.
REQUIRED_MODULES = (
    "torch",
    "transformers",
    "peft",
    "accelerate",
    "huggingface_hub",
    "safetensors",
    "strands_decider",
    "strands_decider.modeling",  # what `strands-decider serve` uses to build the model
    "fastapi",
    "uvicorn",
    "httpx",
)
# transformers imports this lazily when it builds the model classes; torch < 2.8 does not ship it.
TORCH_HF_STORAGE = "torch.distributed.checkpoint.hf_storage"
# What strands_decider.modeling loads for a Qwen3.5 checkpoint (see its _load_torso).
DEFAULT_MODEL_CLASS = "Qwen3_5ForCausalLM"
QWEN3_5_TYPES = {"qwen3_5", "qwen3_5_text"}
DECIDER_MODEL_CLASS = "StrandsDeciderModel"
CONFIG_FILE = "strands_decider_config.json"
STRICT_ENV = "SMOKE_REQUIRE_CHECKPOINT"

ImportModule = Callable[[str], ModuleType]
LoadArchitecture = Callable[[Path], str]


class SmokeError(RuntimeError):
    """A dependency is missing or does not work together with the others."""


def architecture_from_checkpoint(checkpoint_dir: Path) -> str:
    """The model class the checkpoint needs, from its base model's config; nothing is downloaded or loaded.

    Also builds the class on the `meta` device (shapes only, no weights): that runs the model code.
    """
    import torch
    import transformers

    config_path = Path(checkpoint_dir) / CONFIG_FILE
    base_model = json.loads(config_path.read_text())["base_model"]
    config = transformers.AutoConfig.from_pretrained(base_model)  # offline: the baked HF cache
    text_config = config.get_text_config()
    if config.model_type in QWEN3_5_TYPES:
        name = DEFAULT_MODEL_CLASS
    else:
        name = transformers.AutoModelForCausalLM._model_mapping[type(text_config)].__name__
    with torch.device("meta"):
        getattr(transformers, name)(text_config)
    return name


def smoke(
    checkpoint_dir: Path | None = None,
    *,
    require_checkpoint: bool = False,
    import_module: ImportModule = importlib.import_module,
    load_architecture: LoadArchitecture = architecture_from_checkpoint,
    report: Callable[[str], None] = print,
) -> None:
    modules: dict[str, ModuleType] = {}
    for name in REQUIRED_MODULES:
        try:
            modules[name] = import_module(name)
        except Exception as exc:  # any failure at import time breaks the endpoint the same way
            raise SmokeError(f"cannot import {name}: {exc!r}") from exc
        report(f"{name} {getattr(modules[name], '__version__', 'unknown')}")

    try:
        import_module(TORCH_HF_STORAGE)
    except Exception as exc:
        raise SmokeError(
            f"cannot import {TORCH_HF_STORAGE} (torch too old for this transformers): {exc!r}"
        ) from exc
    if not hasattr(modules["strands_decider.modeling"], DECIDER_MODEL_CLASS):
        raise SmokeError(f"strands_decider.modeling has no {DECIDER_MODEL_CLASS}")

    if checkpoint_dir is None and require_checkpoint:
        raise SmokeError(f"no checkpoint on disk ({CONFIG_FILE} missing), and a checkpoint is required")
    model_class = DEFAULT_MODEL_CLASS
    if checkpoint_dir is not None:
        try:
            model_class = load_architecture(Path(checkpoint_dir))
        except Exception as exc:
            raise SmokeError(f"cannot resolve the architecture from {checkpoint_dir}: {exc!r}") from exc
    else:
        report("checkpoint config check skipped: no checkpoint on disk")
    try:
        getattr(modules["transformers"], model_class)  # a lazy import: this is what failed in the incident
    except Exception as exc:
        raise SmokeError(f"transformers cannot import {model_class}: {exc!r}") from exc
    report(f"model class {model_class}: ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build-time dependency smoke check.")
    parser.add_argument(
        "--require-checkpoint",
        action="store_true",
        help=f"fail instead of skipping when the checkpoint config is absent (or set {STRICT_ENV}=1)",
    )
    args = parser.parse_args(argv)
    strict = args.require_checkpoint or os.environ.get(STRICT_ENV, "") not in ("", "0")

    directory = os.environ.get("DECIDER_CHECKPOINT_DIR")
    checkpoint = Path(directory) if directory and (Path(directory) / CONFIG_FILE).is_file() else None
    if strict and checkpoint is None:
        where = f"DECIDER_CHECKPOINT_DIR={directory!r}" if directory else "DECIDER_CHECKPOINT_DIR is unset"
        print(
            f"ERROR: smoke check failed: strict mode needs a checkpoint with {CONFIG_FILE} ({where})",
            file=sys.stderr,
        )
        return 1
    try:
        smoke(checkpoint, require_checkpoint=strict)
    except SmokeError as exc:
        print(f"ERROR: smoke check failed: {exc}", file=sys.stderr)
        return 1
    print("smoke check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
