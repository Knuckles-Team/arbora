"""The served composition of the harness host ports, over backends Arbora already has.

Each adapter in :mod:`arbora.agent_host.host_port_adapters` is handed one backend
callable here. The run-principal call is the registry ``invoke`` under the verified
session of the run that is executing; it never uses a process or service principal and
answers a run that is not executing exactly like an unknown one. A port whose backend
does not exist yet is left unbound, and the harness refuses it closed (see
``/specs/hosted-api-operations`` rows R890 to R894 and the gaps list).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

from arbora.agent_host.host_port_adapters import (
    EngineDecisionAdapter,
    HostPortsBinding,
    RunCall,
    bind_host_ports,
    decision_port,
    liveness_wait,
)


def _run_of(run_ref: str) -> Any:
    from arbora.agent_runs import current_native_run

    return current_native_run(SimpleNamespace(run_id=run_ref))


def run_principal_call(
    services: Any,
    resolve_run: Callable[[str], Any] = _run_of,
) -> RunCall:
    """``RunCall`` over registry ``invoke`` as the executing run's verified principal."""

    async def call(
        op_id: str, params: Mapping[str, Any], run_ref: str
    ) -> Mapping[str, Any]:
        from arbora.api.invoke.pipeline import invoke
        from arbora.api.invoke.steps import OpError
        from arbora.api.mcp.caller import caller_from_session
        from arbora.api.registry import Surface

        try:
            owner = resolve_run(run_ref)
        except RuntimeError:
            owner = None
        if owner is None or owner.run_id != run_ref:
            # A foreign, closed or unknown run answers alike.
            return {"outcome": "refused", "refusal_code": "UNAVAILABLE"}
        from arbora.security.session import use_session

        caller = caller_from_session(owner.session, credential_kind="session")
        with use_session(owner.session):
            result = await invoke(
                op_id, dict(params), caller, Surface.MCP, services=services
            )
        if isinstance(result, OpError):
            return {
                "outcome": "refused",
                "refusal_code": result.code,
                "payload": dict(result.details),
            }
        value = result.value
        return {
            "outcome": "answered",
            "payload": dict(value) if isinstance(value, Mapping) else {"value": value},
        }

    return call


def compose_host_ports(
    services: Any,
    *,
    wait: Callable[[float], Any],
    checkpoint_store: Any = None,
    resolve_run: Callable[[str], Any] = _run_of,
    method_op: Callable[[str], str | None] = lambda _: None,
) -> HostPortsBinding:
    """Bind every port that has a backend; the pinned release refuses ``UNAVAILABLE``."""
    call = run_principal_call(services, resolve_run)
    return bind_host_ports(
        engine_decision=EngineDecisionAdapter(call, method_op),
        decision=decision_port(call),
        liveness_wait=liveness_wait(wait),
        checkpoint_store=checkpoint_store,
    )
