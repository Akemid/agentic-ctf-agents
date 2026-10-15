"""Container entry point: `python -m adapter serve` (SageMaker appends `serve` to the container command)."""

from __future__ import annotations

import logging
import os
import sys

from .config import AdapterConfig, ConfigError
from .runtime import build_app


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if argv and argv[0] != "serve":
        print(f"unknown command {argv[0]!r}; the only command is 'serve'", file=sys.stderr)
        return 2
    try:
        config = AdapterConfig.from_env(os.environ)
    except ConfigError as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2

    import uvicorn  # imported here: tests never start a server

    app, _ = build_app(config)
    uvicorn.run(app, host="0.0.0.0", port=config.port, log_level="info")  # noqa: S104 - SageMaker requires it
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
