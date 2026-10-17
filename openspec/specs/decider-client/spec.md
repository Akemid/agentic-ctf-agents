# Decider Client Specification

## Purpose

SageMaker decider client used by the `strand_decider` and `decider_loop` runtimes: invocation, deadline clamp,
bounded retry, circuit breaker, and LLM fallback. Merged from change `agentcore-ctf-fleet` on 2026-10-16. The
"Decider optional" requirement records the amendment from `decider-optional.md`.

## Requirements

### Requirement: SageMaker invocation
The decider client MUST call the SageMaker runtime `invoke_endpoint` (not raw HTTP) using the endpoint name from SSM-sourced configuration, with SigV4 via the runtime role. It MUST NOT import torch or any model library.

#### Scenario: Successful call
- GIVEN a healthy endpoint
- WHEN the client requests a decision
- THEN it returns the per-question probabilities from the endpoint response

### Requirement: Timeout clamped to tick deadline
The per-call timeout MUST be `min(configured_timeout, remaining_time_in_tick - safety_margin)`. If `remaining_time_in_tick - safety_margin` is below 400 ms, the client MUST skip the call and fall back immediately; this tick-local skip MUST NOT count as a breaker failure.

#### Scenario: Clamp
- GIVEN configured timeout 4.5 s and 2.0 s remaining in the tick
- WHEN the client calls
- THEN the effective timeout is 2.0 s

#### Scenario: No time left
- GIVEN less than 400 ms remaining after the safety margin
- WHEN a decision is requested
- THEN no endpoint call is made, the LLM fallback is used, and the breaker failure count is unchanged

### Requirement: Bounded retry
On transient errors (timeouts, connection errors, throttling, service-unavailable, internal-failure, model-not-ready) the client MUST retry at most once, only if the remaining tick budget allows it, never exceeding the tick deadline. A 503 `queue_full` from the adapter (busy), model 4xx errors, misconfiguration errors and protocol errors MUST NOT be retried.

#### Scenario: Retry then success
- GIVEN the first call is throttled and the second succeeds
- WHEN the client calls
- THEN it returns the second result after one retry

#### Scenario: Retry exhausted
- GIVEN every attempt fails transiently
- WHEN retries are exhausted
- THEN the call counts as one failure and fallback is used

#### Scenario: Validation error
- GIVEN a model 4xx response
- WHEN received
- THEN no retry occurs, the call is logged as a bug and does not count toward the breaker

#### Scenario: Adapter busy
- GIVEN a 503 `queue_full` response
- WHEN received
- THEN no retry occurs and the call counts as one breaker failure

### Requirement: Circuit breaker with cooldown
After K consecutive failed calls (default 3) the breaker MUST open for a cooldown period (default 30 s) during which no endpoint calls are made. A misconfiguration error (access denied, endpoint not found, validation failure) MUST open the breaker immediately for 300 s. After cooldown one probe call is allowed (half-open); success closes the breaker, failure reopens it. This replaces any permanent `decider_down` state.

#### Scenario: Opens
- GIVEN 3 consecutive failures
- WHEN the next decision is requested
- THEN no endpoint call is made and fallback is used

#### Scenario: Misconfiguration
- GIVEN an access-denied or endpoint-not-found error
- WHEN received
- THEN the breaker opens for 300 s and the LLM fallback is used

#### Scenario: Recovers
- GIVEN an open breaker and cooldown elapsed
- WHEN a probe succeeds
- THEN the breaker closes and subsequent calls use the endpoint

#### Scenario: Probe fails
- GIVEN half-open and the probe fails
- WHEN evaluated
- THEN the breaker reopens for a new cooldown

### Requirement: Decider optional
The decider endpoint MAY be absent for a whole match, because `DeciderStack` is destroyed between workshops. Session preflight MUST NOT fail because the endpoint is missing: the parameter `/ctf-decider/endpoint-name` is published by the persistent stack, and preflight only requires that the parameter exists. A missing endpoint is handled by the circuit breaker as a misconfiguration (see Circuit breaker with cooldown): the first call fails with a validation error, which costs one call, not one call per tick. While the breaker is open, `strand_decider` proceeds ungated and `decider_loop` sends ticks to the LLM. Each outage MUST be logged once at INFO as `decider endpoint unavailable; playing on the LLM fallback`.

#### Scenario: Endpoint missing at start
- GIVEN the endpoint parameter exists but the `ctf-decider` endpoint is not deployed
- WHEN a match starts
- THEN preflight passes, the first decision call opens the breaker for 300 s, and the match continues on the LLM fallback (`decider_loop`) or ungated (`strand_decider`)

#### Scenario: Endpoint returns mid-match
- GIVEN an open breaker caused by a missing endpoint, and the endpoint is deployed again
- WHEN the 300 s window elapses and the probe succeeds
- THEN the decider is used again for the following ticks (the gate is active again in `strand_decider`)

### Requirement: LLM fallback
When the endpoint is skipped or fails, the agent MUST continue the match using the LLM path; the match MUST NOT abort because the decider is down. Fallback events MUST be logged with a reason.

#### Scenario: Match continues
- GIVEN the decider is down for the whole match
- WHEN the match runs
- THEN all ticks are handled by the LLM and the match completes

### Requirement: Sensitive data
Logs MUST NOT include full prompts or credentials; only latency, outcome and breaker state.

#### Scenario: Log content
- GIVEN a decision call
- WHEN logged
- THEN the record contains latency, outcome and breaker state only
