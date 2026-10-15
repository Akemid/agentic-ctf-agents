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
