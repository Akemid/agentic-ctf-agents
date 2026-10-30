# End-to-end acceptance (manual)

PREPARED, NOT RUN. This checklist is the acceptance for task 4.6 of the `agentcore-ctf-fleet` change. It plays
a real match with all four agent types and then tears everything down. It needs a real AWS account, the game
stack and the GPU endpoint, so it is run by a person following `docs/workshop-runbook.md`.

## Before you start

- [ ] Local suites are green: `uv run pytest`, both jest suites, `agentcore validate`.
- [ ] Runbook steps 0 to 6 are done (the runtimes were deployed with `make deploy YES=1`, and the output has no `Skipped`): game stack deployed, decider endpoint `InService`, runtimes deployed,
      `fleet.py` can find the runtime ARNs.
- [ ] The deployed Cognito access-token validity for the agent clients is known (runbook step 1). If it is
      1 hour, keep the match under 1 hour. The design wants 4 hours.
- [ ] You know the match id and the game is ready to accept the characters.

## Run

```bash
MATCH=e2e-$(date +%m%d%H%M)
uv run python scripts/fleet.py start --match "$MATCH" \
  --agent harness=1 --agent harness_sdk=1 --agent strand_decider=1 --agent decider_loop=1       # dry run: 4 characters
uv run python scripts/fleet.py start --match "$MATCH" \
  --agent harness=1 --agent harness_sdk=1 --agent strand_decider=1 --agent decider_loop=1 --yes
uv run python scripts/fleet.py status --match "$MATCH"
```

## Acceptance criteria

1. **All four types start.** `start` exits 0 and prints `ok (started)` for `harness-1`, `harness-sdk-1`,
   `strand-decider-1` and `decider-loop-1`. `.fleet/<match>.json` has four sessions with four distinct ids of
   33+ characters.
2. **All four types play.** `status` shows `running` with a tick count that grows between two calls ten
   seconds apart, for each type. The game shows four distinct players.
3. **The decider is used.** For `strand_decider` and `decider_loop`, the runtime logs (`agentcore logs`) show
   decider calls and no permanent fallback, and the endpoint shows invocations
   (`aws cloudwatch get-metric-statistics` on `Invocations`, namespace `AWS/SageMaker`, dimension
   `EndpointName=ctf-decider`).
4. **Tokens equal MCP connections.** Each character holds exactly one MCP session and no 401 appears in any
   runtime log. The invariant "one token per connection" is proven by unit tests (`tests/test_auth_mcp.py`);
   in the real run the observable proxy is the game server's session count equal to the number of characters.
   [UNVERIFIED: counting `GetResourceOauth2Token` calls needs CloudTrail data events for AgentCore Identity,
   which may not be enabled. If they are, count them over the match window and expect one per character.]
5. **The match ends cleanly.** `status` ends in `finished` for every type, or `stop` (below) ends the match and
   `.fleet/<match>.json` holds `"last_status": "finished"` (or `"stopped"` after a stop) for every session. After
   that, `status` prints `ended (not queried)` and sends nothing; it never reports `idle` for an ended session.
6. **No secret leaks.** `agentcore logs` for every runtime contains neither a bearer token nor a client
   secret. `git status` shows no `agentcore/.env.local`, no `.fleet/` and an unchanged `agentcore.json`
   (placeholder `discoveryUrl`) after `hydrate_credentials.py --clean`.

## Stop and teardown

```bash
uv run python scripts/fleet.py stop --match "$MATCH" --yes
uv run python scripts/fleet.py stop --match "$MATCH" --hard --yes     # if any session is still busy
(cd infra/decider && make destroy-decider)
aws sagemaker list-endpoints --query 'Endpoints[].EndpointName' --output text
```

7. **Teardown is complete.** `list-endpoints` prints nothing. The Budget and the ECR repository remain.

## Record the result

Write down the outcome (date, token validity found, first-tick latency, any fallback or 401). The run passes
only when criteria 1 to 7 hold.
