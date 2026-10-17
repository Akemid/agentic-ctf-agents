# Proposal: AgentCore CTF Fleet (4 runtimes + SageMaker decider)

## Intent

The four CTF player agents run only as local scripts, with hand-copied 8 h tokens and a decider on a laptop. Workshops need a repeatable cloud fleet: deploy before, play a match, tear down after. Decisions D1–D6 (`blog-notes/agentcore-ctf-technical-decisions.md`) are fixed and not re-litigated here.

## Scope

### In Scope (delivery slices, in order)
1. Python test scaffold (pytest) + `shared/ctf_common` (prompt, client, auth seams) + `harness_sdk` port.
2. `harness`, `strand_decider`, `decider_loop` ports; SageMaker decider client with circuit breaker.
3. `infra/decider/`: separate CDK app (DeciderStack, `ml.g4dn.xlarge`) + serializing adapter container.
4. `agentcore.json` runtimes + 4 OAuth credentials, `agentcore/cdk/lib/fleet-wiring.ts` (IAM, env), scripts (secret hydration, vendor sync, fleet launcher, status/stop), workshop runbook (deploy order, teardown).

### Out of Scope
- agentic-ctf repo changes (Cognito clients, secrets, SSM params): owned by another agent.
- Streaming responses, VPC mode, scale-to-zero, CI/CD pipelines.

## Capabilities

### New Capabilities
- `match-session-lifecycle`: payload validation, one invocation = one character, async start + `status`/`stop` on the same session.
- `ctf-agent-runtimes`: behavior of the 4 ported agents on AgentCore.
- `mcp-m2m-auth`: token per MCP connection (D6), per-runtime provider.
- `decider-client`: SageMaker invocation, bounded retry, cooldown breaker, LLM fallback.
- `decider-endpoint`: adapter contract (`/invocations`, `/ping`, FIFO serialization) and DeciderStack.
- `fleet-operations`: config from SSM, launcher, secret hydration, runbook.

### Modified Capabilities
None (`openspec/specs/` is empty).

## Approach

- Async one-character-per-invocation entrypoints (`add_async_task`), progress in CloudWatch.
- `shared/ctf_common` is the source of truth, vendored into each `codeLocation` by a sync script.
- Runtime reads `/agentic-ctf/*` SSM contract at session start (D4).
- **Secrets, interim path A**: script hydrates gitignored `agentcore/.env.local` from SSM client-id + Secrets Manager. Final mechanism (A vs B `CfnOAuth2CredentialProvider` with dynamic references) is decided in design after spike S3.

## Affected Areas

| Area | Impact |
|------|--------|
| `app/{harness,harness_sdk,strand_decider,decider_loop}/` | New/Modified |
| `shared/ctf_common/`, `tests/`, root `pyproject.toml` | New |
| `agentcore/agentcore.json`, `agentcore/cdk/lib/` | Modified |
| `infra/decider/`, `scripts/`, runbook | New |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| S1 strands-harness lacks bearer headers for HTTP MCP | Med | Spike; pass pre-built MCPClient |
| S2 no cp314 aarch64 wheels | Med | Fall back to PYTHON_3_13 |
| S3 credential/provider naming via CLI | Med | Path B fallback |
| S4 decider latency on T4 vs 4.5 s | High | Benchmark; breaker falls back to LLM |
| S5 `agentcore deploy` deploys extra stacks | Low | Separate decider CDK app |
| Shared `sub` per runtime blocks multiple joins | Med | Verify agentic-ctf; distinct names |
| Forgotten endpoint (~$537/month), quota 0 | Med | Budget alert outside stack; request quota early |
| Secrets transit laptop | Accepted | First slice only; follow-up |

## Rollback Plan

`cdk destroy` DeciderStack; `agentcore remove` the new runtimes/credentials, then `agentcore deploy`; revert repo changes. No shared state is mutated (SSM is read-only here).

## Dependencies

- agentic-ctf SSM contract: `/agentic-ctf/mcp-url`, `/agentic-ctf/cognito/{user-pool-id,discovery-url,token-endpoint,mcp-scope}`, `/agentic-ctf/agents/<name>/{client-id,secret-arn}`.
- SageMaker `ml.g4dn.xlarge` endpoint quota.

## Success Criteria

- [ ] A real match is played by all 4 agent types on AgentCore against the deployed MCP, with the decider on SageMaker.
- [ ] Workshop runbook covers deploy order and teardown, and was followed end to end.
- [ ] Token requests equal MCP connections, not tool calls.

## Proposal question round (automatic mode: assumptions for user review)

- Scale: up to 2 teams, up to 3 characters per agent type.
- Launcher starting N characters for a `match_id` is in scope.
- Async start + CloudWatch progress; no streaming.
- Laptop transit of secrets via `.env.local` is acceptable for the first slice only.
- Decider failure: bounded retry, then cooldown breaker with LLM fallback (replaces permanent `decider_down`).
