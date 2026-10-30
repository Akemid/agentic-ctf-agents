#!/usr/bin/env bash
#
# Launch the full 3-agent team using the plain-SDK agent (agent/refined/harness_sdk.py),
# all running the SAME model.
#
# Change the model by editing the MODEL variable below, or override it inline:
#   MODEL=opus ./run_agents_sdk.sh
#   MODEL=nova-lite MATCH=llm-2 ./run_agents_sdk.sh
#
# Valid models: haiku | opus | nova-lite

set -euo pipefail

cd "$(dirname "$0")"

# ---- Config (edit here) ----------------------------------------------------
MODEL="${MODEL:-haiku}"          # model for all 3 agents
MATCH="${MATCH:-llm-1}"          # match id to join
NAME_PREFIX="${NAME_PREFIX:-AmazingPlayer}"  # agent name prefix
# ----------------------------------------------------------------------------

pids=()

cleanup() {
  echo "Stopping agents..."
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

echo "Launching team of 3 agents (plain SDK) with model '$MODEL' (match: $MATCH)"

for i in 1 2 3; do
  name="${NAME_PREFIX}-${i}"
  echo "Starting agent '$name'"
  uv run agent/refined/harness_sdk.py --model "$MODEL" --match "$MATCH" --name "$name" &
  pids+=("$!")
done

# Wait for all agents to finish.
wait
