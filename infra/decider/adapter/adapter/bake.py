"""Build-time step: download the checkpoint and its base model INTO the image, then verify.

Runs during `docker build` (network available). At runtime the container is offline
(HF_HUB_OFFLINE=1, network isolation), so everything the decider will load must already be on disk.

    python -m adapter.bake            # configuration comes from the environment (see main())
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from .stage import StageError, find_dtype_key

Download = Callable[..., str]


class BakeError(RuntimeError):
    """The downloaded checkpoint cannot be served as configured."""


def _default_download() -> Download:
    # Installed in the image only (strands-decider depends on it), not in the dev environment.
    from huggingface_hub import snapshot_download  # pyright: ignore[reportMissingImports]

    return snapshot_download


def bake(
    *,
    model_id: str,
    model_revision: str,
    base_revision: str,
    checkpoint_dir: Path,
    hf_home: Path,
    expected_base_model: str | None = None,
    download: Download | None = None,
) -> None:
    download = download or _default_download()
    checkpoint_dir, hf_home = Path(checkpoint_dir), Path(hf_home)

    download(model_id, revision=model_revision, local_dir=checkpoint_dir)

    try:
        find_dtype_key(checkpoint_dir)  # same check as `adapter.stage --check`: no key, no image
    except StageError as exc:
        raise BakeError(str(exc)) from exc

    config_path = checkpoint_dir / "strands_decider_config.json"
    base_model = json.loads(config_path.read_text()).get("base_model") if config_path.is_file() else None
    if not base_model:
        raise BakeError(f"base_model missing in {config_path.name}: cannot tell which base weights to bake")
    if expected_base_model and base_model != expected_base_model:
        raise BakeError(f"checkpoint names base model {base_model!r}, expected {expected_base_model!r}")

    hub_cache = hf_home / "hub"
    download(base_model, revision=base_revision, cache_dir=hub_cache)
    # snapshot_download writes refs/main only when asked for a branch. Offline loading by repo id
    # resolves "main" through that file, so point it at the pinned revision.
    ref = hub_cache / f"models--{base_model.replace('/', '--')}" / "refs" / "main"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(base_revision)


def main() -> int:
    try:
        bake(
            model_id=os.environ["MODEL_ID"],
            model_revision=os.environ["MODEL_REVISION"],
            base_revision=os.environ["BASE_MODEL_REVISION"],
            checkpoint_dir=Path(os.environ["DECIDER_CHECKPOINT_DIR"]),
            hf_home=Path(os.environ["HF_HOME"]),
            expected_base_model=os.environ.get("BASE_MODEL_ID") or None,
        )
    except (BakeError, KeyError) as exc:
        print(f"ERROR: bake failed: {exc!r}", file=sys.stderr)
        return 1
    print("baked checkpoint and base model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
