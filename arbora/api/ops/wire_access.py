"""Outside SQL client identities for the engine wire front end (ARBORA-OPS-R858).

``wire_access.grant`` issues one wire login for one outside identity. Arbora draws a
random password, derives the SCRAM salted credential and sends only that verifier to the
engine, which maps the login to one engine principal and applies its row security. The
cleartext goes to the platform secret store under ``wire_access/<identity>`` and the
caller gets the secret reference, never the password. ``wire_access.revoke`` marks the
login revoked in the engine and deletes the secret; new logins are refused within 60 s.
The login request shapes are engine ``main`` shapes the pinned release lacks, so both
operations refuse ``UNAVAILABLE`` naming the missing shape until the re-pin.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from arbora.api.ops._common import Params, bound_service, build_write_op
from arbora.api.registry import AuditClass, Confirm, Effect, OpSpec, Verb
from arbora.pin_lag.medulla import send_main

SCOPE = "identity:admin"
SECRET_PREFIX = "wire_access/"
#: The request shapes of the engine's wire-login family (engine ``main``).
WIRE_MODELS: tuple[str, ...] = ("IdentityOpWireLoginGrant", "IdentityOpWireLoginRevoke")
#: SCRAM-SHA-256 salted-password iteration count of the wire front end.
SCRAM_ITERATIONS = 4096
_IDENTITY = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@runtime_checkable
class SecretStore(Protocol):
    """The platform secret store, the one place a wire password is written."""

    async def put(self, path: str, value: str) -> None: ...

    async def delete(self, path: str) -> None: ...


class WireAccessGrantParams(Params):
    identity: str = _IDENTITY
    principal_ref: str = Field(min_length=1, max_length=512)


class WireAccessRevokeParams(Params):
    identity: str = _IDENTITY


class WireAccessResult(Params):
    value: dict[str, Any]


def scram_verifier(password: str, salt: bytes) -> str:
    """The salted credential ``<salt hex>:<salted password hex>`` the engine stores."""
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, SCRAM_ITERATIONS, dklen=32
    )
    return f"{salt.hex()}:{salted.hex()}"


def _identity_op(operation: str, request: dict[str, Any]) -> dict[str, Any]:
    return {"op": {"family": "wire_login", "op": operation, "request": request}}


async def handle_wire_access_grant(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Grant a login; the password reaches the secret store and nowhere else."""
    store: SecretStore = bound_service(
        context, "secret_store", reason="the secret store is not composed"
    )
    identity = str(params["identity"])
    password = secrets.token_urlsafe(32)
    verifier = scram_verifier(password, secrets.token_bytes(16))
    path = f"{SECRET_PREFIX}{identity}"
    request = {
        "kind": "Password",
        "login": identity,
        "principal": params["principal_ref"],
        "verifier": verifier,
    }
    answer = await send_main(
        context,
        "Identity",
        _identity_op("grant", request),
        idempotent=True,
        models=WIRE_MODELS,
    )
    await store.put(path, password)
    granted = dict(answer) if isinstance(answer, Mapping) else {}
    return {
        "value": {
            "identity": identity,
            "secret_ref": path,
            "principal": granted.get("principal", params["principal_ref"]),
            "scopes": granted.get("scopes", []),
        }
    }


async def handle_wire_access_revoke(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Revoke in the engine first, then delete the secret."""
    store: SecretStore = bound_service(
        context, "secret_store", reason="the secret store is not composed"
    )
    identity = str(params["identity"])
    answer = await send_main(
        context,
        "Identity",
        _identity_op("revoke", {"login": identity}),
        idempotent=True,
        models=WIRE_MODELS,
    )
    await store.delete(f"{SECRET_PREFIX}{identity}")
    revoked = dict(answer) if isinstance(answer, Mapping) else {}
    return {"value": {"identity": identity, "revoked_ms": revoked.get("revoked_ms")}}


#: Writes replay under the engine method that holds the login, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "wire_access.grant": "medulla:Identity",
    "wire_access.revoke": "medulla:Identity",
}


def operations() -> tuple[OpSpec, ...]:
    def admin(
        op_id: str, summary: str, example: str, params: type[Params], handler: str
    ) -> OpSpec:
        return build_write_op(
            verb=Verb.MANAGE,
            effect=Effect.ADMIN,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=WireAccessResult,
            handler=f"{__name__}.{handler}",
            scope=SCOPE,
        ).model_copy(
            update={"confirm": Confirm.CONSOLE, "audit": AuditClass.IDENTITY_CHAIN}
        )

    return (
        admin(
            "wire_access.grant",
            "Grant an outside identity a wire login under one engine principal",
            "give this client a SQL login",
            WireAccessGrantParams,
            "handle_wire_access_grant",
        ),
        admin(
            "wire_access.revoke",
            "Revoke an outside identity's wire login",
            "revoke this client's SQL login",
            WireAccessRevokeParams,
            "handle_wire_access_revoke",
        ),
    )


specs = operations
