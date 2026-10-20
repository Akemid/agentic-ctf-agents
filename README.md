# ctfagents

[![CI](https://github.com/Akemid/agentic-ctf-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/Akemid/agentic-ctf-agents/actions/workflows/ci.yml)

Capture-the-flag (CTF) game agents built with [Strands Agents](https://strandsagents.com), Strands Harness
(`strands-harness`), and [Strands Decider 2B](https://github.com/strands-labs/strands-decider), a small
decision model that reviews or drives agent actions.

The repository contains four agent runtimes that deploy to Amazon Bedrock AgentCore and play against the
`agentic-ctf` game server over MCP. It also includes the infrastructure to host the decider on a SageMaker
endpoint, and the scripts used to launch a fleet of agents for a workshop.

## Architecture

```
                 +--------------------+      MCP       +-----------------------+
 scripts/fleet.py| AgentCore runtimes |<-------------->| agentic-ctf game      |
 (launch / stop) |  harness           |                | server (separate repo)|
 --------------->|  harness_sdk       |                +-----------------------+
                 |  strand_decider ---+--+
                 |  decider_loop   ---+--+  invoke_endpoint
                 +--------------------+  |
                                         v
                          +-------------------------------+
                          | SageMaker endpoint "ctf-decider"|  (optional, GPU, billed hourly)
                          | FastAPI adapter -> strands-decider serve
                          +-------------------------------+
```

| Runtime | How it plays |
|---|---|
| `harness` | A `strands_harness` agent; the model calls the game tools itself. |
| `harness_sdk` | A plain Strands `Agent` with a sliding-window conversation manager. |
| `strand_decider` | A plain Strands `Agent`; `tag` and `grab_flag` calls are reviewed by the decider (yes/no questions). |
| `decider_loop` | Plain Python loop: one decider request per tick, with the LLM used only when the decider is not confident or does not answer in time. |

### About Strands Decider 2B

[Strands Decider 2B](https://github.com/strands-labs/strands-decider) is an open decision model (Apache-2.0)
from Strands Labs. It is a LoRA adapter on `Qwen/Qwen3.5-2B-Base` with a small readout head instead of a
language-modeling head: it does not generate text, it picks among fixed options and returns a calibrated
confidence. It answers three question types: `noul` (yes/no), `choice` (one of N), and `score` (ordered scale).

This project uses the
[`StrandsAgents/strands-decider-2B-hobson-v21`](https://huggingface.co/StrandsAgents/strands-decider-2B-hobson-v21)
checkpoint (pinned in `infra/decider/lib/names.ts`) for fast, routine game decisions, and escalates to an LLM
when the decider is not confident. `strands-decider serve` has no authentication and is meant for local use, so
the deployed endpoint puts it behind a FastAPI adapter on a SageMaker endpoint, which is only reachable through IAM-signed `InvokeEndpoint` calls.

The decider is optional. If the endpoint is down, `strand_decider` proceeds ungated and `decider_loop` falls
back to the LLM. Shared code (game client, MCP wiring, decider client, hooks) lives in `shared/ctf_common`
and is vendored into each runtime by `scripts/sync_common.py`.

## Repository layout

| Path | Contents |
|---|---|
| `app/` | The four AgentCore runtimes, each with its own `pyproject.toml` and README. |
| `shared/ctf_common/` | Source of truth for code shared by the runtimes. |
| `agentcore/` | AgentCore project: `agentcore.json`, CDK app, deployment targets. |
| `infra/decider/` | Separate CDK app for the SageMaker decider (adapter, image build, budget alert). |
| `scripts/` | Fleet launcher, deploy wrapper, credential hydration, metrics. |
| `local/` | Local Strands/Harness agent used for experimentation and as the origin of the ports. |
| `spikes/` | Exploratory experiments and their findings (`spikes/FINDINGS.md`). |
| `tests/` | Python test suite. |
| `docs/` | Runbooks and acceptance notes. |

## Prerequisites

- Python 3.14 (see `.python-version`) and [uv](https://docs.astral.sh/uv/)
- Node.js 22 and npm (for the CDK apps and tests)
- AWS CLI with credentials for the target account and region
- [AgentCore CLI](https://github.com/aws/agentcore-cli) (`agentcore`) for validate and deploy
- Docker is not required; the decider image is built in CodeBuild

## Quickstart

```bash
# Python environment (root dev environment: tests and shared libraries)
uv sync

# Lint and test (offline)
uv run ruff check .
uv run pytest

# Infra tests
cd infra/decider && npm ci && npm test && cd ../..

# All offline checks in one go (also needs agentcore/cdk dependencies installed)
make check
```

Deploy the runtimes (mutates AWS; run `make deploy` without `YES=1` first to see the plan):

```bash
make deploy YES=1
```

The decider endpoint is deployed separately and bills hourly while it exists:

```bash
cd infra/decider
make install
CTF_BUDGET_EMAIL=<your-email> make deploy-persistent   # once
make build-image YES=1                                  # prints imageTag=<hash>
IMAGE_TAG=<hash> make deploy-decider
make destroy-decider                                    # stop billing
```

To run the local experimentation agent, see `local/README.md`
(`uv run strands-decider serve <model-id> --port 8099` starts a local decider).

## Configuration

No account-specific values are committed.

- `agentcore/aws-targets.json` holds the deployment target (account and region). The committed version is an empty
  list (`[]`). Add your own target locally, for example
  `[{"name": "default", "account": "123456789012", "region": "us-east-2"}]`, and keep that edit out of
  commits with `git update-index --skip-worktree agentcore/aws-targets.json`.
- `local/.env.example` is the template for local runs. Copy it to `local/.env` and add your own provider
  credentials (or use your existing credential chain); `.env` files are gitignored.
- The budget alert email for the decider stack is read from `CTF_BUDGET_EMAIL` and is never committed.
- Runtime secrets (OAuth credentials for the game server) are hydrated at deploy time by
  `scripts/hydrate_credentials.py` and cleaned up afterwards.

## Cost notice

The GPU endpoint (`ml.g4dn.xlarge`) costs roughly USD 0.74 per hour while it exists. Destroy it when you are
done. See the runbook for the full list of billed resources.

## Documentation

- [`docs/workshop-runbook.md`](docs/workshop-runbook.md): stand up and tear down the fleet step by step
- [`docs/e2e-acceptance.md`](docs/e2e-acceptance.md): end-to-end acceptance checks
- [`infra/decider/README.md`](infra/decider/README.md): decider stacks, adapter, failure handling
- [`spikes/FINDINGS.md`](spikes/FINDINGS.md): experiment results
- Per-runtime READMEs under `app/*/`

## License

MIT. See [LICENSE](LICENSE).
