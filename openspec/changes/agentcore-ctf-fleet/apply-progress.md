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
