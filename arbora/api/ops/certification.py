"""The certification-mode run operation (ARBORA-OPS-R872).

``agents.certify`` is the one door of a certification run: it needs the scope
``admin:decision-eval`` (the engine scope of ``DecisionEval``) and never shares the normal invocation path
(``agents.run``, ``agents.plan_run``), which only admit a skill whose current attestation
the gate of ``arbora/api/invoke/certification_gate.py`` finds. The engine decides: it
validates the typed ``LocalCertificationRequest`` (``MEDULLA-DECISION-ENGINE-R142.100``)
and stores the attestation. Arbora stamps the verified tenant and the idempotency key and
forwards one ``DecisionEval`` ``certify_local_skill`` job. The request shape is an engine
``main`` shape the pinned release may lack, so the call goes through
:func:`arbora.pin_lag.medulla.send_main` and refuses ``UNAVAILABLE`` naming the shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_write_op
from arbora.api.registry import OpSpec, Verb
from arbora.pin_lag.medulla import send_main

SCOPE = "admin:decision-eval"


class AgentsCertifyParams(Params):
    #: The engine ``LocalCertificationRequest`` without the fields arbora stamps.
    request: dict[str, Any] = Field(min_length=1)


class AgentsCertifyResult(Params):
    value: dict[str, Any]


async def handle_agents_certify(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    stamped = {
        **params["request"],
        "tenant_id": str(context.caller.tenant),
        "idempotency_key": str(context.idempotency_key),
    }
    answer = await send_main(
        context,
        "DecisionEval",
        {"op": {"op": "certify_local_skill", "request": stamped}},
        idempotent=True,
        models=("LocalCertificationRequest",),
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


#: A certification replays under the engine job that holds the attestation, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {"agents.certify": "medulla:DecisionEval"}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="agents.certify",
            summary="Run one certification-mode run of a local skill and store its attestation",
            examples=("certify this skill",),
            params=AgentsCertifyParams,
            result=AgentsCertifyResult,
            handler=f"{__name__}.handle_agents_certify",
            scope=SCOPE,
            verb=Verb.ACT,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "SCOPE",
    "handle_agents_certify",
    "operations",
    "specs",
]
