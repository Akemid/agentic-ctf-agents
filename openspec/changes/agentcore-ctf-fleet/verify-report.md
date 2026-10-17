# Verify Report: agentcore-ctf-fleet

Date: 2026-10-16. Mode: hybrid, Strict TDD. Verdict: **PASS WITH WARNINGS** (0 CRITICAL, 6 WARNING, 3 SUGGESTION).

## Test execution (fresh run)
| Suite | Result |
|---|---|
| `uv run --with pytest pytest -q` | 1847 passed, 0 failed (117 deprecation warnings, MCP `streamable_http_client`) |
| `agentcore/cdk` jest | 49 passed, 3 suites |
| `infra/decider` jest | 50 passed, 4 suites |
| local unittest (py3.10) | 215 passed (OK) |
| `ruff check .` | clean |
| `sync_common.py --check` | vendored copies in sync |

Total: 2161 automated tests, all green.

## Tasks
45 of 45 task checkboxes are `[x]` (phases 0 to 4, incl. 3.9 and 4.8). The only `[ ]` hit in tasks.md is the legend text. Task 2.9 is marked `[x]` but its text says DESCOPED to GitHub issue #6 (see W3).

## Strict TDD evidence
apply-progress records RED then GREEN tables per slice, mutation checks (env name contract), and differential tests (586 cases) between ports and `local/`. Test files exist for every implemented module (tests/, infra/decider/test, agentcore/cdk/test, local/tests). No test-less requirement was found among the code-level capabilities.

## Spec coverage matrix
T = automated test, L = live evidence, D = design/test only (not live).
| Capability | Requirements | Coverage |
|---|---|---|
| match-session-lifecycle | payload validation (valid, missing, non-string, history rejected, unknown action/key, prompt-wrapped), one invocation = one character, async start/status/stop, double-start guard, preflight, restart | T: test_payload, test_runner, test_adapter_*, test_entrypoints; L: lobby matches with 4 agent types |
| ctf-agent-runtimes | four runtimes, lifecycle 900/14400, no torch, vendored code, behavior parity (gate, loop fallback, fresh history), SSM config, distinct identity | T: test_fleet_config, test_runtime_deps (zip 57/42/42/43 MB), test_vendor_sync, test_gate, test_loop_logic, test_decider_loop, test_config, test_join*; L: 4 runtimes READY, full matches with all 4 types |
| mcp-m2m-auth | tokens = connections, per-runtime provider, no mid-match reconnect, 401 surfaces, secrets not logged/committed | T: test_auth_mcp, test_harness_agent (real-HTTP mid-match 401), test_hydrate, gitignore guard in test_fleet_config; L: 4 OAuth providers, in-place update after redeploy; D: tokens = connections verified by design and tests, not live |
| decider-client | invocation, clamp, no time left, bounded retry, validation error, adapter busy, breaker open/misconfig/recover/probe-fail, LLM fallback, log content | T: test_decider_client (Stubber, injected clock), test_decider_loop; L: "playing on the LLM fallback" log (decider_loop, 2026-10-17 02:18 UTC) with endpoint down, and matches with the real decider |
| decider-endpoint | adapter contract, serialization, queue full/wait, cancelled work, ping during inference/warm-up, separate DeciderStack, SSM publication, no Budget in stack, independent teardown | T: adapter tests (test_adapter_app/e2e/supervisor/build), decider-stack, persistent-stack, app, paths tests; L: image e05acd51 smoke, InService ~5-6 min, /ping 200, S4 p50 446 / p95 646 / max 835 ms (gate 1000 ms), teardown confirmed empty |
| fleet-operations | SSM-only cross-stack, env injection, secret hydration, vendor sync, launcher (limits, partial failure), status/stop, runbook, rollback IAM | T: fleet-wiring, fleet-stack, test_env_contract, test_hydrate, test_vendor_sync, test_fleet_cli, test_deploy_wrapper; L: SSM `/ctfagents/runtimes/*/arn` published, deploy and teardown run; runbook in docs/workshop-runbook.md |

No scenario was found with neither test nor live evidence.

## Live evidence summary
4 runtimes READY (AgentCore-ctfagents-default), 4 OAuth providers, SSM ARNs published; providers updated in place after redeploy (client id and discovery URL match SSM); decider image e05acd51 strict build smoke passed in CodeBuild; endpoint InService in about 5-6 min, /ping 200; S4 over 30 real ticks p50 446 / p95 646 / max 835 ms PASS; user played full matches from the lobby with 4 agent types, with real decider and endpoint down (LLM fallback confirmed); teardown: list-endpoints empty, endpoint configs and models 0.

## CRITICAL
None.

## WARNING
- W1. Recorded deviation: fp16 vs fp32 agreement (S4, +-0.05 on 20 states) was NOT run. Accepted by the user; record in the archive report.
- W2. tokens = MCP connections is verified by design and unit tests only, not measured live (task 4.6 text claims verification).
- W3. Task 2.9 is checked `[x]` although it is DESCOPED to GitHub issue #6. Annotate as "descoped, not done" at archive so the checkbox does not read as delivered. Consequence: `local/agent/refined` still has its own copies; differential tests guard drift only.
- W4. Spec text stale against later amendments (amend at archive, not code defects): timeout values (1200 s vs the shipped 600 s per the later amendments), slot/agent_index identity, decider now optional (decider-optional.md), contract v2 (contract-v2.md: get_map, match_over.reason, no static keys), lobby wake (lobby-wake.md), decider image deps (decider-image-deps.md). The repo specs under `specs/` do not mention these; the amended behavior lives in addenda only. Note I did not find the 1200/600 strings in the delta specs, so that mismatch may live only in the design or in the main spec (id 2919).
- W5. Spec amendments to already recorded: "no Export" scenario (alpha L3 exports its outputs), missing-SSM failure moves to runtime preflight, and credential isolation between runtimes is by convention only (L3 grants GetResourceOauth2Token on token-vault/* to every runtime role).
- W6. Uncommitted/unbranched state: work is not committed (branch chore/sdd-close); the project is not a git repo at the cwd as seen by tooling. Ensure everything is committed before archive.

## SUGGESTION
- S-1. Silence or pin the MCP `streamable_http_client` deprecation warnings (117 warnings in pytest).
- S-2. Run the fp16 vs fp32 agreement check once before any production use of fp16.
- S-3. Add a live counter (token requests vs connections) to the next workshop run to close W2.

## Verdict
PASS WITH WARNINGS. Ready for sdd-archive after amending the stale spec text (W4, W5) and annotating task 2.9 (W3).
