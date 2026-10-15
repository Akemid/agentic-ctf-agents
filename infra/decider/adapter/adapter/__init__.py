"""SageMaker adapter for `strands-decider serve`.

SageMaker speaks `POST /invocations` and `GET /ping` on :8080. The decider speaks `/v1/systemone` and
`/health`, and crashes on concurrent requests. This package bridges the two and supervises the decider.
"""
