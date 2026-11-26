"""Both pin-lag guards raise the one PinLagUnavailable (PIN-LAG unification)."""

from __future__ import annotations

import pytest

from arbora.pin_lag import medulla, meristem
from arbora.pin_lag.unavailable import PinLagUnavailable


def test_the_guards_share_one_refusal_type() -> None:
    assert medulla.PinLagUnavailable is PinLagUnavailable
    assert meristem.PinLagUnavailable is PinLagUnavailable
    with pytest.raises(PinLagUnavailable) as engine:
        medulla.require_methods("NoSuchMethodAnywhere")
    assert engine.value.missing == "NoSuchMethodAnywhere"
    assert engine.value.code == "UNAVAILABLE"
