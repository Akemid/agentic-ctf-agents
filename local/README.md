# AmazingPlayer

A Capture the Flag game agent that plays as a member of a 3-player team, connecting via custom MCP server. Direct and tactical decision-making for fast-paced gameplay.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Set credentials in .env or use your existing provider credential chain.
strands --agent . --env-file .env
```

Edit `agent/agent.py` to change the agent. The CLI imports the agent defined there.
Each new launch imports the code again; there is no separate configuration snapshot.
Packaged tools, skills, plugins, subagents, MCP servers, and policies live under `agent/`.
The harness library is a normal dependency installed from the registry; `.agent/` holds generated sessions and memory.

For a local MCP server, list its helper files and data in `files` so export can include them.
`files` paths are relative to the saved configuration directory and are removed from exported SDK options.
Use environment placeholders for paths and credentials supplied by the receiving machine.

To use the agent in another application, install its dependencies, configure the environment,
and import it from your application entrypoint:

```python
from agent.agent import agent
result = agent("Hello")
```

Steps to use strands decider

Install
```bash
uv add strands-decider
```

```bash
uv run strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8099
```

## Agents

The agents live in `agent/refined/` (see `agent/refined/README.md`). All system prompts are defined in
`agent/refined/prompts.py`. Each agent takes `--prompt {refined,direct}` (default `refined`); `direct` swaps in
the short A/B prompt and a `refined-direct-*` default name.

```bash
uv run python agent/refined/harness_sdk.py --match llm-1
uv run python agent/refined/harness_sdk.py --match llm-1 --prompt direct
```

`run_agents.sh`, `run_agents_sdk.sh` and `run_agents_decider.sh` launch a 3-agent team with the refined agents.
