"""DIRECT-prompt variant of `harness.py` (A/B experiment on the system prompt only).

Identical to `harness.py` -- model, tools, memory, hooks, effort, subagent flag and initial instruction --
except that the whole system prompt is the short `DIRECT_SYSTEM_PROMPT` and the default `--name` is `refined-direct-harness`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: E402
from direct_prompt import DIRECT_SYSTEM_PROMPT  # noqa: E402

DEFAULT_NAME = "refined-direct-harness"


def parse_args():
    return harness.parse_args(DEFAULT_NAME)


def main():
    return harness.main(DIRECT_SYSTEM_PROMPT, DEFAULT_NAME)


if __name__ == "__main__":
    main()
