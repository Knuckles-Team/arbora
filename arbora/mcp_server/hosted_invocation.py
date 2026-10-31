"""Explicit shared invocation dependencies for the native serving lifecycle."""

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.api.http.auth import AmbientHTTPAuthenticator
from arbora.api.invoke.pipeline import InvokeServices, OperationRefused, invoke
from arbora.api.invoke.steps import RequestCaller
from arbora.mcp_server.task_plan import TaskPlanConfig, evaluate_task_plan


@dataclass(frozen=True)
class HostedInvocation:
    """Retain one admitted service bundle and its request authenticator.

    The trusted embedding application supplies real owner dependencies. This
    object neither mints process authority nor fills absent effect owners.
    """

    services: InvokeServices
    authenticator: AmbientHTTPAuthenticator
    task_plan_config: TaskPlanConfig | None = None
    browser_boundary: Any = None
    #: Process-lifecycle work the server lifespan awaits under the process
    #: authority before serving (the task-plan schema publication). No request
    #: path reaches it.
    process_startup: Callable[[], Coroutine[Any, Any, Any]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.services, InvokeServices) or not isinstance(
            self.authenticator, AmbientHTTPAuthenticator
        ):
            raise TypeError("shared services and request authenticator are required")
        if self.task_plan_config is not None and not isinstance(
            self.task_plan_config, TaskPlanConfig
        ):
            raise TypeError("task plan configuration must be trusted TaskPlanConfig")

    async def evaluate_task_plan(
        self, caller: RequestCaller, question_id: str, proposal: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Shared backend evaluation; durable execution admission is separate."""
        if self.task_plan_config is None:
            raise OperationRefused("UNAVAILABLE", {"capability": "task_plan"})
        return await evaluate_task_plan(
            self.services, self.task_plan_config, caller, question_id, proposal
        )

    def configure_acp(self, app: Any, *, trusted_roots: frozenset[str]) -> None:
        """Opt in to the central ACP projection with explicit client roots.

        This uses this invocation's existing authentication, operation policies
        and task owner. It does not register a second execution runtime.
        """
        from arbora.acp.central_runtime import configure_central_acp

        configure_central_acp(app, self, trusted_roots=trusted_roots)

    def http_application(self) -> Any:
        """The operation-invoke API application over these shared services."""
        from arbora.api.http.app import create_api_application

        return create_api_application(
            services=self.services,
            visibility=self.services.policy_gate.check_op,
            authenticator=self.authenticator,
            invoke=invoke,
        )

    def run_compatibility_application(self) -> Any:
        from arbora.api.http.app import create_run_compatibility_application

        return create_run_compatibility_application(
            services=self.services, authenticator=self.authenticator, invoke=invoke
        )

    def mount_http(self, app: Any, *, prefix: str) -> None:
        app.mount((prefix.rstrip("/") or "/api") + "/v1", self.http_application())
        app.mount(
            (prefix.rstrip("/") or "/api") + "/runs",
            self.run_compatibility_application(),
        )

    def mount_on_mcp(self, mcp: Any, *, prefix: str = "/api") -> None:
        """Serve the API beside the MCP endpoint of the served HTTP transport.

        FastMCP builds its HTTP application at run time from the server's
        additional routes; the API is one more mounted route there. Each API
        request authenticates its own bearer through ``authenticator``.
        """
        from starlette.routing import Mount

        path = (prefix.rstrip("/") or "/api") + "/v1"
        mcp._additional_http_routes.append(Mount(path, app=self.http_application()))
        mcp._additional_http_routes.append(
            Mount(
                (prefix.rstrip("/") or "/api") + "/runs",
                app=self.run_compatibility_application(),
            )
        )
