# Decider optional

## Problem

`strand_decider` and `decider_loop` read `/ctf-decider/endpoint-name` at session preflight
(`shared/ctf_common/config.py`). `DeciderStack` created that parameter and is destroyed between workshops. With
the parameter missing, preflight raised `ConfigError` and `start` returned `failed`, so both runtimes could not
play at all. Yet in a match the guarded client (`shared/ctf_common/decider.py`) already survives a dead endpoint:
circuit breaker, a 300 s open on misconfiguration, ungated gate in `strand_decider`, LLM fallback in
`decider_loop`. The fallback existed but preflight made it unreachable.

## Options

- A. Make the parameter optional in `load_config`: the runtime would need a "no decider" mode and could not pick
  up an endpoint brought up mid-match.
- B (chosen). The endpoint name is a fixed constant (`ctf-decider`, `lib/names.ts`), so move the parameter to
  `CtfPersistentStack` (no dependency on the endpoint) and remove it from `DeciderStack`.

B needs no runtime change, keeps preflight strict for real misconfiguration (IAM, wrong parameter name) and
gives mid-match recovery through the existing breaker re-probe. The fleet IAM (`ssm:GetParameter` on the same
ARN) is unchanged.

## Behaviour

| Endpoint | Preflight | `strand_decider` | `decider_loop` |
|---|---|---|---|
| up | passes | gate reviews `tag` / `grab_flag` | decider picks the action |
| down | passes | first call fails (`ValidationError`, endpoint not found), breaker opens 300 s, gate proceeds ungated | warm-up failure is logged, ticks go to the LLM while the breaker is open |
| up again mid-match | passes | first call after the 300 s window re-probes; on success the gate is active again | same: the decider is used again |

Why not transient: `ValidationError` maps to `DeciderMisconfigured` (open for 300 s, never retried), so a missing
endpoint costs one call, not a retried call on every tick. Tests: `tests/test_decider_client.py`,
`tests/test_gate.py::TestEndpointMissing`, `tests/test_decider_loop.py::TestEndpointMissing` (botocore Stubber and
a fake clock). The only runtime change is one INFO line per outage, "decider endpoint unavailable; playing on the
LLM fallback", so the state is visible in CloudWatch.

## Deploy order

A named SSM parameter cannot belong to two stacks. Deploy `CtfPersistentStack` only while `DeciderStack` is
destroyed (or destroy `DeciderStack` first). `make destroy-decider` now leaves the parameter in place on purpose.
