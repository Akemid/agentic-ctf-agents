"""Stage a writable copy of the checkpoint with `torch_dtype` patched.

`strands-decider serve` has no dtype flag: the dtype is a key in the checkpoint's
`strands_decider_config.json` (default bfloat16, which a T4 does not support). The checkpoint is a LoRA
adapter plus a small head, so copying it is cheap; the base weights live in the Hugging Face cache.

Also a CLI used by the Dockerfile to fail the build when the key is gone:
    python -m adapter.stage --check <checkpoint-dir>
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

CONFIG_FILES = ("strands_decider_config.json", "config.json")
DTYPE_KEYS = ("torch_dtype", "dtype")


class StageError(RuntimeError):
    """The checkpoint cannot be staged or has no dtype key to patch."""


def find_dtype_key(directory: Path) -> tuple[Path, str]:
    for name in CONFIG_FILES:
        path = directory / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        for key in DTYPE_KEYS:
            if key in data:
                return path, key
    raise StageError(f"no torch_dtype/dtype key in {' or '.join(CONFIG_FILES)} under {directory}")


def stage_checkpoint(source: Path | str, destination: Path | str, dtype: str) -> Path:
    """Copy `source` to `destination` (replacing any previous copy) and set the dtype there."""
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        raise StageError(f"checkpoint directory not found: {source}")
    find_dtype_key(source)  # fail before copying anything
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    path, key = find_dtype_key(destination)
    data = json.loads(path.read_text())
    data[key] = dtype
    path.write_text(json.dumps(data, indent=2))
    return destination


async def stage_checkpoint_async(source: Path | str, destination: Path | str, dtype: str) -> Path:
    """`stage_checkpoint` in a worker thread: the copy must not stall the event loop (and /ping with it)."""
    return await asyncio.to_thread(stage_checkpoint, source, destination, dtype)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--check":
        print("usage: python -m adapter.stage --check <checkpoint-dir>", file=sys.stderr)
        return 2
    try:
        path, key = find_dtype_key(Path(argv[1]))
    except StageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"ok: {key} found in {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
