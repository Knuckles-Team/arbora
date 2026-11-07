"""Readiness reports the OAuth client contract state (ARBORA-IDENTITY-R030.4.3)."""

from __future__ import annotations

import pytest

from arbora.identity.client_contract import (
    ClientContractReconciler,
    ContractEvent,
    ReconcileResult,
)
from arbora.observability import runtime_health


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.3")
def test_identity_check_follows_contract_state() -> None:
    assert runtime_health.identity_readiness() is None  # not configured
    machine = ClientContractReconciler()
    runtime_health.register_identity_contract(machine)
    try:
        assert runtime_health.identity_readiness() == {"status": "checking"}
        machine.apply(ContractEvent.RECONCILE_START)
        assert runtime_health.identity_readiness() == {"status": "checking"}
        machine.apply(
            ContractEvent.RECONCILE_ERR,
            result=ReconcileResult(False, "client_missing", ("client:x",)),
        )
        assert runtime_health.identity_readiness() == {
            "status": "degraded",
            "reason": "identity_client_missing",
        }
        machine.apply(ContractEvent.RETRY_ELAPSED)
        machine.apply(ContractEvent.RECONCILE_OK, result=ReconcileResult(True))
        assert runtime_health.identity_readiness() == {"status": "ready"}
        # Never gating: identity is not an essential readiness check.
        assert "identity" not in runtime_health.READINESS_ESSENTIAL
    finally:
        runtime_health.register_identity_contract(None)
