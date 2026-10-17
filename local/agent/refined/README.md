# Refined CTF agents

Refined copies of the four agents in `agent/`. The originals are untouched.

| File | Original | What changed and why |
|------|----------|----------------------|
| `decider_loop.py` | `agent-decider-loop.py` | One decider request now asks `threat`, `route_bad` and `action`; pure `combine_answers` switches to tag/defend on threat and to a safe same-goal route when the pick is blocked/risky (fixes ignored threats and insisting on blocked routes). |
| `decider_loop.py` | same | A BLOCKED route is annotated "would continue your path but is BLOCKED now" instead of "continues your current path". |
| `harness_sdk.py` | `agent-harness-sdk.py` | Sliding-window history (`--window`, ~3 ticks, first message pinned), act-fast prompt (each tick was a full LLM turn over a growing conversation). The static-`walls` stripping hook was removed: the server's turn contract v2 no longer sends walls, fields or flag bases (use `join_match.map` / `get_map`). |
| `strand_decider.py` | `agent-strand-decider.py` | Same changes as `harness_sdk.py`; the decider reviews only `tag` and `grab_flag` (no round trip on every move/wait). |
| `harness.py` | `agent-harness.py` | Act-fast prompt only; `programmatic_tool_caller` stays on. `create_harness` has its own context manager, so no window is set. |

All agents default to a lowercase `refined-*` name and use an English initial instruction.

## Launch

Start the decider first (for `decider_loop.py` and `strand_decider.py`):

```bash
uv run strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8099
```

Then, one process per team member, with a timestamped log:

```bash
PYTHONUNBUFFERED=1 uv run python agent/refined/decider_loop.py --match llm-1 --name refined-decider-1 2>&1 | tee "logs/refined-decider-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 uv run python agent/refined/decider_loop.py --match llm-1 --name refined-llmonly-1 --llm-only 2>&1 | tee "logs/refined-llmonly-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/harness_sdk.py --match llm-1 --name refined-sdk-1 --window 12 2>&1 | tee "logs/refined-sdk-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/strand_decider.py --match llm-1 --name refined-strands-decider-1 2>&1 | tee "logs/refined-strands-decider-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/harness.py --match llm-1 --name refined-harness-1 2>&1 | tee "logs/refined-harness-1-$(date +%Y%m%d-%H%M%S).log"
```

The `--llm-only` line is the ablation mode (the LLM decides every tick, the decider is never called): the decider server is NOT needed for it.

`mkdir -p logs` first. `decider_loop.py` also takes `--threshold` and `--threat-threshold`.

## Direct-prompt variants (A/B experiment)

| File | Wraps | Default `--name` |
|------|-------|------------------|
| `decider_loop_direct.py` | `decider_loop.py` | `refined-direct-decider` |
| `harness_sdk_direct.py` | `harness_sdk.py` | `refined-direct-sdk` |
| `strand_decider_direct.py` | `strand_decider.py` | `refined-direct-strands-decider` |
| `harness_direct.py` | `harness.py` | `refined-direct-harness` |

Each is a thin wrapper that runs the original `main` with the short `DIRECT_SYSTEM_PROMPT` from `direct_prompt.py` (the only copy) in place of the whole system prompt. Model, tools, memory, hooks, window, initial instruction, decider usage and CLI args are identical. They exist to measure the effect of the prompt alone: compare their sessions against the refined ones with `../scripts/session_metrics.py` (`ctfagents/scripts/session_metrics.py`). To allow this, the originals' `main`/`parse_args` take optional `system_prompt`/`default_name` parameters (defaults keep the current behavior) and `decider_loop.play` takes `system_prompt`.

Launch (same flags as the refined ones):

```bash
PYTHONUNBUFFERED=1 uv run python agent/refined/decider_loop_direct.py --match llm-1 --name refined-direct-decider-1 2>&1 | tee "logs/refined-direct-decider-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 uv run python agent/refined/decider_loop_direct.py --match llm-1 --name refined-direct-llmonly-1 --llm-only 2>&1 | tee "logs/refined-direct-llmonly-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/harness_sdk_direct.py --match llm-1 --name refined-direct-sdk-1 --window 12 2>&1 | tee "logs/refined-direct-sdk-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/strand_decider_direct.py --match llm-1 --name refined-direct-strands-decider-1 2>&1 | tee "logs/refined-direct-strands-decider-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/harness_direct.py --match llm-1 --name refined-direct-harness-1 2>&1 | tee "logs/refined-direct-harness-1-$(date +%Y%m%d-%H%M%S).log"
```
