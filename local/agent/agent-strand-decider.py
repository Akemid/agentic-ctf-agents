"""CTF agent (plain Strands SDK + MCP) whose tool calls are reviewed by a local strands-decider.

Same agent as `agent-harness-sdk.py` -- a model, a system prompt and the game's MCP tools --
plus an `InterventionHandler` that runs before every game action. A local strands-decider
model answers narrow yes/no questions about the proposed call, and a plain-Python policy
turns those probabilities into `Guide` (send the model back with feedback) or `Proceed`.

    uv run strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8099
    uv run python agent/agent-strand-decider.py --match llm-1

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

from _client import NAME, Decider, ServerUnavailable
from strands import Agent
from strands.interventions import Guide, InterventionHandler, Proceed
from strands.tools.mcp import MCPClient

# Same as agent-harness-sdk.py: the plain Strands Agent takes Bedrock ids WITHOUT "bedrock/".
MODELS = {
    "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "opus": "global.anthropic.claude-opus-4-6-v1",
    "nova-lite": "global.amazon.nova-2-lite-v1:0",
}

SYSTEM_PROMPT = (
    "You are AmazingPlayer, a Capture the Flag game agent competing as a member of a "
    "team. You are direct and tactical—make quick strategic decisions to advance "
    "your team's objectives. Use the MCP connection to interact with the game state, "
    "communicate team positions, coordinate flag captures, and respond to enemy movements "
    "with speed and precision. Analyze game state efficiently and prioritize actions that "
    "give your team the best advantage."
    " Each wait_for_turn result includes state.routes: route options computed by the "
    "server (ids like enemy_flag_direct, enemy_flag_safe, enemy_flag_detour, own_base_*, "
    "tag:<enemy_id>, defend_flag). The server never recommends one; you choose. Weigh each "
    "route's length, blocked_now/blocked_reason, risks (cells where you can be tagged) and "
    "min_enemy_distance, then call move with that route's first_step. A tag route with "
    "length 0 means the enemy is adjacent: call tag with that enemy_id. Call grab_flag when "
    "standing on the enemy flag. Call exactly one action per tick, with the current tick."
)

MCP_SERVER_NAME = "ctf-game"
MCP_SERVERS = {
    MCP_SERVER_NAME: {
        "url": "http://localhost:8080/mcp",
        "transport": "streamable-http",
    },
}

# --------------------------------------------------------------------------- the questions
#
# Each question is a single judgement answered with P(true). The `true`/`false`
# descriptions carry most of the signal, so they are written as carefully as the question.

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
UNGATED_PREFIXES = ("wait_for_turn", "get_", "list_", "join")

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

        if self._decider_down or short.startswith(UNGATED_PREFIXES):
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
    parser = argparse.ArgumentParser(description="AmazingPlayer CTF agent with strands-decider tool review.")
    parser.add_argument("--model", choices=sorted(MODELS.keys()), default="haiku",
                        help="Model to run the agent with (default: haiku).")
    parser.add_argument("--match", default="llm-1", help="Match id to join (default: llm-1).")
    parser.add_argument("--name", default="AmazingPlayer",
                        help="Agent name used to join the match (default: AmazingPlayer).")
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
    )

    agent(
        f"Únete a la partida {args.match} a un equipo que este disponible "
        f"(primero intenta con red) con el nombre '{args.name}' y juega "
        "llamando a wait_for_turn y a una acción en cada turno hasta recibir match_over."
    )

    reviewed = [row for row in gate.log if row["verdict"] != "forced"]
    guided = sum(1 for row in reviewed if row["verdict"] == "guided")
    print(f"\n{decider}; guided {guided} of {len(reviewed)} reviewed actions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
