"""The operator-only audit read (ARBORA-OPS-R052.5).

A count of withheld projections is evidence for the operator alone. The
``query.projections`` handler writes it to the separately authorized chain through
the ``operator_audit`` port; this operation reads it back for a caller holding
``audit:operator``. No response, trace or caller-readable audit view carries it.
PIN-LAG: the engine publishes the operator-only chain (``AuditAppend`` class
``operator`` and ``AuditList``) on ``main`` only. :class:`EngineOperatorAudit` is always composed; until the pinned
release carries the chain it records nothing, the read refuses ``UNAVAILABLE`` naming
the missing method, and ``ops.doctor`` reports ``operator_audit: inactive``. The port
goes live with the re-pin that carries the chain, with no further change here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.registry import OpSpec, Verb

SCOPE = "audit:operator"


class OperatorAuditReadParams(Params):
    request_id: str = Field(min_length=1, max_length=128)


class OperatorAuditReadResult(Params):
    value: dict[str, Any]


async def handle_operator_audit_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return the operator record of one request, or refuse without disclosing others."""
    port = bound_service(
        context, "operator_audit", reason="operator audit is not composed"
    )
    record = await port.read(context, request_id=params["request_id"])
    if record is None:
        raise OperationRefused("NOT_FOUND")
    return {"value": dict(record)}


class EngineOperatorAudit:
    """The ``operator_audit`` port over the engine's operator-only audit chain.

    The write is an ``AuditAppend`` of class ``operator`` holding the request id and
    the withheld count; the read is ``AuditList`` on the operator chain by request id
    (scope ``audit:operator``). Both go as the verified caller, so the engine's ``audit:operator`` scope check
    decides who may read. Arbora keeps no copy.
    """

    async def record_withheld(
        self, context: Any, *, withheld: int, request_id: str
    ) -> None:
        from arbora.api.ops._engine import engine_send
        from arbora.pin_lag.medulla import operator_audit_missing

        if operator_audit_missing() is not None:
            return
        await engine_send(
            context,
            "AuditAppend",
            {
                "audit_class": "operator",
                "op": "query.projections",
                "surface": context.surface or "http",
                "params_sha256": hashlib.sha256(request_id.encode()).hexdigest(),
                "request_id": request_id,
                "status": "ok",
                "evidence": {"withheld_count": str(int(withheld))},
            },
        )

    async def read(self, context: Any, *, request_id: str) -> Mapping[str, Any] | None:
        from arbora.api.ops._engine import engine_send
        from arbora.pin_lag.medulla import (
            PinLagUnavailable,
            operator_audit_missing,
            refusal,
        )

        missing = operator_audit_missing()
        if missing is not None:
            raise refusal(PinLagUnavailable(missing))
        answer = await engine_send(
            context,
            "AuditList",
            {"chain": "operator", "request_id": request_id, "limit": 1},
        )
        if hasattr(answer, "model_dump"):
            answer = answer.model_dump(mode="json")
        records = answer.get("records") if isinstance(answer, Mapping) else None
        if not records:
            return None
        return {"request_id": request_id, **dict(records[0])}


def operator_audit_check() -> str:
    """The ``ops.doctor`` line of the operator audit chain (PIN-LAG, never silent)."""
    from arbora.pin_lag.medulla import operator_audit_missing

    missing = operator_audit_missing()
    if missing is None:
        return "operator_audit: active"
    return f"operator_audit: inactive (engine lacks {missing})"


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.ASK,
            op_id="audit.operator.read",
            summary="Read one operator-only audit record",
            examples=("read the operator audit record of this request",),
            params=OperatorAuditReadParams,
            result=OperatorAuditReadResult,
            handler="arbora.api.ops.operator_audit.handle_operator_audit_read",
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = [
    "EngineOperatorAudit",
    "operator_audit_check",
    "OperatorAuditReadParams",
    "handle_operator_audit_read",
    "operations",
    "specs",
]
