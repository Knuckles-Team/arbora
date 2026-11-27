"""Real local entrypoint; replace only the public model provider for the test."""

import asyncio
import json
from dataclasses import replace

from meristem.host_ports import HostPorts
from pydantic_ai.models.function import FunctionModel

from arbora.acp.local_history import LocalConversationHistory
from arbora.acp.local_runtime import main


async def model(messages, info):
    prompts = [
        part.content
        for message in messages
        for part in message.parts
        if part.part_kind == "user-prompt"
    ]
    if prompts[-1] == "wait":
        yield "waiting"
        await asyncio.Event().wait()
    elif prompts[-1] == "history":
        yield json.dumps(prompts)
    else:
        yield "local "
        yield "answer"


def provider(model_id):
    if model_id != "scripted:local":
        raise ValueError("Unexpected test model")
    return FunctionModel(stream_function=model)


_bind = LocalConversationHistory.bind_ports


def _bind_with_model(self, ports: HostPorts) -> None:
    _bind(self, replace(ports, models=provider))


LocalConversationHistory.bind_ports = _bind_with_model
raise SystemExit(main())
