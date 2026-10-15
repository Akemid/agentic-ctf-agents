#!/usr/bin/env bash
# Safe deploy of the AgentCore fleet. This is the ONLY documented way to run `agentcore deploy` here.
#
#   YES=1 scripts/deploy_fleet.sh      (or: make deploy YES=1)
#
# Why a wrapper: `agentcore deploy` does not fail when an OAuth credential provider has no client id or
# secret in agentcore/.env.local. It logs `Skipped <provider>` and still reports success, so a deploy
# without hydration "works" and the runtimes then fail at their first token request. The steps below run
# in this order, and the secrets are wiped on EXIT whatever happens (failure, Ctrl-C, SIGTERM):
#
#   1. sync_common --check                   vendored ctf_common copies match shared/
#   2. hydrate_credentials                   SSM + Secrets Manager (read-only) -> .env.local, real discoveryUrl
#   3. hydrate_credentials --check           offline: everything the deploy needs is in place
#   4. agentcore validate
#   5. agentcore deploy --yes                MUTATES AWS
#   6. hydrate_credentials --clean           (trap on EXIT) wipe secrets, restore the placeholder
#
# Without YES=1 it only prints this plan and runs nothing. After the game stack was redeployed (new Cognito
# pool) add UPDATE_DISCOVERY_URL=1 to accept the new discovery URL in step 2.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HYDRATE=(uv run python scripts/hydrate_credentials.py)

if [ "${YES:-}" != "1" ]; then
  cat <<'PLAN'
Plan (nothing was run). Deploying changes AWS, so it needs an explicit YES=1:

  1. uv run python scripts/sync_common.py --check
  2. uv run python scripts/hydrate_credentials.py
  3. uv run python scripts/hydrate_credentials.py --check
  4. (cd agentcore && agentcore validate)
  5. agentcore deploy --yes                                <- mutates AWS
  6. uv run python scripts/hydrate_credentials.py --clean   (always, also on failure or Ctrl-C)

Run it with:  YES=1 scripts/deploy_fleet.sh   (or: make deploy YES=1)
PLAN
  exit 0
fi

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  echo "==> hydrate_credentials --clean" >&2
  if ! "${HYDRATE[@]}" --clean; then
    echo "WARNING: hydrate_credentials --clean failed. Run it by hand: ${HYDRATE[*]} --clean" >&2
    if [ "$status" -eq 0 ]; then
      status=1
    fi
  fi
  exit "$status"
}
# Installed before the first step: a signal or failure at any point still ends in `--clean`.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

echo "==> sync_common --check"
uv run python scripts/sync_common.py --check
echo "==> hydrate_credentials"
# UPDATE_DISCOVERY_URL=1 accepts a recreated Cognito pool (a changed discovery URL in SSM).
"${HYDRATE[@]}" ${UPDATE_DISCOVERY_URL:+--update-discovery-url}
echo "==> hydrate_credentials --check"
"${HYDRATE[@]}" --check
echo "==> agentcore validate"
(cd agentcore && agentcore validate)
echo "==> agentcore deploy"
agentcore deploy --yes
echo "==> deployed. The fleet runtimes are ready; secrets are cleaned up next."
