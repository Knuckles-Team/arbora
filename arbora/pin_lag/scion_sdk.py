"""The scion-sdk capability guard (PIN-LAG: remove when the SDK serves these symbols).

scion-sdk 0.13 serves the provisioner, the credential binding, the desktop port, the
environment export and the wallet contracts, and arbora imports them directly. Three
contracts are not in any SDK release yet: the code-host event listing
(``scion_sdk.scm.events.ScmEventLister``), the CSV preview issue list
(``scion_sdk.finance.csv_import.CsvIssue``) and the forecast provider adapter
(``forecast_provider`` and ``forecast_ingest_request``). Every Arbora use of them goes through
the guards below, so an SDK without them refuses ``UNAVAILABLE`` naming the missing symbol,
and nothing falls back to a local stand-in.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from arbora.pin_lag.unavailable import PinLagUnavailable

__all__ = [
    "CSV_PREVIEW_SYMBOL",
    "FORECAST_SYMBOLS",
    "SCM_LISTING_SYMBOL",
    "require_csv_preview",
    "require_forecast",
    "require_scm_event_listing",
]

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
