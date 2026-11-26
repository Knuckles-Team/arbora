"""The scion-sdk capability guard (PIN-LAG: remove after the scion-sdk re-pin).

scion-sdk ``main`` serves the instance provisioner
(``scion_sdk.provisioning.provisioner``: ``Provisioner.allocate`` and
``Provisioner.reconcile``, ``SCION-CONNECTOR-CONTROL-R906``) that the pinned
release lacks. Every Arbora use of it goes through :func:`require_provisioning`, so
a pinned release without it refuses ``UNAVAILABLE`` naming the missing symbol, and
nothing falls back to a local allocator.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from arbora.pin_lag.unavailable import PinLagUnavailable

__all__ = ["PROVISIONER_SYMBOL", "require_provisioning"]

PROVISIONER_SYMBOL = "scion_sdk.provisioning.provisioner.Provisioner"
_MODULE = "scion_sdk.provisioning.provisioner"
_NEEDED = ("AllocationRequest", "Provisioner", "ProvisionError")


def require_provisioning() -> ModuleType:
    """The SDK provisioner module, or ``PinLagUnavailable`` naming the missing symbol."""
    try:
        module = importlib.import_module(_MODULE)
    except ImportError:
        raise PinLagUnavailable(PROVISIONER_SYMBOL) from None
    for name in _NEEDED:
        if not hasattr(module, name):
            raise PinLagUnavailable(f"{_MODULE}.{name}")
    return module
