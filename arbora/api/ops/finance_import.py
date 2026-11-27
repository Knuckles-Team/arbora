"""CSV import preview and commit (ARBORA-DATA-MARKET-R009.5 and R009.6).

``finance.import.preview`` parses an uploaded file with the SDK importer, writes nothing
to the ledger and keeps the preview as one engine node. ``finance.import.commit``
imports the previewed file once per file digest: a second commit returns the first
receipt with ``imported: 0``. Arbora holds no parser and no durable store. The file
bytes come from the ``finance_import_source`` port and the activities go to the
``finance_activity_sink`` port; an unbound port refuses ``UNAVAILABLE``. The SDK preview
that reports issues (``CsvIssue``) is not in the pinned release, so the preview refuses
``UNAVAILABLE`` naming it (PIN-LAG).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import msgpack
from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, node_rows
from arbora.api.ops.market_detail import _verified
from arbora.api.registry import OpSpec
from arbora.pin_lag.scion_sdk import PinLagUnavailable, require_csv_preview

SCOPE = "finance:track"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "finance.import.commit": "medulla:CreateNodeIfAbsent",
}


class ImportPreviewParams(Params):
    artifact: str = Field(min_length=1, max_length=512)
    file_digest: str = Field(pattern=_DIGEST)
    mapping: dict[str, str] = Field(min_length=1, max_length=256)


class ImportCommitParams(Params):
    preview_id: str = Field(pattern=_DIGEST)
    account: str = Field(min_length=1, max_length=256)


class ImportResult(Params):
    value: dict[str, Any]


def preview_node_id(tenant: str, preview_id: str) -> str:
    return f"finance-import-preview:{tenant}:{preview_id}"


def receipt_node_id(tenant: str, file_digest: str) -> str:
    return f"finance-import:{tenant}:{file_digest}"


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


def _preview_id(file_digest: str, mapping: Mapping[str, str]) -> str:
    body = json.dumps([file_digest, sorted(mapping.items())]).encode()
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


async def _node(context: Any, node_id: str, tenant: str) -> dict[str, Any] | None:
    answer = await engine_send(
        context, "GetNodePropertiesBatch", {"node_ids": [node_id]}
    )
    for found, props in node_rows(answer):
        if found == node_id and props.get("tenant_id") == tenant:
            return props
    return None


def _issues(issues: Any) -> list[dict[str, Any]]:
    """Issues as codes and positions only; a cell's content never leaves the SDK."""
    return [
        {"row": i.row, "column": i.column, "code": str(i.code)} for i in issues or ()
    ]


async def _content(context: Any, params: Mapping[str, Any]) -> bytes:
    source = bound_service(
        context, "finance_import_source", reason="the import source is not composed"
    )
    return bytes(await source.read(context, params["artifact"]))


async def handle_import_preview(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant = str(context.caller.tenant)
    try:
        csv = require_csv_preview()
    except PinLagUnavailable as missing:
        raise OperationRefused("UNAVAILABLE", {"missing": missing.missing}) from missing
    content = await _content(context, params)
    mapping = csv.ColumnMapping(header_to_field=dict(params["mapping"]))
    preview = csv.preview_csv_mapping(content, mapping)
    if preview.file_digest != params["file_digest"]:
        raise EngineRefusal("DIGEST_MISMATCH")
    errors = {
        "mapping_errors": _issues(preview.mapping_errors),
        "row_errors": _issues(preview.row_errors),
    }
    preview_id = _preview_id(preview.file_digest, params["mapping"])
    await engine_send(
        context,
        "CreateNodeIfAbsent",
        {
            "node_id": preview_node_id(tenant, preview_id),
            "properties_msgpack": _pack(
                {
                    "tenant_id": tenant,
                    "type": "FinanceImportPreview",
                    "artifact": params["artifact"],
                    "file_digest": preview.file_digest,
                    "mapping": dict(params["mapping"]),
                    **errors,
                }
            ),
        },
    )
    reimport = await _node(
        context, receipt_node_id(tenant, preview.file_digest), tenant
    )
    return {
        "value": {
            "preview_id": preview_id,
            "file_digest": preview.file_digest,
            "rows_total": preview.rows_total,
            "rows_ok": preview.rows_ok,
            **errors,
            "reimport": reimport is not None,
        }
    }


def _first_receipt(held: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "receipt_id": held["receipt_id"],
        "file_digest": held["file_digest"],
        "imported": 0,
        "skipped_duplicate": int(held["imported"]),
    }


async def handle_import_commit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant = str(context.caller.tenant)
    account, _send = await _verified(context)
    if params["account"] != account:
        raise EngineRefusal("ACCOUNT_BINDING_UNVERIFIED")
    stored = await _node(context, preview_node_id(tenant, params["preview_id"]), tenant)
    if stored is None:
        raise EngineRefusal("PREVIEW_NOT_FOUND")
    if stored["mapping_errors"] or stored["row_errors"]:
        raise EngineRefusal("PREVIEW_HAS_ERRORS")
    digest = stored["file_digest"]
    first = await _node(context, receipt_node_id(tenant, digest), tenant)
    if first is not None:
        return {"value": _first_receipt(first)}
    try:
        csv = require_csv_preview()
    except PinLagUnavailable as missing:
        raise OperationRefused("UNAVAILABLE", {"missing": missing.missing}) from missing
    sink = bound_service(
        context, "finance_activity_sink", reason="the activity sink is not composed"
    )
    content = await _content(context, stored)
    activities = csv.import_activities(
        content,
        csv.ColumnMapping(header_to_field=dict(stored["mapping"])),
        csv.AccountContext(
            tenant_id=tenant,
            source_id=f"csv:{digest}",
            provider="csv",
            instrument_id="csv",
            account_id=account,
            license_tag="user-import",
        ),
    )
    imported = int(await sink.ingest(context, activities))
    receipt = hashlib.sha256(f"{tenant}:{digest}".encode()).hexdigest()
    await engine_send(
        context,
        "CreateNodeIfAbsent",
        {
            "node_id": receipt_node_id(tenant, digest),
            "properties_msgpack": _pack(
                {
                    "tenant_id": tenant,
                    "type": "FinanceImportReceipt",
                    "receipt_id": receipt,
                    "file_digest": digest,
                    "imported": imported,
                }
            ),
        },
    )
    return {
        "value": {
            "receipt_id": receipt,
            "file_digest": digest,
            "imported": imported,
            "skipped_duplicate": 0,
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="finance.import.preview",
            summary="Preview a CSV import: mapping and row problems, nothing written",
            examples=("preview this brokerage export",),
            params=ImportPreviewParams,
            result=ImportResult,
            handler="arbora.api.ops.finance_import.handle_import_preview",
            scope=SCOPE,
        ),
        build_write_op(
            op_id="finance.import.commit",
            summary="Commit a previewed CSV import once per file digest",
            examples=("import the previewed file",),
            params=ImportCommitParams,
            result=ImportResult,
            handler="arbora.api.ops.finance_import.handle_import_commit",
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_import_commit",
    "handle_import_preview",
    "operations",
    "specs",
]
