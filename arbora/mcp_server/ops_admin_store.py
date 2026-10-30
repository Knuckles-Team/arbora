"""The served ``ops_admin_store`` (ARBORA-OPS-R024.5).

``ops.config`` and ``ops.tenants`` are Arbora's own configuration: they read
``config.json`` and ``tenants.json`` under :func:`arbora.core.settings.config_dir`
(``ARBORA_CONFIG_DIR`` wins, then ``~/.config/arbora``).  Credential-shaped keys
are redacted.  ``ops.doctor`` reports the engine's ``Health``; ``ops.backup`` and
``ops.restore`` call the engine's ``Backup``/``Restore`` methods under Arbora's
verified process identity.  The step-up, undelegated-human and audit
requirements stay on the operations themselves (``Effect.ADMIN``).

``tenants.json`` is ``{"tenants": [...], "admin_tenants": [...]}``.  A caller
whose tenant is in ``admin_tenants`` sees every configured tenant; any other
caller sees only its own tenant, and only when it is configured.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from arbora.api.ops.ops import (
    BackupReceipt,
    DoctorReport,
    OperationalConfig,
    RestoreReceipt,
    TenantPage,
)
from arbora.core.settings import config_dir, read_config_document

__all__ = ["ArboraOpsAdminStore"]

ClientFactory = Callable[[str], Awaitable[Any]]
REDACTED = "[redacted]"
_CONFIG_FILE = "config.json"
_TENANTS_FILE = "tenants.json"
_SECRET_MARKERS = ("SECRET", "PASSWORD", "TOKEN", "API_KEY", "PRIVATE_KEY", "DSN")


def _redacted(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: REDACTED if any(m in str(key).upper() for m in _SECRET_MARKERS) else value
        for key, value in document.items()
    }


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str | bytes):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {"result": decoded}
    return {"result": value}


class ArboraOpsAdminStore:
    """Arbora-owned config/tenants plus the engine's health, backup and restore."""

    def __init__(
        self,
        client: ClientFactory,
        *,
        graph: str,
        root: Callable[[], Path] = config_dir,
    ) -> None:
        self._client = client
        self._graph = graph
        self._root = root

    async def _send(
        self, tenant: str, method: str, params: dict[str, Any], key: str | None
    ) -> dict[str, Any]:
        from epistemic_graph.generated import send_by_id

        client = await self._client(tenant)
        return _as_dict(
            await send_by_id(client, method, params, self._graph, idempotency_key=key)
        )

    async def doctor(self, *, tenant: str) -> DoctorReport:
        health = await self._send(tenant, "Health", {}, None)
        root = self._root()
        status = str(health.get("status", "unknown"))
        return DoctorReport(
            tenant=tenant,
            status=status,
            checks={
                "engine": health,
                "config_dir": {"path": str(root), "present": root.is_dir()},
            },
        )

    async def get_config(self, *, tenant: str) -> OperationalConfig:
        path = self._root() / _CONFIG_FILE
        document = read_config_document(path) if path.is_file() else {}
        return OperationalConfig(tenant=tenant, settings=_redacted(document))

    def _tenants(self) -> tuple[list[str], set[str]]:
        path = self._root() / _TENANTS_FILE
        if not path.is_file():
            return [], set()
        document = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(document, dict):
            raise ValueError(f"{path} must hold a JSON object")
        tenants = sorted({str(t) for t in document.get("tenants", ())})
        admins = {str(t) for t in document.get("admin_tenants", ())}
        return tenants, admins

    async def list_tenants(
        self, *, tenant: str, cursor: str | None, limit: int
    ) -> TenantPage:
        configured, admins = self._tenants()
        visible = (
            configured if tenant in admins else [t for t in configured if t == tenant]
        )
        start = [t for t in visible if cursor is None or t > cursor]
        page = start[:limit]
        more = len(start) > limit
        return TenantPage(tenants=tuple(page), next_cursor=page[-1] if more else None)

    async def backup(
        self, *, tenant: str, destination: str, label: str | None, idempotency_key: str
    ) -> BackupReceipt:
        receipt = await self._send(
            tenant,
            "Backup",
            {"destination": destination, "label": label},
            idempotency_key,
        )
        return BackupReceipt(
            tenant=tenant,
            backup_id=destination,
            status="completed",
            engine_receipt=receipt,
        )

    async def restore(
        self, *, tenant: str, backup_id: str, target_shards: int, idempotency_key: str
    ) -> RestoreReceipt:
        receipt = await self._send(
            tenant,
            "Restore",
            {"source": backup_id, "target_shards": target_shards},
            idempotency_key,
        )
        return RestoreReceipt(
            tenant=tenant, backup_id=backup_id, status="staged", engine_receipt=receipt
        )
