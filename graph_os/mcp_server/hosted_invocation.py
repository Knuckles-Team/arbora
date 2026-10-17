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

    def mount_http(self, app: Any, *, prefix: str) -> None:
        from graph_os.api.http.app import create_api_application

        self.validate()
        child = create_api_application(
            services=self.services,
            visibility=self.services.policy_gate.check_op,
            authenticator=self.authenticator,
            invoke=invoke,
        )
        app.mount((prefix.rstrip("/") or "/api") + "/v1", child)
