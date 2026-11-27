"""A rejected pack import names every violation, even one the client cannot decode.

ARBORA-FLEET-R064.1: when the generated medulla client cannot decode the
engine's ConnectorPack import answer (an engine newer than the client), the
onboarding failure is read from the raw answer: it names
``engine_contract_mismatch`` and lists each violation as ``code:uri:detail``.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import TypeAdapter

from arbora import semantic_provisioning as sp

#: An engine answer the generated client refuses: the last code is not text.
RAW_REJECTION = {
    "result": "rejected",
    "pack_digest": "sha256:" + "a" * 64,
    "budget_exhausted": False,
    "violations": [
        {
            "code": "ONTOLOGY_ORPHAN",
            "uri": None,
            "detail": "1 class(es) have no rdfs:subClassOf path: c#Lonely",
        },
        {
            "code": "UNRESOLVED_REFERENCE",
            "uri": "ontology://demo-mcp/pack.ttl",
            "detail": "surface link names a component absent from the pack: c#tool-x",
        },
        {"code": 17, "uri": None, "detail": "a code this client cannot read"},
    ],
}


class _Engine:
    """Answers the pack import with the raw rejection; records every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def _send(
        self, method: str, params: Any = None, *args: Any, **kw: Any
    ) -> Any:
        self.calls.append(method)
        return RAW_REJECTION


class _Sink:
    """The SDK sink's import path: one ConnectorPack import, then generated decode."""

    def __init__(self, client: Any, _authority: Any) -> None:
        self._client = client

    async def import_pack(self, pack: Any, *, allow_mass_withdrawal: bool) -> Any:
        from medulla.generated.connector_pack import PackImportResult

        payload = await self._client._send(
            "ConnectorPack", {"op": {"op": "import", "request": {}}}, "g"
        )
        return TypeAdapter(PackImportResult).validate_python(payload)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R064.1")
async def test_an_undecodable_rejection_still_names_every_violation(
    monkeypatch,
) -> None:
    import scion_sdk.sinks.medulla as sinks

    async def binding(*_a: Any, **_k: Any) -> str:
        return "binding"

    async def owner(*_a: Any, **_k: Any) -> str:
        return "principal:sha256:" + "b" * 64

    monkeypatch.setattr(sp, "session_context", lambda _s: {"tenant": "t"})
    monkeypatch.setattr(sp, "attest_self_served_catalog", binding)
    monkeypatch.setattr(sp, "_owner_principal", owner)
    monkeypatch.setattr(sinks, "MedullaSink", _Sink)

    engine = _Engine()
    session = SimpleNamespace(graph="g", with_graph=lambda _g: None)
    with pytest.raises(sp.SemanticProvisioningError) as refused:
        await sp.import_attested_pack(
            None,
            session,
            client=engine,
            commons=engine,
            pack=SimpleNamespace(connector="demo-mcp"),
            action_policies=sp.ActionPolicies(),
            pack_import_authority=lambda *_a, **_k: None,
            bind_graph=lambda _g: contextlib.nullcontext(),
        )
    message = str(refused.value)
    assert "engine_contract_mismatch" in message
    assert (
        "ONTOLOGY_ORPHAN::1 class(es) have no rdfs:subClassOf path: c#Lonely" in message
    )
    assert (
        "UNRESOLVED_REFERENCE:ontology://demo-mcp/pack.ttl:surface link names a "
        "component absent from the pack: c#tool-x"
    ) in message
    assert "17::a code this client cannot read" in message
    assert engine.calls == ["ConnectorPack"]


@pytest.mark.spec("ARBORA-FLEET-R064.1")
def test_an_answer_that_is_no_rejection_is_not_reinterpreted() -> None:
    assert sp._undecodable_rejection({"result": "imported"}, "demo-mcp") is None
    assert sp._undecodable_rejection(None, "demo-mcp") is None
    assert sp._undecodable_rejection({"result": "rejected"}, "demo-mcp") is None
