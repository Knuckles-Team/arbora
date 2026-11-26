"""The mode transition operation (ARBORA-IDENTITY-R014.1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import identity_mode
from arbora.api.registry import Confirm, Effect
from arbora.identity.engine_admin import IdentityRefused


class _Port:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.epoch = 3

    async def identity(self, client, claims, family, op, request, *, expect):
        self.requests.append((family, op, request))
        if op == "get":
            return {"mode": "local", "epoch": self.epoch, "issuer_kid_current": "k1"}
        if request["expected_epoch"] != self.epoch:
            raise IdentityRefused("IDENTITY_EPOCH_CONFLICT")
        return {"mode": request["to"], "epoch": self.epoch + 1}


def _context(port: _Port) -> SimpleNamespace:
    caller = SimpleNamespace(engine_claims={})
    return SimpleNamespace(
        caller=caller, client=None, services={"identity_admin": port}
    )


@pytest.mark.spec("ARBORA-IDENTITY-R014.1")
def test_transition_round_trip_over_http_and_mcp() -> None:
    (spec,) = identity_mode.operations()
    assert (
        spec.effect is Effect.ADMIN and spec.confirm is Confirm.PLAN
    )  # preview, approval
    assert {s.value for s in spec.surfaces} >= {"http", "mcp"}
    port = _Port()
    done = asyncio.run(
        identity_mode.handle_mode_transition(
            _context(port), {"to": "external", "expected_epoch": 3}, spec
        )
    )
    assert done == {"value": {"mode": "external", "epoch": 4}}
    assert port.requests[-1][2] == {
        "to": "external",
        "expected_epoch": 3,
        "issuer_kid": "k1",
    }
    with pytest.raises(
        OperationRefused
    ) as refused:  # the engine decides; its code passes
        asyncio.run(
            identity_mode.handle_mode_transition(
                _context(port), {"to": "external", "expected_epoch": 1}, spec
            )
        )
    assert refused.value.code == "IDENTITY_EPOCH_CONFLICT"
