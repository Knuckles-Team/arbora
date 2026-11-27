"""Fixtures for the sandbox principals."""

from __future__ import annotations

import pytest

from arbora.identity.sandbox_principal import (
    SandboxPrincipal,
    mint_sandbox_principals,
)
from tests.identity._federated_fixture import (  # noqa: F401
    fed_lagged,
    fed_readerless,
    fed_world,
)


@pytest.fixture
def two_sandbox_tenants() -> tuple[SandboxPrincipal, SandboxPrincipal]:
    """An operator in ``sandbox-a`` and a viewer in ``sandbox-b``.

    The pair is the cross-tenant probe: each principal resolves to its own
    tenant only, and a client built from one never names the other's tenant.
    """
    first, second = mint_sandbox_principals(
        [("operator", "sandbox-a"), ("viewer", "sandbox-b")],
        profile="sandbox",
        bind_host="127.0.0.1",
    )
    return first, second
