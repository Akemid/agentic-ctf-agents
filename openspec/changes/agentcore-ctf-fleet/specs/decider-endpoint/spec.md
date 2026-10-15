# Delta Spec: decider-endpoint

## ADDED Requirements

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
`/ping` MUST respond independently of the request queue, including while an inference is in flight. It MUST return 503 until warm-up completes (model loaded and decider `/health` OK), then 200.

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
`infra/decider/` MUST be a separate CDK app with `DeciderStack` using `ml.g4dn.xlarge`, one instance, deployable and destroyable independently of `agentcore deploy`. It MUST NOT use CloudFormation exports; the endpoint name (and ARN) MUST be published only to SSM.

#### Scenario: No exports
- GIVEN the synthesized template
- WHEN inspected
- THEN it has no `Outputs` with `Export`

#### Scenario: SSM publication
- GIVEN a deployed stack
- WHEN SSM is read
- THEN the endpoint name parameter exists

#### Scenario: Independent teardown
- GIVEN AgentCore stack and DeciderStack deployed
- WHEN DeciderStack is destroyed
- THEN the destroy is not blocked by the other stack

### Requirement: No budget resources inside the stack
The budget alert MUST live outside DeciderStack so that destroying the endpoint keeps the alert.

#### Scenario: Stack content
- GIVEN the synthesized template
- WHEN inspected
- THEN it contains no `AWS::Budgets::Budget`
