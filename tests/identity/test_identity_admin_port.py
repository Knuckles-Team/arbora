"""The engine-backed identity administration port (ARBORA-IDENTITY-R018.1)."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest

from arbora.identity.composition import build_engine_identity_ports
from arbora.identity.engine import IdentityUnavailable
from arbora.identity.engine_admin import EngineIdentityAdmin, IdentityRefused
from tests._eg_fakes import FakeEgEngine

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R018.1")


class _Engine(FakeEgEngine):
    def __init__(self, reply: Any) -> None:
        super().__init__()
        self.reply = reply

    def use_verified_context(self, claims: Any) -> Any:
        self.claims = claims
        return nullcontext()

    def _on_Identity(self, params: Any, key: Any) -> Any:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


async def test_one_request_is_sent_and_the_expected_reply_value_returned() -> None:
    engine = _Engine({"kind": "done", "value": {"changed": True}})
    port = EngineIdentityAdmin(graph="g")
    value = await port.identity(
        engine, {}, "user", "unlock", {"id": "u1"}, expect="done"
    )
    assert value == {"changed": True}
    name, graph, params = engine.calls[0]
    assert (name, graph) == ("Identity", "g")
    assert engine.claims == {}
    assert params["op"] == {"family": "user", "op": "unlock", "request": {"id": "u1"}}


async def test_a_reply_of_another_kind_is_unavailable() -> None:
    engine = _Engine({"kind": "done", "value": {"changed": True}})
    with pytest.raises(IdentityUnavailable):
        await EngineIdentityAdmin(graph="g").identity(
            engine, {}, "user", "unlock", {"id": "u1"}, expect="user"
        )


async def test_an_engine_identity_refusal_keeps_its_wire_code() -> None:
    from medulla.client import EngineResponseError

    error = EngineResponseError("IDENTITY_NOT_FOUND", "no such user")
    with pytest.raises(IdentityRefused) as refused:
        await EngineIdentityAdmin(graph="g").identity(
            _Engine(error), {}, "user", "get", {"id": "x"}, expect="user"
        )
    assert refused.value.code == "IDENTITY_NOT_FOUND"


def test_the_composition_root_exposes_the_admin_port_beside_the_session_ports() -> None:
    ports = build_engine_identity_ports(_Engine(None), graph="g")
    assert isinstance(ports.admin, EngineIdentityAdmin)
    assert ports.sessions is ports.credentials
