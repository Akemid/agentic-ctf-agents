# Decider Endpoint Specification

## Purpose

The SageMaker decider endpoint: the serializing FastAPI adapter container, the warm-up gate, the separate
`DeciderStack` CDK app, SSM publication of the endpoint name, and the hash-locked image build. Merged from change
`agentcore-ctf-fleet` on 2026-10-16. Amendments from `design.md` addenda, `decider-optional.md` and
`decider-image-deps.md` are merged in: the 600 s startup timeout, the endpoint-name parameter owner, and the image
dependency lock.

## Requirements

### Requirement: SageMaker adapter contract
The adapter container MUST expose `POST /invocations` and `GET /ping` as SageMaker requires, proxying to the decider (`/v1/systemone`, `/health`) in the same container.

#### Scenario: Invocation proxied
- GIVEN a warm adapter
- WHEN `POST /invocations` carries a valid body
- THEN it returns the decider's response with status 200

#### Scenario: Invalid body
- GIVEN any state
- WHEN the body is not valid JSON
- THEN it returns 400 without calling the decider

### Requirement: Serialized requests
The decider crashes on concurrent requests, so the adapter MUST process at most one request at a time (max concurrency 1), FIFO. At most 8 requests MAY be waiting: a request arriving while 8 or more are already waiting MUST return 503 `queue_full` immediately. Any other request MUST wait in the queue no longer than a bounded wait (default 2 s); on expiry it MUST return 503 and MUST NOT be forwarded later.

#### Scenario: Concurrent requests
- GIVEN 2 simultaneous requests
- WHEN both arrive
- THEN the decider sees them strictly one after another in arrival order

#### Scenario: Queue full
- GIVEN 8 requests are already waiting behind an in-flight one
- WHEN another request arrives
- THEN it returns 503 `queue_full` immediately and the decider never receives it

#### Scenario: Queue wait exceeded
- GIVEN a request is in flight longer than the bounded wait
- WHEN another request is queued
- THEN it returns 503 after the bounded wait and the decider never receives it

#### Scenario: Cancelled work not run
- GIVEN a request timed out in queue
- WHEN the in-flight one finishes
- THEN the timed-out request is not executed

### Requirement: Ping never blocked, warm-up gated
`/ping` MUST respond independently of the request queue, including while an inference is in flight. It MUST return 503 until warm-up completes (model loaded and decider `/health` OK, plus the warm-up questions), then 200.

#### Scenario: Ping during inference
- GIVEN a long inference in flight
- WHEN `/ping` is called
- THEN it responds immediately

#### Scenario: Ping during warm-up
- GIVEN the model is loading
- WHEN `/ping` is called
- THEN it returns 503

#### Scenario: Ping after warm-up
- GIVEN warm-up completed
- WHEN `/ping` is called
- THEN it returns 200

### Requirement: Separate DeciderStack
`infra/decider/` MUST be a separate CDK app with `DeciderStack` using `ml.g4dn.xlarge`, one instance, deployable and destroyable independently of `agentcore deploy`. It MUST NOT use CloudFormation exports. Its EndpointConfig sets `ContainerStartupHealthCheckTimeoutInSeconds` to 600. The earlier 1200 s design value is superseded: a SageMaker endpoint cannot be deleted while it is `Creating`, so a container that never becomes healthy is billed until the timeout expires. The adapter's own startup timeout (`ADAPTER_STARTUP_TIMEOUT_S`) defaults to 550 s to stay under it. The endpoint is named `ctf-decider`, the only fixed name in the stack.

#### Scenario: No exports
- GIVEN the synthesized template
- WHEN inspected
- THEN it has no `Outputs` with `Export`

#### Scenario: Startup health-check timeout
- GIVEN the synthesized `DeciderStack`
- WHEN the EndpointConfig is inspected
- THEN `ContainerStartupHealthCheckTimeoutInSeconds` is 600

#### Scenario: Independent teardown
- GIVEN AgentCore stack and DeciderStack deployed
- WHEN DeciderStack is destroyed
- THEN the destroy is not blocked by the other stack

### Requirement: Endpoint name published by the persistent stack
The endpoint name parameter `/ctf-decider/endpoint-name` MUST be created by `CtfPersistentStack`, not by `DeciderStack`, so that it exists whether or not the endpoint is deployed. A named SSM parameter cannot belong to two stacks. Therefore `CtfPersistentStack` is deployed only while `DeciderStack` is destroyed, or `DeciderStack` is destroyed first. `make destroy-decider` leaves the parameter in place on purpose.

#### Scenario: Parameter exists without the endpoint
- GIVEN `CtfPersistentStack` is deployed and `DeciderStack` is destroyed
- WHEN SSM is read
- THEN `/ctf-decider/endpoint-name` exists and holds `ctf-decider`

### Requirement: No budget resources inside the stack
The budget alert MUST live outside DeciderStack so that destroying the endpoint keeps the alert. The budget is created by `CtfPersistentStack`.

#### Scenario: Stack content
- GIVEN the synthesized DeciderStack template
- WHEN inspected
- THEN it contains no `AWS::Budgets::Budget`

### Requirement: Hash-locked adapter image with build-time smoke check
The adapter image MUST install its dependencies only from hash-pinned locks: `requirements.lock` for linux/amd64 and Python 3.11, with torch installed first and alone from `torch.lock` (`torch==2.14.1+cu126`, cu126 index). Every runtime dependency MUST come from the lock. The base image MUST be pinned by tag and digest (ECR Public `python:3.11.17-slim-bookworm`). The last build step MUST be `python -m adapter.smoke --require-checkpoint`. It imports the serving stack and `strands_decider.modeling`, imports `torch.distributed.checkpoint.hf_storage`, and builds the baked checkpoint's model class (`Qwen3_5ForCausalLM`) on the meta device, with no GPU and no weight load. Without the checkpoint the build MUST fail; it MUST NOT skip the check.

#### Scenario: Missing checkpoint fails the build
- GIVEN the baked checkpoint is absent during the image build
- WHEN the smoke step runs with `--require-checkpoint`
- THEN it exits 1 with a clear message and the image is not produced

#### Scenario: Unpinned dependency rejected
- GIVEN a runtime dependency that is not in the lock
- WHEN the build audit test runs
- THEN the test fails

#### Scenario: Smoke is the last build step
- GIVEN the adapter Dockerfile
- WHEN inspected
- THEN the smoke `RUN` is the final instruction
