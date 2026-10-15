# Apply Progress: agentcore-ctf-fleet

## Batch PR0 (Phase 0) - 2026-10-14
Mode: Strict TDD. Delivery: chained PRs, stacked-to-main. Branch: `feat/agentcore-ctf-fleet-pr0` (uncommitted). Pre-existing `agentcore/aws-targets.json` change untouched.

### Task status
- [~] 0.1 quota: prepared (`spikes/s0_request_quota.sh`); quota L-B67CFA0C is 0.0 in us-east-2, nothing pending. Awaiting user.
- [x] 0.2 S5: A8 confirmed (CLI deploys an explicit stack pattern of the `agentcore/cdk` app only).
- [x] 0.3 S2: PASS, `PYTHON_3_14` for all 4 (CLI falls back to manylinux_2_28; pin `strands-harness>=0.3.0`).
- [~] 0.4 S3: criteria a-d verified from CLI/L3 source (d corrected: no project prefix); Path A selected; real token prepared (`spikes/s3_identity_token.py`), blocked on AgenticCtfStack not deployed. Awaiting user.
- [x] 0.5 S1: PASS, `mcp_servers={"x":{"url","headers"}}` executed against a local server.
- [x] 0.6 open questions answered from agentic-ctf source.
- [~] 0.7 S4: prepared (`spikes/s4_t4_host.sh`, `s4_bench.py`, `s4_lib.py`); finding: no dtype flag in strands-decider. Awaiting user (T4 host).
- [x] 0.8, 0.9, 0.10 specs amended (6 capability specs touched: match-session-lifecycle, ctf-agent-runtimes, fleet-operations, decider-endpoint, decider-client, mcp-m2m-auth).
- [x] 0.11 blog notes appended (D10-D14 + answers + amendments).

### TDD Cycle Evidence
| Task | Test file | RED | GREEN | REFACTOR |
|------|-----------|-----|-------|----------|
| 0.7 helpers (`s4_lib`: parse_states, build_request, percentile, max_prob_delta) | `spikes/tests/test_s4_lib.py` | ModuleNotFoundError (collection error) before `s4_lib.py` existed | 7 passed | none needed |
| 0.5 S1 spike | executable script `spikes/s1_harness_headers.py` (PASS/FAIL self-check) | n/a (investigative) | PASS | - |
| 0.1-0.4, 0.6, 0.8-0.11 | no reusable code (docs, evidence, scripts that call AWS) | n/a | n/a | n/a |

Test runs: `uv run --with pytest pytest spikes/tests` 7 passed; `cd agentcore/cdk && npx jest` 1 passed.

### Deviations from design
- Design wins in specs. Design itself corrected via addendum: token validity found 1 h in PR0 (not 4 h); 4 h requested (D6); re-verify before E2E (task 4.5), no dtype flag, no env var project prefix, L3 already grants identity IAM, hyphen-only agentic-ctf agent names.

### Files
Created: `spikes/{README.md,FINDINGS.md,s0_request_quota.sh,s1_harness_headers.py,s3_identity_token.py,s4_bench.py,s4_lib.py,s4_t4_host.sh,tests/test_s4_lib.py}`, this file.
Modified: `openspec/changes/agentcore-ctf-fleet/{tasks.md,design.md,specs/*/spec.md (6)}`, `../blog-notes/agentcore-ctf-technical-decisions.md`.

### User actions pending (see spikes/README.md)
1. `bash spikes/s0_request_quota.sh` (0.1)
2. S3 steps in `spikes/README.md` after AgenticCtfStack with agent clients is deployed (0.4)
3. `bash spikes/s4_t4_host.sh ./decider-194227.log` on a T4 host (0.7)

### Remaining
Phases 1-4 (PR1-PR4). Gates: 1.1/4.1 unblocked by S2; 2.2 unblocked by S1; 4.1-4.4 and 1.9 pending real S3 token (Path A assumed); 2.6-2.8 and 3.3-3.6 pending S4 numbers (design defaults usable).


---

## Batch PR1 (Phase 1) - 2026-10-15
Mode: Strict TDD. Delivery: chained PRs, stacked-to-main (PR1 of PR0..PR4). Branch: `feat/agentcore-ctf-fleet-slice1` (uncommitted; orchestrator reviews first).
Note: PR0 task 0.1 is now `[x]` (quota approved, `L-B67CFA0C` = 1.0). Sources now live in `local/` (git subtree, not modified).

### Task status (cumulative)
- Phase 0: unchanged from the PR0 batch above, except 0.1 now `[x]`. 0.4 and 0.7 remain `[~]` (user actions).
- [x] 1.1 root `pyproject.toml` (+ `.python-version` 3.14, `uv.lock`), pytest collects.
- [x] 1.2/1.3 `payload.py`. [x] 1.4/1.5 `runner.py`. [x] 1.6/1.7 `config.py`. [x] 1.8/1.9 `auth.py`, `mcp.py`.
- [x] 1.10 `prompts.py`, `hooks.py` (characterization first). [x] 1.11 `scripts/sync_common.py` + drift test + vendored copy.
- [x] 1.12 `app/harness_sdk` ported (template `mcp_client/`, `model/`, `skills/` deleted). [x] 1.13 README. [x] 1.14 full pytest green. [x] 1.15 blog notes (D15-D21).
- Added task 2.9 (follow-up: make `local/` consume `shared/ctf_common`).

### TDD Cycle Evidence
| Task | Test file | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-----|-------|-------------|----------|
| 1.1 scaffold | `spikes/tests` (collect) | no root config | 12 collected, 12 passed on 3.14 | skipped: purely structural (config file) | n/a |
| 1.2/1.3 payload | `tests/test_payload.py` (63) | ModuleNotFoundError `ctf_common` | 63 passed | defaults, unwrap, rejects, regex/option limits, per-runtime options | ruff format, `sanitize_name` extracted |
| 1.4/1.5 runner | `tests/test_runner.py` (29) | ModuleNotFoundError `ctf_common.auth` | 29 passed | same/different pair, STOPPING start, failure paths, ContextVar copy | moved `PrefetchedTokenProvider` to `auth.py` |
| 1.6/1.7 config | `tests/test_config.py` (18) | ImportError `ConfigError` | 18 passed | not found / denied / blank / non-http / local overrides | none |
| 1.8/1.9 auth+mcp | `tests/test_auth_mcp.py` (29) | ImportError `ctf_common.mcp` | 29 passed after fixing 1 test (tool names carry the `ctf-game_` prefix) | 200 calls = 1 token, reconnect = 2, 401 in cause chain / ExceptionGroup, no secret in logs | none |
| 1.10 prompts/hooks | `tests/test_prompts_hooks.py` (25) | ImportError `ctf_common.hooks` | 25 passed (sha256 of SYSTEM_PROMPT equals the original) | strip walls, window trim, stop hook, tick hook | none |
| 1.11 vendor sync | `tests/test_vendor_sync.py` (19) | ModuleNotFoundError `sync_common` | 18 passed, then 19/19 after running the sync (real-repo drift guard was RED until the copies existed) | missing/extra/hand-edited/stale/source change | none |
| 1.12 harness_sdk | `tests/test_sdk_agent.py` (18) | ModuleNotFoundError `sdk_agent` | 18 passed (1 test fixed: `exa` matched "example") | full match, team, window, stop after 2 ticks of a 500-turn match, 401, runner integration, entrypoint | none |

Evidence of real GREEN: the play-loop tests run a real Strands `Agent` and `MCPClient` against an in-memory FastMCP game and a scripted model.

### Test results
- `uv run --with pytest pytest`: 213 passed (201 new in `tests/` + 12 spike tests from PR0), 3 warnings (upstream pydantic deprecation, MCPClient.stop coroutine on failed start).
- `cd agentcore/cdk && npx jest`: 1 passed (untouched).
- `agentcore validate`: Valid.
- Packaging check (read-only): `uv pip install -r app/harness_sdk/pyproject.toml --target <tmp> --python-version 3.14 --python-platform aarch64-manylinux_2_28 --only-binary :all:` OK, 110 MB, no torch.
- `ruff check` and `ruff format --check` clean for `shared tests scripts app` (vendored copies excluded).

### Deviations from design
- Root dev group also includes `strands-agents`, `bedrock-agentcore`, `mcp` (the shared code imports them, tests run against the real APIs).
- `mcp.py` uses `streamable_http_client` + an `httpx.AsyncClient` (the design's `streamablehttp_client` is deprecated in mcp 1.30). Design allowed this.
- `StopRequestedHook` ends the loop via `AfterToolsEvent.end_turn` (verified on strands 1.61); runtime pins `strands-agents>=1.61` (design said 1.59).
- `PrefetchedTokenProvider(token, then)` takes a fallback provider so a reconnect still costs exactly one fetch.
- Invalid payloads return `{"status": "rejected", "error": ...}` (the status set in the spec is closed; no separate `error` status).
- Deleted the template `app/harness_sdk/skills/fetcher.py` together with `mcp_client/` and `model/` (unused template code).
- `uv.lock` added at the repo root and regenerated for `app/harness_sdk` (generated).

### Files
Created: `pyproject.toml`, `.python-version`, `uv.lock`, `shared/ctf_common/{__init__,payload,runner,config,auth,mcp,prompts,hooks}.py`, `shared/ctf_common/README.md`, `scripts/sync_common.py`, `tests/{test_payload,test_runner,test_config,test_auth_mcp,test_prompts_hooks,test_vendor_sync,test_sdk_agent}.py`, `tests/{fake_mcp,fake_model}.py`, `app/harness_sdk/sdk_agent.py`, `app/harness_sdk/ctf_common/*` (GENERATED).
Modified: `app/harness_sdk/{main.py,pyproject.toml,README.md,uv.lock (generated)}`, `.gitignore`, `openspec/config.yaml`, `openspec/changes/agentcore-ctf-fleet/tasks.md`, this file, `../blog-notes/agentcore-ctf-technical-decisions.md` (outside the repo).
Deleted: `app/harness_sdk/{mcp_client,model,skills}/`.

### Remaining
Phases 2-4 (PR2-PR4), plus user actions 0.4 / 0.7.

## Review fixes (judgment-day round 1)

- `runner.py`: preflight now runs WITHOUT the lock (internal STARTING state, reported as `running`; no new visible status). `stop` during preflight ends `stopped` with no task registered. Thread creation failure after `add_async_task` calls `complete_async_task` and sets `failed`. `runtime` is used in log context; docstring states restart after a terminal state is allowed.
- Mid-match 401 NEEDED CODE: Strands turns the failure into an error tool result, so the loop kept going. New `ConnectionLostHook` (`hooks.py`) ends the loop after the tool batch; `sdk_agent.play` wires it, and the 401 surfaces via `raise_on_auth_failure` (fallback: `RuntimeError`). Spec scenario added to mcp-m2m-auth.
- `mcp.py`: private `mcp.shared._httpx_utils` import replaced by `default_http_client` (same 30 s / 300 s read timeouts; redirects left off like the SDK helper, deliberate).
- `test_prompts_hooks.py`: prompts compared against the refined original (importlib) besides the sha256.
- `pyproject.toml`: `[tool.pyright]` (extraPaths mirror pytest pythonpath). READMEs document stop latency (~25 s `wait_for_turn` cap; `StopRuntimeSession` is the hard stop).
- Results: 228 passed; ruff check/format clean; sync_common --check OK; cdk jest 1 passed.


---

## Batch PR2 (Phase 2) - 2026-10-15
Mode: Strict TDD. Delivery: chained PRs, stacked-to-main (PR2 of PR0..PR4). Branch: `feat/agentcore-ctf-fleet-slice2` (uncommitted; orchestrator reviews first). `local/`, `agentcore/agentcore.json` and `agentcore/aws-targets.json` untouched.

### Task status (cumulative)
- Phases 0 and 1: unchanged from the batches above (0.4 and 0.7 remain `[~]`, user actions).
- [x] 2.1/2.2 `shared/ctf_common/decider.py`. [x] 2.3 `app/harness`. [x] 2.4 `app/decider_loop`. [x] 2.5 `app/strand_decider`. [x] 2.6 sync + check. [x] 2.7 verify. [x] 2.8 blog notes (D22-D28).
- [ ] 2.9 deferred (reason in tasks.md). Phases 3-4 untouched.

### TDD Cycle Evidence
| Task | Test file | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-----|-------|-------------|----------|
| 2.1/2.2 decider client | `tests/test_decider_client.py` (59) | ModuleNotFoundError `ctf_common.decider` | 59 passed | every error-map row, clamp x4, skip (+breaker untouched), retry x4, breaker transitions, probe release, HTTP transport, logging leak | ruff `UP041` |
| wiring (shared) | `tests/test_wiring.py` (10) | ModuleNotFoundError `ctf_common.wiring` | 10 passed | SSM vs local overrides, decider param missing, token from env | `harness_sdk` moved onto it (its 19 tests were the safety net) |
| `is_connection_lost`, `prefix_tools` | `tests/test_prompts_hooks.py`, `tests/test_auth_mcp.py` | 9 failed | 71 passed | marker/ordinary error/success/malformed | `ConnectionLostHook` uses the pure function |
| `strands_match` | `tests/test_sdk_agent.py` (19, existing) | approval tests: green before and after | 19 passed | n/a | extracted from `sdk_agent.play` |
| 2.3 harness | `tests/test_harness_agent.py` (17) | ModuleNotFoundError `harness_agent` | 17 passed | 3 models, real HTTP 401, dead server, stop, hooks, state dirs; mutation: removing `agent.cleanup()` fails the 401 test | dropped `type` key (not a `MCPServerConfig` field) |
| 2.5 gate + agent | `tests/test_gate.py` (24), `tests/test_strand_agent.py` (9) | ModuleNotFoundError `gate` / `strand_agent` | 33 passed | guide/proceed/cap/reset/fail-open/no permanent down, e2e block+continue, parity with local `QUESTIONS`/`_render` | none |
| 2.4 loop_logic | `tests/test_loop_logic.py` (586) | ModuleNotFoundError `loop_logic` (port moved away until the test existed) | 586 passed | corpus of 8 turns x 5 plans, grids for combine_answers/choose_path/continues_path; mutation `>=` to `>` in `combine_answers` fails | none |
| 2.4 loop | `tests/test_decider_loop.py` (24) | ModuleNotFoundError `loop` | 24 passed after strengthening the 401 test with a call spy (a first version passed even with the connection-lost check removed; verified by mutation) | decider path, warm-up, join red/blue/fixed/fail, LLM fallback, budget, wait, no permanent down, stop, 401 | `functools.partial` instead of a loop-variable lambda |
| 2.7 entrypoints/deps | `tests/test_entrypoints.py` (33), `tests/test_runtime_deps.py` (18) | config/characterization style: pass on first run (modules existed) | 51 passed | per-runtime options accepted/rejected | none |

### Test results
- `uv run --with pytest pytest`: 1018 passed (790 new in this slice), 88 warnings (upstream pydantic deprecation, `MCPClient.stop` coroutine on a failed start, strands context-window notice).
- `cd agentcore/cdk && npx jest`: 1 passed (untouched).
- `ruff check .` and `ruff format --check` clean (vendored copies excluded); `scripts/sync_common.py --check` OK.
- CodeZip simulation (`uv pip install -r app/<rt>/pyproject.toml --python-version 3.14 --python-platform aarch64-manylinux_2_28 --only-binary :all:`): harness 141 MB unpacked / 57 MB zipped; harness_sdk 110 / 42; strand_decider 110 / 42; decider_loop 110 / 43. No torch, nvidia or triton in any of them.

### Deviations from design
- `harness`: `mcp_servers` entry has no `type` key (not a `MCPServerConfig` field; strands warned and ignored it; `url` implies streamable HTTP) and sets `continue_on_error: false`: `create_harness` otherwise skips a server that fails to start and the agent would play without game tools.
- Added shared modules not in the layout: `wiring.py` (runner wiring) and `strands_match.py` (Strands match loop used by `harness_sdk` and `strand_decider`), so the four runtimes do not carry four copies. `harness_sdk/sdk_agent.py` refactored onto them (tests unchanged and green).
- `decider_loop` is split into `loop_logic.py` (pure, verbatim port) and `loop.py` (play, LLM fallback guards); the design names only `loop.py`.
- `open_mcp_client(prefix_tools=False)` and `hooks.is_connection_lost` added to `ctf_common` (the loop calls tools by raw name and checks results itself).
- botocore cannot set a per-call read timeout: the SageMaker transport keeps a pool of clients keyed by read timeout (rounded down to 100 ms) so the tick clamp is real, not just a ceiling.
- `decider_loop` still sends the warm-up question; a failed warm-up is logged and the breaker handles availability (no permanent down).
- `tests/fake_mcp.py` gained `wait`/`tag`/`grab_flag` tools, `state`/`deadline_remaining_ms`/`reject_teams` options and the shared 401 handler; `tests/fake_model.py` gained `SequencePlayer`.
- 2.9 not done (reason in tasks.md).

### Files
Created: `shared/ctf_common/{decider,wiring,strands_match}.py`, `app/harness/{harness_agent,main}.py`, `app/strand_decider/{gate,strand_agent,main}.py`, `app/decider_loop/{loop_logic,loop,main}.py`, `app/{harness,strand_decider,decider_loop}/{pyproject.toml,README.md}`, `app/{harness,strand_decider,decider_loop}/ctf_common/*` (GENERATED), `tests/{test_decider_client,test_wiring,test_harness_agent,test_gate,test_strand_agent,test_loop_logic,test_decider_loop,test_entrypoints,test_runtime_deps}.py`.
Modified: `shared/ctf_common/{hooks,mcp}.py` and README, `app/harness_sdk/sdk_agent.py`, `app/harness_sdk/ctf_common/*` (GENERATED), `pyproject.toml` (strands-harness dev dep, pythonpath, isort), `uv.lock` (GENERATED), `tests/{fake_mcp,fake_model,test_auth_mcp,test_prompts_hooks,test_sdk_agent}.py`, `openspec/changes/agentcore-ctf-fleet/{tasks,apply-progress}.md`, `../blog-notes/agentcore-ctf-technical-decisions.md` (outside the repo, D22-D28).

### Remaining
Phases 3-4 (PR3-PR4), 2.9 follow-up, and user actions 0.4 / 0.7. Slice 4 must add the three runtimes to `agentcore.json` (not done here by design).

## Slice 2 review fixes (judgment-day round 2)

All behavior fixes were test-first (RED then GREEN). Supersedes the "rounded down to 100 ms" deviation above: buckets are now 0.5 s.

| Fix | Where | Test |
| --- | --- | --- |
| Only the probe owner releases it (`CircuitBreaker.acquire()` -> `Admission.owns_probe`) | `shared/ctf_common/decider.py` | `TestProbeOwnership` (4 tests; interleaved A/B on a fake clock with `threading.Event`) |
| Breaker epoch: stale success/failure ignored | `decider.py` | `TestBreakerEpoch` (3) |
| Malformed answers fail open, `on_error="proceed"` | `app/strand_decider/gate.py` | `TestMalformedAnswersFailOpen` (13) |
| Connect timeout `min(1.0, read)` | `decider.py` | `TestConnectTimeout` |
| 0.5 s read buckets, pool <= 6 | `decider.py` | `TestTimeoutBuckets` (9) |
| Unserializable state -> `DeciderRejected` | `decider.py` | `TestUnserializableState` |
| `last` unbound: explicit guard instead of assert | `decider.py` | n/a (pyright did not report it on this version) |
| `.venv`/`__pycache__` skipped in rglob | `tests/test_runtime_deps.py` | n/a (test infra) |
| SSM factory resolved at call time | `shared/ctf_common/wiring.py` | `test_the_default_ssm_factory_is_resolved_at_call_time` |
| `require_match_id` | `payload.py`, `strands_match.py`, `harness_agent.py`, `loop.py` | `TestRequireMatchId` (2) |

Results: pytest 1052 passed; ruff check/format clean; `sync_common --check` OK; jest 1 passed; pyright source errors 14 -> 10 (the rest are pre-existing: `name` property override x2 handlers, loop_logic Optional x5, auth.py:74). `decider_loop` deadline split intentionally untouched (TODO in design.md addendum).
