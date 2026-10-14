"""Shared A2A application fixtures using the real request-session authority."""

from fastapi import FastAPI

from graph_os.a2a.application import AmbientA2AAuthenticator, create_a2a_application
from graph_os.a2a.service import A2AService
from graph_os.api.http.auth import (
    AmbientHTTPAuthenticator,
    verified_bearer_session_for_request,
)
from graph_os.api.invoke.pipeline import InvokeServices
from tests.a2a.test_application import Authority, Router


def authenticated_operation_app(services: InvokeServices) -> FastAPI:
    """Compose supplied operation services with the production bearer resolver."""
    return create_a2a_application(
        service=A2AService(authority=Authority(), router=Router()),
        authenticator=AmbientA2AAuthenticator(),
        operation_services=services,
        operation_authenticator=AmbientHTTPAuthenticator(
            session_for_request=verified_bearer_session_for_request,
        ),
    )
