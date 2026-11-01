"""The source reader keeps local anonymous metadata out of verified carriers."""

from argparse import Namespace
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import claim_values
from arbora.api.mcp.caller import caller_from_session
from arbora.mcp_server.source_reader import NativeSourceReader
from arbora.security import anonymous
from tests.mcp_server.test_source_reader import Client, context

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.3")


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", None)
    monkeypatch.setenv("ARBORA_AUTH_MODE", "none")
    for key in (
        "AUTH_JWT_JWKS_URI",
        "AUTH_JWT_ISSUER",
        "AUTH_JWT_AUDIENCE",
        "KG_AUTH_TOKEN_REF",
        "KG_IDENTITY_OAUTH2",
        "GRAPH_SERVICE_AUTH_SECRET",
        "MEDULLA_SIGNER_KEYS_JSON",
    ):
        monkeypatch.delenv(key, raising=False)
    anonymous.validate_startup(Namespace(host="127.0.0.1", auth_type="none"))


def anonymous_context():
    class AnonymousClient:
        authenticated = False

        def use_verified_context(self, claims):
            pytest.fail("anonymous metadata entered a verified carrier")

    caller = caller_from_session(anonymous.session(), credential_kind="none")
    return ExecutionContext(
        caller=caller,
        client=AnonymousClient(),
        owner=caller.principal,
        service_identity=False,
        services={},
    )


@pytest.fixture
def reads(monkeypatch):
    import medulla.generated as generated

    calls = []

    async def send(client, method, params, graph):
        calls.append(
            (client, graph, client.claims.get() if hasattr(client, "claims") else None)
        )
        return []

    monkeypatch.setattr(generated, "send_by_id", send)
    return calls


async def test_anonymous_source_read_never_uses_verified_carrier(local_mode, reads):
    ctx = anonymous_context()
    result = await NativeSourceReader().tree(
        repository="repo",
        ref="main",
        path="",
        context=ctx,
    )
    assert result.found is False
    assert reads == [(ctx.client, "tenant__local", None)]
    assert ctx.caller.authenticated is False


async def test_verified_source_read_still_binds_and_clears_claims(local_mode, reads):
    client = Client()
    for principal, tenant in (("caller-a", "tenant-a"), ("caller-b", "tenant-b")):
        ctx = context(principal=principal, tenant=tenant, client=client)
        await NativeSourceReader().tree(
            repository="repo",
            ref="main",
            path="",
            context=ctx,
        )
        assert reads[-1] == (client, "control", claim_values(ctx.caller.engine_claims))
        assert client.claims.get() is None
    assert reads[0][2] != reads[1][2]


@pytest.mark.parametrize(
    "change", ["verified_transport", "foreign_owner", "missing_scope"]
)
async def test_anonymous_source_read_refuses_mismatches(local_mode, reads, change):
    ctx = anonymous_context()
    if change == "verified_transport":
        ctx = replace(ctx, client=SimpleNamespace(authenticated=True))
    elif change == "foreign_owner":
        ctx = replace(ctx, owner="another-caller")
    else:
        ctx = replace(ctx, caller=replace(ctx.caller, effective_scopes=frozenset()))
    with pytest.raises((PermissionError, OperationRefused)):
        await NativeSourceReader().tree(
            repository="repo", ref="main", path="", context=ctx
        )
    assert reads == []


async def test_verified_source_read_refuses_anonymous_transport(local_mode, reads):
    ctx = context(client=anonymous_context().client)
    with pytest.raises(PermissionError, match="anonymous transport"):
        await NativeSourceReader().tree(
            repository="repo", ref="main", path="", context=ctx
        )
    assert reads == []
