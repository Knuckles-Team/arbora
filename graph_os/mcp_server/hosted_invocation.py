"""Explicit shared invocation dependencies for the native serving lifecycle."""

from dataclasses import dataclass
from typing import Any

from graph_os.api.http.auth import AmbientHTTPAuthenticator
from graph_os.api.invoke.pipeline import InvokeServices, invoke
from graph_os.api.registry.contract_admission import validate_contract_admission


@dataclass(frozen=True)
class HostedInvocation:
    """Retain one admitted service bundle and its request authenticator.

    The trusted embedding application supplies real owner dependencies. This
    object neither mints process authority nor fills absent effect owners.
    """

    services: InvokeServices
    authenticator: AmbientHTTPAuthenticator

    def __post_init__(self) -> None:
        if not isinstance(self.services, InvokeServices) or not isinstance(
            self.authenticator, AmbientHTTPAuthenticator
        ):
            raise TypeError("shared services and request authenticator are required")
        self.validate()

    def validate(self) -> None:
        """Recheck provider admission before each surface is mounted."""
        validate_contract_admission(self.services.registry)

    def http_application(self) -> Any:
        """The operation-invoke API application over these shared services."""
        from graph_os.api.http.app import create_api_application

        self.validate()
        return create_api_application(
            services=self.services,
            visibility=self.services.policy_gate.check_op,
            authenticator=self.authenticator,
            invoke=invoke,
        )

    def mount_http(self, app: Any, *, prefix: str) -> None:
        app.mount((prefix.rstrip("/") or "/api") + "/v1", self.http_application())

    def mount_on_mcp(self, mcp: Any, *, prefix: str = "/api") -> None:
        """Serve the API beside the MCP endpoint of the served HTTP transport.

        FastMCP builds its HTTP application at run time from the server's
        additional routes; the API is one more mounted route there. Each API
        request authenticates its own bearer through ``authenticator``.
        """
        from starlette.routing import Mount

        path = (prefix.rstrip("/") or "/api") + "/v1"
        mcp._additional_http_routes.append(Mount(path, app=self.http_application()))
