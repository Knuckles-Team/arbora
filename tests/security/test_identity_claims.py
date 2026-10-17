"""Claims from any OIDC provider normalize to one identity and capability set."""

from __future__ import annotations

import pytest

from graph_os.security.identity import base_capabilities, normalize_identity


@pytest.mark.parametrize(
    ("claims", "subject", "tenant"),
    [
        ({"sub": "u1", "tenant_id": "t1"}, "u1", "t1"),
        ({"client_id": "svc", "tid": "t2"}, "svc", "t2"),
        ({"azp": "app", "org": "t3"}, "app", "t3"),
    ],
)
def test_subject_and_tenant_keys(claims: dict, subject: str, tenant: str) -> None:
    identity = normalize_identity(claims)
    assert (identity.subject, identity.tenant) == (subject, tenant)


def test_group_map_grants_mapped_capabilities_only() -> None:
    identity = normalize_identity({"sub": "u", "groups": ["ops"], "scope": "kg:read"})
    caps = set(base_capabilities(identity, {"ops": ["fleet:admin"]}))
    assert "fleet:admin" in caps
    assert "kg:read" in caps
    assert "other:admin" not in caps
