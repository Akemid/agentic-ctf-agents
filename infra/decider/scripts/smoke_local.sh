#!/usr/bin/env bash
# Build the lightweight adapter image (no torch, no weights) and exercise it against the FAKE decider.
# Local only: no AWS calls, no GPU. Needs Docker. Apple Silicon runs it through amd64 emulation by default
# (the production architecture); PLATFORM=linux/arm64 skips the emulation and is faster.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="$HERE/../adapter"
PLATFORM="${PLATFORM:-linux/amd64}"
IMAGE="ctf-decider-adapter-local:smoke"
NAME="ctf-decider-adapter-smoke"
PORT="${PORT:-18080}"
BASE="http://127.0.0.1:$PORT"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "building $IMAGE for $PLATFORM"
docker build --platform "$PLATFORM" -f "$ADAPTER_DIR/Dockerfile.local" -t "$IMAGE" "$ADAPTER_DIR"

# Run exactly as SageMaker does: the container command is `serve`.
docker run -d --name "$NAME" --platform "$PLATFORM" -p "127.0.0.1:$PORT:8080" -e FAKE_STARTUP_S=3 "$IMAGE" serve >/dev/null

echo "waiting for /ping (the fake decider needs a few seconds, then the adapter warms up)"
code=000
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ping" || true)"
  [[ "$code" == "200" ]] && break
  sleep 1
done
if [[ "$code" != "200" ]]; then
  echo "FAIL: /ping answered $code" >&2
  docker logs "$NAME" >&2 || true
  exit 1
fi
echo "ping: 200"

body="$(python3 -c 'import json,sys; print(json.dumps({"state":"tick: 1","model":"hobson-latest","questions":{"threat":{"type":"noul","instructions":"Is there a threat?"}}}))')"
response="$(curl -s -w '\n%{http_code}' -X POST -H 'content-type: application/json' --data "$body" "$BASE/invocations")"
status="${response##*$'\n'}"
payload="${response%$'\n'*}"
echo "invocations: $status $payload"
[[ "$status" == "200" ]] || { echo "FAIL: /invocations answered $status" >&2; exit 1; }
echo "$payload" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "answers" in d and "queue_wait_ms" in d["adapter"], d'

bad="$(curl -s -o /dev/null -w '%{http_code}' -X POST --data 'not json' "$BASE/invocations")"
echo "invalid body: $bad"
[[ "$bad" == "400" ]] || { echo "FAIL: invalid body answered $bad, expected 400" >&2; exit 1; }

echo "OK: adapter smoke test passed"
