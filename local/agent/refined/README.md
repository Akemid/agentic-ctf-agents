# Refined CTF agents

The four CTF agents. Every system prompt lives in `prompts.py` (single source of truth); the agents import their prompt from it.

| File | What it does and why |
|------|----------------------|
| `decider_loop.py` | One decider request now asks `threat`, `route_bad` and `action`; pure `combine_answers` switches to tag/defend on threat and to a safe same-goal route when the pick is blocked/risky (fixes ignored threats and insisting on blocked routes). |
| `decider_loop.py` | A BLOCKED route is annotated "would continue your path but is BLOCKED now" instead of "continues your current path". |
| `harness_sdk.py` | Sliding-window history (`--window`, ~3 ticks, first message pinned), act-fast prompt (each tick was a full LLM turn over a growing conversation). The static-`walls` stripping hook was removed: the server's turn contract v2 no longer sends walls, fields or flag bases (use `join_match.map` / `get_map`). |
| `strand_decider.py` | Same changes as `harness_sdk.py`; the decider reviews only `tag` and `grab_flag` (no round trip on every move/wait). |
| `harness.py` | Act-fast prompt only; `programmatic_tool_caller` stays on. `create_harness` has its own context manager, so no window is set. |

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

## Direct-prompt mode (A/B experiment)

Every agent takes `--prompt {refined,direct}` (default `refined`). `direct` runs the same agent with the short `DIRECT_SYSTEM_PROMPT` from `prompts.py` in place of the whole system prompt. When `--name` is not given, the default name changes too:

| Agent | Default `--name` (`refined`) | Default `--name` (`direct`) |
|-------|------------------------------|-----------------------------|
| `decider_loop.py` | `refined-decider` | `refined-direct-decider` |
| `harness_sdk.py` | `refined-sdk` | `refined-direct-sdk` |
| `strand_decider.py` | `refined-strands-decider` | `refined-direct-strands-decider` |
| `harness.py` | `refined-harness` | `refined-direct-harness` |

Model, tools, memory, hooks, window, initial instruction, decider usage and the other CLI args are identical. The flag exists to measure the effect of the prompt alone: compare the sessions against the refined ones with `../scripts/session_metrics.py` (`ctfagents/scripts/session_metrics.py`).

Launch (same flags as the refined ones, plus `--prompt direct`):

```bash
PYTHONUNBUFFERED=1 uv run python agent/refined/decider_loop.py --prompt direct --match llm-1 --name refined-direct-decider-1 2>&1 | tee "logs/refined-direct-decider-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 uv run python agent/refined/decider_loop.py --prompt direct --match llm-1 --name refined-direct-llmonly-1 --llm-only 2>&1 | tee "logs/refined-direct-llmonly-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/harness_sdk.py --prompt direct --match llm-1 --name refined-direct-sdk-1 --window 12 2>&1 | tee "logs/refined-direct-sdk-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/strand_decider.py --prompt direct --match llm-1 --name refined-direct-strands-decider-1 2>&1 | tee "logs/refined-direct-strands-decider-1-$(date +%Y%m%d-%H%M%S).log"
PYTHONUNBUFFERED=1 .venv/bin/python agent/refined/harness.py --prompt direct --match llm-1 --name refined-direct-harness-1 2>&1 | tee "logs/refined-direct-harness-1-$(date +%Y%m%d-%H%M%S).log"
```
