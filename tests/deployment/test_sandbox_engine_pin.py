"""The sandbox engine is the version the package pins (ARBORA-DEPLOY-R019.2)."""

from __future__ import annotations

import pytest

from arbora.deployment.sandbox import resolve_engine
from arbora.identity.sandbox_principal import SandboxPrincipalRefusal


@pytest.mark.spec("ARBORA-DEPLOY-R019.2")
def test_sandbox_refuses_an_engine_that_differs_from_the_lock() -> None:
    assert resolve_engine(pinned="3.3.5", installed="3.3.5") == "3.3.5"
    with pytest.raises(SandboxPrincipalRefusal) as refused:
        resolve_engine(pinned="3.3.5", installed="3.3.3")
    assert refused.value.code == "SANDBOX_ENGINE_VERSION_MISMATCH"
    assert "3.3.3" in str(refused.value) and "3.3.5" in str(refused.value)
    # No pin at all is a refusal too: a different engine never starts silently.
    with pytest.raises(SandboxPrincipalRefusal):
        resolve_engine(pinned="", installed="3.3.5")
