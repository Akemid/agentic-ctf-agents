# Repo-level entry points. `deploy` is the documented way to run `agentcore deploy`: it hydrates the OAuth
# credentials first (the CLI silently skips providers without them) and always cleans up afterwards.
#
#   make check            offline checks, no AWS
#   make deploy           prints the plan only
#   make deploy YES=1     MUTATES AWS: hydrate, check, validate, deploy, clean
#   make deploy YES=1 UPDATE_DISCOVERY_URL=1   same, accepting a recreated Cognito pool
.PHONY: check deploy

check:
	uv run python scripts/sync_common.py --check
	uv run pytest
	cd agentcore/cdk && node_modules/.bin/jest
	cd infra/decider && node_modules/.bin/jest
	cd agentcore && agentcore validate

deploy:
	YES=$(YES) UPDATE_DISCOVERY_URL=$(UPDATE_DISCOVERY_URL) scripts/deploy_fleet.sh
