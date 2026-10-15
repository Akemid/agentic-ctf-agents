"""REFINED CTF agent (plain Strands SDK + MCP) whose tool calls are reviewed by a local strands-decider.

Refined version of `agent/agent-strand-decider.py` (the original is untouched). Same agent as
`harness_sdk.py` -- model, prompt, MCP tools, bounded history (`--window`) and act-fast prompt
(all imported from it) -- plus an `InterventionHandler`. Change versus the
original review: only `tag` and `grab_flag` are gated by the decider; `move` and `wait` proceed
immediately, so no decider round trip is paid on every move.

The original description follows. An `InterventionHandler` runs before the gated game actions. A local strands-decider
model answers narrow yes/no questions about the proposed call, and a plain-Python policy
turns those probabilities into `Guide` (send the model back with feedback) or `Proceed`.

    uv run strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8099
    uv run python agent/refined/strand_decider.py --match llm-1

The model classifies, the code decides. The questions, thresholds and policy are
illustrative: tune them against real games. STRANDS_DECIDER_URL overrides the endpoint.
"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import sys
import tempfile

# `_client` lives in the parent `agent/` folder; `harness_sdk` is next to this file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _client import NAME, Decider, ServerUnavailable  # noqa: E402
from harness_sdk import (  # noqa: E402
    DEFAULT_WINDOW,
    MODELS,
    SYSTEM_PROMPT,
    build_instruction,
    make_conversation_manager,
)
from strands import Agent  # noqa: E402
from strands.interventions import Guide, InterventionHandler, Proceed  # noqa: E402
from strands.tools.mcp import MCPClient  # noqa: E402

MCP_SERVER_NAME = "ctf-game"
MCP_SERVERS = {
    MCP_SERVER_NAME: {
        "url": "http://localhost:8080/mcp",
        "transport": "streamable-http",
    },
}

QUESTIONS = {
    "args_grounded": Decider.noul(
        "Are the tool's argument values grounded in the user's request or in game state "
        "returned by earlier tool results?",
        {
            "true": "every argument value traces back to the user request or a previous tool result",
            "false": "an argument value was guessed or invented, not seen anywhere in the conversation",
        },
    ),
    "stale_state": Decider.noul(
        "Is the assistant acting on outdated game state, without having observed the "
        "current turn since its last action?",
        {
            "true": "the assistant acts without a fresh game state result since its previous action",
            "false": "the latest tool result describes the current turn and the action follows from it",
        },
    ),
}

# A probability at or above this reads as a confident "yes". Policy knob, owned by the code.
YES = 0.45

# Observation/sync tools are cheap and safe; reviewing them would only add latency per turn.
# Matched against the tool name without the MCP server prefix. Adjust to the real tool list.
# Only these actions are reviewed: they are the costly-to-get-wrong ones. move/wait are cheap and
# frequent, and a decider round trip on every move was the dominant per-tick cost.
GATED_TOOLS = ("tag", "grab_flag")

# Guide cancels the call and the model retries. Cap consecutive guides so a strict decider
# can never stall the game: after this many in a row, the next call proceeds.
MAX_CONSECUTIVE_GUIDES = 2

# The decider has a bounded context window; only the recent tail of the game matters.
MAX_STATE_MESSAGES = 12
MAX_BLOCK_CHARS = 1500

# Shared by every agent process on this machine to serialize requests to the decider.
DECIDER_LOCK_FILE = os.path.join(tempfile.gettempdir(), "strands-decider.lock")


# --------------------------------------------------------------------------- the gate


class ToolCallReviewer(InterventionHandler):
    """Review each proposed game action before it runs, using strands-decider."""

    name = f"{NAME}-ctf-tool-call-reviewer"

    def __init__(self, decider: Decider) -> None:
        self._decider = decider
        self._consecutive_guides = 0
        self._decider_down = False
        self.log: list[dict[str, object]] = []

    @staticmethod
    def _short_name(tool_name: str) -> str:
        prefix = f"{MCP_SERVER_NAME}_"
        return tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name

    @staticmethod
    def _clip(text: str) -> str:
        return text if len(text) <= MAX_BLOCK_CHARS else text[:MAX_BLOCK_CHARS] + " ...[truncated]"

    @classmethod
    def _render(cls, messages: list[dict]) -> str:
        """Flatten recent messages to text, including tool calls and results (the game state)."""
        lines: list[str] = []
        for message in messages[-MAX_STATE_MESSAGES:]:
            role = message.get("role", "unknown")
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                if "text" in block:
                    lines.append(f"{role}: {cls._clip(block['text'])}")
                elif "toolUse" in block:
                    use = block["toolUse"]
                    lines.append(f"{role} called {use.get('name')}: {json.dumps(use.get('input') or {})}")
                elif "toolResult" in block:
                    parts = [c.get("text") or json.dumps(c.get("json")) for c in block["toolResult"].get("content", [])]
                    lines.append(f"tool result: {cls._clip(' '.join(p for p in parts if p))}")
        return "\n".join(lines) if lines else "(no conversation yet)"

    def _record(self, tool: str, verdict: str, **scores: float) -> None:
        self.log.append({"tool": tool, **{k: round(v, 2) for k, v in scores.items()}, "verdict": verdict})

    def _ask_serialized(self, state: str) -> dict:
        """Ask the decider holding a cross-process file lock.

        The server runs inference in a thread pool with no lock, so concurrent requests from
        the 3 team agents crash it (MPS). One request at a time across all processes.
        """
        with open(DECIDER_LOCK_FILE, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._decider.ask(state, QUESTIONS)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def before_tool_call(self, event, **kwargs):
        tool_name = event.tool_use["name"]
        short = self._short_name(tool_name)
        arguments = event.tool_use.get("input") or {}

        if self._decider_down or short not in GATED_TOOLS:
            return Proceed(reason="ungated")

        if self._consecutive_guides >= MAX_CONSECUTIVE_GUIDES:
            self._consecutive_guides = 0
            self._record(short, "forced")
            print(f"[intervention] {short}: guide cap reached -> proceeding\n")
            return Proceed(reason="guide cap reached")

        state = (
            "A Capture the Flag game played by an AI assistant through tools is below, "
            "followed by the action the assistant now wants to take.\n\n"
            f"--- RECENT GAME ---\n{self._render(event.agent.messages)}\n\n"
            f"--- PROPOSED TOOL CALL ---\ntool: {short}\narguments: {json.dumps(arguments)}"
        )

        try:
            answers = self._ask_serialized(state)
        except (ServerUnavailable, OSError, http.client.HTTPException) as exc:
            # RemoteDisconnected is an http.client error, not a URLError, so _client does not
            # wrap it in ServerUnavailable -- catch the raw connection errors here as well.
            # Fail open: the game must not stop because the reviewer died mid-match.
            self._decider_down = True
            print(f"[intervention] {exc}\n    -> reviewer disabled, proceeding without it\n")
            return Proceed(reason="decider unavailable")

        args_grounded = answers["args_grounded"]["noul"]
        stale_state = answers["stale_state"]["noul"]
        print(f"[intervention] {short} args_grounded={args_grounded:.2f} stale_state={stale_state:.2f}")

        if args_grounded < YES:
            action = Guide(
                feedback=(
                    "Some argument values are not backed by the game state or the user's request. "
                    "Re-read the latest tool result and use only values that appear there."
                )
            )
        elif stale_state >= YES:
            action = Guide(
                feedback=(
                    "You are acting on outdated game state. Call wait_for_turn (or read the "
                    "current state) before taking another action."
                )
            )
        else:
            action = Proceed()

        verdict = "guided" if isinstance(action, Guide) else "proceeded"
        self._consecutive_guides = self._consecutive_guides + 1 if verdict == "guided" else 0
        self._record(short, verdict, args_grounded=args_grounded, stale_state=stale_state)
        print(f"    {'~>' if verdict == 'guided' else '->'} {verdict}\n")
        return action


# --------------------------------------------------------------------------- the agent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refined AmazingPlayer CTF agent with strands-decider tool review.")
    parser.add_argument("--model", choices=sorted(MODELS.keys()), default="haiku",
                        help="Model to run the agent with (default: haiku).")
    parser.add_argument("--match", default="llm-1", help="Match id to join (default: llm-1).")
    parser.add_argument("--name", default="refined-strands-decider",
                        help="Agent name used to join the match (default: refined-strands-decider).")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help=f"Conversation window in messages, 4 per tick (default: {DEFAULT_WINDOW}, ~3 ticks).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    decider = Decider()
    try:
        print(decider.banner(), "\n")
    except ServerUnavailable as exc:
        print(exc)
        return 1

    gate = ToolCallReviewer(decider)
    # Each MCPClient is a ToolProvider: the agent's registry starts and owns the connection.
    clients = MCPClient.load_servers(MCP_SERVERS, prefix_with_server_name=True)

    agent = Agent(
        name=args.name,
        model=MODELS[args.model],
        system_prompt=SYSTEM_PROMPT,
        tools=clients,
        interventions=[gate],
        conversation_manager=make_conversation_manager(args.window),
    )

    agent(build_instruction(args.match, args.name))

    reviewed = [row for row in gate.log if row["verdict"] != "forced"]
    guided = sum(1 for row in reviewed if row["verdict"] == "guided")
    print(f"\n{decider}; guided {guided} of {len(reviewed)} reviewed actions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
