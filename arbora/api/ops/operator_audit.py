"""The operator-only audit read (ARBORA-OPS-R052.5).

A count of withheld projections is evidence for the operator alone. The
``query.projections`` handler writes it to the separately authorized chain through
the ``operator_audit`` port; this operation reads it back for a caller holding
``audit:operator``. No response, trace or caller-readable audit view carries it.
PIN-LAG: the engine publishes no operator-only audit chain yet, so the port is not
composed by default and the read refuses ``UNAVAILABLE`` until it is.
"""

from __future__ import annotations

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
    "OperatorAuditReadParams",
    "handle_operator_audit_read",
    "operations",
    "specs",
]
