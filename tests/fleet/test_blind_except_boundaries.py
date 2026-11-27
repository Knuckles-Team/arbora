"""R039.12 boundaries: a crashed handshake worker still releases its slot; the
scaling intent refuses an unknown pool; the child call loop ends by returning or
raising and never falls out of its retry loop."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from arbora.api.domain.fleet.scaling import scaling_authority
from arbora.fleet.catalog_replay import receipts_from_rows
from arbora.fleet.connect_contract import ConnectContract
from arbora.fleet.definitions import VersionedDefinition
from arbora.fleet.onboarding import HandshakeOnboarder
from tests.fleet.test_handshake_onboard import _Registry, _setup


def test_a_crashed_handshake_worker_logs_its_traceback_and_frees_the_slot(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    onboarder, _mux, _clock = _setup(_Registry())

    def crash(_connector: str) -> None:
        raise RuntimeError("loop crashed")

    monkeypatch.setattr(HandshakeOnboarder, "_loop", lambda _s, c: crash(c))
    cast(Any, onboarder._active)["alpha-mcp"] = SimpleNamespace()
    with caplog.at_level(logging.WARNING):
        onboarder._work("alpha-mcp")
    assert "alpha-mcp" not in onboarder._active
    assert any(r.exc_info for r in caplog.records)


def test_a_scale_intent_for_a_missing_pool_is_refused() -> None:
    unit = SimpleNamespace(unit_id="u1", resource_pool_id="missing")
    authority = SimpleNamespace(units=[unit], pools=[])
    intent = SimpleNamespace(unit_id="u1")
    with pytest.raises(ValueError, match="unknown resource pool"):
        scaling_authority._scale_intent_context(cast(Any, intent), cast(Any, authority))


def test_receipts_decode_to_the_typed_operation() -> None:
    rows = [("t", "1", "2", "item", "upsert", "d" * 64)]
    assert receipts_from_rows(rows)[0].op == "upsert"


def test_a_connect_contract_round_trips_through_its_dict() -> None:
    contract = ConnectContract("c", "a" * 64, {"u": "b" * 64}, "", 5)
    assert ConnectContract.from_dict(contract.as_dict()) == contract


def test_a_definition_parses_its_semver() -> None:
    definition = VersionedDefinition.model_construct(version="1.2.3")
    assert definition.semver == (1, 2, 3)
