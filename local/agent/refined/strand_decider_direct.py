"""DIRECT-prompt variant of `strand_decider.py` (A/B experiment on the system prompt only).

Identical to `strand_decider.py` -- model, tools, decider gating, window, tools and initial instruction --
except that the whole system prompt is the short `DIRECT_SYSTEM_PROMPT` and the default `--name` is `refined-direct-strands-decider`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strand_decider  # noqa: E402
from direct_prompt import DIRECT_SYSTEM_PROMPT  # noqa: E402

DEFAULT_NAME = "refined-direct-strands-decider"


def parse_args():
    return strand_decider.parse_args(DEFAULT_NAME)


def main():
    return strand_decider.main(DIRECT_SYSTEM_PROMPT, DEFAULT_NAME)


if __name__ == "__main__":
    sys.exit(main())
