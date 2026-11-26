"""Outside SQL client identities for the engine wire front end (ARBORA-OPS-R858)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import wire_access
from arbora.api.registry import AuditClass, Confirm, Effect, PrincipalRule
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble, context

_OPS = {op.id: op for op in wire_access.operations()}


class _Secrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def put(self, path: str, value: str) -> None:
        self.values[path] = value

    async def delete(self, path: str) -> None:
        self.values.pop(path, None)


def _context(secrets: _Secrets | None) -> Any:
    ctx = context()
    ctx.services = {} if secrets is None else {"secret_store": secrets}
    return ctx


@pytest.mark.spec("ARBORA-OPS-R858.1")
def test_registry_lists_wire_access_operations() -> None:
    assert sorted(_OPS) == ["wire_access.grant", "wire_access.revoke"]
    for op in _OPS.values():
        assert op.effect is Effect.ADMIN
        assert op.confirm is Confirm.CONSOLE
        assert op.audit is AuditClass.IDENTITY_CHAIN
        assert op.principals is PrincipalRule.HUMAN_UNDELEGATED
        assert op.scopes == frozenset({"identity:admin"})
    with pytest.raises(ValidationError):
        wire_access.WireAccessGrantParams(identity="bad name", principal_ref="p")
    with pytest.raises(ValidationError):
        wire_access.WireAccessGrantParams(
            identity="ok",
            principal_ref="p",
            password="x",  # type: ignore[call-arg]
        )


@pytest.mark.spec("ARBORA-OPS-R858.1")
async def test_a_pinned_engine_without_the_shape_refuses_naming_it() -> None:
    from medulla.generated import models

    if hasattr(models, "IdentityOpWireLoginGrant"):
        pytest.skip("the pin carries the wire login shapes")
    with pytest.raises(OperationRefused) as refused:
        await wire_access.handle_wire_access_grant(
            _context(_Secrets()),
            {"identity": "bi-tool", "principal_ref": "principal:one"},
            _OPS["wire_access.grant"],
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "IdentityOpWireLoginGrant"


@pytest.mark.spec("ARBORA-OPS-R858", "ARBORA-OPS-R858.1", "ARBORA-OPS-R858.2")
async def test_grant_returns_reference_not_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    for name in wire_access.WIRE_MODELS:
        monkeypatch.setattr(models, name, object, raising=False)
    engine = EngineDouble(monkeypatch)
    logins: dict[str, dict[str, Any]] = {}

    def identity(p: dict[str, Any]) -> Any:
        op = p["op"]
        assert op["family"] == "wire_login"
        if op["op"] == "grant":
            logins[op["request"]["login"]] = op["request"]
            return {"principal": op["request"]["principal"], "scopes": ["kg:read"]}
        logins.pop(op["request"]["login"], None)
        return {"login": op["request"]["login"], "revoked_ms": 42}

    engine.serve("Identity", identity)
    engine.install(pin_lag)
    secrets = _Secrets()
    ctx = _context(secrets)

    granted = await wire_access.handle_wire_access_grant(
        ctx,
        {"identity": "bi-tool", "principal_ref": "principal:one"},
        _OPS["wire_access.grant"],
    )
    value = granted["value"]
    password = secrets.values["wire_access/bi-tool"]
    assert value["secret_ref"] == "wire_access/bi-tool"
    assert password not in repr(granted)
    # The engine holds only the SCRAM salted credential, never the password.
    verifier = logins["bi-tool"]["verifier"]
    salt_hex, salted_hex = verifier.split(":")
    assert password not in verifier
    assert (
        salted_hex
        == hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), 4096, dklen=32
        ).hex()
    )
    assert logins["bi-tool"]["principal"] == "principal:one"

    revoked = await wire_access.handle_wire_access_revoke(
        ctx, {"identity": "bi-tool"}, _OPS["wire_access.revoke"]
    )
    assert revoked["value"]["revoked_ms"] == 42
    assert "bi-tool" not in logins and secrets.values == {}


@pytest.mark.spec("ARBORA-OPS-R858.1")
async def test_grant_refuses_when_the_secret_store_is_not_composed() -> None:
    with pytest.raises(OperationRefused) as refused:
        await wire_access.handle_wire_access_grant(
            _context(None),
            {"identity": "bi-tool", "principal_ref": "principal:one"},
            _OPS["wire_access.grant"],
        )
    assert refused.value.code == "UNAVAILABLE"
