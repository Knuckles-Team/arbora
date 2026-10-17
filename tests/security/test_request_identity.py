"""Request identity names tenant graphs and refuses unverified actors."""

from __future__ import annotations

import pytest

from graph_os.security.actor_context import ActorContext
from graph_os.security.request_identity import (
    ensure_correlation_id,
    tenant_graph_name,
    validate_carrier_claims,
)


@pytest.mark.parametrize(
    ("tenant", "graph"),
    [
        (None, "__commons__"),
        ("", "__commons__"),
        ("Acme Corp", "tenant__acme_corp____commons__"),
        ("!!", "__commons__"),
    ],
)
def test_tenant_graph_name(tenant: str | None, graph: str) -> None:
    assert tenant_graph_name(tenant) == graph


def test_correlation_id_is_stable_within_a_context() -> None:
    assert ensure_correlation_id() == ensure_correlation_id()


def test_carrier_claims_missing_fields_refuse() -> None:
    with pytest.raises((PermissionError, ValueError)):
        validate_carrier_claims({"sub": "x"})


def test_unauthenticated_actor_cannot_mint_a_session() -> None:
    from graph_os.security.request_identity import mint_graph_session

    with pytest.raises(PermissionError):
        mint_graph_session(ActorContext(actor_id="x", authenticated=False))
