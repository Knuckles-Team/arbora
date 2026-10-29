"""Real stdio host with a scripted model and synthetic test authority only."""

import asyncio
import json
import sys
from dataclasses import replace
from datetime import timedelta

from agent_utilities import host_ports
from agent_utilities.runtime import RunPlan
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from arbora.acp import HostedACPAgent, serve_acp_stdio
from arbora.acp.local_history import LocalConversationHistory
from arbora.acp.session_policy import AcpSessionPolicy, PrincipalContext


async def model(messages, info):
    part = messages[-1].parts[-1]
    if part.part_kind == "tool-return":
        yield str(part.content)
        return
    text = str(part.content)
    if text == "history":
        yield json.dumps(
            [
                part.content
                for message in messages
                for part in message.parts
                if part.part_kind == "user-prompt"
            ]
        )
    elif text == "wait":
        yield "waiting"
        await asyncio.Event().wait()
    elif text == "read":
        yield {0: DeltaToolCall(name="read_file", json_args='{"path":"test.txt"}')}
    elif text == "escape":
        yield {0: DeltaToolCall(name="read_file", json_args='{"path":"../escape"}')}
    elif text == "bigwrite":
        yield {
            0: DeltaToolCall(
                name="write_file",
                json_args=json.dumps({"path": "test.txt", "content": "x" * 8192}),
            )
        }
    elif text == "large":
        yield "x" * 8192
    elif text == "write":
        yield {
            0: DeltaToolCall(
                name="write_file", json_args='{"path":"test.txt","content":"new"}'
            )
        }
    else:
        yield "hello "
        yield "client"


def plan(request):
    value = RunPlan(model=FunctionModel(stream_function=model), steps=(request.task,))
    return replace(
        value,
        harness=replace(
            value.harness, plan_scaffold="off", client_tools=("read_file", "write_file")
        ),
    )


host_ports.register_run_decision_provider(plan)
history = LocalConversationHistory("scripted:model")
host_ports.register_run_history_provider(history.read)


async def complete(request, result):
    if request.task == "savefail":
        raise OSError("private storage detail")
    await history.complete(request, result)


agent = HostedACPAgent(
    lambda: PrincipalContext("test-user", "test-tenant", frozenset({"kg:write"})),
    policy=AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
    trusted_roots=frozenset({sys.argv[1]}),
    max_output_bytes=int(sys.argv[2]),
    request_factory=history.request,
    completed=complete,
    closed=history.close,
)
asyncio.run(serve_acp_stdio(agent))
