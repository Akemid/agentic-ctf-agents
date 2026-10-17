# Archive Report: agentcore-ctf-fleet

Date: 2026-10-16. Mode: hybrid (openspec + engram). Verdict carried from verify: PASS WITH WARNINGS (0 CRITICAL).

## Summary

Four AgentCore CodeZip runtimes (`harness`, `harness_sdk`, `strand_decider`, `decider_loop`) play CTF matches against
the deployed agentic-ctf MCP server. The two LLM-with-decider runtimes call a SageMaker decider endpoint
(`ctf-decider`, ml.g4dn.xlarge) through a serializing FastAPI adapter, with a circuit breaker and LLM fallback. The
fleet is deployed and torn down through a separate CDK app (`infra/decider/`), SSM-only cross-stack configuration, and
per-runtime OAuth M2M credentials. Live evidence: 4 runtimes READY, full matches with all 4 agent types, LLM fallback
with the endpoint down, teardown verified.

## Source of truth

- Main specs merged to `openspec/specs/`: `decider-client`, `decider-endpoint`, `match-session-lifecycle`,
  `ctf-agent-runtimes`, `mcp-m2m-auth`, `fleet-operations` (6 capabilities, 33 requirements, 76 scenarios in the
  verify matrix).
- Amendments merged at archive (verify W4/W5), each traced to its note:
  - `design.md` addenda, `decider-image-deps.md`: startup health-check timeout 600 s (not 1200 s); adapter startup 550 s.
  - `match-session-lifecycle`: `slot` and `agent_index` are equivalent seat keys; explicit team required; disagreeing keys rejected; synchronous start up to the seat; `rejected` with `join_match failed: <reason>`; join ownership per runtime (`lobby-wake.md`).
  - `decider-client`, `decider-endpoint`: decider optional; `/ctf-decider/endpoint-name` owned by `CtfPersistentStack` (`decider-optional.md`).
  - `ctf-agent-runtimes`: game contract v2 (`contract-v2.md`); preflight-time failure for missing SSM values.
  - `mcp-m2m-auth`: credential isolation is by convention only (L3 grants `token-vault/*`).
  - `fleet-operations`: "no exports" means none authored here (the alpha L3 emits its own); runtime ARN publication to `/ctfagents/runtimes/<runtime>/arn` (`lobby-wake.md`); safe deploy wrapper.
  - `decider-endpoint`: hash-locked adapter image with build-time smoke check (`decider-image-deps.md`).
- Delta markers (`ADDED Requirements`) removed; requirements carried as the main spec.

## Task completion gate

`openspec/changes/agentcore-ctf-fleet/tasks.md`: 45 of 45 checkboxes `[x]`. No implementation task is unchecked.
Task 2.9 is `[x]` but DESCOPED to GitHub issue #6 (annotated in the task line). It was not delivered in this change.

## Engram observations (traceability)

| Artifact | Topic key | Observation ID |
|---|---|---|
| Proposal | `sdd/agentcore-ctf-fleet/proposal` | #2918 |
| Spec (delta index) | `sdd/agentcore-ctf-fleet/spec` | #2919 |
| Design | `sdd/agentcore-ctf-fleet/design` | #2920 |
| Tasks | `sdd/agentcore-ctf-fleet/tasks` | #2921 (older copy, see Risks) |
| Verify report | `sdd/agentcore-ctf-fleet/verify-report` | #3022 |

## Delivered PRs

#1, #2, #3, #4, #5, #7, #9, #10, #11, #12, #13 (stacked-to-main chain; PR #6 is the descoped follow-up issue, not a PR).

## Deviations (accepted, recorded)

1. **fp16 vs fp32 agreement not run.** The S4 check (±0.05 on 20 states) was skipped by user decision. S4 latency was run and passed: p50 446 ms, p95 646 ms, max 835 ms over 30 real ticks (gate 1000 ms). Revisit before any production use of fp16 (verify S-2).
2. **Tokens = MCP connections verified by design and tests, not measured live.** CloudTrail data events were off. Live proof is a follow-up (verify S-3).
3. **Task 2.9 descoped to issue #6.** The `local/agent/refined/` copies still exist. Differential tests guard drift only. `local/` requires Python 3.10 while `ctf_common` needs 3.11+.

## Follow-ups

- GitHub issue #6: local agents consume `shared/ctf_common` (task 2.9).
- CloudWatch log retention (orchestrator follow-up; no source note in this change sets or documents a retention period).
- MCP deprecation warnings: 117 `streamable_http_client` deprecation warnings in pytest (verify S-1).
- Also open from verify: fp16 vs fp32 check (S-2); live token counter (S-3).

## Final test counts (fresh run, verify 2026-10-16)

- pytest: 1847 passed, 0 failed.
- `agentcore/cdk` jest: 49 passed (3 suites). `infra/decider` jest: 50 passed (4 suites).
- Local unittest (py3.10): 215 passed.
- Total automated: 2161. `ruff check .` clean. `sync_common.py --check` clean.

## Archive steps

- [x] Main specs written to `openspec/specs/{decider-client,decider-endpoint,match-session-lifecycle,ctf-agent-runtimes,mcp-m2m-auth,fleet-operations}/spec.md`.
- [x] Task 2.9 annotated.
- [x] This report written.
- [ ] Change folder move: NOT executed by the archive agent (no shell tool available). Required command, run by the orchestrator:
  `git mv openspec/changes/agentcore-ctf-fleet openspec/changes/archive/2026-10-17-agentcore-ctf-fleet`
  (the date follows the orchestrator instruction; today is 2026-10-16, see Risks).

## Risks and open items

- Change folder not yet moved (see above). Until it is, `openspec/changes/agentcore-ctf-fleet/` is still active.
- Archive date: the orchestrator asked for `2026-10-17`; the session date is `2026-10-16`. Confirm the intended date before the move.
- Engram `sdd/agentcore-ctf-fleet/tasks` (#2921) is an older copy: it shows `[ ] 2.9` and `[~]` items for 0.4, 0.7, 3.7, 4.6. The file `tasks.md` is the authoritative final state (45/45). The engram copy should be refreshed by a later save, not by this archive.
- Engram `sdd/agentcore-ctf-fleet/design` (#2920) carries a pending conflict candidate (`obs-255e75d073d78c66`). The file `design.md` was not edited. Resolution is outside this archive.
- Name default for seat starts: the notes say the human-facing name is `<runtime-with-dashes>-<team>-<agent_index+1>` but do not state whether a supplied `name` overrides it. The spec states the notes' wording only.
- Git state: the archive agent had no shell, so `git status --short` could not be run. Uncommitted changes are expected per verify W6: the six new `openspec/specs/*/spec.md` files, the edit to `tasks.md`, this `archive-report.md`, and the pending folder move. The orchestrator commits.
- Live evidence limits: the lobby wake path WAS verified live (2026-10-16/17: the user woke all 4 agent types from the web lobby against the deployed server, with and without the decider endpoint); the Strands `join_match` cancel and the `/health` behavior during decider compute remain unverified against the real server (`design.md` slice 3 notes).
- The base image is ECR Public (`python:3.11.17-slim-bookworm`), not the DLC; torch is cu126. Not verified on a GPU (`decider-image-deps.md`).
