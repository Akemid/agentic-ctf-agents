"""strand_decider gate: an `InterventionHandler` that reviews `tag` and `grab_flag` with the decider.

Ported from `local/agent/refined/strand_decider.py`. The model classifies, the code decides: two
yes/no questions about the proposed call become `Guide` (send the model back with feedback) or
`Proceed`. Only `tag` and `grab_flag` are gated; `move` and `wait` proceed immediately so no decider
round trip is paid on every move.

Differences from the local script: the cross-process file lock is gone (serialization lives in the
SageMaker adapter), and there is no permanent "decider down" state. A failed call proceeds (fail
open, the game must not stop because the reviewer died) and the decider client's circuit breaker
decides when to ask again.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from strands.interventions import Guide, InterventionHandler, OnError, Proceed

from ctf_common.decider import DeciderPort, ServerUnavailable, noul
from ctf_common.mcp import DEFAULT_SERVER_NAME

log = logging.getLogger("strand_decider")

MCP_SERVER_NAME = DEFAULT_SERVER_NAME
NAME = "strands-decider"

QUESTIONS = {
    "args_grounded": noul(
        "Are the tool's argument values grounded in the user's request or in game state "
        "returned by earlier tool results?",
        {
            "true": "every argument value traces back to the user request or a previous tool result",
            "false": "an argument value was guessed or invented, not seen anywhere in the conversation",
        },
    ),
    "stale_state": noul(
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

# Only these actions are reviewed: they are the costly-to-get-wrong ones. move/wait are cheap and
# frequent, and a decider round trip on every move was the dominant per-tick cost.
GATED_TOOLS = ("tag", "grab_flag")

# Guide cancels the call and the model retries. Cap consecutive guides so a strict decider
# can never stall the game: after this many in a row, the next call proceeds.
MAX_CONSECUTIVE_GUIDES = 2

# The decider has a bounded context window; only the recent tail of the game matters.
MAX_STATE_MESSAGES = 12
MAX_BLOCK_CHARS = 1500

GUIDE_UNGROUNDED = (
    "Some argument values are not backed by the game state or the user's request. "
    "Re-read the latest tool result and use only values that appear there."
)
GUIDE_STALE = (
    "You are acting on outdated game state. Call wait_for_turn (or read the "
    "current state) before taking another action."
)


class ToolCallReviewer(InterventionHandler):
    """Review each proposed game action before it runs, using the decider."""

    name = f"{NAME}-ctf-tool-call-reviewer"

    def __init__(self, decider: DeciderPort) -> None:
        self._decider = decider
        self._consecutive_guides = 0
        self.log: list[dict[str, object]] = []

    @staticmethod
    def _short_name(tool_name: str) -> str:
        prefix = f"{MCP_SERVER_NAME}_"
        return tool_name[len(prefix) :] if tool_name.startswith(prefix) else tool_name

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
                    parts = [
                        c.get("text") or json.dumps(c.get("json"))
                        for c in block["toolResult"].get("content", [])
                    ]
                    lines.append(f"tool result: {cls._clip(' '.join(p for p in parts if p))}")
        return "\n".join(lines) if lines else "(no conversation yet)"

    @property
    def on_error(self) -> OnError:
        """A reviewer bug must never stop a live match: strands lets the call proceed."""
        return "proceed"

    @staticmethod
    def _probabilities(answers: Any) -> tuple[float, float] | None:
        """`(args_grounded, stale_state)`, or None when the decider answered with a bad shape."""
        try:
            values = (answers["args_grounded"]["noul"], answers["stale_state"]["noul"])
        except (KeyError, TypeError, IndexError):
            return None
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                return None
        return float(values[0]), float(values[1])

    def _record(self, tool: str, verdict: str, **scores: float) -> None:
        self.log.append({"tool": tool, **{k: round(v, 2) for k, v in scores.items()}, "verdict": verdict})

    def before_tool_call(self, event: Any, **kwargs: Any) -> Proceed | Guide:
        tool_name = event.tool_use["name"]
        short = self._short_name(tool_name)
        arguments = event.tool_use.get("input") or {}

        if short not in GATED_TOOLS:
            return Proceed(reason="ungated")

        if self._consecutive_guides >= MAX_CONSECUTIVE_GUIDES:
            self._consecutive_guides = 0
            self._record(short, "forced")
            log.info("%s: guide cap reached, proceeding", short)
            return Proceed(reason="guide cap reached")

        state = (
            "A Capture the Flag game played by an AI assistant through tools is below, "
            "followed by the action the assistant now wants to take.\n\n"
            f"--- RECENT GAME ---\n{self._render(event.agent.messages)}\n\n"
            f"--- PROPOSED TOOL CALL ---\ntool: {short}\narguments: {json.dumps(arguments)}"
        )

        try:
            answers = self._decider.ask(state, QUESTIONS)
        except ServerUnavailable as exc:
            # Fail open: the game must not stop because the reviewer is unavailable.
            log.warning("%s: decider unavailable (%s), proceeding without review", short, type(exc).__name__)
            return Proceed(reason="decider unavailable")
        except Exception as exc:  # noqa: BLE001 - a reviewer bug must never kill a live match
            log.error(
                "%s: unexpected decider failure (%s), proceeding without review", short, type(exc).__name__
            )
            return Proceed(reason="decider unavailable")

        probabilities = self._probabilities(answers)
        if probabilities is None:
            # Fail open, and do not count it as a guide: a garbled answer is not a verdict.
            log.warning("%s: malformed decider answer, proceeding without review", short)
            return Proceed(reason="decider unavailable")
        args_grounded, stale_state = probabilities
        log.info("%s args_grounded=%.2f stale_state=%.2f", short, args_grounded, stale_state)

        if args_grounded < YES:
            action: Proceed | Guide = Guide(feedback=GUIDE_UNGROUNDED)
        elif stale_state >= YES:
            action = Guide(feedback=GUIDE_STALE)
        else:
            action = Proceed()

        verdict = "guided" if isinstance(action, Guide) else "proceeded"
        self._consecutive_guides = self._consecutive_guides + 1 if verdict == "guided" else 0
        self._record(short, verdict, args_grounded=args_grounded, stale_state=stale_state)
        return action
