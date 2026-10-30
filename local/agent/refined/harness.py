"""REFINED CTF agent on `strands_harness.create_harness`.

The only behavioral
feature on top of the plain harness is the act-fast prompt appended to the system prompt
(shared with `harness_sdk.py`); `programmatic_tool_caller` stays enabled. Default `--name` is `refined-harness` and the initial
instruction is in English.

Not changed: `create_harness` has its own `context_manager` (default on) which takes precedence
over a `conversation_manager` passed through `agent_kwargs`, so no bounded window is applied here.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness_sdk import PROMPT_CHOICES, SYSTEM_PROMPT, build_instruction  # noqa: E402
from prompts import DIRECT_SYSTEM_PROMPT  # noqa: E402
from strands_harness import create_harness  # noqa: E402

# Model alias (CLI) -> Bedrock model id.
MODELS = {
    "haiku": "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "opus": "bedrock/global.anthropic.claude-opus-4-6-v1",
    "nova-lite": "bedrock/global.amazon.nova-2-lite-v1:0",
}

DEFAULT_NAME = "refined-harness"
DIRECT_NAME = "refined-direct-harness"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refined AmazingPlayer CTF agent (strands_harness)."
    )
    parser.add_argument(
        "--model",
        choices=sorted(MODELS.keys()),
        default="haiku",
        help="Model to run the agent with (default: haiku).",
    )
    parser.add_argument(
        "--match",
        default="llm-1",
        help="Match id to join (default: llm-1).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help=f"Agent name used to join the match (default: {DEFAULT_NAME}; {DIRECT_NAME} with --prompt direct).",
    )
    parser.add_argument(
        "--prompt",
        choices=PROMPT_CHOICES,
        default="refined",
        help="System prompt: the refined act-fast prompt or the short direct A/B prompt (default: refined).",
    )
    args = parser.parse_args()
    args.name = args.name or (DIRECT_NAME if args.prompt == "direct" else DEFAULT_NAME)
    return args


def main() -> None:
    args = parse_args()
    system_prompt = DIRECT_SYSTEM_PROMPT if args.prompt == "direct" else SYSTEM_PROMPT
    model_id = MODELS[args.model]

    agent = create_harness(
        name=args.name,
        description="A Capture the Flag game agent that plays as a member of a team, connecting via custom MCP server. Direct and tactical decision-making for fast-paced gameplay.",
        model=model_id,
        instructions=system_prompt,
        effort="low",
        mcp_servers={
            "ctf-game": {
                "type": "http",
                "url": "http://localhost:8080/mcp",
            },
        },
        builtin_tools={
            "web_search": False,
            "programmatic_tool_caller": True,
            "subagent": False,
        },
        skills=False,
        session={
            "dir": "./.agent/sessions",
        },
        memory={
            "dir": "./.agent/memory",
        },
    )

    agent(build_instruction(args.match, args.name))

if __name__ == "__main__":
    main()
