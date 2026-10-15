#!/usr/bin/env python3
"""Vendor `shared/ctf_common` into every runtime `codeLocation` (design A1).

AgentCore CodeZip packages one directory per runtime, so the shared code is copied into
`app/<runtime>/ctf_common/`. The copies are committed (a fresh clone must deploy) and are
generated: never edit them by hand.

    python scripts/sync_common.py            # copy shared -> every existing runtime
    python scripts/sync_common.py --check    # exit 1 and name the files if a copy drifted
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

RUNTIMES = ("harness", "harness_sdk", "strand_decider", "decider_loop")
PACKAGE = "ctf_common"
VENDORED_SUFFIXES = {".py"}
VENDORED_NAMES = {"py.typed"}
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SyncReport:
    targets: list[Path]


def source_dir(root: Path) -> Path:
    return root / "shared" / PACKAGE


def target_dirs(root: Path) -> list[Path]:
    """Vendored directories for the runtimes that exist (runtimes are added slice by slice)."""
    return [root / "app" / rt / PACKAGE for rt in RUNTIMES if (root / "app" / rt).is_dir()]


def _vendored(path: Path) -> bool:
    return "__pycache__" not in path.parts and (
        path.suffix in VENDORED_SUFFIXES or path.name in VENDORED_NAMES
    )


def snapshot(directory: Path) -> dict[str, str]:
    """Relative path -> sha256 for every vendored-type file under `directory`."""
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and _vendored(path.relative_to(directory))
    }


def sync(root: Path = DEFAULT_ROOT) -> SyncReport:
    src = source_dir(root)
    if not src.is_dir():
        raise FileNotFoundError(f"source of truth not found: {src}")
    files = snapshot(src)
    targets = target_dirs(root)
    for dest in targets:
        shutil.rmtree(dest, ignore_errors=True)
        for rel in files:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest / rel)
    return SyncReport(targets=targets)


def check(root: Path = DEFAULT_ROOT) -> list[str]:
    """Human-readable drift problems (empty list means every copy equals the source)."""
    expected = snapshot(source_dir(root))
    problems: list[str] = []
    for dest in target_dirs(root):
        label = dest.relative_to(root).as_posix()
        actual = snapshot(dest)
        if not dest.is_dir():
            problems.append(f"{label}: vendored copy is missing (run scripts/sync_common.py)")
            continue
        for rel, digest in expected.items():
            if rel not in actual:
                problems.append(f"{label}/{rel}: missing")
            elif actual[rel] != digest:
                problems.append(f"{label}/{rel}: differs from shared/{PACKAGE}/{rel}")
        problems.extend(
            f"{label}/{rel}: unexpected file (not in shared/{PACKAGE})"
            for rel in actual
            if rel not in expected
        )
    return problems


def main(argv: list[str] | None = None, *, root: Path = DEFAULT_ROOT) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="verify the copies without modifying anything")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(root)
        if problems:
            print("Vendored copies drifted from shared/ctf_common:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(f"{PACKAGE}: vendored copies are in sync")
        return 0
    report = sync(root)
    for dest in report.targets:
        print(f"synced {dest.relative_to(root).as_posix()}")
    if not report.targets:
        print("no runtime directories found under app/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
