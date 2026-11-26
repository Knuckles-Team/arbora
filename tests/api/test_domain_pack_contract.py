"""The domain pack read composes three engine reads (ARBORA-FLEET-R039.1 to R039.5)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import domain_pack
from arbora.api.registry import Verb

TENANT = "tenant:one"


def _engine(monkeypatch: pytest.MonkeyPatch, *, tenant: str = TENANT, fail=None):
    sent: list[tuple[str, dict[str, Any]]] = []
    answers = {
        "ConnectorPack": {
            "head": {"pack_digest": "sha256:abc", "record_id": "rec-1"},
            "tenant_id": tenant,
        },
        "OntologyInspect": {
            "classes": ["Employee"],
            "enumerations": ["Grade"],
            "composed_digest": "cd-2",
        },
        "ShaclValidate": {"conforms": True, "composed_digest": "cd-3"},
    }

    async def send(context, method, params, **_):
        sent.append((method, params))
        if fail == method:
            raise EngineRefusal("PACK_NOT_FOUND")
        return answers[method]

    monkeypatch.setattr(domain_pack, "engine_send", send)
    return sent, answers


def _context() -> Any:
    return SimpleNamespace(caller=SimpleNamespace(tenant=TENANT))


def _op() -> Any:
    return domain_pack.operations()[0]


@pytest.mark.spec("ARBORA-FLEET-R039.1")
def test_request_and_result_fields() -> None:
    assert set(domain_pack.DomainPackRequest.model_fields) == {"domain", "version"}
    assert set(domain_pack.DomainPackResult.model_fields) == {
        "pack_digest",
        "classes",
        "enumerations",
        "shapes_verdict",
        "evidence_ids",
    }
    with pytest.raises(ValidationError):
        domain_pack.DomainPackRequest(domain="legal", version="1.0.0", extra="x")
    with pytest.raises(ValidationError):
        domain_pack.DomainPackRequest(domain="retail", version="1.0.0")


@pytest.mark.spec("ARBORA-FLEET-R039.2")
@pytest.mark.asyncio
async def test_result_equals_three_engine_responses(monkeypatch) -> None:
    sent, answers = _engine(monkeypatch)
    out = await domain_pack.read_domain_pack(
        _context(), {"domain": "legal", "version": "1.2.0"}, _op()
    )
    value = out["value"]
    assert [m for m, _ in sent] == ["ConnectorPack", "OntologyInspect", "ShaclValidate"]
    assert value["classes"] == answers["OntologyInspect"]["classes"]
    assert value["enumerations"] == answers["OntologyInspect"]["enumerations"]
    assert value["shapes_verdict"] == answers["ShaclValidate"]
    assert value["pack_digest"] == "sha256:abc"


@pytest.mark.spec("ARBORA-FLEET-R039.3")
@pytest.mark.asyncio
async def test_served_tool_reaches_three_engine_methods(monkeypatch) -> None:
    from arbora.api.ops.registry_factory import get_registry

    spec = get_registry().get("domain.pack")
    assert spec is not None
    assert spec.verb is Verb.ASK and "validation:read" in {str(s) for s in spec.scopes}
    sent, _ = _engine(monkeypatch)
    await domain_pack.read_domain_pack(
        _context(), {"domain": "medical", "version": "1.0.0"}, spec
    )
    packs = [p for m, p in sent if m == "ConnectorPack"]
    assert packs[0]["op"]["request"]["tenant_id"] == TENANT
    assert len(sent) == 3


@pytest.mark.spec("ARBORA-FLEET-R039.4")
@pytest.mark.asyncio
async def test_refusals_carry_stable_codes(monkeypatch) -> None:
    _engine(monkeypatch, fail="ConnectorPack")
    with pytest.raises(EngineRefusal) as refused:
        await domain_pack.read_domain_pack(
            _context(), {"domain": "legal", "version": "9.9.9"}, _op()
        )
    assert refused.value.code == "PACK_NOT_FOUND"
    _engine(monkeypatch, tenant="tenant:other")
    with pytest.raises(OperationRefused) as denied:
        await domain_pack.read_domain_pack(
            _context(), {"domain": "legal", "version": "1.0.0"}, _op()
        )
    assert denied.value.code == "TENANT_DENIED"


@pytest.mark.spec("ARBORA-FLEET-R039.5")
@pytest.mark.asyncio
async def test_result_carries_digest_and_request_ids(monkeypatch) -> None:
    _, answers = _engine(monkeypatch)
    out = await domain_pack.read_domain_pack(
        _context(), {"domain": "government", "version": "1.0.0"}, _op()
    )
    assert out["value"]["evidence_ids"] == ["rec-1", "cd-2", "cd-3"]
    answers["ShaclValidate"].pop("composed_digest")
    with pytest.raises(OperationRefused) as refused:
        await domain_pack.read_domain_pack(
            _context(), {"domain": "government", "version": "1.0.0"}, _op()
        )
    assert refused.value.code == "UNAVAILABLE"
