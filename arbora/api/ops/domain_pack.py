"""Read one domain pack from the engine (ARBORA-FLEET-R039.1 to R039.5).

``domain.pack`` composes three engine reads, one per engine method, and returns
their answers unchanged: ``ConnectorPack`` (the pack and its digest),
``OntologyInspect`` (its classes and enumerations) and ``ShaclValidate`` (its
shapes verdict). Arbora adds no ranking, keeps no cache and holds no local
pack: an engine refusal reaches the caller with the engine's own code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec

DOMAINS = ("human-resources", "legal", "medical", "government")
Domain = Literal["human-resources", "legal", "medical", "government"]
_VERSION = r"^[0-9]+\.[0-9]+\.[0-9]+$"


class DomainPackRequest(_common.Params):
    domain: Domain
    version: str = Field(pattern=_VERSION)


class DomainPackResult(_common.Params):
    pack_digest: str = Field(min_length=1)
    classes: list[Any]
    enumerations: list[Any]
    shapes_verdict: dict[str, Any]
    request_ids: list[str] = Field(min_length=3, max_length=3)


def pack_id(domain: str, version: str) -> str:
    """The engine pack name of one domain release."""
    return f"domain.{domain}@{version}"


def _request_id(answer: Any) -> str:
    value = answer.get("request_id") if isinstance(answer, Mapping) else None
    if not isinstance(value, str) or not value:
        raise unavailable("the engine answer carries no request id")
    return value


async def read_domain_pack(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Three engine reads under the caller's tenant; engine refusals pass through."""
    request = DomainPackRequest.model_validate(dict(params))
    name = pack_id(request.domain, request.version)
    tenant = str(context.caller.tenant)
    pack = await engine_send(
        context,
        "ConnectorPack",
        {"op": {"op": "status", "request": {"connector": name, "tenant_id": tenant}}},
    )
    if isinstance(pack, Mapping) and pack.get("tenant_id", tenant) != tenant:
        raise OperationRefused("TENANT_DENIED", {})
    inspected = await engine_send(context, "OntologyInspect", {"source_ids": [name]})
    verdict = await engine_send(context, "ShaclValidate", {"shapes": name})
    try:
        result = DomainPackResult(
            pack_digest=pack["digest"],
            classes=list(inspected["classes"]),
            enumerations=list(inspected.get("enumerations", [])),
            shapes_verdict={k: v for k, v in verdict.items() if k != "request_id"},
            request_ids=[
                _request_id(pack),
                _request_id(inspected),
                _request_id(verdict),
            ],
        )
    except (KeyError, TypeError, AttributeError, ValidationError):
        raise unavailable("the engine domain pack answer is incomplete") from None
    return {"value": result.model_dump(mode="json")}


class DomainPackReply(_common.Params):
    value: DomainPackResult


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_read_op(
            op_id="domain.pack",
            summary="Read a domain pack's classes, enumerations and shapes verdict",
            examples=("read the legal domain pack",),
            params=DomainPackRequest,
            result=DomainPackReply,
            handler="arbora.api.ops.domain_pack.read_domain_pack",
            scope="validation:read",
        ),
    )


specs = operations

__all__ = [
    "DOMAINS",
    "DomainPackReply",
    "DomainPackRequest",
    "DomainPackResult",
    "operations",
    "read_domain_pack",
    "specs",
]
