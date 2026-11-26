"""Shared fixtures for the repository write-back tests (ARBORA-FLEET-R003)."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from typing import Any

from scion_sdk.testing.writeback import make_writeback_fixture
from scion_sdk.writeback import (
    RepositorySourceEffectIntent,
    RepositorySourceEffectReceipt,
    RepositorySourceEffectRequest,
)

from arbora.api.ops.write_back import WriteBackBinding, WriteBackBindings

PAYLOAD = b"\x00hello\n"


def intent_fields() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository_id": "repository-1",
        "target_ref": "refs/heads/main",
        "relative_path": "nested/file.bin",
        "base_revision": "a" * 40,
        "expected_file_digest": "b" * 64,
        "expected_executable": False,
        "action": "update",
        "payload_base64": base64.b64encode(PAYLOAD).decode(),
        "payload_sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "payload_size": len(PAYLOAD),
        "executable": False,
        "adapter_contract_digest": "c" * 64,
    }


def make_request() -> RepositorySourceEffectRequest:
    intent = RepositorySourceEffectIntent.model_validate(intent_fields())
    change = make_writeback_fixture().change_set.model_copy(
        update={
            "entity_id": intent.repository_id,
            "base_source_version": intent.base_revision,
            "desired_patch": {"repository_effect": intent.model_dump(mode="json")},
            "field_scope": ["repository_effect"],
            "field_provenance": {"repository_effect": "fixture"},
        }
    )
    change = change.model_copy(update={"change_set_digest": change.canonical_digest()})
    return RepositorySourceEffectRequest(
        schema_version=1, change_set=change, intent=intent
    )


def binding_for(request: RepositorySourceEffectRequest, **kw: Any) -> WriteBackBinding:
    change = request.change_set
    fields: dict[str, Any] = {
        "connector_id": change.connector_id,
        "source_instance_id": change.source_instance_id,
        "repository_id": request.intent.repository_id,
        "server": "repository-connector",
        "tool": "apply_repository_effect",
        "max_payload_size": 1024,
    }
    fields.update(kw)
    return WriteBackBinding(**fields)


def receipt_for(
    request: RepositorySourceEffectRequest, **override: Any
) -> dict[str, Any]:
    change = request.change_set
    intent = request.intent
    fields: dict[str, Any] = {
        "schema_version": 1,
        "tenant_id": change.tenant_id,
        "actor": change.actor,
        "connector_id": change.connector_id,
        "source_instance_id": change.source_instance_id,
        "repository_id": intent.repository_id,
        "target_ref": intent.target_ref,
        "relative_path": intent.relative_path,
        "base_revision": intent.base_revision,
        "change_set_id": change.change_set_id,
        "change_set_digest": change.change_set_digest,
        "idempotency_key": change.idempotency_key,
        "approval_ref": change.authorization.authorization_ref,
        "approval_decision_digest": change.authorization.decision_digest,
        "audit_reservation_id": "audit-1",
        "adapter_contract_digest": intent.adapter_contract_digest,
        "effect_revision": "e" * 40,
        "readback_sha256": intent.payload_sha256,
        "readback_size": intent.payload_size,
        "executable": intent.executable,
    }
    fields.update(override)
    return RepositorySourceEffectReceipt.model_validate(fields).model_dump(mode="json")


class FakeGateway:
    """Records every dispatch to a connector tool."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._result = result
        self._error = error

    async def call(
        self, caller: Any, server: str, tool: str, arguments: Any, **kw: Any
    ) -> Any:
        self.calls.append((server, tool, dict(arguments)))
        if self._error is not None:
            raise self._error
        return self._result


def context(
    *,
    bindings: WriteBackBindings | None,
    gateway: FakeGateway | None,
    key: str | None = "plan:approved-1",
) -> SimpleNamespace:
    services: dict[str, Any] = {}
    if bindings is not None:
        services["write_back_bindings"] = bindings
    if gateway is not None:
        services["fleet_gateway"] = gateway
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant-a", principal="caller-a"),
        services=services,
        idempotency_key=key,
        service_identity=False,
        owner=None,
        owner_ref=None,
        fleet_decision=None,
        registry_digest="",
    )


def params(request: RepositorySourceEffectRequest, binding: WriteBackBinding) -> dict:
    return {
        "request": request.model_dump(mode="json"),
        "binding_digest": binding.digest(),
    }
