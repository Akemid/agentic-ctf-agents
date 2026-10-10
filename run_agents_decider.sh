set -euo pipefail

cd "$(dirname "$0")"

MODEL="${MODEL:-haiku}"          # model for all 3 agents
MATCH="${MATCH:-llm-1}"          # match id to join
NAME_PREFIX="${NAME_PREFIX:-AmazingPlayer}"  # agent name prefix
DECIDER_URL="${STRANDS_DECIDER_URL:-http://127.0.0.1:8099}"  # shared local decider

# All 3 agents share one decider server, so check it once before launching anything.
if ! curl -sf "$DECIDER_URL/health" >/dev/null; then
  echo "strands-decider is not reachable at $DECIDER_URL. Start it with:"
  echo "  uv run strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8099"
  exit 1
fi

pids=()

cleanup() {
  echo "Stopping agents..."
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

echo "Launching team of 3 agents (SDK + decider) with model '$MODEL' (match: $MATCH)"

for i in 1 2 3; do
  name="${NAME_PREFIX}-${i}"
  echo "Starting agent '$name'"
  STRANDS_DECIDER_URL="$DECIDER_URL" \
    uv run agent/agent-strand-decider.py --model "$MODEL" --match "$MATCH" --name "$name" &
  pids+=("$!")
done

wait
