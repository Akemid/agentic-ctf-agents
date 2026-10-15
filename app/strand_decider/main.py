"""AgentCore entrypoint for the strand_decider runtime.

Thin on purpose: payload validation, the async start/status/stop state machine and the agent
live in `ctf_common` and `strand_agent`. The entrypoint is a plain `def`, so AgentCore runs it in
a threadpool and `/ping` is never blocked.
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from ctf_common.runner import handle_invocation
from strand_agent import RUNTIME, build_runner

app = BedrockAgentCoreApp()
runner = build_runner(app)


@app.entrypoint
def invoke(payload, context):
    session_id = getattr(context, "session_id", None)
    return handle_invocation(runner, payload, runtime=RUNTIME, session_id=session_id)


if __name__ == "__main__":
    app.run()
