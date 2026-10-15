"""A scripted Strands model: plays the CTF loop deterministically, no Bedrock."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Any

from strands.models import Model


def _last_tool_result_text(messages: list[dict]) -> str | None:
    for block in reversed(messages[-1].get("content", [])) if messages else []:
        result = block.get("toolResult")
        if result:
            return "".join(part.get("text", "") for part in result.get("content", []))
    return None


class ScriptedPlayer(Model):
    """join_match -> wait_for_turn -> move -> wait_for_turn ... until match_over, then a final text."""

    def __init__(self, join_args: dict[str, Any] | None = None) -> None:
        self.calls: list[list[dict]] = []  # a snapshot of the messages for every model call
        self.system_prompts: list[str | None] = []
        self.join_args = join_args or {"match_id": "m1", "name": "p1"}
        self._n = 0

    def update_config(self, **model_config: Any) -> None:
        return None

    def get_config(self) -> Any:
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    def _decide(self, messages: list[dict]) -> tuple[str, dict[str, Any]] | str:
        text = _last_tool_result_text(messages)
        if text is None:
            return ("ctf-game_join_match", self.join_args)
        if '"status": "joined"' in text:
            return ("ctf-game_wait_for_turn", {})
        if '"status": "ok"' in text:
            return ("ctf-game_wait_for_turn", {})
        payload = json.loads(text) if text.startswith("{") else {}
        if payload.get("status") == "turn":
            return ("ctf-game_move", {"tick": payload["tick"], "direction": "up"})
        return "match over, good game"

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncIterable[dict]:
        self.calls.append(json.loads(json.dumps(messages)))
        self.system_prompts.append(system_prompt)
        decision = self._decide(messages)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(decision, str):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": decision}}}
            yield {"contentBlockStop": {}}
            stop_reason = "end_turn"
        else:
            self._n += 1
            name, args = decision
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tu-{self._n}", "name": name}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}}
            yield {"contentBlockStop": {}}
            stop_reason = "tool_use"
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


class SequencePlayer(ScriptedPlayer):
    """Plays a fixed list of (tool name, arguments) calls whatever the tool results say, then a final text."""

    def __init__(self, script: list[tuple[str, dict[str, Any]]]) -> None:
        super().__init__()
        self.script = list(script)
        self._i = 0

    def _decide(self, messages: list[dict]) -> tuple[str, dict[str, Any]] | str:
        if self._i >= len(self.script):
            return "done"
        decision = self.script[self._i]
        self._i += 1
        return decision
