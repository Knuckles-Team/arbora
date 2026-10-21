"""Shared context builders for the direct-handler ops tests in ``tests/api/``.

Each of ``test_capacity_ops.py``, ``test_work_ops.py``,
``test_policy_swarm_ops.py``, ``test_telemetry_security_usage_ops.py``,
``test_memory_ops.py``, and the ``test_harness_*.py`` modules exercises one
ops module's handler directly against a ``SimpleNamespace`` stand-in for the
invoke pipeline's request context, rather than through the full
``get_registry()`` assembly (that coverage lives in
``test_registry_factory.py``). This module factors out the three context
shapes those tests build, so the shape doesn't drift per module.
"""

from __future__ import annotations

from types import SimpleNamespace


def service_context(
    *, tenant: str | None = None, **services: object
) -> SimpleNamespace:
    """A session-scoped context bound to zero or more named services."""
    caller_kwargs: dict[str, object] = {"session": object()}
    if tenant is not None:
        caller_kwargs["tenant"] = tenant
    return SimpleNamespace(
        caller=SimpleNamespace(**caller_kwargs),
        client=object(),
        idempotency_key="key-1",
        services=services,
    )


def tenant_reader_context(
    *, reader: object | None, service_name: str
) -> SimpleNamespace:
    """A tenant-scoped context bound to one reader port, or none when uncomposed."""
    caller = SimpleNamespace(tenant="tenant-a", principal="caller-a")
    services: dict[str, object] = {} if reader is None else {service_name: reader}
    return SimpleNamespace(caller=caller, services=services, idempotency_key=None)


def tenant_store_context(
    *,
    store: object | None,
    service_name: str = "memory_store",
    idempotency_key: str | None = "idem-1",
    tenant: str = "tenant-a",
) -> SimpleNamespace:
    """A tenant-scoped context bound to one read/write store port."""
    caller = SimpleNamespace(tenant=tenant, principal="caller-a")
    services: dict[str, object] = {} if store is None else {service_name: store}
    return SimpleNamespace(
        caller=caller, services=services, idempotency_key=idempotency_key
    )


def harness_context(
    *,
    services: dict[str, object],
    scopes: tuple[str, ...] = ("kg:read",),
    principal_kind: str = "human",
    tenant: str = "tenant-a",
) -> SimpleNamespace:
    """A verified-caller context for the harness surface ops.

    The harness handlers admit on the verified caller's authority (the
    ``harness_admission`` boundary), so the caller stand-in carries the
    full authority shape: tenant, principal, principal kind and exact
    effective scopes.
    """
    caller = SimpleNamespace(
        tenant=tenant,
        principal="caller-a",
        principal_kind=principal_kind,
        effective_scopes=frozenset(scopes),
    )
    return SimpleNamespace(caller=caller, services=services, idempotency_key=None)
