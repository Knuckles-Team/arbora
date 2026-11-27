from functools import partial

from arbora.core.settings import RuntimeConfig

"""Shared A2A application fixtures using the real request-session authority."""

from typing import Any

from fastapi import FastAPI

from arbora.a2a.application import AmbientA2AAuthenticator, create_a2a_application
from arbora.a2a.service import A2AService
from arbora.api.http.auth import (
    AmbientHTTPAuthenticator,
    verified_bearer_session_for_request,
)
from arbora.api.invoke.pipeline_services import InvokeServices
from arbora.security.key_cache import JwksCache
from tests.a2a.test_application import Authority, Router


def authenticated_operation_app(services: InvokeServices) -> FastAPI:
    """Compose supplied operation services with the production bearer resolver."""
    return create_a2a_application(
        service=A2AService(
            authority=Authority(), router=Router(), config=RuntimeConfig()
        ),
        authenticator=AmbientA2AAuthenticator(JwksCache(RuntimeConfig())),
        operation_services=services,
        operation_authenticator=AmbientHTTPAuthenticator(
            session_for_request=partial(
                verified_bearer_session_for_request, JwksCache(RuntimeConfig())
            ),
        ),
    )


def streaming_model(function: Any) -> Any:
    """A ``FunctionModel`` that also streams the text its ``function`` answers.

    A served A2A run streams the model so its answer reaches the event log as
    deltas (ARBORA-A2A-R001.4.1); a function model needs a stream function too.
    """
    import inspect

    from pydantic_ai.messages import TextPart
    from pydantic_ai.models.function import FunctionModel

    async def stream(messages: Any, info: Any) -> Any:
        response = function(messages, info)
        if inspect.isawaitable(response):
            response = await response
        for part in response.parts:
            if not isinstance(part, TextPart):
                raise AssertionError("streaming_model answers text only")
            yield part.content

    return FunctionModel(function, stream_function=stream)
