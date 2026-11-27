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

__all__ = [
    "CSV_PREVIEW_SYMBOL",
    "WALLET_INTENT_SYMBOL",
    "WALLET_TRUST_SYMBOL",
    "require_wallet_trust",
    "require_wallet_intent",
    "FORECAST_SYMBOLS",
    "require_csv_preview",
    "require_forecast",
    "BINDING_SYMBOL",
    "DESKTOP_SYMBOL",
    "PROVISIONER_SYMBOL",
    "SCM_LISTING_SYMBOL",
    "require_credential_binding",
    "require_desktop",
    "require_environment_declarations",
    "require_scm_event_listing",
    "require_provisioning",
]

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


BINDING_SYMBOL = "scion_sdk.credentials.binding.CredentialAccessBinding"
_BINDING_MODULE = "scion_sdk.credentials.binding"
_BINDING_NEEDED = (
    "CredentialAccessBinding",
    "binding_digest",
    "use_credential_binding",
)


def require_credential_binding() -> ModuleType:
    """The SDK credential-binding module, or ``PinLagUnavailable`` naming the missing symbol.

    scion-sdk ``main`` serves ``CredentialAccessBinding`` and ``CredentialAuthorityPort``
    (``SCION-CONNECTOR-CONTROL-R035.1``); the pinned release lacks them.
    """
    try:
        module = importlib.import_module(_BINDING_MODULE)
    except ImportError:
        raise PinLagUnavailable(BINDING_SYMBOL) from None
    for name in _BINDING_NEEDED:
        if not hasattr(module, name):
            raise PinLagUnavailable(f"{_BINDING_MODULE}.{name}")
    return module


SCM_LISTING_SYMBOL = "scion_sdk.scm.events.ScmEventLister"
_SCM_EVENTS_MODULE = "scion_sdk.scm.events"


def require_scm_event_listing() -> ModuleType:
    """The SDK code-host event listing contract, or ``PinLagUnavailable`` naming the symbol.

    scion-sdk ``main`` is to serve the read tool ``<prefix>_list_scm_events`` that the polling
    fallback of ``ARBORA-OPS-R896.8`` calls (``ScmEventLister``); the pinned release lacks it.
    """
    try:
        module = importlib.import_module(_SCM_EVENTS_MODULE)
    except ImportError:
        raise PinLagUnavailable(SCM_LISTING_SYMBOL) from None
    if not hasattr(module, "ScmEventLister"):
        raise PinLagUnavailable(SCM_LISTING_SYMBOL)
    return module


DESKTOP_SYMBOL = "scion_sdk.desktop.DesktopProvider"


def require_desktop() -> ModuleType:
    """The SDK native desktop port (``SCION-CONNECTOR-CONTROL-R909``), or refuse.

    A pinned release without it leaves ``browser.desktop_action`` unavailable,
    naming the symbol; no local desktop driver stands in.
    """
    try:
        module = importlib.import_module("scion_sdk.desktop")
    except ImportError:
        raise PinLagUnavailable(DESKTOP_SYMBOL) from None
    for name in (
        "DesktopAction",
        "DesktopActionKind",
        "DesktopGrant",
        "DesktopProvider",
    ):
        if not hasattr(module, name):
            raise PinLagUnavailable(f"scion_sdk.desktop.{name}")
    return module


ENVIRONMENT_SYMBOL = "scion_sdk.environment.environment_declarations"


def require_environment_declarations() -> ModuleType:
    """The SDK environment export (``SCION-CONNECTOR-CONTROL-R908``), or refuse.

    A pinned release without it leaves the host environment manifest unavailable,
    naming the symbol; Arbora copies none of the SDK's names to stand in.
    """
    try:
        module = importlib.import_module("scion_sdk.environment")
    except ImportError:
        raise PinLagUnavailable(ENVIRONMENT_SYMBOL) from None
    if not hasattr(module, "environment_declarations"):
        raise PinLagUnavailable(ENVIRONMENT_SYMBOL)
    return module


CSV_PREVIEW_SYMBOL = "scion_sdk.finance.csv_import.CsvIssue"


def require_csv_preview() -> ModuleType:
    """The SDK CSV preview that reports issues (``SCION-FINANCE-SOURCES-R004``), or refuse.

    The pinned preview raises on the first bad row and returns no issue lists, so the
    preview read refuses ``UNAVAILABLE`` naming ``CsvIssue`` until the SDK re-pin.
    """
    try:
        module = importlib.import_module("scion_sdk.finance.csv_import")
    except ImportError:
        raise PinLagUnavailable(CSV_PREVIEW_SYMBOL) from None
    if not hasattr(module, "CsvIssue"):
        raise PinLagUnavailable(CSV_PREVIEW_SYMBOL)
    return module


FORECAST_SYMBOLS = (
    "ForecastRequest",
    "request_forecast",
    "forecast_provider",
    "forecast_ingest_request",
)


def require_forecast() -> ModuleType:
    """The SDK forecast contract and its provider adapter (``SCION-FINANCE-SOURCES-R901``)."""
    try:
        module = importlib.import_module("scion_sdk.finance.forecast")
    except ImportError:
        raise PinLagUnavailable("scion_sdk.finance.forecast") from None
    for name in FORECAST_SYMBOLS:
        if not hasattr(module, name):
            raise PinLagUnavailable(f"scion_sdk.finance.forecast.{name}")
    return module


WALLET_INTENT_SYMBOL = "scion_sdk.writeback.wallet_intent.WalletBusinessIntent"
_WALLET_NEEDED = (
    "WalletBusinessIntent",
    "WalletConfirmation",
    "WalletExecutionEnvelope",
    "wallet_plan_digest",
)


def require_wallet_intent() -> ModuleType:
    """The SDK wallet intent contract (``SCION-GOVERNED-WRITEBACK-R001.1``), or refuse.

    Wallet operations stay unavailable on a pinned release without it, naming the
    missing symbol; arbora hashes no intent of its own.
    """
    try:
        module = importlib.import_module("scion_sdk.writeback.wallet_intent")
    except ImportError:
        raise PinLagUnavailable(WALLET_INTENT_SYMBOL) from None
    for name in _WALLET_NEEDED:
        if not hasattr(module, name):
            raise PinLagUnavailable(f"scion_sdk.writeback.wallet_intent.{name}")
    return module


WALLET_TRUST_SYMBOL = "scion_sdk.writeback.wallet_trust.EngineWalletKeyResolver"


def require_wallet_trust() -> ModuleType:
    """The SDK engine wallet-key resolver (``SCION-GOVERNED-WRITEBACK-R001.4``), or refuse.

    The signature verifier of ``wallet_proof`` ships in the pinned release; the resolver
    that reads key trust from the engine is on SDK ``main``. Arbora resolves no key of its own.
    """
    try:
        module = importlib.import_module("scion_sdk.writeback.wallet_trust")
    except ImportError:
        raise PinLagUnavailable(WALLET_TRUST_SYMBOL) from None
    if not hasattr(module, "EngineWalletKeyResolver"):
        raise PinLagUnavailable(WALLET_TRUST_SYMBOL)
    return module
