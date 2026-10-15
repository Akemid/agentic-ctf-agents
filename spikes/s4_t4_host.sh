#!/usr/bin/env bash
# Task 0.7 / S4: benchmark the decider on a real NVIDIA T4 host, fp16 vs fp32.
# Run ON the T4 host (EC2 g4dn.xlarge with a Deep Learning AMI, or any T4 such as a Colab T4 runtime).
# Running it on EC2 creates billable AWS resources: that launch is the user's action, not this repo's.
#
# Findings that shape this script (verified in PR0 from strands-decider 0.1.0 source):
#   * `strands-decider serve` has NO dtype flag (only --device, --host, --port, --no-prefix-cache).
#   * dtype comes from the checkpoint's strands_decider_config.json (`torch_dtype`, default "bfloat16").
#     A T4 has no native bf16, so the adapter must stage a checkpoint copy with the dtype key patched.
#
# Usage on the host (copy spikes/ and one decider log, e.g. decider-194227.log, next to it):
#   bash spikes/s4_t4_host.sh ./decider-194227.log
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${1:?path to a decider_loop log containing '[tick N] state sent to decider' blocks}"
LOG="$(cd "$(dirname "$LOG")" && pwd)/$(basename "$LOG")"
REPO_ID="StrandsAgents/strands-decider-2B-hobson-v21"
PORT=8099
WORK="${WORK:-$PWD/s4-work}"
mkdir -p "$WORK"

SERVER=""
cleanup() {
  if [[ -n "$SERVER" ]]; then
    kill "$SERVER" 2>/dev/null || true
    wait "$SERVER" 2>/dev/null || true
  fi
}
trap cleanup EXIT

VENV="$WORK/venv"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet strands-decider huggingface_hub
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | tee "$WORK/gpu.txt"

CKPT="$WORK/ckpt-base"
python - <<PY
from huggingface_hub import snapshot_download
print(snapshot_download("$REPO_ID", local_dir="$CKPT"))
PY

for DTYPE in float16 float32; do
  DIR="$WORK/ckpt-$DTYPE"
  rm -rf "$DIR"; cp -r "$CKPT" "$DIR"
  python - <<PY
import json, pathlib, sys
d = pathlib.Path("$DIR")
for cfg in (d / "strands_decider_config.json", d / "config.json"):
    if not cfg.exists():
        continue
    data = json.loads(cfg.read_text())
    for key in ("torch_dtype", "dtype"):
        if key in data:
            data[key] = "$DTYPE"
            cfg.write_text(json.dumps(data, indent=2))
            print("patched", cfg, key, "=", "$DTYPE")
            sys.exit(0)
sys.exit("no torch_dtype/dtype key found in strands_decider_config.json or config.json")
PY
  strands-decider serve "$DIR" --device cuda --port "$PORT" >"$WORK/serve-$DTYPE.log" 2>&1 &
  SERVER=$!
  HEALTHY=0
  for _ in $(seq 1 180); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then HEALTHY=1; break; fi
    sleep 5
  done
  if [[ "$HEALTHY" -ne 1 ]]; then
    echo "ERROR: server never became healthy for $DTYPE; see $WORK/serve-$DTYPE.log" >&2
    exit 1
  fi
  nvidia-smi --query-gpu=memory.used --format=csv,noheader | tee "$WORK/vram-$DTYPE.txt"
  python "$SCRIPT_DIR/s4_bench.py" run --log "$LOG" --url "http://127.0.0.1:$PORT" --n 20 --out "$WORK/answers-$DTYPE.json" \
    | tee "$WORK/bench-$DTYPE.txt"
  kill "$SERVER"; wait "$SERVER" 2>/dev/null || true
  SERVER=""
done

python "$SCRIPT_DIR/s4_bench.py" compare "$WORK/answers-float16.json" "$WORK/answers-float32.json" | tee "$WORK/compare.txt"
echo "Send back: $WORK/bench-*.txt, $WORK/compare.txt, $WORK/vram-*.txt, $WORK/gpu.txt (no secrets inside)."
