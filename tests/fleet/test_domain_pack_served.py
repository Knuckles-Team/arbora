"""The domain pack read over each of the four domains (ARBORA-FLEET-R039.6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arbora.api.ops import domain_pack


@pytest.mark.spec("ARBORA-FLEET-R039.6")
@pytest.mark.asyncio
@pytest.mark.parametrize("domain", domain_pack.DOMAINS)
async def test_domain_pack_read_composes_engine_methods(monkeypatch, domain) -> None:
    seen: list[str] = []

    async def send(context, method, params, **_):
        seen.append(method)
        base = {"request_id": f"id-{method}"}
        if method == "ConnectorPack":
            return {**base, "digest": f"d-{domain}", "tenant_id": "t"}
        if method == "OntologyInspect":
            return {**base, "classes": [domain]}
        return {**base, "conforms": True}

    monkeypatch.setattr(domain_pack, "engine_send", send)
    context = SimpleNamespace(caller=SimpleNamespace(tenant="t"))
    out = await domain_pack.read_domain_pack(
        context, {"domain": domain, "version": "1.0.0"}, domain_pack.operations()[0]
    )
    assert seen == ["ConnectorPack", "OntologyInspect", "ShaclValidate"]
    assert out["value"]["pack_digest"] == f"d-{domain}"
