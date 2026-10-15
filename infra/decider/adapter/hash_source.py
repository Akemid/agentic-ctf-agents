"""Content hash of the adapter source plus the model id: the image tag.

Used by scripts/build_image.sh (locally) and by the CodeBuild buildspec (on the uploaded source), so a
stale upload is caught: both sides must produce the same tag. Standard library only.

    python3 hash_source.py --model-id <id> [--dir <path>]
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

EXCLUDED_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc"}
TAG_LENGTH = 16


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or EXCLUDED_DIRS & set(relative.parts) or path.suffix in EXCLUDED_SUFFIXES:
            continue
        yield relative, path


def source_hash(root: Path | str, model_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"model:{model_id}\n".encode())
    for relative, path in _files(Path(root)):
        digest.update(f"file:{relative.as_posix()}:{path.stat().st_size}\n".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:TAG_LENGTH]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent))
    args = parser.parse_args()
    print(source_hash(args.dir, args.model_id))


if __name__ == "__main__":
    main()
