# Tasks: AgentCore CTF Fleet (4 runtimes + SageMaker decider)

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~4,000-4,500 total (spikes ~150, S1 ~1,100, S2 ~1,200, S3 ~800, S4 ~1,100) |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR0 (spikes+spec amend) -> PR1 -> PR2 -> PR3 -> PR4 |
| Delivery strategy | ask-on-risk |
| Chain strategy | stacked-to-main |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

Note: each slice alone also exceeds 400 lines. Within a slice, split further by work-unit commits (RED+GREEN pairs). Vendored `app/<rt>/ctf_common/` copies are generated and count toward diff size; mark them generated in PR descriptions.

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 0 | Spikes, quota, spec amendments | PR 0 | Base: tracker/main. Docs + spike scripts only. Rollback: revert. |
| 1 | pytest scaffold, `ctf_common`, `harness_sdk` | PR 1 | Base: PR 0 (or main). Independent of AWS. Rollback: revert; harness_sdk updated in place. |
| 2 | `harness`, `strand_decider`, `decider_loop`, SageMaker client + breaker | PR 2 | Base: PR 1. Harness branch gated by S1. |
| 3 | `infra/decider/` CDK app + adapter | PR 3 | Base: PR 2 (code-independent; may branch from PR 0). Rollback: `cdk destroy DeciderStack`. |
| 4 | agentcore.json, fleet-wiring, scripts, runbook, E2E | PR 4 | Base: PR 3. Rollback: `agentcore remove` + deploy. |

## Phase 0: Spikes and day-0 (PR 0)

Legend: `[x]` done, `[~]` prepared - awaiting user execution (AWS-mutating or GPU step; scripts in `spikes/`, results in `spikes/FINDINGS.md`).

- [x] 0.1 DAY 0: request SageMaker endpoint quota `ml.g4dn.xlarge` (us-east-2). DONE 2026-10-14: request `9cf62dab75704e41a9648bd55ce941ffvCArSmVu` APPROVED; quota `L-B67CFA0C` = 1.0. Gates: 3.x deploy, 0.7.
- [x] 0.2 Spike S5: confirm `agentcore deploy` does not touch `infra/decider/`. Exit: documented result. Confirms A8. Fallback: move decider stack under a manual-only script.
- [x] 0.3 Spike S2: check cp314 aarch64 wheels incl. strands-harness, mcp, boto3 via `uv pip install --python-platform aarch64-manylinux2014 --python-version 3.14`. Exit: PASS -> PYTHON_3_14, FAIL -> PYTHON_3_13 for all 4 (A11). Gates 1.1, 4.1.
- [x] 0.4 Spike S3: identity criteria a-d, synth role inspection, one real M2M token. Exit: Path A or B recorded. Fallback: Path B (`lib/ctf-credentials.ts`, remove `credentials[]`). Gates 4.1-4.4, 1.9 provider name. DONE 2026-10-16: Path A confirmed live; the 4 OAuth providers were created by `agentcore deploy` and real M2M tokens were minted by the runtimes (matches played against the deployed MCP). On a game redeploy the CLI updated the providers' client id/secret/discovery URL in place (verified against SSM).
- [x] 0.5 Spike S1: harness HTTP MCP bearer headers. Exit: PASS / prebuilt MCPClient / descope harness (3 runtimes, reported). Gates 2.2.
- [x] 0.6 Resolve open questions: join_match multi-character per sub; Cognito pool weekly persistence; client-secret JSON key (Path B); wait_for_turn vs CloudFront timeout. Exit: answers in blog notes.
- [x] 0.7 Spike S4: T4 decider benchmark (InService <= 20 min, p95 <= 1.0 s, fp16 vs fp32 within +-0.05 on 20 states). Exit: dtype, read timeout, breaker thresholds, image/AMI pinned. Gates 2.6-2.8, 3.3-3.6. Fallback: fp32; if p95 > 2.5 s, breaker-first LLM path and report. DONE 2026-10-16: image e05acd51 (torch 2.14.1+cu126, fp16) on ml.g4dn.xlarge; /ping 200 ~4 min after create (timeout lowered to 600 s); S4 over 30 real ticks p50 446 ms, p95 646 ms, max 835 ms -> PASS. Design-default read timeout and breaker kept. NOT run: the fp16 vs fp32 agreement check (would need a second fp32 deploy); decisions looked sane and matches played correctly - accepted, revisit only if decision quality is questioned.
- [x] 0.8 Amend specs (design wins): `match-session-lifecycle` and `ctf-agent-runtimes` payload fields -> `action, match_id, team, name, model, options` (drop `character`/`player_name`); status states add `stopping`, `already_running`, `rejected`.
- [x] 0.9 Amend `fleet-operations`/`ctf-agent-runtimes` IAM: SSM read `/agentic-ctf/mcp-url` plus `/ctf-decider/*` (decider runtimes only); `decider-endpoint`: queue limit 8 waiting or 2 s wait (not 3 s); `decider-client`: misconfig opens breaker 300 s, clamp skip < 400 ms.
- [x] 0.10 Sweep specs vs design for other conflicts (`mcp-m2m-auth` 401 handling, lifecycleConfiguration 900/14400); design wins; log each in blog notes.
- [x] 0.11 Append new technical decisions to `../blog-notes/agentcore-ctf-technical-decisions.md`.

## Phase 1: Slice 1 - scaffold, ctf_common, harness_sdk (PR 1)

Sources to port from now live in this repo: `local/agent/refined/` and `local/agent/_client.py` (git subtree). `local/` is NOT modified in this phase.

- [x] 1.1 Root `pyproject.toml`: uv dev group (pytest, pytest-asyncio, httpx, fastapi, boto3 stubs, plus strands-agents/bedrock-agentcore/mcp), Python per S2 (`.python-version` 3.14). Verify `uv run --with pytest pytest` collects.
- [x] 1.2 RED `tests/test_payload.py` (payload scenarios: defaults, unwrap `{"prompt"}`, reject messages/unknown keys/multi-character, regex limits). [x] 1.3 GREEN `shared/ctf_common/payload.py`.
- [x] 1.4 RED `tests/test_runner.py` (fake app/PlayFn: double start, already_running/rejected, status, stop, preflight FAILED, finally complete_async_task). [x] 1.5 GREEN `shared/ctf_common/runner.py`.
- [x] 1.6 RED `tests/test_config.py` (SSM Stubber, missing param named in error, local overrides). [x] 1.7 GREEN `config.py`.
- [x] 1.8 RED `tests/test_auth_mcp.py` (N tool calls = 1 token; reconnect = +1; no secret logging; 401 -> failed). [x] 1.9 GREEN `auth.py`, `mcp.py`.
- [x] 1.10 Characterization tests then `prompts.py`, `hooks.py` extracted from `local/agent/refined/harness_sdk.py` (adds `StopRequestedHook`, `TickCounterHook`).
- [x] 1.11 RED `tests/test_vendor_sync.py` (hash drift), GREEN `scripts/sync_common.py` (+`--check`); commit vendored copies.
- [x] 1.12 Port `app/harness_sdk/` (`sdk_agent.py`, thin `main.py`, pyproject; delete `mcp_client/`, `model/`, template `skills/`) with fake-MCP play-loop test.
- [x] 1.13 Docs: short `shared/ctf_common/README.md`. [x] 1.14 Verify: full pytest green. [x] 1.15 Blog-notes append (path above).

## Phase 2: Slice 2 - remaining ports, decider client (PR 2)

Legend adds `[ ]` for 2.9 only: deferred to a follow-up PR (see apply-progress).

- [x] 2.1 RED `tests/test_decider_client.py` (Stubber + injected clock: each error-map row, clamp, 1 retry, breaker 3/30 s/half-open, misconfig 300 s). [x] 2.2 GREEN `shared/ctf_common/decider.py` (builders, DeciderPort, SageMakerDecider, HttpDecider, CircuitBreaker). Gated by 0.7 for timeouts: design defaults used (2.5 s read ceiling, 3 failures, 30 s).
- [x] 2.3 Port `app/harness/` (`harness_agent.py`); S1 branch `mcp_servers={"ctf-game": {url, headers, continue_on_error: false}}`. Play-loop test with fake MCP plus a real-HTTP mid-match 401 test.
- [x] 2.4 Characterization tests (586 cases, differential vs the local original) then port `app/decider_loop/{loop_logic,loop}.py`; RED/GREEN loop test with fake decider (breaker fallback to LLM, stop per tick, dead connection fails the match).
- [x] 2.5 Port `app/strand_decider/{gate,strand_agent}.py` (fcntl lock removed); gate tests with fake decider plus end-to-end block/proceed/fail-open.
- [x] 2.6 Re-run `sync_common.py`; `--check` green. [x] 2.7 Verify: pytest green, no torch in any pyproject (`tests/test_runtime_deps.py`, CodeZip simulation: 57/42/42/43 MB zipped).
- [x] 2.8 Blog-notes append (path above): D22-D28.
- [x] 2.9 Follow-up (own PR if it exceeds budget): make `local/agent/refined/` consume `shared/ctf_common` (prompts, hooks, payload/option parsing, decider question builders) instead of its own copies, so the original and the deployed ports cannot drift. Keep `cd local && python -m unittest discover -s tests` green and add a guard test that fails if `local/` re-defines a shared symbol. Do not change local CLI behavior. DEFERRED from slice 2: `local/` is a git subtree with `requires-python >=3.10` while `ctf_common` needs 3.11+ (`StrEnum`, `datetime.UTC`); slice 2 added differential tests (ports vs the local originals) that already fail on drift on either side. DESCOPED from this change: tracked in GitHub issue #6 (local agents: CTF_MCP_TOKEN + consume shared/ctf_common). Differential tests keep app/ and local/ from drifting meanwhile.

## Phase 3: Slice 3 - infra/decider (PR 3)

- [x] 3.1 Scaffold `infra/decider/` (package.json, jest config, `bin/decider.ts`).
- [x] 3.2 RED `test/persistent-stack.test.ts` (ECR keep-3 RETAIN, Budget $25 80%/100%, SNS email); GREEN `lib/persistent-stack.ts`.
- [x] 3.3 RED `test/decider-stack.test.ts` (g4dn.xlarge x1, fixed EndpointName only, generated Model/Config names, startup timeout, SSM `/ctf-decider/endpoint-name`, no Export, no Budget); GREEN `lib/decider-stack.ts`.
- [x] 3.4 RED adapter tests (TestClient + MockTransport: passthrough + queue_wait_ms, concurrency 1, queue_full at 8, 2 s wait -> 503, /ping unblocked, 503 until warm); GREEN `adapter/adapter/app.py`, `warmup.py`.
- [x] 3.5 RED/GREEN supervisor tests (restart once, then permanent 503); `supervisor.py`, `__main__.py`.
- [x] 3.6 `adapter/Dockerfile` (digest-pinned, amd64, HF_HUB_OFFLINE) and `scripts/build_image.sh` (CodeBuild, content-hash tag, `--yes` required), `Dockerfile.local` + `scripts/smoke_local.sh`.
- [x] 3.7 Verify: `cd infra/decider && npx jest`, `cdk synth`. Automated part DONE (jest 49, pytest 1202, synth of both stacks, local Docker smoke). Manual part awaiting user (needs 0.1): deploy, InService, ping, teardown. DONE 2026-10-16/17: deployed twice, InService (~5-6 min total), real inference OK, teardown verified (`list-endpoints` empty, endpoint configs/models 0). First attempt failed on unpinned deps - fixed by the hash lock + strict build smoke (see decider-image-deps.md).
- [x] 3.8 Blog-notes append (path above).
- [x] 3.9 Slice 3 judgment-day fixes (13 items): always-synthesized stacks with deploy-blocking error annotations (destroy needs no inputs) and Makefile `deploy-persistent` / `deploy-decider` / `destroy-decider`; adapter drain after upstream timeout, probe skipping while busy, spawn-failure FAILED state, disconnect and NaN handling, threaded staging, exposed knobs; AWS DLC base image (no Docker Hub); IAM metrics and confused-deputy conditions; build script ECR error handling and single model-id source; buildspec push guard; hash parity test. See apply-progress and the design addendum.

## Phase 4: Slice 4 - agentcore wiring, scripts, runbook, E2E (PR 4)

- [x] 4.1 `agentcore/agentcore.json`: 4 runtimes (CodeZip, PYTHON_3_14, lifecycle 900/14400) + 4 outbound OAuth credentials (Path A, `ctf_<runtime>`, placeholder `discoveryUrl` hydrated locally). `agentcore validate` = Valid. Single naming table `scripts/fleet_config.py`; tests `tests/test_fleet_config.py`.
- [x] 4.2 RED `agentcore/cdk/test/fleet-wiring.test.ts` (fake RuntimeEnvLike: env vars, per-runtime IAM, decider-only SSM/InvokeEndpoint, throws on missing/unknown, no agents/* secrets); GREEN `lib/fleet-wiring.ts`, 2-line call in `cdk-stack.ts`. Run `cd agentcore/cdk && npx jest`.
- [x] 4.3 RED/GREEN `tests/test_hydrate.py` + `scripts/hydrate_credentials.py` (0600, `--clean`, `--check`, discoveryUrl drift fails, placeholder restored by `--clean`).
- [x] 4.4 RED/GREEN `tests/test_fleet_cli.py` + `scripts/fleet.py` (start/status/stop[--hard]; limits 3/type, 2 teams, rejected before invoke; session id >= 33 chars).
- [x] 4.5 `docs/workshop-runbook.md`: deploy order, teardown, Budget, cost warning (+ `docs/e2e-acceptance.md`).
- [x] 4.6 (PREPARED in `docs/e2e-acceptance.md`; awaits user, needs a real account) E2E acceptance (manual): deploy per runbook, play a real match with all 4 types, verify tokens = MCP connections, teardown, `list-endpoints` empty. DONE 2026-10-16/17: the user played full matches from the lobby with all 4 agent types, with the real decider and with the endpoint down (LLM fallback confirmed by the runtime log line). Tokens = MCP connections verified by design and tests (one fetch per connection); not measured live (CloudTrail data events are off). Teardown verified.
- [x] 4.7 Blog-notes append (path above).
- [x] 4.8 Slice 4 review fixes (judgment-day round 3), RED then GREEN: launcher never revives ended sessions (`last_status`/`last_seen`/`started_at`, `ended (not queried)`, `--wake`, `StopRuntimeSession` for ended sessions); state lock, cap ignores stopped sessions, `--match` validation, corrupt-state errors; safe deploy wrapper `scripts/deploy_fleet.sh` + `Makefile` (`make deploy YES=1`) with `--clean` in an EXIT trap; hydrate writes `agentcore.json` first and keeps its mode; decider SSM grant narrowed to `/ctf-decider/endpoint-name`; `(spec.runtimes?.length ?? 0) > 0` guard; env-name contract test (TS vs Python); real `AgentCoreStack` jest test (about 10 s, packager stubbed); spec, design and runbook amendments; `PUBLIC_FILES` built by glob.

> Review fixes (judgment-day round 1) applied to Phase 1 code: see `apply-progress.md` "Review fixes". No task status changed.

> Slice 2 review fixes (judgment-day round 2): probe ownership + breaker epoch, malformed-answer fail-open in the strand gate, 0.5 s read-timeout buckets, connect timeout clamp, JSON-safe state, lazy SSM factory, `require_match_id`. See `apply-progress.md` "Slice 2 review fixes". No task status changed.
