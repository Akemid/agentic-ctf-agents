"""DIRECT-prompt variant of `decider_loop.py` (A/B experiment on the system prompt only).

Identical to `decider_loop.py` -- model, tools, decider usage, thresholds, CLI args and initial instruction --
except that the whole system prompt is the short `DIRECT_SYSTEM_PROMPT` and the default `--name` is `refined-direct-decider`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import decider_loop  # noqa: E402
from direct_prompt import DIRECT_SYSTEM_PROMPT  # noqa: E402

DEFAULT_NAME = "refined-direct-decider"


def parse_args():
    return decider_loop.parse_args(DEFAULT_NAME)


def main():
    return decider_loop.main(DIRECT_SYSTEM_PROMPT, DEFAULT_NAME)


if __name__ == "__main__":
    exit_code = decider_loop.run_guarded(main)
    sys.stdout.flush()
    sys.stderr.flush()
    decider_loop.exit_if_threads_linger(exit_code)
    sys.exit(exit_code)
